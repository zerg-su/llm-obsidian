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
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DISPATCH_RUNNER = ROOT / "scripts/dispatch-runner.py"
DEFAULT_STATE = ROOT / ".vault-meta/acceptance/rc1-streak-state.json"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import v267_stabilization as stab


CorridorLauncher = Callable[..., dict[str, Any]]


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_streak_state(path: Path, *, expected_digest: str) -> dict[str, Any]:
    """Durable streak progress; a changed behavioral digest starts fresh."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("expected_digest") != expected_digest
    ):
        return {
            "schema_version": 1,
            "expected_digest": expected_digest,
            "receipts": [],
            "reservation": None,
        }
    if not isinstance(value.get("receipts"), list):
        raise stab.StabilizationError("streak state receipts are malformed")
    reservation = value.get("reservation")
    if reservation is not None and (
        not isinstance(reservation, dict)
        or not isinstance(reservation.get("cell_id"), str)
        or reservation.get("status") not in {"reserved", "launched"}
    ):
        raise stab.StabilizationError("streak state reservation is malformed")
    return value


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
        self,
        receipts: list[object],
        *,
        expected_digest: str,
        evidence_root: Path | None = None,
    ) -> dict[str, object]:
        """Validate the accepted receipts and name the next configured cell."""

        verdict = stab.validate_streak(
            receipts,
            expected_digest=expected_digest,
            config=self._config,
            gate=self._declaration,
            root=evidence_root if evidence_root is not None else ROOT,
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

    def reserve_and_launch(
        self,
        *,
        coordinator_authorized: bool,
        expected_digest: str,
        state_path: Path,
        launcher: CorridorLauncher,
        evidence_root: Path | None = None,
        timeout: int = 1200,
        **launcher_options: Any,
    ) -> dict[str, Any]:
        """Reserve exactly the next configured cell and launch its corridor.

        Containment precedes authorization: without an explicit coordinator
        authorization no state is written and the launcher is never invoked.
        The reservation is persisted before launch, so a crashed or
        interrupted run resumes the same cell after restart instead of
        skipping ahead.
        """

        state = _read_streak_state(state_path, expected_digest=expected_digest)
        plan = self.plan(
            state["receipts"],
            expected_digest=expected_digest,
            evidence_root=evidence_root,
        )
        if plan["complete"] or plan["next_cell"] is None:
            raise stab.StabilizationError(
                "the RC1 streak is already complete; no further cell may run"
            )
        next_cell = str(plan["next_cell"])
        reservation = state.get("reservation")
        if reservation is not None and reservation["cell_id"] != next_cell:
            raise stab.StabilizationError(
                f"streak state reserves {reservation['cell_id']} but the "
                f"validated prefix requires {next_cell}"
            )
        if coordinator_authorized is not True:
            raise stab.StabilizationError(
                f"RC1 cell {next_cell} requires coordinator authorization"
            )
        contract = self.authorize(next_cell, coordinator_authorized=True)
        state["reservation"] = {"cell_id": next_cell, "status": "reserved"}
        _atomic_json(state_path, state)
        launch = launcher(
            ROOT,
            contract,
            timeout=timeout,
            **launcher_options,
        )
        if not isinstance(launch, dict):
            raise stab.StabilizationError("corridor launcher returned no launch record")
        state["reservation"] = {
            "cell_id": next_cell,
            "status": "launched",
            "launch": launch,
        }
        _atomic_json(state_path, state)
        return {
            "schema_version": 1,
            "cell_id": next_cell,
            "status": "launched",
            "launch": launch,
            "receipt_template": self.receipt_template(
                next_cell, expected_digest=expected_digest
            ),
        }

    def record_receipt(
        self,
        receipt: dict[str, Any],
        *,
        expected_digest: str,
        state_path: Path,
        evidence_root: Path | None = None,
    ) -> dict[str, Any]:
        """Complete the open reservation with one validated authoritative receipt.

        The candidate list is validated through the streak authority before
        anything is persisted, so a tampered or unbound receipt leaves the
        durable state untouched.
        """

        state = _read_streak_state(state_path, expected_digest=expected_digest)
        reservation = state.get("reservation")
        if reservation is None:
            raise stab.StabilizationError(
                "no reserved RC1 cell is awaiting a receipt"
            )
        if not isinstance(receipt, dict) or receipt.get("cell_id") != reservation["cell_id"]:
            raise stab.StabilizationError(
                f"receipt must complete the reserved cell {reservation['cell_id']}"
            )
        candidate = [*state["receipts"], receipt]
        verdict = stab.validate_streak(
            candidate,
            expected_digest=expected_digest,
            config=self._config,
            gate=self._declaration,
            root=evidence_root if evidence_root is not None else ROOT,
        )
        state["receipts"] = candidate
        state["reservation"] = None
        _atomic_json(state_path, state)
        return {
            "schema_version": 1,
            "cell_id": reservation["cell_id"],
            "streak": verdict["streak"],
            "material_finding_cycle": verdict["material_finding_cycle"],
            "complete": verdict["complete"],
        }


def dispatch_corridor_driver(
    root: Path,
    contract: dict[str, Any],
    *,
    timeout: int,
    spec_path: Path,
    approval_token: str,
) -> dict[str, Any]:
    """Launch the reserved corridor through the existing dispatch owner.

    The coordinator-approved dispatch spec must bind exactly the reserved
    cell: same pipeline and same executor/review routes.  The launch itself
    is `dispatch-runner.py start`, the one registered engineering/change
    corridor owner; this driver adds no scheduler and no recovery authority.
    """

    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise stab.StabilizationError(f"cannot read dispatch spec: {exc}") from exc
    if not isinstance(spec, dict):
        raise stab.StabilizationError("dispatch spec must be an object")
    if spec.get("pipeline") != contract["corridor"]:
        raise stab.StabilizationError(
            f"dispatch spec pipeline must be {contract['corridor']}"
        )
    executor = {
        key: str((spec.get("executor") or {}).get(key) or "")
        for key in ("runtime", "model", "effort")
    }
    if executor != contract["executor_route"]:
        raise stab.StabilizationError(
            "dispatch spec executor route drifts from the reserved cell"
        )
    review = spec.get("review") or {}
    reviewed = {
        key: str(review.get(key) or "")
        for key in ("mode", "runtime", "model", "effort")
    }
    if reviewed != contract["review_route"]:
        raise stab.StabilizationError(
            "dispatch spec review route drifts from the reserved cell"
        )
    result = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_RUNNER),
            "start",
            "--spec",
            str(spec_path),
            "--approval-token",
            approval_token,
        ],
        cwd=str(root),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise stab.StabilizationError(
            "dispatch corridor launch failed: "
            + (result.stderr.strip().splitlines() or ["no stderr"])[-1]
        )
    return {
        "owner": "dispatch-runner",
        "returncode": result.returncode,
        "stdout": result.stdout.strip()[-2000:],
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
    run = sub.add_parser(
        "run",
        help="reserve the next configured cell and launch it through the "
        "dispatch owner (coordinator-authorized only)",
    )
    run.add_argument("--root", type=Path, default=ROOT)
    run.add_argument("--coordinator-authorized", action="store_true")
    run.add_argument("--expected-digest", required=True)
    run.add_argument("--state", type=Path, default=DEFAULT_STATE)
    run.add_argument("--dispatch-spec", type=Path, required=True)
    run.add_argument("--approval-token", default="")
    run.add_argument("--timeout", type=int, default=1200)
    record = sub.add_parser(
        "record", help="complete the reserved cell with its authoritative receipt"
    )
    record.add_argument("--root", type=Path, default=ROOT)
    record.add_argument("--expected-digest", required=True)
    record.add_argument("--state", type=Path, default=DEFAULT_STATE)
    record.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        gate = load_gate(args.root.expanduser().resolve())
        if args.command == "run":
            report = gate.reserve_and_launch(
                coordinator_authorized=bool(args.coordinator_authorized),
                expected_digest=args.expected_digest,
                state_path=args.state,
                launcher=dispatch_corridor_driver,
                timeout=args.timeout,
                spec_path=args.dispatch_spec,
                approval_token=args.approval_token,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "record":
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            report = gate.record_receipt(
                receipt,
                expected_digest=args.expected_digest,
                state_path=args.state,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
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
