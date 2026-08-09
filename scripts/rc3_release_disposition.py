#!/usr/bin/env python3
"""Compile RC3 release disposition from exact immutable evidence bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rc3_attempt_ledger import (  # noqa: E402
    MAX_ATTEMPTS,
    AttemptLedgerError,
    AttemptLedgerStore,
)
from verification_receipt import ReceiptError, verify_receipt  # noqa: E402
from harness.ephemeral_provider import (  # noqa: E402
    EphemeralProviderError,
    _load_schema,
    validate_output_instance,
)
from harness.review_program_contracts import (  # noqa: E402
    ReviewBoundaryInput,
    ReviewProgramError,
)
from harness.verification import load_profiles  # noqa: E402
from model_routing_config import load_tracked_config  # noqa: E402
from outcome_contract import OutcomeContractError, extract_from_bytes  # noqa: E402
from review_contract import ReviewContractError, validate_finding  # noqa: E402
from review_contract import VERDICTS as REVIEW_VERDICTS  # noqa: E402


SHA = re.compile(r"[0-9a-f]{40,64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ROUTING = load_tracked_config(ROOT)
#: 2.6.6 release evidence is immutable: it stays bound to the finalization
#: routes registered when that evidence was produced (the exact routing
#: config committed at the v2.6.6-rc4-fix3 tag), not to whatever the live
#: routing config registers later (2.6.7 re-registered both aliases).
_EVIDENCE_ERA_SHA = "5ac90ef94029371507320d900f32c4c0b254d790"


def _evidence_era_route(alias: str) -> dict[str, str]:
    shown = subprocess.run(
        ["git", "show", f"{_EVIDENCE_ERA_SHA}:config/model-routing.toml"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    config = tomllib.loads(shown.stdout)
    route = config["finalization_routes"][alias]
    aliases = config.get("model_aliases", {})
    target = aliases.get(route["model"], {}).get("target", route["model"])
    return {
        "alias": alias,
        "runtime": str(route["runtime"]),
        "model": str(target),
        "effort": str(route["effort"]),
    }


_INDEPENDENT_ROUTE = _evidence_era_route("finalization-independent")
_INDEPENDENT_ROLE = _INDEPENDENT_ROUTE["model"]
_REVIEW_PROFILE = load_profiles(ROOT / "config/verification-profiles.toml")[
    "scoped"
]
_PRIMARY_ROUTE = _evidence_era_route("finalization-primary")
ROLE_AXES = {
    _INDEPENDENT_ROLE: "anthropic-holistic",
    "independent-configured": "openai-holistic",
    "holistic-primary": "anthropic-holistic",
}
ROLE_ROUTES = {
    _INDEPENDENT_ROLE: _INDEPENDENT_ROUTE,
    "independent-configured": _ROUTING.reviewer_default("codex", "simple"),
    "holistic-primary": _PRIMARY_ROUTE,
}
#: The exact release review role set each release is allowed to bind.  RC3 ran
#: two configured holistic roles; RC4's approved boundary is one Simple holistic
#: session, so requiring two roles would make a valid RC4 disposition impossible.
#: This is a release-scoped vocabulary, not a new lane, provider, or model.
RELEASE_ROLE_SETS = {
    "2.6.6-rc3": frozenset({_INDEPENDENT_ROLE, "independent-configured"}),
    "2.6.6-rc4": frozenset({"holistic-primary"}),
}
DEFAULT_RELEASE = "2.6.6-rc3"
SCHEMA_PATH = ROOT / "schemas/rc3-release-disposition-v1.schema.json"


class DispositionError(ValueError):
    """Release evidence is incomplete, drifted, or not independently bound."""


def _exact_subject(root: Path, value: object) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise DispositionError("release subject must be an exact Git SHA")
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{value}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode or result.stdout.strip() != value:
        raise DispositionError("release subject commit is unavailable or abbreviated")
    return value


def _bytes(path: Path, label: str) -> bytes:
    source = path.expanduser()
    path = source.resolve()
    if source.is_symlink() or not path.is_file():
        raise DispositionError(f"{label} is unavailable")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DispositionError(f"{label} is unreadable") from exc


def _json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _bytes(path, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispositionError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise DispositionError(f"{label} must be an object")
    return value, raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_payload_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _matches_boundary_pointer(root: Path, path: Path, pointer: str) -> bool:
    root = root.expanduser().resolve()
    if not root.is_dir() or not pointer or Path(pointer).is_absolute():
        return False
    source = root
    for part in Path(pointer).parts:
        if part in {"", ".", ".."}:
            return False
        source /= part
        if source.is_symlink():
            return False
    expected = source.resolve()
    supplied = path.expanduser()
    actual = supplied.resolve()
    return (
        expected != root
        and root in expected.parents
        and expected.is_file()
        and not supplied.is_symlink()
        and actual == expected
    )


def _release_boundary(
    path: Path,
    *,
    artifact_root: Path,
    subject: str,
    plan_path: Path,
    outcome_evidence_path: Path,
    accepted_deviations_path: Path,
) -> ReviewBoundaryInput:
    value, _raw = _json(path, "release review boundary")
    try:
        boundary = ReviewBoundaryInput.from_mapping(value)
    except ReviewProgramError as exc:
        raise DispositionError("release review boundary is invalid") from exc
    if boundary.purpose != "release":
        raise DispositionError("release review boundary purpose is invalid")
    if boundary.integration_head_sha != subject:
        raise DispositionError("release review boundary integration HEAD is stale")

    plan_raw = _bytes(plan_path, "release plan")
    if _sha256(plan_raw) != boundary.plan_sha256:
        raise DispositionError("release review boundary plan digest is stale")
    try:
        outcome_contract = extract_from_bytes(plan_raw)
    except OutcomeContractError as exc:
        raise DispositionError("release Outcome Contract is invalid") from exc
    if outcome_contract.sha256 != boundary.outcome_contract_sha256:
        raise DispositionError(
            "release review boundary Outcome Contract digest is stale"
        )

    outcome_evidence_raw = _bytes(
        outcome_evidence_path, "release outcome evidence"
    )
    if not _matches_boundary_pointer(
        artifact_root,
        outcome_evidence_path,
        boundary.outcome_evidence_map_path,
    ):
        raise DispositionError(
            "release review boundary outcome evidence path is stale"
        )
    if _sha256(outcome_evidence_raw) != boundary.outcome_evidence_map_sha256:
        raise DispositionError(
            "release review boundary outcome evidence digest is stale"
        )
    accepted_deviations_raw = _bytes(
        accepted_deviations_path, "release accepted deviations"
    )
    if not _matches_boundary_pointer(
        artifact_root,
        accepted_deviations_path,
        boundary.accepted_deviations_path,
    ):
        raise DispositionError(
            "release review boundary accepted deviations path is stale"
        )
    if _sha256(accepted_deviations_raw) != boundary.accepted_deviations_sha256:
        raise DispositionError(
            "release review boundary accepted deviations digest is stale"
        )
    return boundary


def _gate(path: Path, subject: str) -> dict[str, str]:
    try:
        value = verify_receipt(
            path,
            expected_head=subject,
            expected_profile="release-final",
        )
    except (OSError, ReceiptError, ValueError) as exc:
        raise DispositionError("release gate receipt is not canonical") from exc
    return {
        "attempt_id": str(value["attempt_id"]),
        "profile_sha256": str(value["profile_sha256"]),
        "receipt_sha256": _sha256(_bytes(path, "release gate receipt")),
    }


def _attempts(ledger_root: Path, gate: dict[str, str], subject: str) -> dict[str, Any]:
    try:
        value = AttemptLedgerStore(ledger_root).load()
    except AttemptLedgerError as exc:
        raise DispositionError("release attempt ledger is invalid") from exc
    rows = value["attempts"]
    if value.get("maximum_attempts") != MAX_ATTEMPTS:
        raise DispositionError("release attempt extension is not authorized")
    if any(row["state"] == "reserved" for row in rows):
        raise DispositionError("release attempt ledger has an unfinished reservation")
    matching = [
        row
        for row in rows
        if row["attempt_id"] == gate["attempt_id"]
        and row["subject_head_sha"] == subject
        and row["profile_sha256"] == gate["profile_sha256"]
        and row["artifact_sha256"] == gate["receipt_sha256"]
        and row["state"] == "published"
        and row["exit_status"] == 0
    ]
    if len(matching) != 1:
        raise DispositionError("release gate is not one authoritative ledger attempt")
    counts = Counter(str(row["state"]) for row in rows)
    return {
        "ledger_sha256": _sha256(
            _bytes(AttemptLedgerStore(ledger_root).path, "release attempt ledger")
        ),
        "maximum_attempts": MAX_ATTEMPTS,
        "attempts_consumed": len(rows),
        "attempts_remaining": MAX_ATTEMPTS - len(rows),
        "state": "exhausted" if len(rows) == MAX_ATTEMPTS else "available",
        "classification_counts": {
            key: counts.get(key, 0)
            for key in ("published", "test-only", "unpublished")
        },
    }


def _review_bundle(
    role: str,
    meta_path: Path,
    callback_path: Path,
    subject: str,
    boundary_input_sha256: str,
) -> dict[str, Any]:
    expected_axis = ROLE_AXES.get(role)
    if expected_axis is None:
        raise DispositionError("release review role is invalid")
    meta, meta_raw = _json(meta_path, "release review metadata")
    callback, callback_raw = _json(callback_path, "release review callback")
    payload = callback.get("payload")
    profile = meta.get("verification_profile")
    route = meta.get("route")
    expected_route = ROLE_ROUTES[role]
    if meta.get("review_boundary_input_sha256") != boundary_input_sha256:
        raise DispositionError("release review boundary identity is invalid")
    if (
        meta.get("schema_version") != 1
        or meta.get("head_sha") != subject
        or meta.get("axis") != expected_axis
        or meta.get("review_purpose") != "release"
        or not isinstance(profile, dict)
        or profile.get("name") != _REVIEW_PROFILE.name
        or profile.get("sha256") != _REVIEW_PROFILE.sha256
        or not isinstance(route, dict)
        or route.get("runtime") != expected_route["runtime"]
        or route.get("model") != expected_route["model"]
        or not isinstance(route.get("effort"), str)
        or not route["effort"]
        or callback.get("schema_version") != 1
        or callback.get("kind") != "review"
        or callback.get("operation_id") != meta.get("operation_id")
        or callback.get("run_id") != meta.get("run_id")
        or not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("axis") != expected_axis
        or payload.get("parent_session_operation_id")
        != meta.get("parent_session_operation_id")
        or payload.get("verification_iteration") != meta.get("verification_iteration")
        or payload.get("verdict") not in REVIEW_VERDICTS
        or not isinstance(payload.get("findings"), list)
        or callback.get("payload_sha256") != _canonical_payload_sha256(payload)
        or not isinstance(callback.get("callback_id"), str)
        or not callback["callback_id"].strip()
    ):
        raise DispositionError("release review receipt identity is invalid")
    findings = payload["findings"]
    try:
        findings = [
            validate_finding(row, f"release review findings[{index}]")
            for index, row in enumerate(findings)
        ]
    except ReviewContractError as exc:
        raise DispositionError("release review findings are invalid") from exc
    finding_ids = [str(row["finding_id"]) for row in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise DispositionError("release review finding identities are duplicated")
    return {
        "role": role,
        "axis": expected_axis,
        "runtime": str(route["runtime"]),
        "model": str(route["model"]),
        "effort": str(route["effort"]),
        "review_id": str(meta.get("review_id") or ""),
        "callback_id": str(callback["callback_id"]),
        "verdict": str(payload["verdict"]),
        "profile_sha256": str(profile["sha256"]),
        "review_boundary_input_sha256": boundary_input_sha256,
        "receipt_sha256": hashlib.sha256(meta_raw + b"\0" + callback_raw).hexdigest(),
        "finding_ids": sorted(finding_ids),
    }


def _release_identity(spec: Mapping[str, Any]) -> str:
    """Resolve which release vocabulary this manifest binds."""

    release = spec.get("release", DEFAULT_RELEASE)
    if release not in RELEASE_ROLE_SETS:
        raise DispositionError("release identity is not a supported release")
    return str(release)


def _reviews(
    spec_path: Path, subject: str, boundary_input_sha256: str
) -> tuple[str, list[dict[str, Any]]]:
    spec, _raw = _json(spec_path, "release review manifest")
    rows = spec.get("reviews")
    release = _release_identity(spec)
    expected_roles = RELEASE_ROLE_SETS[release]
    if (
        spec.get("schema_version") != 1
        or spec.get("subject_head_sha") != subject
        or not isinstance(rows, list)
        or len(rows) != len(expected_roles)
    ):
        raise DispositionError("release review manifest is invalid")
    base = spec_path.expanduser().resolve().parent
    values = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"role", "meta", "callback"}:
            raise DispositionError("release review manifest row is invalid")
        values.append(
            _review_bundle(
                str(row["role"]),
                base / str(row["meta"]),
                base / str(row["callback"]),
                subject,
                boundary_input_sha256,
            )
        )
    if {row["role"] for row in values} != set(expected_roles):
        raise DispositionError(
            f"{release} requires exactly its declared release review roles"
        )
    return release, sorted(values, key=lambda row: row["role"])


def _finding_evidence(
    path: Path,
    subject: str,
    expected_finding_ids: set[str],
) -> dict[str, Any]:
    value, raw = _json(path, "release finding evidence")
    rows = value.get("findings")
    if (
        set(value) != {"schema_version", "subject_head_sha", "findings"}
        or value.get("schema_version") != 1
        or value.get("subject_head_sha") != subject
        or not isinstance(rows, list)
    ):
        raise DispositionError("release finding evidence schema is invalid")
    base = path.expanduser().resolve().parent
    checked = []
    identities: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"finding_id", "disposition", "evidence"}
            or row.get("disposition") not in {"fixed", "accepted-deviation"}
            or not isinstance(row.get("finding_id"), str)
            or not isinstance(row.get("evidence"), str)
            or Path(row["evidence"]).is_absolute()
            or row["finding_id"] in identities
        ):
            raise DispositionError("release finding disposition is invalid")
        evidence_raw = _bytes(base / row["evidence"], "release finding proof")
        identities.add(row["finding_id"])
        checked.append(
            {
                "finding_id": row["finding_id"],
                "disposition": str(row["disposition"]),
                "evidence_sha256": _sha256(evidence_raw),
            }
        )
    if identities != expected_finding_ids:
        raise DispositionError("finding dispositions do not match review findings")
    return {
        "receipt_sha256": _sha256(raw),
        "findings": sorted(checked, key=lambda row: row["finding_id"]),
    }


def _accepted_deviations(path: Path, subject: str) -> dict[str, Any]:
    value, raw = _json(path, "release accepted deviations")
    rows = value.get("deviations")
    forbidden = value.get("forbidden")
    if (
        set(value)
        != {"schema_version", "integration_head_sha", "deviations", "forbidden"}
        or value.get("schema_version") != 1
        or value.get("integration_head_sha") != subject
        or not isinstance(rows, list)
        or not isinstance(forbidden, list)
        or any(not isinstance(item, str) or not item.strip() for item in forbidden)
    ):
        raise DispositionError("release accepted deviations schema is invalid")
    identities: set[str] = set()
    checked = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "disposition", "rationale"}
            or row.get("disposition") not in {"accepted", "operator-authorized"}
            or not isinstance(row.get("id"), str)
            or not row["id"].strip()
            or row["id"] in identities
            or not isinstance(row.get("rationale"), str)
            or not row["rationale"].strip()
        ):
            raise DispositionError("release accepted deviation is invalid")
        identities.add(row["id"])
        checked.append(str(row["id"]))
    return {
        "receipt_sha256": _sha256(raw),
        "deviation_ids": sorted(checked),
    }


def compile_disposition(
    root: Path,
    *,
    subject_head_sha: str,
    gate_receipt_path: Path,
    attempt_ledger_root: Path,
    review_boundary_path: Path,
    plan_path: Path,
    outcome_evidence_path: Path,
    review_manifest_path: Path,
    finding_evidence_path: Path,
    accepted_deviations_path: Path,
) -> dict[str, Any]:
    subject = _exact_subject(root, subject_head_sha)
    boundary = _release_boundary(
        review_boundary_path,
        artifact_root=attempt_ledger_root,
        subject=subject,
        plan_path=plan_path,
        outcome_evidence_path=outcome_evidence_path,
        accepted_deviations_path=accepted_deviations_path,
    )
    gate = _gate(gate_receipt_path, subject)
    attempts = _attempts(attempt_ledger_root, gate, subject)
    release, reviews = _reviews(
        review_manifest_path, subject, boundary.input_sha256
    )
    review_finding_ids = {
        finding_id for review in reviews for finding_id in review["finding_ids"]
    }
    if sum(len(review["finding_ids"]) for review in reviews) != len(
        review_finding_ids
    ):
        raise DispositionError("release review finding identities are duplicated")
    findings = _finding_evidence(
        finding_evidence_path,
        subject,
        review_finding_ids,
    )
    deviations = _accepted_deviations(accepted_deviations_path, subject)
    blocked = [
        f"review:{row['role']}:{row['verdict']}"
        for row in reviews
        if row["verdict"] != "approve"
    ]
    inputs = {
        "subject_head_sha": subject,
        "review_boundary_input_sha256": boundary.input_sha256,
        "gate": gate,
        "attempt_ledger": attempts,
        "reviews": reviews,
        "finding_evidence": findings,
        "accepted_deviations": deviations,
    }
    result = {
        "schema_version": 1,
        "release": release,
        **inputs,
        "input_sha256": _canonical_payload_sha256(inputs),
        "outcome": "blocked" if blocked else "approved",
        "blocked_reasons": blocked,
    }
    try:
        schema = _load_schema(SCHEMA_PATH)
    except EphemeralProviderError as exc:
        raise DispositionError("release disposition schema is invalid") from exc
    if not validate_output_instance(result, schema):
        raise DispositionError("release disposition contradicts its public schema")
    return result


def validate_disposition(
    root: Path,
    payload: object,
    *,
    gate_receipt_path: Path,
    attempt_ledger_root: Path,
    review_boundary_path: Path,
    plan_path: Path,
    outcome_evidence_path: Path,
    review_manifest_path: Path,
    finding_evidence_path: Path,
    accepted_deviations_path: Path,
) -> None:
    if not isinstance(payload, dict):
        raise DispositionError("release disposition must be an object")
    rebuilt = compile_disposition(
        root,
        subject_head_sha=payload.get("subject_head_sha"),
        gate_receipt_path=gate_receipt_path,
        attempt_ledger_root=attempt_ledger_root,
        review_boundary_path=review_boundary_path,
        plan_path=plan_path,
        outcome_evidence_path=outcome_evidence_path,
        review_manifest_path=review_manifest_path,
        finding_evidence_path=finding_evidence_path,
        accepted_deviations_path=accepted_deviations_path,
    )
    if payload != rebuilt:
        raise DispositionError("release disposition bytes do not match compiled evidence")


def _load(path: Path) -> dict[str, Any]:
    return _json(path, "release disposition")[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    budget = sub.add_parser("budget")
    budget.add_argument("--attempt-ledger-root", type=Path, required=True)
    compile_cmd = sub.add_parser("compile")
    check = sub.add_parser("check")
    check.add_argument("disposition", type=Path)
    for command in (compile_cmd, check):
        command.add_argument("--gate-receipt", type=Path, required=True)
        command.add_argument("--attempt-ledger-root", type=Path, required=True)
        command.add_argument("--review-boundary", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--outcome-evidence", type=Path, required=True)
        command.add_argument("--review-manifest", type=Path, required=True)
        command.add_argument("--finding-evidence", type=Path, required=True)
        command.add_argument("--accepted-deviations", type=Path, required=True)
    compile_cmd.add_argument("--subject-head-sha", required=True)
    args = parser.parse_args()
    try:
        if args.command == "budget":
            ledger = AttemptLedgerStore(args.attempt_ledger_root).load()
            maximum = int(ledger["maximum_attempts"])
            result = {
                "schema_version": 1,
                "maximum_attempts": maximum,
                "attempts_consumed": len(ledger["attempts"]),
                "attempts_remaining": maximum - len(ledger["attempts"]),
                "extension_authorized": maximum == MAX_ATTEMPTS,
            }
        elif args.command == "compile":
            result = compile_disposition(
                ROOT,
                subject_head_sha=args.subject_head_sha,
                gate_receipt_path=args.gate_receipt,
                attempt_ledger_root=args.attempt_ledger_root,
                review_boundary_path=args.review_boundary,
                plan_path=args.plan,
                outcome_evidence_path=args.outcome_evidence,
                review_manifest_path=args.review_manifest,
                finding_evidence_path=args.finding_evidence,
                accepted_deviations_path=args.accepted_deviations,
            )
        else:
            validate_disposition(
                ROOT,
                _load(args.disposition),
                gate_receipt_path=args.gate_receipt,
                attempt_ledger_root=args.attempt_ledger_root,
                review_boundary_path=args.review_boundary,
                plan_path=args.plan,
                outcome_evidence_path=args.outcome_evidence,
                review_manifest_path=args.review_manifest,
                finding_evidence_path=args.finding_evidence,
                accepted_deviations_path=args.accepted_deviations,
            )
            result = {"status": "valid"}
        print(json.dumps(result, indent=2, sort_keys=True))
    except (AttemptLedgerError, DispositionError, OSError, ReceiptError) as exc:
        print(f"RC3 release disposition: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
