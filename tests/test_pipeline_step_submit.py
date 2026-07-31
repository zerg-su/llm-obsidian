#!/usr/bin/env python3
"""Typed engineering/fix phase submission contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pipeline-step-submit.py"
TASK_ID = "11111111-1111-4111-8111-111111111111"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def run(
    worktree: Path,
    step: str,
    result: Path,
    *,
    pass_index: int = 1,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--worktree",
            str(worktree),
            "--step",
            step,
            "--pass-index",
            str(pass_index),
            "--result",
            str(result.relative_to(worktree)),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def write_result(
    path: Path,
    summary: str,
    *,
    outcome: str = "complete",
    evidence: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome": outcome,
                "summary": summary,
                "evidence": evidence or [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


with tempfile.TemporaryDirectory(prefix="pipeline-step-submit.") as raw:
    worktree = Path(raw) / "worktree"
    worktree.mkdir()
    (worktree / ".task-meta.json").write_text(
        json.dumps(
            {
                "version": 3,
                "task_id": TASK_ID,
                "worktree": str(worktree),
                "pipeline_policy": {
                    "name": "engineering/fix",
                    "definition_sha256": "a" * 64,
                    "completion_policy": "autonomous",
                    "total_pass_limit": 3,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (worktree / "evidence.txt").write_text("reproduced\n", encoding="utf-8")

    reproduce = worktree / "results" / "reproduce.json"
    write_result(
        reproduce,
        "The failure reproduces.",
        evidence=["evidence.txt"],
    )
    first = run(worktree, "reproduce", reproduce)
    check("first phase is accepted", first.returncode == 0, first.stderr)
    first_receipt = json.loads(first.stdout)
    check(
        "receipt binds exact pipeline and result",
        first_receipt["parent_operation_id"] == TASK_ID
        and first_receipt["definition_sha256"] == "a" * 64
        and first_receipt["step_id"] == "reproduce"
        and first_receipt["pass_index"] == 1
        and first_receipt["previous_receipt_sha256"] == ""
        and len(first_receipt["input_sha256"]) == 64
        and len(first_receipt["output_sha256"]) == 64
        and len(first_receipt["receipt_sha256"]) == 64,
        first_receipt,
    )
    duplicate = run(worktree, "reproduce", reproduce)
    check(
        "exact duplicate is idempotent",
        duplicate.returncode == 0
        and json.loads(duplicate.stdout) == first_receipt,
        duplicate.stderr,
    )

    regression = worktree / "results" / "regression.json"
    write_result(regression, "A regression test now fails.")
    out_of_order = run(worktree, "regression-test", regression)
    check(
        "out-of-order phase fails closed",
        out_of_order.returncode == 2
        and "root-cause" in out_of_order.stderr,
        out_of_order.stderr,
    )

    previous = first_receipt
    for step in ("root-cause", "regression-test", "minimal-fix"):
        result = worktree / "results" / f"{step}.json"
        write_result(result, f"{step} complete")
        accepted = run(worktree, step, result)
        check(f"{step} is accepted", accepted.returncode == 0, accepted.stderr)
        receipt = json.loads(accepted.stdout)
        check(
            f"{step} chains the prior receipt",
            receipt["previous_receipt_sha256"]
            == previous["receipt_sha256"],
            receipt,
        )
        previous = receipt

    (worktree / ".task-verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": TASK_ID,
                "status": "attention-required",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    pass_two = worktree / "results" / "root-cause-pass-2.json"
    write_result(pass_two, "Revised diagnosis after failed verification.")
    resumed = run(
        worktree,
        "root-cause",
        pass_two,
        pass_index=2,
    )
    resumed_receipt = json.loads(resumed.stdout)
    check(
        "next pass binds verification attention evidence",
        resumed.returncode == 0
        and resumed_receipt["pass_index"] == 2
        and resumed_receipt["verification_packet_sha256"],
        resumed.stderr,
    )

    cannot = Path(raw) / "cannot"
    cannot.mkdir()
    (cannot / ".task-meta.json").write_text(
        json.dumps(
            {
                "version": 3,
                "task_id": "22222222-2222-4222-8222-222222222222",
                "worktree": str(cannot),
                "pipeline_policy": {
                    "name": "engineering/fix",
                    "definition_sha256": "b" * 64,
                    "completion_policy": "attention",
                    "total_pass_limit": 2,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    cannot_result = cannot / "cannot.json"
    write_result(
        cannot_result,
        "The supplied fixture does not reproduce.",
        outcome="cannot-reproduce",
    )
    cannot_run = run(cannot, "reproduce", cannot_result)
    check(
        "cannot-reproduce is a typed terminal outcome",
        cannot_run.returncode == 0
        and json.loads(cannot_run.stdout)["outcome"]
        == "cannot-reproduce",
        cannot_run.stderr,
    )
    too_many = run(
        cannot,
        "root-cause",
        cannot_result,
        pass_index=3,
    )
    check(
        "pass limit fails closed",
        too_many.returncode == 2
        and "pass limit" in too_many.stderr,
        too_many.stderr,
    )
