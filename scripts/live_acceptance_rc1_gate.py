#!/usr/bin/env python3
"""RC1 gate: the production owner of the three configured [rc1] cells.

Three commands with distinct effect boundaries:

- ``preflight`` is read-only: it validates the gate declaration from
  ``config/acceptance-cells.toml`` and names the next configured cell.
- ``run`` is effectful and coordinator-authorized only: it writes an
  exclusive durable reservation for exactly the next cell (state file:
  ``.vault-meta/acceptance/rc1-streak-state.json``, claim serialized by a
  file lock and bound to the dispatch spec digest and request identity)
  and launches the engineering/change corridor through the existing
  dispatch owner, ``scripts/dispatch-runner.py start``.  Without explicit
  coordinator authorization it refuses before any state or launch effect.
- ``record`` mutates durable state: it completes a *launched* reservation
  with one authoritative schema-2 receipt, but only after the whole
  receipt list re-validates through the ``v267_stabilization`` streak
  authority; a rejected receipt leaves the state untouched.

The legacy four-cell release path (``release-acceptance.py`` /
``live-acceptance-runner.py``) is untouched: the RC1 gate is additive
beside it.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


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


@contextmanager
def _claim_lock(state_path: Path) -> Iterator[None]:
    """Serialize the read/claim/launch/persist transaction on the state file.

    Atomic file replacement alone protects bytes, not the transaction; the
    exclusive lock makes the reservation claim linearizable so two callers
    cannot both claim and launch the same cell.
    """

    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _spec_identity(spec_path: Path) -> tuple[str, str]:
    """The dispatch spec digest and request identity that bind one claim."""

    try:
        payload = Path(spec_path).read_bytes()
        spec = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise stab.StabilizationError(f"cannot read dispatch spec: {exc}") from exc
    request_id = spec.get("request_id") if isinstance(spec, dict) else None
    if not isinstance(request_id, str) or not request_id:
        raise stab.StabilizationError(
            "dispatch spec requires a non-empty request_id"
        )
    return hashlib.sha256(payload).hexdigest(), request_id


def _read_streak_state(path: Path, *, expected_digest: str) -> dict[str, Any]:
    """Read durable progress without erasing an active dispatch identity."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "schema_version": 1,
            "expected_digest": expected_digest,
            "receipts": [],
            "reservation": None,
        }
    except (OSError, json.JSONDecodeError) as exc:
        raise stab.StabilizationError("streak state is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise stab.StabilizationError("streak state schema is invalid")
    if not isinstance(value.get("receipts"), list):
        raise stab.StabilizationError("streak state receipts are malformed")
    reservation = value.get("reservation")
    if reservation is not None and (
        not isinstance(reservation, dict)
        or not isinstance(reservation.get("cell_id"), str)
        or reservation.get("status") not in {"reserved", "launched"}
    ):
        raise stab.StabilizationError("streak state reservation is malformed")
    stored_digest = value.get("expected_digest")
    if reservation is not None and (
        not isinstance(reservation.get("request_id"), str)
        or not reservation["request_id"]
        or not isinstance(reservation.get("spec_sha256"), str)
        or len(reservation["spec_sha256"]) != 64
        or not set(reservation["spec_sha256"]) <= stab.HEX_DIGITS
        or reservation.get("lifecycle_subject_sha256") != stored_digest
    ):
        raise stab.StabilizationError(
            "streak state reservation changed its behavioral identity"
        )
    if reservation is not None and reservation.get("status") == "launched":
        launch = reservation.get("launch")
        harness = launch.get("harness") if isinstance(launch, dict) else None
        if (
            not isinstance(launch, dict)
            or launch.get("schema_version") != 1
            or launch.get("status") != "launched"
            or launch.get("request_id") != reservation["request_id"]
            or not isinstance(launch.get("worktree"), str)
            or not Path(launch["worktree"]).is_absolute()
            or not isinstance(harness, dict)
            or harness.get("operation_id") != reservation["request_id"]
            or harness.get("owner_id") != reservation["request_id"]
            or not all(
                isinstance(harness.get(field), str) and harness.get(field)
                for field in ("owner_id", "operation_id", "lane_id", "run_id")
            )
        ):
            raise stab.StabilizationError(
                "streak state launched identity is malformed"
            )
    if stored_digest != expected_digest:
        if reservation is not None:
            raise stab.StabilizationError(
                "behavioral digest drift cannot replace an active RC1 dispatch "
                "identity"
            )
        return {
            "schema_version": 1,
            "expected_digest": expected_digest,
            "receipts": [],
            "reservation": None,
        }
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
        spec_path: Path,
        evidence_root: Path | None = None,
        timeout: int = 1200,
        **launcher_options: Any,
    ) -> dict[str, Any]:
        """Reserve exactly the next configured cell and launch its corridor.

        Containment precedes authorization: without an explicit coordinator
        authorization no state is written and the launcher is never invoked.
        The whole read/claim/launch/persist transaction runs under an
        exclusive file lock, and the claim is bound immutably to the
        dispatch spec digest and request identity, so a second caller — or
        a changed spec — cannot relaunch the same cell.  A crashed launch
        leaves a ``reserved`` claim that only the identical spec identity
        may resume; a ``launched`` claim must be completed by ``record``
        before any further run.
        """

        if coordinator_authorized is not True:
            raise stab.StabilizationError(
                "RC1 cells require coordinator authorization"
            )
        spec_sha256, request_id = _spec_identity(spec_path)
        with _claim_lock(state_path):
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
            if reservation is not None:
                if reservation["cell_id"] != next_cell:
                    raise stab.StabilizationError(
                        f"streak state reserves {reservation['cell_id']} but "
                        f"the validated prefix requires {next_cell}"
                    )
                if reservation.get("status") == "launched":
                    raise stab.StabilizationError(
                        f"RC1 cell {next_cell} is already launched; record "
                        "its receipt before any further run"
                    )
                if (
                    reservation.get("spec_sha256") != spec_sha256
                    or reservation.get("request_id") != request_id
                ):
                    raise stab.StabilizationError(
                        f"RC1 cell {next_cell} is reserved by a different "
                        "dispatch identity; restart requires the identical spec"
                    )
            contract = self.authorize(next_cell, coordinator_authorized=True)
            state["reservation"] = {
                "cell_id": next_cell,
                "status": "reserved",
                "spec_sha256": spec_sha256,
                "request_id": request_id,
                "lifecycle_subject_sha256": expected_digest,
            }
            _atomic_json(state_path, state)
            launch = launcher(
                ROOT,
                contract,
                timeout=timeout,
                spec_path=spec_path,
                **launcher_options,
            )
            if not isinstance(launch, dict):
                raise stab.StabilizationError(
                    "corridor launcher returned no launch record"
                )
            harness = launch.get("harness")
            if (
                launch.get("schema_version") != 1
                or launch.get("status") != "launched"
                or launch.get("request_id") != request_id
                or not isinstance(launch.get("worktree"), str)
                or not Path(launch["worktree"]).is_absolute()
                or not isinstance(harness, dict)
                or harness.get("operation_id") != request_id
                or harness.get("owner_id") != request_id
                or not all(
                    isinstance(harness.get(field), str) and harness.get(field)
                    for field in ("owner_id", "operation_id", "lane_id", "run_id")
                )
            ):
                raise stab.StabilizationError(
                    "corridor launcher returned no durable Harness identity"
                )
            state["reservation"] = {
                "cell_id": next_cell,
                "status": "launched",
                "spec_sha256": spec_sha256,
                "request_id": request_id,
                "lifecycle_subject_sha256": expected_digest,
                "launch": launch,
            }
            _atomic_json(state_path, state)
        return {
            "schema_version": 1,
            "cell_id": next_cell,
            "status": "launched",
            "request_id": request_id,
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
        """Complete one *launched* reservation with its authoritative receipt.

        A receipt can only be recorded against a reservation whose corridor
        actually launched, and it must carry the launched dispatch request
        identity.  The candidate list is validated through the streak
        authority before anything is persisted, so a fabricated, tampered,
        or unbound receipt leaves the durable state untouched.
        """

        with _claim_lock(state_path):
            state = _read_streak_state(state_path, expected_digest=expected_digest)
            reservation = state.get("reservation")
            if reservation is None:
                raise stab.StabilizationError(
                    "no reserved RC1 cell is awaiting a receipt"
                )
            if reservation.get("status") != "launched":
                raise stab.StabilizationError(
                    f"RC1 cell {reservation['cell_id']} was never launched; "
                    "a receipt cannot be recorded for a failed or pending claim"
                )
            if (
                not isinstance(receipt, dict)
                or receipt.get("cell_id") != reservation["cell_id"]
            ):
                raise stab.StabilizationError(
                    f"receipt must complete the reserved cell "
                    f"{reservation['cell_id']}"
                )
            if receipt.get("request_id") != reservation.get("request_id"):
                raise stab.StabilizationError(
                    "receipt request_id must equal the launched dispatch "
                    "request identity"
                )
            launch = reservation.get("launch")
            harness = launch.get("harness") if isinstance(launch, dict) else None
            if (
                not isinstance(harness, dict)
                or receipt.get("owner_id") != harness.get("owner_id")
                or receipt.get("run_id") != harness.get("run_id")
                or receipt.get("worktree_id") != launch.get("worktree")
            ):
                raise stab.StabilizationError(
                    "receipt must match the reserved durable Harness identity"
                )
            candidate = [*state["receipts"], receipt]
            verdict = stab.validate_streak(
                candidate,
                expected_digest=expected_digest,
                config=self._config,
                gate=self._declaration,
                root=evidence_root if evidence_root is not None else ROOT,
            )
            # A negative/non-fresh receipt is a typed closure, not a streak
            # cell.  Its durable OperationStore record remains the evidence;
            # clearing the prefix makes the next reservation cell 1 again.
            state["receipts"] = candidate if verdict["streak"] else []
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
    try:
        launch = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise stab.StabilizationError(
            "dispatch corridor launch returned invalid identity"
        ) from exc
    harness = launch.get("harness") if isinstance(launch, dict) else None
    if (
        not isinstance(launch, dict)
        or launch.get("schema_version") != 1
        or launch.get("status") != "launched"
        or launch.get("request_id") != spec.get("request_id")
        or not isinstance(launch.get("worktree"), str)
        or not isinstance(harness, dict)
        or not all(
            isinstance(harness.get(field), str) and harness.get(field)
            for field in ("owner_id", "operation_id", "lane_id", "run_id")
        )
    ):
        raise stab.StabilizationError(
            "dispatch corridor launch returned no durable Harness identity"
        )
    return launch


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
