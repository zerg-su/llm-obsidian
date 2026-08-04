#!/usr/bin/env python3
"""Append-only coordinator decision record and latest-pointer regressions."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_escalation_records import (  # noqa: E402
    EscalationRecordError,
    append_amendment,
    append_raise,
    append_resolution,
    load_chain,
    load_latest,
    record_path,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def expect_error(label: str, action: object, message: str) -> None:
    try:
        action()
    except EscalationRecordError as exc:
        check(label, message in str(exc))
    else:
        raise AssertionError(label)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def meta(worktree: Path, *, task_id: str = "task-a") -> None:
    write_json(
        worktree / ".task-meta.json",
        {
            "version": 4,
            "project_id": "project-a",
            "task_id": task_id,
            "origin_session": "coordinator-a",
            "task_surface": "11111111-1111-4111-8111-111111111111",
            "worktree": str(worktree.resolve()),
        },
    )


def raised_payload(worktree: Path, escalation_id: str, reason: str) -> dict[str, object]:
    return {
        "version": 1,
        "id": escalation_id,
        "status": "pending",
        "task_name": "durable decisions",
        "category": "scope",
        "reason": reason,
        "question": "Keep the approved boundary?",
        "worktree": str(worktree.resolve()),
        "task_surface": "11111111-1111-4111-8111-111111111111",
        "raised_at": "2026-08-04T12:00:00Z",
    }


with tempfile.TemporaryDirectory(prefix="task-escalation-records.") as raw:
    worktree = Path(raw) / "task"
    worktree.mkdir()
    meta(worktree)

    first = append_raise(
        worktree,
        raised_payload(worktree, "escalation-1", "first decision"),
    )
    marker_path = worktree / ".task-needs-attention.json"
    marker_bytes = marker_path.read_bytes()
    marker = json.loads(marker_bytes)
    check(
        "new writer emits a pointer-only latest marker",
        set(marker) == {"schema_version", "record_id", "record_sha256"}
        and marker["schema_version"] == 2
        and marker["record_id"] == first.record_id
        and marker["record_sha256"] == first.sha256,
    )
    first_bytes = record_path(worktree, first.record_id).read_bytes()
    replay = append_raise(
        worktree,
        raised_payload(worktree, "escalation-1", "first decision"),
    )
    check(
        "repeated immutable raise preserves record and pointer bytes",
        replay.sha256 == first.sha256
        and record_path(worktree, first.record_id).read_bytes() == first_bytes
        and marker_path.read_bytes() == marker_bytes,
    )

    first_resolution = append_resolution(
        worktree,
        "keep the first boundary",
        resolved_at="2026-08-04T12:01:00Z",
    )
    second = append_raise(
        worktree,
        raised_payload(worktree, "escalation-2", "second decision"),
    )
    second_resolution = append_resolution(
        worktree,
        "keep the second boundary",
        resolved_at="2026-08-04T12:02:00Z",
    )
    chain = load_chain(worktree)
    check(
        "two decisions retain the complete ordered history",
        [item.record_type for item in chain]
        == ["raise", "resolution", "raise", "resolution"]
        and chain[0].payload["reason"] == "first decision"
        and chain[1].payload["decision"] == "keep the first boundary"
        and chain[2].payload["reason"] == "second decision"
        and chain[3].payload["decision"] == "keep the second boundary"
        and load_latest(worktree).sha256 == second_resolution.sha256,
    )

    amendment = append_amendment(
        worktree,
        plan_sha256="a" * 64,
        outcome_sha256="b" * 64,
        decision="approve the digest-bound amendment",
        recorded_at="2026-08-04T12:03:00Z",
    )
    amendment_bytes = record_path(worktree, amendment.record_id).read_bytes()
    amendment_replay = append_amendment(
        worktree,
        plan_sha256="a" * 64,
        outcome_sha256="b" * 64,
        decision="approve the digest-bound amendment",
        recorded_at="2026-08-04T12:03:00Z",
    )
    check(
        "amendment binds frozen plan and Outcome digests idempotently",
        amendment.payload["plan_sha256"] == "a" * 64
        and amendment.payload["outcome_sha256"] == "b" * 64
        and amendment.payload["decision"] == "approve the digest-bound amendment"
        and amendment_replay.sha256 == amendment.sha256
        and record_path(worktree, amendment.record_id).read_bytes() == amendment_bytes,
    )

    expect_error(
        "stale expected predecessor cannot overwrite a newer decision",
        lambda: append_raise(
            worktree,
            raised_payload(worktree, "escalation-stale", "stale writer"),
            expected_record_sha256=first_resolution.sha256,
        ),
        "latest record changed",
    )
    check(
        "stale writer leaves the authoritative pointer unchanged",
        load_latest(worktree).sha256 == amendment.sha256,
    )

    record = record_path(worktree, second.record_id)
    original = record.read_bytes()
    record.write_bytes(original.replace(b"second decision", b"tamper decision"))
    expect_error(
        "record tamper fails closed",
        lambda: load_chain(worktree),
        "digest",
    )
    record.write_bytes(original)

    copied = Path(raw) / "copied-task"
    copied.mkdir()
    meta(copied, task_id="task-b")
    shutil.copytree(
        worktree / ".task-escalation-records",
        copied / ".task-escalation-records",
    )
    shutil.copy2(marker_path, copied / marker_path.name)
    expect_error(
        "record chain is bound to its originating task identity",
        lambda: load_chain(copied),
        "origin",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-duplicate.") as raw:
    worktree = Path(raw)
    meta(worktree)
    append_raise(
        worktree,
        raised_payload(worktree, "duplicate-id", "original payload"),
    )
    expect_error(
        "duplicate record identity with changed payload fails closed",
        lambda: append_raise(
            worktree,
            raised_payload(worktree, "duplicate-id", "changed payload"),
        ),
        "record identity",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-legacy.") as raw:
    worktree = Path(raw)
    meta(worktree)
    legacy = raised_payload(worktree, "legacy-escalation", "legacy full marker")
    write_json(worktree / ".task-needs-attention.json", legacy)
    legacy_view = load_latest(worktree)
    check(
        "legacy full marker remains read-compatible without implicit mutation",
        legacy_view.legacy is True
        and legacy_view.payload == legacy
        and not (worktree / ".task-escalation-records").exists(),
    )
    resolved = append_resolution(
        worktree,
        "resolve the legacy escalation",
        resolved_at="2026-08-04T12:04:00Z",
    )
    legacy_chain = load_chain(worktree)
    check(
        "first legacy resolve deterministically backfills before resolution",
        len(legacy_chain) == 2
        and legacy_chain[0].legacy is False
        and legacy_chain[0].record_type == "raise"
        and legacy_chain[0].payload == legacy
        and legacy_chain[1].sha256 == resolved.sha256
        and json.loads((worktree / ".task-needs-attention.json").read_text())
        == {
            "schema_version": 2,
            "record_id": resolved.record_id,
            "record_sha256": resolved.sha256,
        },
    )


print("All task escalation record tests passed.")
