#!/usr/bin/env python3
"""Build the exact identity-bound response for a repaired verification HEAD."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn

from harness.verification_attempt import (
    VerificationAttempt,
    VerificationAttemptError,
    verification_resolution_authorizes,
)
from task_escalation_records import EscalationRecordError, load_latest


PACKET_NAME = ".task-verification.json"
RESPONSE_NAME = ".task-verification-response.json"
MAX_BYTES = 65_536
GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")
PACKET_FIELDS_V1 = {
    "schema_version",
    "operation_id",
    "verification_operation_id",
    "verification_lane_id",
    "verification_run_id",
    "definition_sha256",
    "step_id",
    "head_sha",
    "status",
    "reason",
    "safe_boundary",
    "allowed_responses",
    "response_pointer",
    "receipt_pointer",
    "evidence",
}
PACKET_FIELDS_V2 = PACKET_FIELDS_V1 | {
    "verification_attempt",
    "verification_attempt_sha256",
}


class ResubmitError(RuntimeError):
    pass


def die(message: str, code: int = 2) -> NoReturn:
    print(f"pipeline-verification-resubmit: {message}", file=os.sys.stderr)
    raise SystemExit(code)


def _read_packet(path: Path) -> tuple[dict[str, object], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ResubmitError("verification packet must be a regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_BYTES:
        raise ResubmitError("verification packet is empty or oversized")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in {1, 2}
        or set(value)
        != (
            PACKET_FIELDS_V2
            if value.get("schema_version") == 2
            else PACKET_FIELDS_V1
        )
        or value.get("status") != "attention-required"
        or value.get("reason") != "verification-failed"
        or value.get("step_id") != "verify"
        or value.get("response_pointer") != RESPONSE_NAME
        or not isinstance(value.get("allowed_responses"), list)
        or not all(
            isinstance(item, str) for item in value["allowed_responses"]
        )
        or not GIT_OID.fullmatch(str(value.get("head_sha") or ""))
    ):
        raise ResubmitError("verification packet contract is invalid")
    if value["schema_version"] == 2:
        try:
            attempt = VerificationAttempt.from_dict(
                value.get("verification_attempt")
            )
        except VerificationAttemptError as exc:
            raise ResubmitError("verification packet attempt is invalid") from exc
        if (
            value.get("verification_attempt_sha256") != attempt.sha256
            or attempt.parent_operation_id != value.get("operation_id")
            or attempt.exact_head_sha != value.get("head_sha")
        ):
            raise ResubmitError("verification packet attempt is invalid")
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    return value, canonical


def _head(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode or not GIT_OID.fullmatch(value):
        raise ResubmitError("current Git HEAD is unavailable")
    return value


def _same_head_response(
    worktree: Path,
    packet: dict[str, object],
    canonical: bytes,
    escalation_id: str,
) -> dict[str, object]:
    if packet.get("schema_version") != 2:
        raise ResubmitError(
            "same-HEAD retry requires a versioned VerificationAttempt packet"
        )
    if "retry-mechanism-flake" not in packet["allowed_responses"]:
        raise ResubmitError("same-HEAD mechanism-flake retry is not authorized")
    try:
        failed_attempt = VerificationAttempt.from_dict(
            packet.get("verification_attempt")
        )
        next_attempt = failed_attempt.same_head_retry()
    except VerificationAttemptError as exc:
        raise ResubmitError(str(exc)) from exc
    try:
        decision_record = load_latest(worktree)
    except EscalationRecordError as exc:
        raise ResubmitError(
            "same-HEAD mechanism-flake authorization is invalid"
        ) from exc
    payload = decision_record.payload if decision_record is not None else {}
    if (
        decision_record is None
        or decision_record.record_type != "resolution"
        or payload.get("status") != "resolved"
        or payload.get("category") != "mechanism-failure"
        or payload.get("id") != escalation_id
        or Path(str(payload.get("worktree") or "")).expanduser().resolve()
        != worktree
        or not str(payload.get("reason") or "").startswith(
            "verification-mechanism-flake:"
        )
        or not verification_resolution_authorizes(
            payload.get("verification_resolution"),
            failed_attempt,
            str(packet["verification_operation_id"]),
        )
    ):
        raise ResubmitError(
            "same-HEAD mechanism-flake authorization is invalid"
        )
    return {
        "schema_version": 2,
        "operation_id": packet["operation_id"],
        "verification_operation_id": packet["verification_operation_id"],
        "failed_head_sha": packet["head_sha"],
        "packet_sha256": hashlib.sha256(canonical).hexdigest(),
        "response": "retry-mechanism-flake",
        "resubmitted_head_sha": packet["head_sha"],
        "failed_attempt_sha256": failed_attempt.sha256,
        "next_attempt": next_attempt.as_dict(),
        "next_attempt_sha256": next_attempt.sha256,
        "mechanism_flake_decision_id": escalation_id,
        "mechanism_flake_decision_sha256": decision_record.sha256,
    }


def build_response(
    worktree: Path, *, same_head_mechanism_flake: str = ""
) -> dict[str, object]:
    packet, canonical = _read_packet(worktree / PACKET_NAME)
    failed_head = str(packet["head_sha"])
    current_head = _head(worktree)
    if current_head == failed_head:
        if not same_head_mechanism_flake:
            raise ResubmitError(
                "verification repair must commit a new HEAD or provide exact "
                "same-HEAD mechanism-flake authorization"
            )
        return _same_head_response(
            worktree, packet, canonical, same_head_mechanism_flake
        )
    if same_head_mechanism_flake:
        raise ResubmitError(
            "same-HEAD mechanism-flake authorization cannot replace changed-HEAD repair"
        )
    if "fix-and-resubmit" not in packet["allowed_responses"]:
        raise ResubmitError("changed-HEAD fix-and-resubmit is not authorized")
    return {
        "schema_version": 1,
        "operation_id": packet["operation_id"],
        "verification_operation_id": packet["verification_operation_id"],
        "failed_head_sha": failed_head,
        "packet_sha256": hashlib.sha256(canonical).hexdigest(),
        "response": "fix-and-resubmit",
        "resubmitted_head_sha": current_head,
    }


def write_response(worktree: Path, response: dict[str, object]) -> str:
    path = worktree / RESPONSE_NAME
    if path.is_symlink():
        raise ResubmitError("verification response cannot be a symlink")
    encoded = (
        json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if path.is_file() and path.read_bytes() == encoded:
        return "already-ready"
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=worktree)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument(
        "--same-head-mechanism-flake",
        default="",
        metavar="ESCALATION_ID",
        help=(
            "consume one exact resolved mechanism-flake decision for attempt 1"
        ),
    )
    args = parser.parse_args()
    worktree = args.worktree.expanduser().resolve()
    if not worktree.is_dir():
        die("worktree must be an existing directory")
    try:
        response = build_response(
            worktree,
            same_head_mechanism_flake=args.same_head_mechanism_flake,
        )
        status = write_response(worktree, response)
    except (ResubmitError, OSError, ValueError, json.JSONDecodeError) as exc:
        die(str(exc))
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "operation_id": response["operation_id"],
                "response": RESPONSE_NAME,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
