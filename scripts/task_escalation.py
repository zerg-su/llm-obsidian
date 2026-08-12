#!/usr/bin/env python3
"""Relay a task stop condition through the owning coordinator surface."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event
from task_contract import ContractError, normalize_for_runtime
from task_escalation_records import (
    EscalationRecordError,
    append_amendment,
    append_delivery_failure,
    append_raise,
    append_resolution,
    load_latest,
)
from harness.adapters.cmux import run_cmux
from harness.artifact_repair import (
    build_verification_escalation,
    resolve_verification_escalation,
)
from harness.verification_attempt import (
    VerificationAttempt,
    VerificationAttemptError,
)


CATEGORIES = {
    "blocking-review",
    "scope",
    "public-interface",
    "migration",
    "security",
    "external-effect",
    "contract-drift",
    "mechanism-failure",
    "pipeline-decision",
    "permission",
}

MECHANISM_REPAIR_POLICY = "classify-and-auto-repair-if-eligible"


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(value: str, field: str, limit: int = 2000) -> str:
    value = " ".join(value.split()).strip()
    if not value or len(value) > limit:
        die(f"{field} must contain 1..{limit} characters")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        die(f"{path} must contain an object")
    return value


def read_surface(worktree: Path, meta: dict[str, Any], key: str, fallback: str) -> str:
    value = str(meta.get(key) or "").strip()
    path = worktree / fallback
    if not value and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    if not value:
        die(f"missing {key} surface metadata")
    return value


def send(surface: str, message: str, *, clear_codex: bool = False) -> None:
    if clear_codex:
        for _ in range(40):
            run_cmux(["send-key", "--surface", surface, "backspace"])
    sent = run_cmux(["send", "--surface", surface, message])
    if sent.returncode != 0:
        die((sent.stdout + sent.stderr).strip() or "cmux send failed", 3)
    time.sleep(0.2)
    entered = run_cmux(["send-key", "--surface", surface, "Enter"])
    if entered.returncode != 0:
        die((entered.stdout + entered.stderr).strip() or "cmux send-key failed", 3)


def load_unattended(worktree: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = read_json(worktree / ".task-meta.json")
    try:
        policy = normalize_for_runtime(meta, worktree)
    except ContractError as exc:
        die(str(exc))
    if policy["interaction_policy"] != "unattended":
        die("task escalation relay is only for unattended tasks")
    return meta, policy


def notify(surface: str, title: str, body: str) -> None:
    result = run_cmux(["notify", "--surface", surface, "--title", title, "--body", body])
    if result.returncode != 0:
        die((result.stdout + result.stderr).strip() or "cmux notify failed", 3)


def raise_escalation(worktree: Path, category: str, reason: str, question: str) -> int:
    meta, _ = load_unattended(worktree)
    coordinator = read_surface(worktree, meta, "wiki_surface", ".wiki-cmux-surface")
    task_surface = read_surface(worktree, meta, "task_surface", ".task-cmux-surface")
    task_name = compact(str(meta.get("task_name") or "task"), "task_name", 200)
    marker = {
        "version": 1,
        "id": str(uuid.uuid4()),
        "status": "pending",
        "task_name": task_name,
        "category": category,
        "reason": compact(reason, "reason"),
        "question": compact(question, "question"),
        "worktree": str(worktree),
        "task_surface": task_surface,
        "raised_at": utc_now(),
    }
    if category == "mechanism-failure":
        marker["coordinator_policy"] = MECHANISM_REPAIR_POLICY
        packet_path = worktree / ".task-verification.json"
        if reason.startswith("verification-mechanism-flake:") and packet_path.is_file():
            try:
                packet = read_json(packet_path)
                attempt = VerificationAttempt.from_dict(
                    packet.get("verification_attempt")
                )
                marker["verification_escalation"] = build_verification_escalation(
                    attempt, str(packet.get("verification_operation_id") or "")
                )
            except VerificationAttemptError as exc:
                die(f"verification escalation authority is invalid: {exc}", 3)
    marker_path = worktree / ".task-needs-attention.json"
    try:
        raised = append_raise(worktree, marker)
    except EscalationRecordError as exc:
        die(str(exc), 3)
    title = f"Task {task_name} needs a decision"
    if category == "mechanism-failure":
        action = (
            "Coordinator: classify now. Auto-repair only when the defect is repo-owned, "
            "local, reproducible, reversible, scope-preserving, and has no permission, "
            "security, dependency, public-interface, migration, destructive, or external-effect "
            "boundary; otherwise ask the user once. "
        )
    else:
        action = "Coordinator decision required. "
    body = (
        f"{category}: {marker['reason']} Requested decision: {marker['question']} {action}"
        f"Task remains paused. Resolve with task_escalation.py resolve --worktree "
        f"{shlex.quote(str(worktree))}."
    )
    wake = (
        "Typed task escalation callback received. "
        f"Category: {category}. "
        f"Reason: {marker['reason'][:800]}. "
        f"Requested decision: {marker['question'][:800]}. "
        f"Inspect {marker_path} and resolve from this originating coordinator "
        f"session with {Path(__file__).name} resolve --worktree "
        f"{shlex.quote(str(worktree))} --decision <decision>. "
        "The task remains paused until that decision is relayed."
    )
    if len(wake.encode()) > 4096:
        die("task escalation callback exceeds the bounded cmux message size", 3)
    toast_failed = False
    try:
        notify(coordinator, title, body)
    except SystemExit:
        toast_failed = True
    try:
        send(coordinator, wake)
    except SystemExit:
        try:
            append_delivery_failure(
                worktree,
                expected_record_sha256=raised.sha256,
            )
        except EscalationRecordError as exc:
            die(str(exc), 3)
        emit_lifecycle_event(
            worktree,
            "task-escalation",
            actor=f"raise:{category}",
            counts={"raised": 1, "delivery_failures": 1},
            status="error",
        )
        raise
    emit_lifecycle_event(
        worktree,
        "task-escalation",
        actor=f"raise:{category}",
        counts={
            "raised": 1,
            **({"toast_failures": 1} if toast_failed else {}),
        },
    )
    print(f"escalation {marker['id']} sent to coordinator; task must remain paused")
    return 0


def resolve_escalation(worktree: Path, decision: str) -> int:
    meta, _ = load_unattended(worktree)
    try:
        latest = load_latest(worktree)
    except EscalationRecordError as exc:
        die(str(exc), 3)
    if latest is None:
        die("there is no unresolved task escalation", 3)
    marker = latest.payload
    unresolved_status = str(marker.get("status") or "")
    answer = compact(decision, "decision")
    replay = unresolved_status == "resolved" and marker.get("decision") == answer
    if unresolved_status not in {"pending", "delivery-failed"} and not replay:
        die("there is no unresolved task escalation", 3)
    current = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"
    if current == "unknown" or current != str(meta.get("origin_session") or ""):
        die("only the originating coordinator session may resolve this escalation", 3)
    task_surface = read_surface(worktree, meta, "task_surface", ".task-cmux-surface")
    typed_resolution = None
    typed_escalation = marker.get("verification_escalation")
    if typed_escalation is not None:
        try:
            typed_resolution = resolve_verification_escalation(
                typed_escalation,
                action=answer,
                evidence_note="Coordinator classified the exact verification attempt.",
            )
        except VerificationAttemptError as exc:
            die(f"verification resolution is invalid: {exc}", 3)
    try:
        resolved = append_resolution(
            worktree,
            answer,
            verification_resolution=typed_resolution,
        )
    except EscalationRecordError as exc:
        die(str(exc), 3)
    send(
        task_surface,
        f"[Coordinator decision for escalation {marker.get('id')}] {answer} "
        "Continue only within this decision and the approved plan; escalate again on further drift.",
        clear_codex=str(meta.get("executor_runtime") or meta.get("runtime") or "") == "codex",
    )
    duration = elapsed_ms(
        resolved.payload.get("raised_at"), resolved.payload.get("resolved_at")
    )
    emit_lifecycle_event(
        worktree,
        "task-escalation",
        actor=f"resolve:{marker.get('category') or 'unknown'}",
        counts={"resolved": 1, **({"duration_ms": duration} if duration is not None else {})},
    )
    print(f"decision relayed to task surface {task_surface}")
    return 0


def record_amendment(
    worktree: Path,
    plan_sha256: str,
    outcome_sha256: str,
    decision: str,
) -> int:
    meta, _ = load_unattended(worktree)
    current = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"
    if current == "unknown" or current != str(meta.get("origin_session") or ""):
        die("only the originating coordinator session may record an amendment", 3)
    try:
        record = append_amendment(
            worktree,
            plan_sha256=plan_sha256,
            outcome_sha256=outcome_sha256,
            decision=compact(decision, "decision"),
        )
    except EscalationRecordError as exc:
        die(str(exc), 3)
    emit_lifecycle_event(
        worktree,
        "task-escalation",
        actor="amendment",
        counts={"amendments": 1},
    )
    print(f"amendment {record.record_id} recorded")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    raised = sub.add_parser("raise")
    raised.add_argument("--worktree", default=".")
    raised.add_argument("--category", choices=sorted(CATEGORIES), required=True)
    raised.add_argument("--reason", required=True)
    raised.add_argument("--question", required=True)
    resolved = sub.add_parser("resolve")
    resolved.add_argument("--worktree", default=".")
    resolved.add_argument("--decision", required=True)
    amendment = sub.add_parser("record-amendment")
    amendment.add_argument("--worktree", default=".")
    amendment.add_argument("--plan-sha256", required=True)
    amendment.add_argument("--outcome-sha256", required=True)
    amendment.add_argument("--decision", required=True)
    args = parser.parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    if args.command == "raise":
        return raise_escalation(worktree, args.category, args.reason, args.question)
    if args.command == "resolve":
        return resolve_escalation(worktree, args.decision)
    return record_amendment(
        worktree,
        args.plan_sha256,
        args.outcome_sha256,
        args.decision,
    )


if __name__ == "__main__":
    raise SystemExit(main())
