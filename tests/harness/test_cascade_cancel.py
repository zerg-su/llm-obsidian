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
    AttentionReason,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.state_machine import TERMINAL  # noqa: E402
from harness.store import OperationStore, StoreError  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402
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


def cancel_root(
    store,
    operation_id="op-root",
    *,
    process_adapter=None,
    cmux_adapter=None,
):
    return harness_cli._cancel_or_close_subtree(
        store,
        OWNER,
        operation_id,
        process_adapter=process_adapter or UnusedProcessAdapter(),
        cmux_adapter=cmux_adapter or UnusedCmuxAdapter(),
        bounded_cancel=True,
    )


def states(store, owner=OWNER) -> dict[str, str]:
    return {record.spec.operation_id: record.state for record in store.list(owner)}


class RecordingCmuxAdapter:
    """Cmux adapter that records every probe and replays scripted surface states."""

    def __init__(self, statuses) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str]] = []

    def status(self, surface_id: str) -> str:
        self.calls.append(("status", surface_id))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def workspace_status(self, workspace_id: str, window_id: str) -> str:
        self.calls.append(("workspace_status", workspace_id))
        return "missing"


class RecordingProcessAdapter:
    """Process adapter that records every probe instead of raising."""

    def __init__(self, status: str = "dead") -> None:
        self.status_value = status
        self.calls: list[tuple[str, object]] = []

    def process_status(self, process_group: int, identity: str) -> str:
        self.calls.append(("process_status", process_group))
        return self.status_value

    def supervisor_status(self, supervisor_pid: int, identity: str) -> str:
        self.calls.append(("supervisor_status", supervisor_pid))
        return self.status_value

    def status(self, *args, **kwargs) -> str:
        self.calls.append(("status", args))
        return self.status_value


class ExitThenDeadProcessAdapter(RecordingProcessAdapter):
    def __init__(self) -> None:
        super().__init__("alive")
        self.guardian_requests: list[dict[str, object]] = []

    def pid_status(self, supervisor_pid: int, identity: str) -> str:
        self.calls.append(("pid_status", supervisor_pid))
        return self.status_value

    def request_guardian_signal(
        self,
        _control_path: Path,
        **request: object,
    ) -> None:
        self.guardian_requests.append(dict(request))
        self.status_value = "dead"


class ClosingCmuxAdapter(RecordingCmuxAdapter):
    def close_exact(self, surface_id: str) -> None:
        self.calls.append(("close_exact", surface_id))
        self.statuses[:] = ["missing"]


def prepare_pending_effect(store) -> dict[str, object]:
    """An unreconciled effect from a crash blocks the descendant on attention."""

    store.begin_effect(OWNER, "op-verify", "request-exit")
    return {
        "process_adapter": RecordingProcessAdapter("dead"),
        "cmux_adapter": RecordingCmuxAdapter(["missing"]),
    }


def prepare_live_surface(store) -> dict[str, object]:
    """A live owned surface without a process group blocks on attention."""

    bind_surface(store, "op-verify", "surface-verify")
    return {
        "process_adapter": RecordingProcessAdapter("dead"),
        "cmux_adapter": RecordingCmuxAdapter(["alive"]),
    }


def corrupt_parent(store, operation_id, parent_operation_id) -> None:
    """Write a corrupt parent chain directly, simulating durable-store damage."""

    path = store.root / "owners" / OWNER / "operations" / f"{operation_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["spec"]["parent_operation_id"] = parent_operation_id
    path.write_text(json.dumps(value), encoding="utf-8")


def bind_surface(store, operation_id, surface_id):
    """Bind one exact owned surface to an operation, as a live session would."""

    return OperationSupervisor(store, OWNER, operation_id).bind_resources(
        OwnedResources(surface_id=surface_id)
    )


def bind_provider(store, operation_id, surface_id):
    record = OperationSupervisor(store, OWNER, operation_id).bind_resources(
        OwnedResources(
            surface_id=surface_id,
            process_group=4201,
            supervisor_pid=4202,
            process_identity="a" * 64,
            supervisor_identity="b" * 64,
        )
    )
    runtime_root = (
        store.root / "owners" / OWNER / "runtime" / operation_id
    )
    runtime_root.mkdir(parents=True)
    (runtime_root / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "run_id": record.run_id,
                "placement": "split",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime_root / "callback-target.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 1,
                "operation_id": operation_id,
                "run_id": record.run_id,
                "callback_pointer": "callbacks/result.json",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return record


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

        fixture_transitions = len(store.transitions)
        cancel_root(store)
        cascade_transitions = store.transitions[fixture_transitions:]
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
            "the cascade transitions no foreign owner and nothing outside the subtree",
            all(
                owner == OWNER and operation_id in SUBTREE
                for owner, operation_id, _state in cascade_transitions
            ),
            cascade_transitions,
        )

        drift_store = OperationStore(root / "cascade-drift")
        build_incident_fixture(drift_store)
        drift_before = states(drift_store)
        try:
            cancel_root(drift_store, "op-not-in-store")
        except StoreError as exc:
            drift = exc
        else:
            drift = None
        check(
            "unknown root is rejected as StoreError, not any exception",
            isinstance(drift, StoreError),
            type(drift).__name__,
        )
        check(
            "rejected drift applies no transition",
            states(drift_store) == drift_before,
            states(drift_store),
        )

        # --- FIX1-CANCEL-E3: a corrupt parent cycle fails closed instead of looping.
        cycle_store = OperationStore(root / "cascade-cycle")
        build_incident_fixture(cycle_store)
        cycle_before = states(cycle_store)
        corrupt_parent(cycle_store, "op-root", "op-review")
        try:
            harness_cli._exact_cancel_subtree(cycle_store, OWNER, "op-root")
        except StoreError as exc:
            cycle = exc
        else:
            cycle = None
        check(
            "a corrupt parent cycle raises StoreError",
            isinstance(cycle, StoreError),
            type(cycle).__name__,
        )
        check(
            "the cycle error names the repeated operation",
            "op-root" in str(cycle),
            str(cycle),
        )
        check(
            "the cycle guard applies no transition",
            states(cycle_store) == cycle_before,
            states(cycle_store),
        )

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

        live_store = ObservingStore(root / "cascade-live-provider")
        build_incident_fixture(live_store)
        bind_provider(live_store, "op-verify", "surface-live-verify")
        live_process = ExitThenDeadProcessAdapter()
        live_cmux = ClosingCmuxAdapter(["alive"])
        live_outcome = cancel_root(
            live_store,
            process_adapter=live_process,
            cmux_adapter=live_cmux,
        )
        live_after = live_store.read(OWNER, "op-verify")
        check(
            "one cascade cancel converges an exact live descendant",
            live_outcome.complete
            and live_after.state == "cancelled"
            and live_after.resources == OwnedResources()
            and len(live_process.guardian_requests) == 1
            and live_process.guardian_requests[0]["action"] == "request-exit",
            live_outcome,
        )

        # --- FIX1-CANCEL-E2: the cascade really releases owned resources.
        release_store = ObservingStore(root / "cascade-release")
        build_incident_fixture(release_store)
        bind_surface(release_store, "op-verify", "surface-verify")
        bind_surface(release_store, "op-created", "surface-created")
        bound_before = sorted(
            record.spec.operation_id
            for record in release_store.list(OWNER)
            if record.resources != OwnedResources()
        )
        check(
            "the release fixture actually binds resources first",
            bound_before == ["op-created", "op-verify"],
            bound_before,
        )
        release_cmux = RecordingCmuxAdapter(["missing"])
        release_process = RecordingProcessAdapter("dead")
        release_outcome = cancel_root(
            release_store,
            process_adapter=release_process,
            cmux_adapter=release_cmux,
        )
        check(
            "a resource-bearing subtree still cascades to completion",
            release_outcome.complete,
            release_outcome,
        )
        release_after = states(release_store)
        check(
            "every resource-bearing descendant reaches a terminal state",
            all(
                release_after[item] in TERMINAL
                for item in ("op-verify", "op-created", "op-root")
            ),
            release_after,
        )
        still_bound = sorted(
            record.spec.operation_id
            for record in release_store.list(OWNER)
            if record.spec.operation_id in SUBTREE
            and record.resources != OwnedResources()
        )
        check(
            "the cascade releases every exact owned surface",
            still_bound == [],
            still_bound,
        )
        probed = {surface for _call, surface in release_cmux.calls}
        check(
            "resource probes target only the exact owned surfaces",
            probed <= {"surface-verify", "surface-created"} and probed,
            release_cmux.calls,
        )

        # --- FIX1-CANCEL-E3: a reviewer descendant is never closed without proof.
        reviewer_store = ObservingStore(root / "cascade-reviewer")
        build_incident_fixture(reviewer_store)
        bind_surface(reviewer_store, "op-review-round", "surface-round")
        reviewer_cmux = RecordingCmuxAdapter(["missing"])
        reviewer_outcome = cancel_root(
            reviewer_store,
            process_adapter=RecordingProcessAdapter("dead"),
            cmux_adapter=reviewer_cmux,
        )
        check(
            "an unprovable reviewer descendant truncates the cascade",
            not reviewer_outcome.complete
            and reviewer_outcome.blocked.operation_id == "op-review-round",
            reviewer_outcome,
        )
        check(
            "the unprovable reviewer descendant is held for cleanup attention",
            reviewer_outcome.blocked.attention_reason
            == AttentionReason.CLEANUP_INCOMPLETE,
            reviewer_outcome.blocked,
        )
        check(
            "no surface is closed without durable cleanup ownership",
            reviewer_cmux.calls == [],
            reviewer_cmux.calls,
        )
        check(
            "the reviewer descendant keeps its owned surface",
            reviewer_store.read(OWNER, "op-review-round").resources.surface_id
            == "surface-round",
            reviewer_store.read(OWNER, "op-review-round").resources,
        )

        # --- FIX1-CANCEL-E2/E4: a truncated cascade is never reported as success.
        for label, prepare in (
            ("pending effect", prepare_pending_effect),
            ("live owned surface", prepare_live_surface),
        ):
            blocked_store = ObservingStore(root / f"cascade-blocked-{len(label)}")
            build_incident_fixture(blocked_store)
            adapters = prepare(blocked_store)
            outcome = cancel_root(blocked_store, **adapters)
            check(
                f"{label}: the cascade reports itself truncated",
                not outcome.complete,
                outcome,
            )
            check(
                f"{label}: the outcome names the requested root, not the blocker",
                outcome.root_operation_id == "op-root",
                outcome.root_operation_id,
            )
            check(
                f"{label}: the blocking descendant is identified",
                outcome.blocked is not None
                and outcome.blocked.operation_id == "op-verify",
                outcome.blocked,
            )
            payload = harness_cli._cascade_payload(outcome)
            check(
                f"{label}: the payload keys on the root and marks it partial",
                payload["operation_id"] == "op-root"
                and payload["status"] == "partial"
                and payload["blocked_operation_id"] == "op-verify"
                and payload["state"] not in TERMINAL,
                payload,
            )
            check(
                f"{label}: the root is left nonterminal, not falsely cancelled",
                states(blocked_store)["op-root"] not in TERMINAL,
                states(blocked_store)["op-root"],
            )

        # --- The public seam must not exit 0 on a truncated cascade.
        blocked_root = root / "cascade-blocked-cli"
        blocked_cli_store = OperationStore(blocked_root)
        build_incident_fixture(blocked_cli_store)
        blocked_cli_store.begin_effect(OWNER, "op-verify", "request-exit")
        blocked_proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/harness-cli.py"),
                "--store",
                str(blocked_root),
                "--owner",
                OWNER,
                "--json",
                "cancel",
                "op-root",
            ],
            capture_output=True,
            text=True,
        )
        check(
            "public cancel does not exit 0 when the cascade is truncated",
            blocked_proc.returncode == harness_cli.CASCADE_PARTIAL_EXIT,
            (blocked_proc.returncode, blocked_proc.stdout, blocked_proc.stderr),
        )
        blocked_payload = json.loads(blocked_proc.stdout.strip().splitlines()[0])
        check(
            "the truncated payload names the requested root",
            blocked_payload["operation_id"] == "op-root"
            and blocked_payload["status"] == "partial"
            and blocked_payload["blocked_operation_id"] == "op-verify",
            blocked_payload,
        )
        check(
            "the truncated cascade leaves the root nonterminal",
            states(OperationStore(blocked_root))["op-root"] not in TERMINAL,
            states(OperationStore(blocked_root)),
        )

        # --- FIX1-CANCEL-E4: a recoverable blocker makes forward progress on retry.
        retry_store = ObservingStore(root / "cascade-retry")
        build_incident_fixture(retry_store)
        bind_surface(retry_store, "op-verify", "surface-verify")
        retry_cmux = RecordingCmuxAdapter(["alive", "missing"])
        retry_process = RecordingProcessAdapter("dead")
        first_pass = cancel_root(
            retry_store,
            process_adapter=retry_process,
            cmux_adapter=retry_cmux,
        )
        check(
            "a live owned surface truncates the first cascade",
            not first_pass.complete,
            first_pass,
        )
        second_pass = cancel_root(
            retry_store,
            process_adapter=retry_process,
            cmux_adapter=retry_cmux,
        )
        check(
            "re-running the same command makes forward progress",
            second_pass.complete,
            second_pass,
        )
        retry_after = states(retry_store)
        retry_nonterminal = sorted(
            operation_id
            for operation_id, state in retry_after.items()
            if operation_id in SUBTREE and state not in TERMINAL
        )
        check(
            "the retried cascade terminalizes the whole exact subtree",
            retry_nonterminal == [],
            retry_after,
        )

    print("cascade cancellation regressions: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
