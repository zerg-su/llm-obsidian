#!/usr/bin/env python3
"""Compile the bounded attempt budget and exact-head RC3 release disposition."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_routing_config import load_config  # noqa: E402


SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_ATTEMPTS = 5
ATTEMPT_CLASSIFICATIONS = frozenset({"published", "unpublished", "test-only"})
DEEP_REVIEW_ROLE = str(load_config(ROOT).reviewer_default("claude", "deep")["model"])
REVIEW_ROLES = frozenset({DEEP_REVIEW_ROLE, "independent-configured"})
REVIEW_VERDICTS = frozenset({"approved", "changes-requested", "blocked", "unavailable"})


class DispositionError(ValueError):
    """Reject ambiguous attempt accounting or drifted release evidence."""


def _exact_subject(root: Path, value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise DispositionError("release subject must be an exact Git SHA")
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{value}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise DispositionError("release subject commit is unavailable")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise DispositionError(f"{field} must be a SHA-256 digest")
    return value


def _validate_attempt(root: Path, row: object) -> dict[str, object]:
    required = {
        "attempt_id",
        "classification",
        "subject_head_sha",
        "profile_sha256",
        "receipt_sha256",
        "exit_status",
    }
    if not isinstance(row, dict) or set(row) != required:
        raise DispositionError("candidate attempt fields are invalid")
    if not isinstance(row["attempt_id"], str) or not row["attempt_id"].strip():
        raise DispositionError("candidate attempt_id is required")
    if row["classification"] not in ATTEMPT_CLASSIFICATIONS:
        raise DispositionError("candidate attempt classification is invalid")
    _exact_subject(root, row["subject_head_sha"])
    _digest(row["profile_sha256"], "candidate profile")
    _digest(row["receipt_sha256"], "candidate receipt")
    if type(row["exit_status"]) is not int or not 0 <= row["exit_status"] <= 255:
        raise DispositionError("candidate attempt exit status is invalid")
    return row


def validate_attempt_ledger(root: Path, ledger: object) -> list[dict[str, object]]:
    if (
        not isinstance(ledger, dict)
        or set(ledger) != {"schema_version", "release", "attempts"}
        or ledger.get("schema_version") != 1
        or ledger.get("release") != "2.6.6-rc3"
        or not isinstance(ledger.get("attempts"), list)
    ):
        raise DispositionError("candidate attempt ledger schema is invalid")
    attempts = [_validate_attempt(root, row) for row in ledger["attempts"]]
    identities = [str(row["attempt_id"]) for row in attempts]
    if len(identities) != len(set(identities)):
        raise DispositionError("candidate attempt identities must be unique")
    if len(attempts) > MAX_ATTEMPTS:
        raise DispositionError("sixth full-profile attempt has zero authority")
    return attempts


def evaluate_attempt_budget(root: Path, ledger: object) -> dict[str, object]:
    attempts = validate_attempt_ledger(root, ledger)
    consumed = len(attempts)
    counts = Counter(str(row["classification"]) for row in attempts)
    return {
        "schema_version": 1,
        "maximum_attempts": MAX_ATTEMPTS,
        "attempts_consumed": consumed,
        "attempts_remaining": MAX_ATTEMPTS - consumed,
        "state": "exhausted" if consumed == MAX_ATTEMPTS else "available",
        "next_attempt_ordinal": None if consumed == MAX_ATTEMPTS else consumed + 1,
        "classification_counts": {
            classification: counts.get(classification, 0)
            for classification in sorted(ATTEMPT_CLASSIFICATIONS)
        },
    }


def _gate_receipt(raw: bytes, subject_head_sha: str) -> dict[str, object]:
    try:
        gate = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispositionError("release gate receipt is invalid JSON") from exc
    if not isinstance(gate, dict) or gate.get("schema_version") != 1:
        raise DispositionError("release gate receipt schema is invalid")
    if gate.get("subject_head_sha") != subject_head_sha:
        raise DispositionError("gate subject does not match candidate HEAD")
    if gate.get("status") != "passed":
        raise DispositionError("release gate receipt is not passed")
    profile = _digest(gate.get("profile_sha256"), "release gate profile")
    commands = gate.get("commands")
    if not isinstance(commands, list) or not commands:
        raise DispositionError("release gate commands are missing")
    for command in commands:
        if (
            not isinstance(command, dict)
            or command.get("exit_code") != 0
            or not isinstance(command.get("command_id"), str)
            or not command["command_id"]
            or not DIGEST.fullmatch(str(command.get("output_sha256") or ""))
        ):
            raise DispositionError("release gate command evidence is invalid")
    return {"profile_sha256": profile, "receipt_sha256": hashlib.sha256(raw).hexdigest()}


def _reviews(rows: object) -> list[dict[str, str]]:
    required = {"role", "review_id", "verdict", "receipt_sha256"}
    if not isinstance(rows, list):
        raise DispositionError("release review verdicts must be a list")
    values = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise DispositionError("release review verdict fields are invalid")
        if row["role"] not in REVIEW_ROLES or row["verdict"] not in REVIEW_VERDICTS:
            raise DispositionError("release review role or verdict is invalid")
        if not isinstance(row["review_id"], str) or not row["review_id"].strip():
            raise DispositionError("release review identity is required")
        _digest(row["receipt_sha256"], "release review receipt")
        values.append(dict(row))
    if {row["role"] for row in values} != REVIEW_ROLES or len(values) != 2:
        raise DispositionError("both configured review roles are required exactly once")
    return sorted(values, key=lambda row: row["role"])


def _findings(rows: object) -> list[dict[str, str]]:
    if not isinstance(rows, list) or not rows:
        raise DispositionError("release finding dispositions are required")
    values = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("finding_id"), str):
            raise DispositionError("release finding is invalid")
        kind = row.get("disposition")
        expected = {
            "fixed": {"finding_id", "disposition", "evidence_sha256"},
            "waived": {"finding_id", "disposition", "waiver_id"},
            "blocked": {"finding_id", "disposition", "rationale"},
        }.get(kind)
        if expected is None or set(row) != expected:
            raise DispositionError("release finding disposition fields are invalid")
        proof = row.get("evidence_sha256")
        if kind == "fixed":
            _digest(proof, "release finding evidence")
        elif not isinstance(row.get("waiver_id") if kind == "waived" else row.get("rationale"), str):
            raise DispositionError("release finding disposition detail is invalid")
        values.append(dict(row))
    identities = [row["finding_id"] for row in values]
    if len(identities) != len(set(identities)):
        raise DispositionError("release finding identities must be unique")
    return sorted(values, key=lambda row: row["finding_id"])


def _waivers(rows: object, findings: list[dict[str, str]]) -> list[dict[str, str]]:
    required = {"waiver_id", "finding_id", "approved_by", "rationale", "evidence_sha256"}
    if not isinstance(rows, list):
        raise DispositionError("release waivers must be a list")
    values = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise DispositionError("release waiver fields are invalid")
        if not all(isinstance(row[key], str) and row[key].strip() for key in required):
            raise DispositionError("release waiver values are invalid")
        _digest(row["evidence_sha256"], "release waiver evidence")
        values.append(dict(row))
    required_pairs = {
        (row["waiver_id"], row["finding_id"])
        for row in findings
        if row["disposition"] == "waived"
    }
    actual_pairs = {(row["waiver_id"], row["finding_id"]) for row in values}
    if len(values) != len(actual_pairs) or actual_pairs != required_pairs:
        raise DispositionError("waiver coverage is incomplete or contains unused authority")
    return sorted(values, key=lambda row: row["waiver_id"])


def compile_disposition(
    root: Path,
    *,
    subject_head_sha: str,
    gate_receipt_bytes: bytes,
    attempt_ledger: object,
    reviews: object,
    findings: object,
    waivers: object,
) -> dict[str, object]:
    subject = _exact_subject(root, subject_head_sha)
    gate = _gate_receipt(gate_receipt_bytes, subject)
    attempts = validate_attempt_ledger(root, attempt_ledger)
    matching = [
        row
        for row in attempts
        if row["subject_head_sha"] == subject
        and row["profile_sha256"] == gate["profile_sha256"]
        and row["receipt_sha256"] == gate["receipt_sha256"]
        and row["classification"] == "published"
        and row["exit_status"] == 0
    ]
    if len(matching) != 1:
        raise DispositionError("release gate is not bound to one counted published attempt")
    checked_reviews = _reviews(reviews)
    checked_findings = _findings(findings)
    checked_waivers = _waivers(waivers, checked_findings)
    blocked = [
        f"review:{row['role']}:{row['verdict']}"
        for row in checked_reviews
        if row["verdict"] != "approved"
    ]
    blocked.extend(
        f"finding:{row['finding_id']}:blocked"
        for row in checked_findings
        if row["disposition"] == "blocked"
    )
    inputs = {
        "subject_head_sha": subject,
        "gate": gate,
        "attempt_ledger": attempt_ledger,
        "reviews": checked_reviews,
        "findings": checked_findings,
        "waivers": checked_waivers,
    }
    input_digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "release": "2.6.6-rc3",
        **inputs,
        "attempt_budget": evaluate_attempt_budget(root, attempt_ledger),
        "input_sha256": input_digest,
        "outcome": "blocked" if blocked else "approved",
        "blocked_reasons": blocked,
    }


def validate_disposition(
    root: Path, payload: object, *, gate_receipt_bytes: bytes
) -> None:
    if not isinstance(payload, dict):
        raise DispositionError("release disposition must be an object")
    gate = payload.get("gate")
    if not isinstance(gate, dict) or gate.get("receipt_sha256") != hashlib.sha256(gate_receipt_bytes).hexdigest():
        raise DispositionError("gate receipt digest drift")
    rebuilt = compile_disposition(
        root,
        subject_head_sha=payload.get("subject_head_sha"),
        gate_receipt_bytes=gate_receipt_bytes,
        attempt_ledger=payload.get("attempt_ledger"),
        reviews=payload.get("reviews"),
        findings=payload.get("findings"),
        waivers=payload.get("waivers"),
    )
    if payload != rebuilt:
        raise DispositionError("release disposition bytes do not match compiled evidence")


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispositionError(f"cannot load release evidence: {path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    budget = subparsers.add_parser("budget")
    budget.add_argument("ledger", type=Path)
    compile_cmd = subparsers.add_parser("compile")
    compile_cmd.add_argument("--subject-head-sha", required=True)
    compile_cmd.add_argument("--gate-receipt", type=Path, required=True)
    compile_cmd.add_argument("--attempt-ledger", type=Path, required=True)
    compile_cmd.add_argument("--reviews", type=Path, required=True)
    compile_cmd.add_argument("--findings", type=Path, required=True)
    compile_cmd.add_argument("--waivers", type=Path, required=True)
    check = subparsers.add_parser("check")
    check.add_argument("disposition", type=Path)
    check.add_argument("--gate-receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "budget":
            result = evaluate_attempt_budget(ROOT, _load(args.ledger))
        elif args.command == "compile":
            result = compile_disposition(
                ROOT,
                subject_head_sha=args.subject_head_sha,
                gate_receipt_bytes=args.gate_receipt.read_bytes(),
                attempt_ledger=_load(args.attempt_ledger),
                reviews=_load(args.reviews),
                findings=_load(args.findings),
                waivers=_load(args.waivers),
            )
        else:
            validate_disposition(
                ROOT,
                _load(args.disposition),
                gate_receipt_bytes=args.gate_receipt.read_bytes(),
            )
            result = {"status": "valid"}
        print(json.dumps(result, indent=2, sort_keys=True))
    except (DispositionError, OSError) as exc:
        print(f"RC3 release disposition: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
