#!/usr/bin/env python3
"""RC1 gate preflight facade: the production consumer of the [rc1] cells.

Contract/preflight only.  The facade consumes the exact three configured
RC1 corridor cells from ``config/acceptance-cells.toml``, plans their
strictly sequential execution, refuses any run that is not coordinator
authorized, binds every receipt to ``lifecycle_subject_sha256`` and the
registered Fable High routes, and emits templates that the
``v267_stabilization`` streak validator consumes as its single authority.
It never launches a provider cell, opens a surface, or mutates state;
live execution stays coordinator-owned.  The legacy four-cell release
path (``release-acceptance.py`` / ``live-acceptance-runner.py``) is
untouched: the RC1 gate is additive beside it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import v267_stabilization as stab


class RC1GatePreflight:
    """Read-only sequencing and binding over the declared RC1 gate."""

    def __init__(self, declaration: stab.RC1Gate, config: stab.SubjectConfig):
        self._declaration = declaration
        self._config = config

    @property
    def cells(self) -> tuple[stab.RC1GateCell, ...]:
        return self._declaration.cells

    @property
    def corridor(self) -> str:
        return self._declaration.corridor

    @property
    def streak_target(self) -> int:
        return self._declaration.streak_target

    def plan(
        self, receipts: list[object], *, expected_digest: str
    ) -> dict[str, object]:
        """Validate the accepted receipts and name the next configured cell."""

        verdict = stab.validate_streak(
            receipts,
            expected_digest=expected_digest,
            config=self._config,
            gate=self._declaration,
        )
        next_cell = None
        if not verdict["complete"] and len(receipts) < len(self.cells):
            next_cell = self.cells[len(receipts)].cell_id
        return {
            "schema_version": 1,
            "corridor": self.corridor,
            "streak": verdict["streak"],
            "streak_target": self.streak_target,
            "material_finding_cycle": verdict["material_finding_cycle"],
            "complete": verdict["complete"],
            "next_cell": next_cell,
        }

    def receipt_template(
        self, cell_id: str, *, expected_digest: str
    ) -> dict[str, object]:
        """A streak-consumable receipt skeleton bound to one configured cell."""

        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or not set(expected_digest) <= stab.HEX_DIGITS
        ):
            raise stab.StabilizationError(
                "expected digest must be 64 hex characters"
            )
        cell = self._declaration.cell_by_id(cell_id)
        return {
            "schema_version": 2,
            "cell_id": cell.cell_id,
            "sequence": cell.sequence,
            "corridor": self.corridor,
            "lifecycle_subject_sha256": expected_digest,
            "executor_route": cell.executor_route,
            "review_route": cell.review_route,
        }

    def authorize(
        self, cell_id: str, *, coordinator_authorized: bool
    ) -> dict[str, object]:
        """Return the bound cell contract for one coordinator-authorized run.

        Pure data: authorization here never launches, schedules, or touches
        a provider, process, or surface.
        """

        cell = self._declaration.cell_by_id(cell_id)
        if coordinator_authorized is not True:
            raise stab.StabilizationError(
                f"RC1 cell {cell.cell_id} requires coordinator authorization"
            )
        return {
            "schema_version": 1,
            "cell_id": cell.cell_id,
            "sequence": cell.sequence,
            "corridor": self.corridor,
            "executor_route": cell.executor_route,
            "review_route": cell.review_route,
            "expected_trace": list(cell.expected),
        }


def load_gate(root: Path = ROOT) -> RC1GatePreflight:
    """Load the RC1 gate declaration and its stabilization denominator."""

    declaration = stab.load_rc1_gate(Path(root) / "config/acceptance-cells.toml")
    config = stab.load_subject_config(Path(root) / declaration.subject_config)
    if declaration.streak_target != config.streak_target:
        raise stab.StabilizationError(
            "RC1 gate streak target disagrees with the stabilization denominator"
        )
    return RC1GatePreflight(declaration, config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser(
        "preflight", help="validate the RC1 gate declaration and plan the next cell"
    )
    preflight.add_argument("--root", type=Path, default=ROOT)
    preflight.add_argument("--json", action="store_true")
    preflight.add_argument("--receipts", type=Path)
    preflight.add_argument("--expected-digest")
    args = parser.parse_args()
    try:
        gate = load_gate(args.root.expanduser().resolve())
        payload: dict[str, object] = {
            "schema_version": 1,
            "corridor": gate.corridor,
            "streak_target": gate.streak_target,
            "cells": [
                {"cell_id": cell.cell_id, "sequence": cell.sequence}
                for cell in gate.cells
            ],
            "complete": False,
            "next_cell": gate.cells[0].cell_id,
        }
        if args.receipts is not None:
            if not args.expected_digest:
                raise stab.StabilizationError(
                    "--receipts requires --expected-digest"
                )
            document = json.loads(args.receipts.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or not isinstance(document.get("receipts"), list)
            ):
                raise stab.StabilizationError(
                    "receipt file requires schema_version 1 and a receipts list"
                )
            plan = gate.plan(
                document["receipts"], expected_digest=args.expected_digest
            )
            payload.update(
                {
                    "streak": plan["streak"],
                    "material_finding_cycle": plan["material_finding_cycle"],
                    "complete": plan["complete"],
                    "next_cell": plan["next_cell"],
                }
            )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError) as exc:
        print(f"rc1-gate: cannot load receipts: {exc}", file=sys.stderr)
        return 3
    except stab.StabilizationError as exc:
        print(f"rc1-gate: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
