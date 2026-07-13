#!/usr/bin/env python3
"""Hermetic privacy/runtime tests for shared pipeline events and reporting."""

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
MODULE = ROOT / "scripts" / "pipeline_events.py"
LIFECYCLE_MODULE = ROOT / "scripts" / "lifecycle_telemetry.py"


class Fail(SystemExit):
    pass


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise Fail(f"FAIL {label}{': ' + detail if detail else ''}")
    print(f"OK   {label}")


def load_module():
    spec = importlib.util.spec_from_file_location("pipeline_events_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_lifecycle_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("lifecycle_telemetry_test", LIFECYCLE_MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run() -> None:
    events = load_module()
    lifecycle = load_lifecycle_module()
    with tempfile.TemporaryDirectory(prefix="pipeline-events-test.") as tmp:
        root = Path(tmp)
        claude_env = {"CLAUDE_CODE_SESSION_ID": "claude-session"}
        codex_env = {"CODEX_THREAD_ID": "codex-thread"}
        check(
            "claude event emitted",
            events.emit_event(
                "retrieve",
                actor="retrieve",
                session="session with arbitrary content",
                paths=["wiki/concepts/Secret Page.md", "/absolute/leak", "../escape"],
                counts={"results": 2, "degraded": 0, "duration_ms": 12.5},
                root=root,
                environ=claude_env,
            ),
        )
        check(
            "codex event emitted",
            events.emit_event(
                "vault-write",
                actor="save",
                paths=["wiki/concepts/Safe.md", ".raw/.manifest.json"],
                counts={"writes": 2, "duration_ms": 25},
                root=root,
                environ=codex_env,
            ),
        )
        check(
            "unknown runtime event emitted",
            events.emit_event("retrieval-index", actor="retrieve", counts={"rebuilt": 0}, root=root, environ={}),
        )

        (root / "wiki/plans").mkdir(parents=True)
        worktree = root / "task"
        worktree.mkdir()
        (worktree / ".task-meta.json").write_text(
            json.dumps({"origin_session": "origin-1", "plan_file": str(root / "wiki/plans/demo.md")}),
            encoding="utf-8",
        )
        check("lifecycle origin vault resolved", lifecycle.origin_vault(worktree) == root.resolve())
        standalone_review = root / "standalone-review"
        standalone_review.mkdir()
        (standalone_review / ".task-meta.json").write_text("{}\n", encoding="utf-8")
        (standalone_review / ".review-meta.json").write_text(
            json.dumps({"vault_root": str(root)}) + "\n", encoding="utf-8"
        )
        check(
            "standalone review origin vault resolved",
            lifecycle.origin_vault(standalone_review) == root.resolve(),
        )
        check("lifecycle elapsed duration", lifecycle.elapsed_ms("2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z") == 2000)
        check("lifecycle invalid counter is safe", lifecycle.nonnegative_int("broken") == 0)

        lifecycle_samples = [
            ("agent-run", "task:codex", {"duration_ms": 1000, "watchdog_warnings": 1}),
            ("agent-run", "reviewer:claude", {"duration_ms": 2500, "watchdog_recoveries": 1}),
            ("review-round-start", "review:claude:fable:full", {"rounds_started": 1, "iteration": 1}),
            (
                "review-round",
                "review:claude:fable:full",
                {
                    "duration_ms": 2000,
                    "valid_callbacks": 1,
                    "findings": 2,
                    "warning_findings": 1,
                    "nit_findings": 1,
                },
            ),
            ("task-escalation", "raise:permission", {"raised": 1, "delivery_failures": 1}),
            ("task-escalation", "resolve:scope", {"resolved": 1, "duration_ms": 1200}),
            ("surface-lifecycle", "reviewer:claude", {"closed": 1, "auto_close_expected": 1}),
            ("surface-lifecycle", "reviewer:claude", {"left_open": 1, "auto_close_expected": 0}),
            ("surface-lifecycle", "task:codex", {"left_open": 1, "auto_close_expected": 1}),
            ("task-complete", "reap", {"tasks": 1, "duration_ms": 5000}),
        ]
        for op, actor, counts in lifecycle_samples:
            check(
                f"synthetic {op} emitted",
                events.emit_event(op, actor=actor, counts=counts, root=root, environ=codex_env),
            )

        log = root / ".vault-meta/pipeline-events.jsonl"
        records = read_jsonl(log)
        check("all records", len(records) == 3 + len(lifecycle_samples))
        check("runtime classification", [item["runtime"] for item in records[:3]] == ["claude", "codex", "unknown"])
        check("unsafe session hashed", records[0]["session"].startswith("sha256:"))
        check("unsafe paths omitted", records[0]["paths"] == ["wiki/concepts/Secret Page.md"])
        allowed = {"schema", "ts", "runtime", "session", "actor", "op", "status", "paths", "counts"}
        check("fixed event schema", all(set(item) == allowed for item in records))
        serialized = log.read_text(encoding="utf-8")
        check("no prompt/query/content fields", not any(f'"{key}"' in serialized for key in ("prompt", "query", "content", "command", "snippet", "reason")))
        check("unsafe session content absent", "arbitrary content" not in serialized)

        before = len(records)
        check(
            "string metadata rejected",
            not events.emit_event(
                "retrieve",
                counts={"query": "private search terms"},
                root=root,
                environ=claude_env,
            ),
        )
        check("rejected event not appended", len(read_jsonl(log)) == before)
        check(
            "oversized number rejected safely",
            not events.emit_event("retrieve", counts={"calls": 10**10000}, root=root, environ={}),
        )

        rotate_root = root / "rotate"
        events.ROTATE_BYTES = 1
        events.emit_event("one", counts={"calls": 1}, root=rotate_root, environ={})
        events.emit_event("two", counts={"calls": 1}, root=rotate_root, environ={})
        check("rotation keeps prior log", (rotate_root / ".vault-meta/pipeline-events.jsonl.1").is_file())
        check("rotation keeps current log", (rotate_root / ".vault-meta/pipeline-events.jsonl").is_file())

        # The report labels shared operations separately from Claude-only skill data.
        (root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/pipeline-stats.py", root / "scripts/pipeline-stats.py")
        env = dict(os.environ)
        env["HOME"] = str(root / "home")
        result = subprocess.run(
            [sys.executable, str(root / "scripts/pipeline-stats.py"), "--days", "1"],
            text=True,
            capture_output=True,
            env=env,
        )
        check("pipeline stats exit 0", result.returncode == 0, result.stderr)
        check("runtime-neutral section", "## Runtime-neutral observed operations" in result.stdout)
        check("codex operation reported", "| codex | vault-write | ok | 1 |" in result.stdout)
        check("claude operation reported", "| claude | retrieve | ok | 1 |" in result.stdout)
        check("numeric latency percentiles reported", "| claude | retrieve | ok | 1 | 12.5 | 12.5 |" in result.stdout)
        check("lifecycle dogfood section", "## Unattended lifecycle dogfood" in result.stdout)
        check("callback validation rate", "| Callback schema-valid rate | 100.0% |" in result.stdout)
        check("lifecycle completion counted", "| Validated task completions | 1 |" in result.stdout)
        check(
            "surface outcomes counted",
            "| Surfaces auto-closed | 1 |" in result.stdout
            and "| Surfaces left open (expected) | 1 |" in result.stdout
            and "| Auto-close misses | 1 |" in result.stdout,
        )
        check("escalation delivery failure counted", "| Escalation delivery failures | 1 |" in result.stdout)
        check("lifecycle latency reported", "| Task end-to-end | 1 | 5.0 | 5.0 |" in result.stdout)
        check("Claude-only section explicit", "## Claude-only skill telemetry" in result.stdout)

        for filename in (
            "vault-write.py", "plan_lifecycle.py", "vault_schema.py", "pipeline_events.py"
        ):
            shutil.copy2(ROOT / "scripts" / filename, root / "scripts" / filename)
        report_run = subprocess.run(
            [sys.executable, str(root / "scripts/pipeline-stats.py"), "--days", "1", "--report"],
            text=True,
            capture_output=True,
            env=env,
        )
        report = root / "wiki/meta/reports" / f"pipeline-stats-{events.datetime.now().date().isoformat()}.md"
        check("report writer exit 0", report_run.returncode == 0, report_run.stderr)
        check("report created transactionally", report.is_file())
        check("report has strict sessions", "sessions: []" in report.read_text(encoding="utf-8"))
        second_report = subprocess.run(
            [sys.executable, str(root / "scripts/pipeline-stats.py"), "--days", "1", "--report"],
            text=True,
            capture_output=True,
            env=env,
        )
        check("report update exit 0", second_report.returncode == 0, second_report.stderr)


if __name__ == "__main__":
    try:
        run()
    except Fail as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
    print("\nAll pipeline event tests passed.")
