#!/usr/bin/env python3
"""Hermetic tests for hash-derived, counter-free log folding."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fold-log.py"


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


def make_sandbox():
    root = Path(tempfile.mkdtemp(prefix="fold-log-test."))
    (root / "scripts").mkdir()
    (root / "wiki").mkdir()
    (root / ".vault-meta").mkdir()
    for filename in (
        "fold-log.py", "vault-write.py", "plan_lifecycle.py",
        "vault_schema.py", "pipeline_events.py",
    ):
        shutil.copy2(ROOT / "scripts" / filename, root / "scripts" / filename)
    entries = []
    for number in range(10, 0, -1):
        operation = "fold" if number == 8 else "save"
        entries.append(
            f"## [2026-01-{number:02d}] {operation} | Entry {number}\n\n"
            f"Deterministic outcome number {number}.\n"
        )
    (root / "wiki/log.md").write_text(
        """---
type: meta
title: "Log"
status: evergreen
created: 2026-01-01
updated: 2026-01-10
tags: [meta]
sessions: []
---

# Log

"""
        + "\n".join(entries),
        encoding="utf-8",
    )
    return root


def run_cli(root, *args):
    env = dict(os.environ)
    env["CODEX_THREAD_ID"] = "fold-test-session"
    return subprocess.run(
        [sys.executable, str(root / "scripts/fold-log.py"), *args],
        text=True,
        capture_output=True,
        env=env,
    )


def load_module(root):
    sys.path.insert(0, str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("fold_log_test", root / "scripts/fold-log.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = root
    module.LOG = root / "wiki/log.md"
    module.FOLDS = root / "wiki/folds"
    module.WRITER = root / "scripts/vault-write.py"
    return module


def test_fold():
    root = make_sandbox()
    try:
        module = load_module(root)
        entries = module.parse_entries((root / "wiki/log.md").read_text())
        assert_eq("parser sees all log entries", 10, len(entries))
        assert_eq("content hash length", 64, len(entries[0].id))
        assert_eq("fold operation excluded", 9, len([item for item in entries if item.operation != "fold"]))

        result = run_cli(root, "status", "--k", "2", "--json")
        assert_eq("status exit 0", 0, result.returncode)
        status = json.loads(result.stdout)
        assert_eq("status batch size", 4, status["batch_size"])
        assert_eq("status unprocessed", 9, status["unprocessed_entries"])
        assert_eq("status excluded fold", 1, status["excluded_fold_entries"])
        assert_true("status ready", status["ready"])

        expected_oldest = [
            item.id
            for item in list(reversed([entry for entry in entries if entry.operation != "fold"]))[:4]
        ]
        assert_eq("selects oldest unprocessed", expected_oldest, status["selected_ids"])

        result = run_cli(root, "--k", "2")
        assert_eq("dry-run exit 0", 0, result.returncode)
        assert_true("dry-run writes no fold", not (root / "wiki/folds").exists())
        assert_true("dry-run embeds boundary IDs", expected_oldest[0][:12] in result.stdout and expected_oldest[-1][:12] in result.stdout)

        first_entries = list(reversed([entry for entry in entries if entry.operation != "fold"]))[:4]
        identifier, one = module.render_page(2, first_entries, "fold-test-session")
        _, two = module.render_page(2, first_entries, "fold-test-session")
        assert_eq("render is deterministic", one, two)
        assert_true("fold id uses boundary hashes", expected_oldest[0][:12] in identifier and expected_oldest[-1][:12] in identifier)

        result = run_cli(root, "--k", "2", "--commit")
        assert_eq("first commit exit 0", 0, result.returncode)
        folds = sorted((root / "wiki/folds").glob("*.md"))
        assert_eq("first fold page created", 1, len(folds))
        assert_true("fold records every selected id", all(entry_id in folds[0].read_text() for entry_id in expected_oldest))
        assert_true("no legacy counter", not (root / ".vault-meta/last-fold-count.txt").exists())
        events = [
            json.loads(line)
            for line in (root / ".vault-meta/pipeline-events.jsonl").read_text().splitlines()
        ]
        assert_true("writer event observed", any(item["op"] == "vault-write" for item in events))
        fold_event = next(item for item in events if item["op"] == "fold")
        assert_eq("fold event runtime", "codex", fold_event["runtime"])
        assert_eq("fold event count", 4, fold_event["counts"]["entries"])
        assert_true("fold event has path not content", fold_event["paths"] == [f"wiki/folds/{identifier}.md"] and "content" not in fold_event)

        result = run_cli(root, "status", "--k", "2", "--json")
        after = json.loads(result.stdout)
        assert_eq("processed derived from fold pages", 4, after["processed_entries"])
        assert_eq("new fold log entry excluded", 2, after["excluded_fold_entries"])
        assert_eq("remaining unprocessed", 5, after["unprocessed_entries"])
        assert_true("next batch disjoint", not set(expected_oldest) & set(after["selected_ids"]))

        result = run_cli(root, "--k", "2", "--commit")
        assert_eq("second commit exit 0", 0, result.returncode)
        assert_eq("second distinct fold", 2, len(list((root / "wiki/folds").glob("*.md"))))
        result = run_cli(root, "--k", "2", "--commit")
        assert_eq("insufficient batch is no-op", 0, result.returncode)
        assert_eq("no third partial fold", 2, len(list((root / "wiki/folds").glob("*.md"))))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_fold()
    except Fail as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
    print("\nAll deterministic fold tests passed.")
