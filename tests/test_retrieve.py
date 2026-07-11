#!/usr/bin/env python3
"""Hermetic tests for section chunking, sparse retrieval, and degradation."""

from __future__ import annotations

import importlib.util
import fcntl
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "retrieve.py"


class Fail(SystemExit):
    pass


def assert_true(name, condition, extra=""):
    if not condition:
        raise Fail(f"FAIL {name}{': ' + extra if extra else ''}")
    print(f"OK   {name}")


def assert_eq(name, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {name}: expected {expected!r}, got {actual!r}")
    print(f"OK   {name}")


def load_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("retrieve_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def page(title, address, tags, body):
    return f'''---
type: concept
title: "{title}"
address: {address}
status: developing
created: 2026-07-09
updated: 2026-07-09
tags: [{tags}]
sessions: []
---

# {title}

{body}
'''


def patch_paths(module, root):
    module.ROOT = root
    module.WIKI = root / "wiki"
    module.META = root / ".vault-meta/retrieval"
    module.SPARSE_INDEX = module.META / "index.json"
    module.DENSE_INDEX = module.META / "dense.json"
    module.LOCK = root / ".vault-meta/.retrieval.lock"
    module.DENSE_LOCK = root / ".vault-meta/.dense-refresh.lock"


def run():
    module = load_module()
    with tempfile.TemporaryDirectory(prefix="retrieve-test.") as tmp:
        root = Path(tmp)
        (root / "wiki/concepts").mkdir(parents=True)
        (root / ".vault-meta").mkdir()
        patch_paths(module, root)

        long_words = [f"token{i}" for i in range(1700)]
        long_body = "## Long Section\n\n" + " ".join(long_words)
        (root / "wiki/concepts/Long.md").write_text(
            page("Long Retrieval Page", "c-000001", "retrieval, windows", long_body),
            encoding="utf-8",
        )
        (root / "wiki/concepts/Agents.md").write_text(
            page(
                "Agent Loop Architecture",
                "c-000002",
                "agents, orchestration",
                "## Verification Loop\n\nHarness feedback repeats context action verification until done.\n\n"
                "### Stop Conditions\n\nA bounded agent needs explicit terminal conditions.",
            ),
            encoding="utf-8",
        )
        (root / "wiki/concepts/Other.md").write_text(
            page("Other", "c-000003", "misc", "## Unrelated\n\nNothing useful here."),
            encoding="utf-8",
        )

        chunks = module.chunks_for_page(root / "wiki/concepts/Long.md")
        long_chunks = [chunk for chunk in chunks if chunk.heading == "Long Section"]
        assert_true("long section splits", len(long_chunks) >= 3)
        assert_true("chunk cap 800 words", all(len(chunk.text.split()) <= 800 for chunk in chunks))
        first, second = long_chunks[0].text.split(), long_chunks[1].text.split()
        assert_eq("chunk overlap 100 words", first[-100:], second[:100])

        index, rebuilt = module.ensure_sparse()
        assert_true("initial sparse build", rebuilt)
        assert_true("section index has more chunks than pages", index["chunk_count"] > index["page_count"])
        results, meta = module.retrieve(index, "verification loop harness", top=5, dense_mode="off")
        assert_eq("heading query page", "wiki/concepts/Agents.md", results[0].path)
        assert_eq("heading query best section", "Verification Loop", results[0].heading)
        assert_true("result snippet present", "Harness" in results[0].snippet)
        assert_eq("sparse explicit not degraded", False, meta["degraded"])

        title_results, _ = module.retrieve(index, "Agent Loop Architecture", top=3, dense_mode="off")
        assert_eq("exact title boost", "wiki/concepts/Agents.md", title_results[0].path)
        assert_eq("unique pages", len({item.path for item in results}), len(results))

        agent_id = next(chunk_id for chunk_id, doc in index["docs"].items() if doc["path"].endswith("Agents.md") and doc["heading"] == "Verification Loop")
        other_id = next(chunk_id for chunk_id, doc in index["docs"].items() if doc["path"].endswith("Other.md"))
        reranked = module.lexical_rerank(index, "verification loop harness", [(other_id, 2.0), (agent_id, 1.0)])
        assert_eq("experimental lexical reranker", agent_id, reranked[0][0])
        assert_true("context header identifies source", "Agent Loop Architecture" in module.contextual_header(index["docs"][agent_id]))

        same, rebuilt = module.ensure_sparse()
        assert_true("fresh ensure no rebuild", not rebuilt)
        before = same["source_fingerprint"]
        with (root / "wiki/concepts/Other.md").open("a", encoding="utf-8") as fh:
            fh.write("\nnewfingerprintword\n")
        changed, rebuilt = module.ensure_sparse()
        assert_true("content change rebuilds", rebuilt)
        assert_true("fingerprint changes", changed["source_fingerprint"] != before)

        # Dense refresh checkpoints completed batches as incomplete, never
        # serves them, and resumes from them after a normal failure.
        module.DENSE_INDEX.unlink(missing_ok=True)
        module.EMBED_BATCH = 2
        module.DENSE_CHECKPOINT_SECONDS = 0
        calls = []

        def flaky_embed(texts, model=module.MODEL):
            calls.append(len(texts))
            if len(calls) == 3:
                raise RuntimeError("synthetic embedding failure")
            return [[1.0, 0.0] for _ in texts]

        module.embed_batch = flaky_embed
        try:
            module.refresh_dense(changed, quiet=True)
            raise Fail("FAIL dense partial refresh should fail")
        except RuntimeError as exc:
            assert_true("dense partial propagates failure", "synthetic" in str(exc))
        partial = json.loads(module.DENSE_INDEX.read_text(encoding="utf-8"))
        partial_count = len(partial["embeddings"])
        assert_eq("dense partial marked incomplete", False, partial["complete"])
        assert_true("dense partial persists completed batches", 0 < partial_count < len(changed["docs"]))
        unavailable, reason = module.load_dense(changed)
        assert_eq("dense partial is never served", None, unavailable)
        assert_true("dense partial reports stale", bool(reason))

        calls.clear()
        module.embed_batch = lambda texts, model=module.MODEL: (
            calls.append(len(texts)) or [[1.0, 0.0] for _ in texts]
        )
        dense = module.refresh_dense(changed, quiet=True)
        assert_eq("dense cache covers every chunk", set(changed["docs"]), set(dense["embeddings"]))
        assert_eq("dense cache carries source fingerprint", changed["source_fingerprint"], dense["source_fingerprint"])
        assert_eq("dense resumed only missing chunks", len(changed["docs"]) - partial_count, sum(calls))
        assert_eq("dense final marked complete", True, dense["complete"])
        first_embedded = partial_count + sum(calls)
        calls.clear()
        module.refresh_dense(changed, quiet=True)
        assert_eq("dense refresh reuses unchanged chunks", 0, sum(calls))
        assert_true("initial dense refresh embedded chunks", first_embedded > 0)
        with module.DENSE_LOCK.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                module.refresh_dense(changed, quiet=True)
                raise Fail("FAIL concurrent dense refresh should be rejected")
            except RuntimeError as exc:
                assert_true("concurrent dense refresh rejected", "in progress" in str(exc))
        module.embed = lambda text, model=module.MODEL: [1.0, 0.0]
        hybrid, hybrid_meta = module.retrieve(changed, "verification loop", top=3)
        assert_eq("current dense cache enables hybrid", "hybrid", hybrid_meta["mode"])
        assert_true("hybrid does not degrade", not hybrid_meta["degraded"] and bool(hybrid))

        # CLI default is hybrid-auto but must return sparse results and exit 0
        # when no dense cache exists.
        sandbox = root / "cli"
        (sandbox / "scripts").mkdir(parents=True)
        (sandbox / "wiki").mkdir()
        (sandbox / ".vault-meta").mkdir()
        for filename in ("retrieve.py", "semantic-search.py", "vault_schema.py", "pipeline_events.py"):
            shutil.copy2(ROOT / "scripts" / filename, sandbox / "scripts" / filename)
        (sandbox / "wiki/Target.md").write_text(
            page("Target", "c-000001", "buildkit", "## Disk Leak\n\nBuildkit disk leak cleanup."),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, str(sandbox / "scripts/retrieve.py"), "buildkit disk", "--json"],
            text=True,
            capture_output=True,
        )
        assert_eq("dense-down CLI exit 0", 0, completed.returncode)
        data = json.loads(completed.stdout)
        assert_eq("dense-down degraded true", True, data["meta"]["degraded"])
        assert_eq("dense-down sparse mode", "sparse", data["meta"]["mode"])
        assert_eq("CLI returns heading", "Disk Leak", data["results"][0]["heading"])
        assert_true("CLI returns snippet", bool(data["results"][0]["snippet"]))
        event_log = sandbox / ".vault-meta/pipeline-events.jsonl"
        events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
        retrieve_event = next(item for item in events if item["op"] == "retrieve")
        assert_eq("CLI telemetry result path", ["wiki/Target.md"], retrieve_event["paths"])
        assert_true("CLI telemetry omits query", "buildkit" not in event_log.read_text(encoding="utf-8"))

        wrapper = subprocess.run(
            [sys.executable, str(sandbox / "scripts/semantic-search.py"), "buildkit", "--hybrid"],
            text=True,
            capture_output=True,
        )
        assert_eq("compat wrapper exit 0", 0, wrapper.returncode)
        assert_true("compat wrapper returns path", "wiki/Target.md" in wrapper.stdout)


if __name__ == "__main__":
    try:
        run()
    except Fail as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
    print("\nAll section retrieval tests passed.")
