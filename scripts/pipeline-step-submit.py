#!/usr/bin/env python3
"""Publish one immutable engineering/fix phase result in the task worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn


PHASES = ("reproduce", "root-cause", "regression-test", "minimal-fix")
OUTCOMES = {"complete", "cannot-reproduce"}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_RESULT_BYTES = 32_768
MAX_SUMMARY_BYTES = 8_192
MAX_EVIDENCE = 16


class SubmitError(ValueError):
    pass


def die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_object(path: Path, label: str, *, maximum: int) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise SubmitError(f"{label} must be a regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > maximum:
        raise SubmitError(f"{label} exceeds its bounded size")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubmitError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise SubmitError(f"{label} must be an object")
    return value


def contained_file(root: Path, value: str, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or "\\" in value
    ):
        raise SubmitError(f"{label} must be a worktree-relative path")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise SubmitError(f"{label} must remain inside the worktree")
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SubmitError(f"{label} escapes the worktree") from exc
    if not path.is_file() or path.is_symlink():
        raise SubmitError(f"{label} must name a regular file")
    return path


def receipt_path(root: Path, pass_index: int, step: str) -> Path:
    return (
        root
        / ".task-pipeline"
        / "receipts"
        / f"pass-{pass_index}"
        / f"{step}.json"
    )


def load_receipt(path: Path) -> dict[str, Any]:
    value = read_object(path, "previous receipt", maximum=MAX_RESULT_BYTES)
    claimed = value.get("receipt_sha256")
    unsigned = dict(value)
    unsigned.pop("receipt_sha256", None)
    if not isinstance(claimed, str) or claimed != digest(unsigned):
        raise SubmitError("previous receipt digest is invalid")
    return value


def previous_receipt(
    root: Path,
    pass_index: int,
    step: str,
) -> dict[str, Any] | None:
    phase_index = PHASES.index(step)
    if pass_index == 1 and phase_index == 0:
        return None
    if pass_index > 1 and step == "reproduce":
        raise SubmitError("reproduce is valid only in pass 1")
    if pass_index > 1 and step == "root-cause":
        previous = receipt_path(
            root, pass_index - 1, "minimal-fix"
        )
    else:
        expected_index = phase_index - 1
        if expected_index < 0:
            raise SubmitError("root-cause requires a prior fix pass")
        previous = receipt_path(
            root, pass_index, PHASES[expected_index]
        )
    if not previous.is_file():
        expected = previous.stem
        raise SubmitError(
            f"phase is out of order; expected {expected} first"
        )
    value = load_receipt(previous)
    if value.get("outcome") != "complete":
        raise SubmitError("a terminal prior receipt cannot be continued")
    return value


def atomic_create(path: Path, payload: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def submit(
    worktree: Path,
    *,
    step: str,
    pass_index: int,
    result_pointer: str,
) -> dict[str, Any]:
    worktree = worktree.expanduser().resolve()
    if not worktree.is_dir():
        raise SubmitError("worktree is unavailable")
    if step not in PHASES:
        raise SubmitError("step is not an engineering/fix phase")
    meta = read_object(
        worktree / ".task-meta.json",
        "task metadata",
        maximum=MAX_RESULT_BYTES,
    )
    try:
        task_id = str(uuid.UUID(str(meta.get("task_id") or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SubmitError("task identity is invalid") from exc
    if task_id != meta.get("task_id"):
        raise SubmitError("task identity must be canonical")
    if Path(str(meta.get("worktree") or "")).resolve() != worktree:
        raise SubmitError("task metadata belongs to another worktree")
    policy = meta.get("pipeline_policy")
    if not isinstance(policy, dict) or policy.get("name") != "engineering/fix":
        raise SubmitError("task is not bound to engineering/fix")
    definition_sha256 = str(policy.get("definition_sha256") or "")
    completion_policy = str(policy.get("completion_policy") or "")
    pass_limit = policy.get("total_pass_limit")
    if (
        not SHA256_RE.fullmatch(definition_sha256)
        or completion_policy not in {"attention", "autonomous"}
        or type(pass_limit) is not int
        or pass_limit
        != {"attention": 2, "autonomous": 3}[completion_policy]
    ):
        raise SubmitError("pipeline policy is invalid")
    if pass_index < 1 or pass_index > pass_limit:
        raise SubmitError("pass limit is exhausted")

    result_path = contained_file(
        worktree, result_pointer, "result"
    )
    result = read_object(
        result_path, "result", maximum=MAX_RESULT_BYTES
    )
    if set(result) != {
        "schema_version",
        "outcome",
        "summary",
        "evidence",
    } or result.get("schema_version") != 1:
        raise SubmitError("result fields are not exact")
    outcome = result.get("outcome")
    summary = result.get("summary")
    evidence = result.get("evidence")
    if outcome not in OUTCOMES:
        raise SubmitError("result outcome is invalid")
    if outcome == "cannot-reproduce" and (
        step != "reproduce" or pass_index != 1
    ):
        raise SubmitError(
            "cannot-reproduce is valid only for the first phase"
        )
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary.encode()) > MAX_SUMMARY_BYTES
    ):
        raise SubmitError("result summary is invalid")
    if (
        not isinstance(evidence, list)
        or len(evidence) > MAX_EVIDENCE
        or any(not isinstance(item, str) for item in evidence)
        or len(set(evidence)) != len(evidence)
    ):
        raise SubmitError("result evidence is invalid")
    normalized_evidence: list[str] = []
    for item in evidence:
        path = contained_file(worktree, item, "evidence")
        normalized_evidence.append(
            path.relative_to(worktree).as_posix()
        )

    previous = previous_receipt(worktree, pass_index, step)
    previous_sha256 = (
        str(previous["receipt_sha256"]) if previous else ""
    )
    verification_sha256 = ""
    if pass_index > 1 and step == "root-cause":
        verification = read_object(
            worktree / ".task-verification.json",
            "verification packet",
            maximum=MAX_RESULT_BYTES,
        )
        verification_sha256 = digest(verification)

    normalized_result = {
        "schema_version": 1,
        "outcome": outcome,
        "summary": summary.strip(),
        "evidence": normalized_evidence,
    }
    output_sha256 = digest(normalized_result)
    input_sha256 = digest(
        {
            "schema_version": 1,
            "parent_operation_id": task_id,
            "definition_sha256": definition_sha256,
            "step_id": step,
            "pass_index": pass_index,
            "previous_receipt_sha256": previous_sha256,
            "verification_packet_sha256": verification_sha256,
        }
    )
    unsigned = {
        "schema_version": 1,
        "parent_operation_id": task_id,
        "definition_sha256": definition_sha256,
        "step_id": step,
        "phase_index": PHASES.index(step),
        "pass_index": pass_index,
        "completion_policy": completion_policy,
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "previous_receipt_sha256": previous_sha256,
        "verification_packet_sha256": verification_sha256,
        **normalized_result,
    }
    receipt = {**unsigned, "receipt_sha256": digest(unsigned)}
    target = receipt_path(worktree, pass_index, step)
    encoded = canonical(receipt) + b"\n"
    if not atomic_create(target, encoded):
        existing = read_object(
            target, "existing receipt", maximum=MAX_RESULT_BYTES
        )
        if existing != receipt:
            raise SubmitError(
                "accepted receipt cannot be replaced"
            )
        receipt = existing
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--pass-index", type=int, default=1)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    try:
        receipt = submit(
            args.worktree,
            step=args.step,
            pass_index=args.pass_index,
            result_pointer=args.result,
        )
    except (OSError, SubmitError, ValueError) as exc:
        die(str(exc))
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
