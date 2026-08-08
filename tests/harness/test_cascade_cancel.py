#!/usr/bin/env python3
"""Exact-subtree cascading cancellation regressions for the public harness CLI.

Covers the RC4 Fix1 defect where cancelling a root dispatch terminalized only
that operation and left its exact ``parent_operation_id`` descendants — notably
a Simple review parent and its review-round — nonterminal.

Evidence ids: FIX1-CANCEL-E1 (repro), E2 (cascade), E3 (isolation),
E4 (idempotency / no replay).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.state_machine import TERMINAL  # noqa: E402
from harness.store import OperationStore  # noqa: E402
import harness.cli as harness_cli  # noqa: E402


OWNER = "owner-cascade-root"
FOREIGN_OWNER = "owner-cascade-foreign"

EXECUTOR_ROUTE = RuntimeRoute("claude", "claude-opus-5", "medium", "executor", "a" * 64)
REVIEWER_ROUTE = RuntimeRoute(
    "claude", "claude-opus-5", "high", "reviewer-callback", "b" * 64
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}" if detail else label)
    print(f"OK   {label}")


class ObservingStore(OperationStore):
    """Store that records the exact order of observed transitions."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.transitions: list[tuple[str, str, str]] = []

    def transition(self, owner_id, operation_id, state, **kwargs):
        result = super().transition(owner_id, operation_id, state, **kwargs)
        if result.changed:
            self.transitions.append((owner_id, operation_id, state))
        return result

    def cancel_order(self) -> list[str]:
        """First observed cancellation-side transition per operation, in order."""

        order: list[str] = []
        for _owner, operation_id, state in self.transitions:
            if state in {"cancelling", "exiting"} or state in TERMINAL:
                if operation_id not in order:
                    order.append(operation_id)
        return order


class UnusedProcessAdapter:
    def status(self, *args, **kwargs):
        raise AssertionError("process adapter must not run for resource-free records")


class UnusedCmuxAdapter:
    def status(self, *args, **kwargs):
        raise AssertionError("cmux adapter must not run for resource-free records")


def _create(store, operation_id, kind, route, states, *, owner=OWNER, parent=""):
    spec = OperationSpec(
        operation_id,
        f"key-{operation_id}",
        kind,
        owner,
        route,
        "packet.json",
        "scoped",
        parent_operation_id=parent,
    )
    store.create(spec, lane_id=f"lane-{operation_id}", run_id=f"run-{operation_id}")
    for state in states:
        store.transition(owner, operation_id, state)
    return store.read(owner, operation_id)


RUNNING = ("preflight", "starting", "running")


def build_incident_fixture(store) -> None:
    """The exact incident lineage plus isolation neighbours.

    op-root (dispatch, running)
      op-verify        (verification,   running)
      op-review        (review-session, awaiting-callback, reviewer-callback)
        op-review-round(review-round,   awaiting-callback, reviewer-callback)
        op-review-done (review-round,   cancelled — already terminal)
      op-created       (verification,   created)
      op-finalizing    (verification,   finalizing)
    op-sibling-root (dispatch, running)   — same owner, different subtree
      op-sibling-child (verification, running)
    op-foreign (dispatch, running)        — different owner
    """

    _create(store, "op-root", "dispatch", EXECUTOR_ROUTE, RUNNING)
    _create(store, "op-verify", "verification", EXECUTOR_ROUTE, RUNNING, parent="op-root")
    _create(
        store,
        "op-review",
        "review-session",
        REVIEWER_ROUTE,
        RUNNING + ("awaiting-callback",),
        parent="op-root",
    )
    _create(
        store,
        "op-review-round",
        "review-round",
        REVIEWER_ROUTE,
        RUNNING + ("awaiting-callback",),
        parent="op-review",
    )
    _create(
        store,
        "op-review-done",
        "review-round",
        REVIEWER_ROUTE,
        ("preflight", "starting", "cancelling", "exiting", "cancelled"),
        parent="op-review",
    )
    _create(store, "op-created", "verification", EXECUTOR_ROUTE, (), parent="op-root")
    _create(
        store,
        "op-finalizing",
        "verification",
        EXECUTOR_ROUTE,
        RUNNING + ("finalizing",),
        parent="op-root",
    )
    _create(store, "op-sibling-root", "dispatch", EXECUTOR_ROUTE, RUNNING)
    _create(
        store,
        "op-sibling-child",
        "verification",
        EXECUTOR_ROUTE,
        RUNNING,
        parent="op-sibling-root",
    )
    _create(store, "op-foreign", "dispatch", EXECUTOR_ROUTE, RUNNING, owner=FOREIGN_OWNER)


SUBTREE = {
    "op-root",
    "op-verify",
    "op-review",
    "op-review-round",
    "op-review-done",
    "op-created",
    "op-finalizing",
}
OUTSIDE = {"op-sibling-root", "op-sibling-child"}


def cancel_root(store, operation_id="op-root"):
    return harness_cli._cancel_or_close(
        store,
        OWNER,
        operation_id,
        process_adapter=UnusedProcessAdapter(),
        cmux_adapter=UnusedCmuxAdapter(),
    )


def states(store, owner=OWNER) -> dict[str, str]:
    return {record.spec.operation_id: record.state for record in store.list(owner)}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="harness-cascade-cancel.") as raw:
        root = Path(raw)

        # --- FIX1-CANCEL-E1 / E2: one root cancel terminalizes the exact subtree.
        store = ObservingStore(root / "cascade")
        build_incident_fixture(store)
        before = states(store)
        check(
            "fixture reproduces the incident shape",
            before["op-review"] == "awaiting-callback"
            and before["op-review-round"] == "awaiting-callback",
            before,
        )

        cancel_root(store)
        after = states(store)
        nonterminal = sorted(
            operation_id
            for operation_id, state in after.items()
            if operation_id in SUBTREE and state not in TERMINAL
        )
        check(
            "one root cancel leaves every exact descendant terminal",
            nonterminal == [],
            {"nonterminal": nonterminal, "after": after},
        )
        check(
            "root itself is terminal after cascade",
            after["op-root"] in TERMINAL,
            after["op-root"],
        )
        check(
            "already-terminal descendants keep their terminal state",
            after["op-review-done"] == "cancelled",
            after["op-review-done"],
        )
        resource_bound = sorted(
            record.spec.operation_id
            for record in store.list(OWNER)
            if record.spec.operation_id in SUBTREE
            and record.resources != OwnedResources()
        )
        check(
            "cascade leaves the exact subtree resource-free",
            resource_bound == [],
            resource_bound,
        )

        order = [item for item in store.cancel_order() if item in SUBTREE]
        check(
            "cascade is child-first: review-round precedes its review parent",
            order.index("op-review-round") < order.index("op-review"),
            order,
        )
        check(
            "cascade is child-first: every descendant precedes the root",
            order and order[-1] == "op-root",
            order,
        )

        # --- FIX1-CANCEL-E3: isolation.
        check(
            "sibling subtree under the same owner is untouched",
            all(after[item] == before[item] for item in OUTSIDE),
            {item: (before[item], after[item]) for item in OUTSIDE},
        )
        check(
            "foreign owner is untouched",
            states(store, FOREIGN_OWNER) == {"op-foreign": "running"},
            states(store, FOREIGN_OWNER),
        )
        check(
            "no transition was ever applied to a foreign owner",
            all(owner == OWNER for owner, _op, _state in store.transitions),
            store.transitions,
        )

        drift = store_error_for_unknown_root(root / "cascade-drift")
        check("unknown root is rejected as identity drift", drift, drift)

        # --- FIX1-CANCEL-E4: idempotency and no replay.
        repeat_store = ObservingStore(root / "cascade-repeat")
        build_incident_fixture(repeat_store)
        cancel_root(repeat_store)
        first = states(repeat_store)
        first_revisions = {
            record.spec.operation_id: record.revision
            for record in repeat_store.list(OWNER)
        }
        cancel_root(repeat_store)
        second = states(repeat_store)
        second_revisions = {
            record.spec.operation_id: record.revision
            for record in repeat_store.list(OWNER)
        }
        check("repeated root cancel is state-idempotent", first == second, (first, second))
        check(
            "repeated root cancel writes no new revisions",
            first_revisions == second_revisions,
            (first_revisions, second_revisions),
        )

        # --- FIX1-CANCEL-E2 at the public seam.
        cli_root = root / "cascade-cli"
        build_incident_fixture(OperationStore(cli_root))
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/harness-cli.py"),
                "--store",
                str(cli_root),
                "--owner",
                OWNER,
                "--json",
                "cancel",
                "op-root",
            ],
            capture_output=True,
            text=True,
        )
        check("public cancel exits 0", proc.returncode == 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[0])
        check(
            "public cancel reports a terminal root",
            payload.get("state") in TERMINAL,
            payload,
        )
        cli_after = states(OperationStore(cli_root))
        cli_nonterminal = sorted(
            operation_id
            for operation_id, state in cli_after.items()
            if operation_id in SUBTREE and state not in TERMINAL
        )
        check(
            "public cancel leaves no exact descendant nonterminal",
            cli_nonterminal == [],
            {"nonterminal": cli_nonterminal, "after": cli_after},
        )
        check(
            "public cancel does not touch the sibling subtree",
            all(cli_after[item] == "running" for item in OUTSIDE),
            {item: cli_after[item] for item in OUTSIDE},
        )

    print("cascade cancellation regressions: ok")
    return 0


def store_error_for_unknown_root(path: Path) -> str:
    """Cancelling an id that is not in the store must fail closed."""

    store = OperationStore(path)
    build_incident_fixture(store)
    try:
        harness_cli._cancel_or_close(
            store,
            OWNER,
            "op-not-in-store",
            process_adapter=UnusedProcessAdapter(),
            cmux_adapter=UnusedCmuxAdapter(),
        )
    except Exception as exc:  # StoreError / ContractError
        return type(exc).__name__
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
