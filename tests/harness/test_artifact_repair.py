#!/usr/bin/env python3
"""Deterministic five-family artifact repair and correction-budget matrix."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

from harness.artifact_repair import (  # noqa: E402
    ArtifactRepairError,
    CorrectionBudgetExhausted,
    CorrectionNotificationUncertain,
    ContractArtifactOwner,
    observe_stable_artifact,
)
from harness.contracts import CanonicalContractTemplate, ContractFamily  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def summary_template(attempt: str = "summary-attempt") -> CanonicalContractTemplate:
    return CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id=attempt,
        target_pointer=".task-summary.json",
        value={
            "schema_version": 2,
            "type": "repo-touch",
            "title": "",
            "session": "session-1",
            "body": "",
            "outcome_disposition": "partially-achieved",
            "outcome_evidence_ids": [],
            "residual_gap_pointers": ["wiki/plans/approved.md"],
        },
        code_owned_fields={"schema_version", "type", "session"},
        model_owned_fields={
            "title",
            "body",
            "outcome_disposition",
            "outcome_evidence_ids",
            "residual_gap_pointers",
        },
    )


with tempfile.TemporaryDirectory(prefix="artifact-repair.") as raw:
    root = Path(raw)
    worktree = root / "worktree"
    state = root / "state"
    worktree.mkdir()
    state.mkdir()
    target = worktree / ".task-summary.json"
    owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=worktree,
        template=summary_template(),
        actual_target=target,
    )
    sidecar_before = owner.sidecar_path.read_bytes()
    same = ContractArtifactOwner.publish(
        state_root=state,
        worktree=worktree,
        template=summary_template(),
        actual_target=target,
    )
    check(
        "template publication is immutable and idempotent",
        same.sidecar_path == owner.sidecar_path
        and same.sidecar_path.read_bytes() == sidecar_before
        and same.sidecar_path.stat().st_mode & 0o222 == 0,
    )

    target.write_text(
        "```json\n"
        + json.dumps(
            {
                "schemaVersion": 1,
                "type": "decision",
                "title": "Bounded result",
                "session_id": "copied-wrong",
                "body": "Implemented the slice.",
                "outcome": "partial",
                "evidence_ids": [],
                "gaps": ["wiki/plans/approved.md"],
            }
        )[:-1]
        + ",\n```\n",
        encoding="utf-8",
    )
    repaired = owner.repair(authoritative_fields={})
    value = json.loads(target.read_text(encoding="utf-8"))
    check(
        "syntax shape aliases and uniquely bound identity repair deterministically",
        repaired.changed
        and value
        == {
            "schema_version": 2,
            "type": "repo-touch",
            "title": "Bounded result",
            "session": "session-1",
            "body": "Implemented the slice.",
            "outcome_disposition": "partially-achieved",
            "outcome_evidence_ids": [],
            "residual_gap_pointers": ["wiki/plans/approved.md"],
        },
        value,
    )

    first = observe_stable_artifact(target, previous_sha256="", stable_reads=0)
    second = observe_stable_artifact(
        target,
        previous_sha256=first.sha256,
        stable_reads=first.stable_reads,
    )
    check(
        "artifact requires two identical bounded reads",
        first.state == "unstable" and second.state == "stable",
        (first, second),
    )

    real = worktree / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(real)
    try:
        owner.repair(authoritative_fields={})
    except ArtifactRepairError:
        pass
    else:
        raise AssertionError("symlinked model artifact was repaired")
    check("symlink artifact fails closed", True)
    target.unlink()
    owner.restore_template()

    sends: list[str] = []
    reservation = owner.reserve_correction("a" * 64)
    owner.deliver_correction(reservation, "correct the artifact", sends.append)
    replay = owner.reserve_correction("a" * 64)
    delivered = owner.deliver_correction(replay, "correct the artifact", sends.append)
    check(
        "duplicate observation reuses one durable reservation and one notification",
        reservation.attempt == replay.attempt == 1
        and delivered is False
        and sends == ["correct the artifact"],
        sends,
    )
    try:
        owner.reserve_correction("b" * 64)
    except CorrectionBudgetExhausted:
        pass
    else:
        raise AssertionError("task-summary correction budget expanded")
    check("registered one-shot task-summary budget exhausts exactly", True)

    crash_owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=worktree,
        template=summary_template("summary-crash"),
        actual_target=target,
    )
    crash_reservation = crash_owner.reserve_correction("c" * 64)

    def crash(stage: str) -> None:
        if stage == "notification-reserved":
            raise RuntimeError("synthetic crash")

    try:
        crash_owner.deliver_correction(
            crash_reservation,
            "do not duplicate",
            sends.append,
            fault_observer=crash,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("notification reservation crash was hidden")
    restarted = ContractArtifactOwner.load(
        state_root=state,
        worktree=worktree,
        family=ContractFamily.TASK_SUMMARY,
        attempt_id="summary-crash",
    )
    try:
        restarted.deliver_correction(
            restarted.reserve_correction("c" * 64),
            "do not duplicate",
            sends.append,
        )
    except CorrectionNotificationUncertain:
        pass
    else:
        raise AssertionError("uncertain correction notification was duplicated")
    check(
        "restart after notification reservation fails closed without a duplicate prompt",
        sends == ["correct the artifact"],
        sends,
    )

print("artifact repair matrix: ok")
