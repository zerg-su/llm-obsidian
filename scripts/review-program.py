#!/usr/bin/env python3
"""Compile and reconcile purpose-bound review checkpoints."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from harness.review_program import (
    ReviewBoundaryInput,
    ReviewBoundaryReceipt,
    ReviewProgramError,
    compile_review_program,
    reconcile_review_program,
)
from harness.review_program_authority import (
    approved_risk_from_plan,
    trusted_review_receipt,
    validate_trusted_receipts,
)


def _object(path: Path, label: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ReviewProgramError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewProgramError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ReviewProgramError(f"{label} must be an object")
    return value


def _emit(value: object) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--worktree", type=Path, required=True)
    status.add_argument("--plan", type=Path, required=True)
    status.add_argument("--input", type=Path, action="append", required=True)
    status.add_argument("--receipt", type=Path, action="append", default=[])
    receipt = sub.add_parser("receipt")
    receipt.add_argument("--worktree", type=Path, required=True)
    receipt.add_argument("--input", type=Path, required=True)
    receipt.add_argument("--operation-id", required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "receipt":
            boundary = ReviewBoundaryInput.from_mapping(
                _object(args.input, "review boundary input")
            )
            value = trusted_review_receipt(
                args.worktree,
                boundary,
                args.operation_id,
            )
            _emit(value.payload())
            return 0
        inputs = tuple(
            ReviewBoundaryInput.from_mapping(_object(path, "review boundary input"))
            for path in args.input
        )
        receipts = tuple(
            ReviewBoundaryReceipt.from_mapping(_object(path, "review boundary receipt"))
            for path in args.receipt
        )
        risk_profile = approved_risk_from_plan(args.worktree, args.plan, inputs)
        program = compile_review_program(risk_profile, inputs)
        validate_trusted_receipts(args.worktree, inputs, receipts)
        decision = reconcile_review_program(program, receipts)
        _emit(
            {
                "schema_version": 1,
                "risk_profile": program.risk_profile,
                "definition_sha256": program.definition_sha256,
                "purposes": list(program.purposes),
                "boundaries": [
                    {
                        "purpose": boundary.purpose,
                        "input_sha256": boundary.input.input_sha256,
                        "question": boundary.question,
                        "max_verify_iterations": (boundary.max_verify_iterations),
                        "intent_collapsed": boundary.intent_collapsed,
                    }
                    for boundary in program.boundaries
                ],
                "receipt_count": len(receipts),
                "action": decision.action,
                "purpose": decision.purpose,
                "may_fix": decision.may_fix,
            }
        )
        return 0
    except (OSError, ReviewProgramError, TypeError, ValueError) as exc:
        print(f"review-program: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
