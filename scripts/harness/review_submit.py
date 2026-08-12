#!/usr/bin/env python3
"""Publish a schema-valid review through a narrow callback port."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("review-input",)

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.contracts import CallbackEnvelope, to_dict
from harness.workflows.review import (
    ReviewFinding,
    ReviewResult,
    review_round_payload,
)
from review_contract import (
    SEVERITIES,
    VERDICTS,
    ReviewContractError,
    finding_constraint_lines,
    parse_review_json,
    require_unqualified_finding_ids,
    require_unique_finding_ids,
    validate_finding,
)
from harness.artifact_repair import (
    ArtifactRepairError,
    ContractArtifactOwner,
    review_input_contract_template,
)


# The authoritative review-round shape. _round_result accepts a reviewer object
# only when its key set is exactly equal to these, so every prompt that asks a
# reviewer for a round must be rendered from them rather than restating them.
ROUND_FIELDS: tuple[str, ...] = (
    "schema_version",
    "axis",
    "verdict",
    "verification_iteration",
    "findings",
)
FINDING_FIELDS: tuple[str, ...] = (
    "finding_id",
    "severity",
    "file",
    "line",
    "summary",
    "evidence",
    "recommendation",
)
ROUND_STRING_FIELDS: tuple[str, ...] = ("axis", "verdict")


def round_schema_lines(
    *, verification_iteration: int | None = None
) -> tuple[str, ...]:
    """Render the enforced round schema as reviewer-facing prompt lines.

    Keys and enforced value vocabularies both come from the code that rejects
    them, so a reviewer cannot satisfy the key set and still fail on a value.
    When the caller knows the authoritative ``verification_iteration`` for
    the round it is stated as an exact integer, so a reviewer never has to
    infer it from surrounding fix-cycle context.
    """

    def names(fields: Iterable[str]) -> str:
        return ", ".join(f"`{field}`" for field in fields)

    iteration_lines: tuple[str, ...] = ()
    if verification_iteration is not None:
        iteration_lines = (
            "`verification_iteration` is exactly "
            f"`{verification_iteration}` for this round; use that integer "
            "verbatim.",
        )
    return (
        f"Return exactly one review-round JSON object with fields: {names(ROUND_FIELDS)}.",
        f"Each finding has {names(FINDING_FIELDS)}.",
        "Use exactly these keys: any extra or missing key is rejected.",
        f"`verdict` is exactly one of {names(sorted(VERDICTS))}.",
        f"`severity` is exactly one of {names(sorted(SEVERITIES))}.",
        "`line` is null or a positive integer, and `schema_version` is `1`.",
        "A `verdict` of `approve` cannot carry a `critical` or `important` finding.",
        *iteration_lines,
    ) + finding_constraint_lines()


class ReviewSubmitError(ValueError):
    """A rejected submission, optionally typed for the correction loop."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "invalid-review-submission",
        expected: object = None,
        actual: object = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.expected = expected
        self.actual = actual


class ReviewCallbackPort(Protocol):
    """Port implemented by the callback broker adapter at integration time."""

    def publish(self, envelope: CallbackEnvelope) -> None: ...


class FileCallbackPort:
    """Atomic outbox adapter consumed by the operation-scoped broker."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def publish(self, envelope: CallbackEnvelope) -> None:
        atomic_json(self.path, to_dict(envelope))


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def publish_review_input_template(
    *, state_root: Path, state_dir: Path, worktree: Path, meta: dict[str, Any]
) -> dict[str, Any]:
    """Publish the review sidecar before provider input becomes writable."""

    owner = ContractArtifactOwner.publish(
        state_root=state_root,
        worktree=state_dir,
        template=review_input_contract_template(meta),
        actual_target=state_dir / ".review-input.json",
    )
    if owner.publication_created or not owner.actual_target.exists():
        owner.restore_template()
    return {**meta, "contract_template_pointer": str(owner.sidecar_path)}


def _repair_review_input(
    raw: str, meta: dict[str, Any], worktree: Path, input_file: Path
) -> str:
    pointer = meta.get("contract_template_pointer")
    if not isinstance(pointer, str) or not pointer:
        raise ReviewSubmitError("review input contract template is unavailable")
    sidecar = Path(pointer).expanduser()
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ReviewSubmitError("review input contract template is invalid")
    try:
        owner = ContractArtifactOwner.load(
            state_root=sidecar.parents[2],
            worktree=input_file.parent,
            family="review-input",
            attempt_id=_required_text(meta, "operation_id"),
        )
        if owner.sidecar_path != sidecar.resolve() or owner.actual_target != input_file:
            raise ArtifactRepairError("review input template binding changed")
        owner.repair(authoritative_fields={})
        return input_file.read_text(encoding="utf-8")
    except (ArtifactRepairError, OSError, IndexError) as exc:
        raise ReviewSubmitError("review input contract repair failed") from exc


def _required_text(meta: dict[str, Any], field: str) -> str:
    value = meta.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReviewSubmitError(f"review metadata is missing {field}")
    return value.strip()


def _require_string_fields(
    value: dict[str, Any], fields: Iterable[str], *, label: str
) -> None:
    for field in fields:
        if not isinstance(value.get(field), str):
            raise ReviewSubmitError(f"{label} {field} must be a string")


def _round_result(raw: str, meta: dict[str, Any]) -> ReviewResult:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewSubmitError("review round is not valid JSON") from exc
    expected = set(ROUND_FIELDS)
    if not isinstance(value, dict) or set(value) != expected:
        raise ReviewSubmitError("review round has invalid fields")
    if value.get("schema_version") != 1:
        raise ReviewSubmitError("review round has an unsupported schema")
    _require_string_fields(value, ROUND_STRING_FIELDS, label="review round field")
    axis = _required_text(value, "axis")
    if axis != _required_text(meta, "axis"):
        raise ReviewSubmitError("review round axis does not match metadata")
    iteration = value.get("verification_iteration")
    if (
        not isinstance(iteration, int)
        or isinstance(iteration, bool)
        or iteration != meta.get("verification_iteration")
    ):
        raise ReviewSubmitError(
            "review round iteration does not match metadata "
            f"(expected {meta.get('verification_iteration')!r}, "
            f"actual {iteration!r})",
            error_code="verification-iteration-mismatch",
            expected=meta.get("verification_iteration"),
            actual=iteration,
        )
    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > 50:
        raise ReviewSubmitError("review round findings must be bounded")
    findings: list[ReviewFinding] = []
    for index, item in enumerate(raw_findings):
        try:
            finding = validate_finding(
                item,
                f"review round findings[{index}]",
            )
            findings.append(
                ReviewFinding(
                    finding_id=finding["finding_id"],
                    axis=axis,
                    severity=finding["severity"],
                    summary=finding["summary"],
                    evidence=finding["evidence"],
                    file=finding["file"],
                    line=finding["line"],
                    recommendation=finding["recommendation"],
                )
            )
        except (ReviewContractError, ValueError) as exc:
            raise ReviewSubmitError(
                f"review round finding is invalid: {exc}"
            ) from exc
    try:
        require_unqualified_finding_ids(
            axis,
            (finding.finding_id for finding in findings),
        )
        require_unique_finding_ids(
            (finding.finding_id for finding in findings),
            "review round finding_id values",
        )
    except ReviewContractError as exc:
        raise ReviewSubmitError(f"review round findings are invalid: {exc}") from exc
    try:
        return ReviewResult(
            axis=axis,
            verdict=value["verdict"],
            findings=tuple(findings),
            verification_iteration=iteration,
        )
    except ValueError as exc:
        raise ReviewSubmitError(f"review round is invalid: {exc}") from exc


def submit_review(
    raw: str,
    *,
    meta: dict[str, Any],
    worktree: Path,
    port: ReviewCallbackPort,
) -> CallbackEnvelope:
    """Validate identity/evidence, then publish exactly one typed envelope."""

    if not isinstance(meta, dict) or meta.get("schema_version") != 1:
        raise ReviewSubmitError("review metadata has an unsupported schema")
    expected_worktree = Path(_required_text(meta, "worktree")).expanduser().resolve()
    if expected_worktree != worktree.expanduser().resolve():
        raise ReviewSubmitError("worktree identity mismatch")
    profile = meta.get("verification_profile")
    if not isinstance(profile, dict):
        raise ReviewSubmitError("review metadata is missing verification_profile")
    if meta.get("transport") == "review-round":
        result = _round_result(raw, meta)
        payload = review_round_payload(
            _required_text(meta, "parent_session_operation_id"),
            result,
        )
    else:
        payload = parse_review_json(
            raw,
            expected_operation_id=_required_text(meta, "operation_id"),
            expected_run_id=_required_text(meta, "run_id"),
            expected_mode=_required_text(meta, "review_mode"),
            expected_head_sha=_required_text(meta, "head_sha"),
            expected_profile=str(profile.get("name") or ""),
            expected_profile_sha256=str(profile.get("sha256") or ""),
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    envelope = CallbackEnvelope(
        callback_id=f"review-{digest[:24]}",
        operation_id=_required_text(meta, "operation_id"),
        run_id=_required_text(meta, "run_id"),
        kind="review",
        payload=payload,
        payload_sha256=digest,
    )
    port.publish(envelope)
    return envelope


def _record_rejection(state_dir: Path, raw: str, exc: Exception) -> None:
    """Durably key one rejection by operation, input hash, and attempt.

    Resubmitting identical bytes reuses the existing receipt instead of
    consuming another attempt; a corrected input hashes differently and
    receives the next attempt number.  Receipts are bounded, content-free
    beyond the typed error fields, and never overwritten.
    """

    if not raw:
        return
    try:
        meta = json.loads(
            (state_dir / ".review-meta.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    input_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    rejections = state_dir / ".review-submit-rejections"
    rejections.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = sorted(rejections.glob("*.json"))
    for path in existing:
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(prior, dict) and prior.get("input_sha256") == input_sha256:
            return
    attempt = len(existing) + 1
    receipt = {
        "schema_version": 1,
        "status": "rejected",
        "operation_id": str(meta.get("operation_id") or ""),
        "run_id": str(meta.get("run_id") or ""),
        "axis": str(meta.get("axis") or ""),
        "input_sha256": input_sha256,
        "attempt": attempt,
        "error_code": getattr(exc, "error_code", "invalid-review-submission"),
        "error": str(exc)[:500],
        "expected": getattr(exc, "expected", None),
        "actual": getattr(exc, "actual", None),
    }
    atomic_json(rejections / f"{input_sha256[:12]}-a{attempt}.json", receipt)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--input-file", type=Path)
    args = parser.parse_args()
    worktree = args.worktree.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    input_file: Path | None = None
    try:
        if args.input_file is not None:
            input_file = args.input_file.expanduser()
            expected_input = state_dir / ".review-input.json"
            if (
                input_file.is_symlink()
                or input_file.resolve() != expected_input
                or not input_file.is_file()
                or input_file.stat().st_size > 1_000_000
            ):
                raise ReviewSubmitError(
                    "review input must be the exact bounded scratch file"
                )
            input_file = expected_input
            raw = input_file.read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read()
        meta = json.loads(
            (state_dir / ".review-meta.json").read_text(encoding="utf-8")
        )
        if input_file is not None and meta.get("transport") == "review-round":
            raw = _repair_review_input(raw, meta, worktree, input_file)
        envelope = submit_review(
            raw,
            meta=meta,
            worktree=worktree,
            port=FileCallbackPort(state_dir / ".review-callback.json"),
        )
        if input_file is not None:
            input_file.unlink()
    except (
        OSError,
        json.JSONDecodeError,
        ReviewContractError,
        ReviewSubmitError,
        ValueError,
    ) as exc:
        try:
            _record_rejection(state_dir, raw if "raw" in dir() else "", exc)
        except OSError:
            pass
        print(f"review-submit: invalid outbox: {exc}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "status": "callback-ready",
                "operation_id": envelope.operation_id,
                "callback_id": envelope.callback_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
