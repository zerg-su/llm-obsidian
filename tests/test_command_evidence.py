#!/usr/bin/env python3
"""Strict command-evidence ingestion and grouping regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "command_evidence.py"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def run(
    root: Path,
    command: list[str],
    payload: object | None = None,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *command],
        input=(json.dumps(payload) if payload is not None else None),
        text=True,
        capture_output=True,
        env=env,
    )


with tempfile.TemporaryDirectory(prefix="command-evidence-test.") as raw:
    vault = Path(raw)
    (vault / ".vault-meta").mkdir()
    log = vault / ".vault-meta" / "command-log.jsonl"
    agent_records = [
        {
            "schema_version": 2,
            "ts": "2026-08-01T12:00:00",
            "session_id": "exec-a",
            "execution_session": "exec-a",
            "provenance_session": "origin-a",
            "origin": "agent-executed",
            "cwd": str(vault),
            "command": "python3 verify.py",
            "outcome": "success",
            "is_error": False,
            "event_id": "a" * 64,
        },
        {
            "schema_version": 2,
            "ts": "2026-08-01T12:01:00",
            "session_id": "exec-b",
            "execution_session": "exec-b",
            "provenance_session": "origin-a",
            "origin": "agent-executed",
            "cwd": str(vault),
            "command": "python3 other.py",
            "outcome": "error",
            "is_error": True,
            "event_id": "b" * 64,
        },
    ]
    log.write_text("".join(json.dumps(item) + "\n" for item in agent_records), encoding="utf-8")

    success_payload = {
        "schema_version": 1,
        "command": "deploy --token=abcdef123456 service-a",
        "cwd": str(vault),
        "provenance_session": "origin-a",
        "origin": "user-reported",
        "outcome": "success",
        "result_excerpt": "service healthy; api_key=abcdef123456",
    }
    first = run(vault, ["ingest-user"], success_payload)
    duplicate = run(vault, ["ingest-user"], success_payload)
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    user_success = records[-1]
    check(
        "user success is sanitized and deduplicated",
        first.returncode == 0
        and json.loads(first.stdout)["status"] == "recorded"
        and duplicate.returncode == 0
        and json.loads(duplicate.stdout)["status"] == "duplicate"
        and len(records) == 3
        and user_success["origin"] == "user-reported"
        and user_success["outcome"] == "success"
        and "abcdef123456" not in json.dumps(user_success)
        and "REDACTED" in user_success["command"]
        and "REDACTED" in user_success["result_excerpt"],
        first.stderr + duplicate.stderr,
    )

    failure_payload = {
        "schema_version": 1,
        "command": "deploy service-b",
        "cwd": str(vault),
        "provenance_session": "origin-a",
        "origin": "user-reported",
        "outcome": "error",
    }
    failure = run(vault, ["ingest-user"], failure_payload)
    check(
        "user failure remains a typed error without invented output",
        failure.returncode == 0
        and json.loads(failure.stdout)["status"] == "recorded"
        and json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["is_error"] is True
        and "result_excerpt" not in json.loads(log.read_text(encoding="utf-8").splitlines()[-1]),
        failure.stderr,
    )

    grouped = run(vault, ["sessions", "--provenance-session", "origin-a"])
    grouped_value = json.loads(grouped.stdout)
    check(
        "session grouping separates provenance and execution",
        grouped.returncode == 0
        and grouped_value["provenance_session"] == "origin-a"
        and grouped_value["execution_sessions"] == {"exec-a": 1, "exec-b": 1}
        and grouped_value["user_reported"] == 2,
        grouped.stderr,
    )

    collected = run(
        vault,
        [
            "collect",
            "--provenance-session",
            "origin-a",
            "--execution-session",
            "exec-a",
        ],
    )
    evidence = json.loads(collected.stdout)
    check(
        "collection combines one execution with provenance user reports",
        collected.returncode == 0
        and [item["origin"] for item in evidence["records"]]
        == ["agent-executed", "user-reported", "user-reported"]
        and evidence["counts"]
        == {
            "agent_executed": 1,
            "user_reported": 2,
            "validation_grade": 0,
        },
        collected.stderr,
    )

    malformed = dict(success_payload, extra="not-allowed")
    oversized = dict(success_payload, command="x" * 17000)
    residual = dict(success_payload, command="-----BEGIN PRIVATE KEY-----")
    wrong_scalar = dict(success_payload, command=7)
    for label, payload in (
        ("malformed", malformed),
        ("oversized", oversized),
        ("residual secret", residual),
        ("non-string", wrong_scalar),
    ):
        result = run(vault, ["ingest-user"], payload)
        check(
            f"{label} user evidence is rejected",
            result.returncode == 2 and len(log.read_text(encoding="utf-8").splitlines()) == 4,
            result.stderr,
        )

    (vault / ".task-origin-session").write_text("origin-a\n", encoding="utf-8")
    runner_env = dict(os.environ, CODEX_THREAD_ID="exec-validation")
    validation_success = run(
        vault,
        [
            "run-validation",
            "--run-id",
            "validation-success",
            "--cwd",
            str(vault),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
        env=runner_env,
    )
    validation_failure = run(
        vault,
        [
            "run-validation",
            "--run-id",
            "validation-failure",
            "--cwd",
            str(vault),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
        env=runner_env,
    )
    validation_records = [
        record
        for record in (
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
        )
        if record.get("validation_grade") is True
    ]
    check(
        "validation runner records only its own explicit exit status",
        validation_success.returncode == 0
        and validation_failure.returncode == 7
        and [record["outcome"] for record in validation_records]
        == ["success", "error"]
        and [record["exit_code"] for record in validation_records] == [0, 7]
        and all(record["execution_session"] == "exec-validation" for record in validation_records)
        and all(record["provenance_session"] == "origin-a" for record in validation_records)
        and all("result_excerpt" not in record for record in validation_records),
        validation_success.stderr + validation_failure.stderr,
    )

    sanitizer_self_test = run(vault, ["self-test-sanitization"])
    check(
        "sanitization self-test generates its fixture internally",
        sanitizer_self_test.returncode == 0
        and json.loads(sanitizer_self_test.stdout) == {
            "redactions": 1,
            "status": "ok",
        }
        and "abcdef123456" not in sanitizer_self_test.stdout,
        sanitizer_self_test.stderr,
    )

print("\nAll command evidence tests passed.")
