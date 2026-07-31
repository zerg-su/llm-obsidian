#!/usr/bin/env python3
"""Publish a schema-valid review through a narrow callback port."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.contracts import CallbackEnvelope, to_dict
from harness.workflows.review import (
    ReviewFinding,
    ReviewResult,
    review_round_payload,
)
from review_contract import ReviewContractError, parse_review_json


class ReviewSubmitError(ValueError):
    pass


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


def _required_text(meta: dict[str, Any], field: str) -> str:
    value = meta.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReviewSubmitError(f"review metadata is missing {field}")
    return value.strip()


def _round_result(raw: str, meta: dict[str, Any]) -> ReviewResult:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewSubmitError("review round is not valid JSON") from exc
    expected = {
        "schema_version",
        "axis",
        "verdict",
        "verification_iteration",
        "findings",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ReviewSubmitError("review round has invalid fields")
    if value.get("schema_version") != 1:
        raise ReviewSubmitError("review round has an unsupported schema")
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
            "review round iteration does not match metadata"
        )
    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > 50:
        raise ReviewSubmitError("review round findings must be bounded")
    fields = {
        "finding_id",
        "severity",
        "file",
        "line",
        "summary",
        "evidence",
        "recommendation",
    }
    findings: list[ReviewFinding] = []
    for item in raw_findings:
        if not isinstance(item, dict) or set(item) != fields:
            raise ReviewSubmitError("review round finding has invalid fields")
        file = str(item.get("file") or "")
        path = PurePosixPath(file)
        if (
            not file
            or "\\" in file
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() == "."
        ):
            raise ReviewSubmitError(
                "review round finding file must be repository-relative"
            )
        try:
            findings.append(
                ReviewFinding(
                    finding_id=str(item.get("finding_id") or ""),
                    axis=axis,
                    severity=str(item.get("severity") or ""),
                    summary=str(item.get("summary") or ""),
                    evidence=str(item.get("evidence") or ""),
                    file=file,
                    line=item.get("line"),
                    recommendation=str(item.get("recommendation") or ""),
                )
            )
        except ValueError as exc:
            raise ReviewSubmitError(
                f"review round finding is invalid: {exc}"
            ) from exc
    try:
        return ReviewResult(
            axis=axis,
            verdict=str(value.get("verdict") or ""),
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
            raw = input_file.read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read()
        meta = json.loads(
            (state_dir / ".review-meta.json").read_text(encoding="utf-8")
        )
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
