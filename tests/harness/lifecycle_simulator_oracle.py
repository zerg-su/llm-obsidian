"""Independent lifecycle invariants over content-free durable snapshots.

The oracle intentionally does not import the production state machine.  Its
rules are frozen test expectations derived from the simulator Outcome Contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
TERMINAL = frozenset({"complete", "failed", "cancelled"})
ACTIONS = frozenset(
    {
        "start-worker",
        "publish-provider-event",
        "reserve-effect",
        "resolve-effect",
        "publish-callback",
        "publish-summary",
        "publish-resolution",
        "publish-liveness",
        "publish-error-latch",
        "resource-disappears",
        "advance-clock",
        "worker-tick",
        "crash-at",
        "restart-worker",
        "reconcile",
        "close",
    }
)


@dataclass(frozen=True)
class InvariantViolation(AssertionError):
    invariant_id: str
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant_id}: {self.detail}"


def _fail(invariant_id: str, detail: str) -> None:
    raise InvariantViolation(invariant_id, detail)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("SIM-INV-SCHEMA", f"{label} must be an object")
    return value


def _identifier(value: object, label: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail("SIM-INV-SCHEMA", f"{label} must be a bounded identifier")
    return value


def _sha256(value: object, label: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        _fail("SIM-INV-SCHEMA", f"{label} must be a sha256")
    return value


def load_scenario(path: Path) -> dict[str, object]:
    """Load one bounded declarative scenario without executable fixture hooks."""

    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        _fail("SIM-INV-SCHEMA", "scenario must be one bounded regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("SIM-INV-SCHEMA", f"scenario JSON is invalid: {type(exc).__name__}")
    scenario = dict(_mapping(value, "scenario"))
    required = {
        "schema_version",
        "scenario_id",
        "seed",
        "max_steps",
        "expected_initial_invariant",
        "expected_terminal_states",
        "forbidden_effects",
        "initial_snapshot",
        "actions",
    }
    allowed = required | {"ordering_constraints", "max_schedules", "tags"}
    if not required <= set(scenario) or not set(scenario) <= allowed or scenario.get("schema_version") != 1:
        _fail("SIM-INV-SCHEMA", "scenario fields or version are invalid")
    _identifier(scenario.get("scenario_id"), "scenario_id")
    if type(scenario.get("seed")) is not int or scenario["seed"] < 0:
        _fail("SIM-INV-SCHEMA", "seed must be a non-negative integer")
    maximum = scenario.get("max_steps")
    actions = scenario.get("actions")
    if (
        type(maximum) is not int
        or not 1 <= maximum <= 256
        or not isinstance(actions, list)
        or not actions
        or len(actions) > maximum
    ):
        _fail("SIM-INV-SCHEMA", "scenario actions exceed their bound")
    for index, raw_action in enumerate(actions):
        action = _mapping(raw_action, f"actions[{index}]")
        if action.get("action") not in ACTIONS:
            _fail("SIM-INV-SCHEMA", f"actions[{index}] is not in the closed vocabulary")
        if any(callable(item) for item in action.values()):
            _fail("SIM-INV-SCHEMA", "scenario actions cannot contain callbacks")
        after = action.get("after", [])
        if not isinstance(after, list) or any(
            not isinstance(item, str) or not IDENTIFIER.fullmatch(item)
            for item in after
        ):
            _fail("SIM-INV-SCHEMA", "scenario dependencies are invalid")
    constraints = scenario.get("ordering_constraints", [])
    if not isinstance(constraints, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or any(not isinstance(part, str) or not IDENTIFIER.fullmatch(part) for part in item)
        for item in constraints
    ):
        _fail("SIM-INV-SCHEMA", "scenario ordering constraints are invalid")
    terminals = scenario.get("expected_terminal_states")
    forbidden = scenario.get("forbidden_effects")
    if (
        not isinstance(terminals, list)
        or not terminals
        or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in terminals)
        or not isinstance(forbidden, list)
        or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in forbidden)
    ):
        _fail("SIM-INV-SCHEMA", "terminal or forbidden-effect declarations are invalid")
    _identifier(scenario.get("expected_initial_invariant"), "expected_initial_invariant")
    _mapping(scenario.get("initial_snapshot"), "initial_snapshot")
    return scenario


def assert_snapshot(snapshot: Mapping[str, object]) -> None:
    """Raise the first stable invariant violated by one durable world snapshot."""

    value = _mapping(snapshot, "snapshot")
    operation = _mapping(value.get("operation"), "operation")
    liveness = _mapping(value.get("liveness"), "liveness")
    recovery = _mapping(value.get("recovery", {}), "recovery")
    latch = _mapping(value.get("error_latch", {}), "error_latch")

    _identifier(operation.get("operation_id"), "operation.operation_id")
    _identifier(operation.get("run_id"), "operation.run_id")
    revision = operation.get("revision")
    live_revision = liveness.get("operation_revision")
    state = _identifier(operation.get("state"), "operation.state")
    live_state = _identifier(liveness.get("operation_state"), "liveness.operation_state")
    if (
        type(revision) is not int
        or revision < 0
        or type(live_revision) is not int
        or live_revision < 0
    ):
        _fail("SIM-INV-SCHEMA", "operation and liveness revisions must be non-negative integers")

    coherent = revision == live_revision and state == live_state
    permitted_inflight = (
        recovery.get("status") == "prepared"
        and recovery.get("allow_inflight") is True
        and recovery.get("source_revision") == live_revision
        and recovery.get("target_revision") == revision
        and recovery.get("resume_state") == state
    )
    if not coherent and not permitted_inflight:
        _fail(
            "SIM-INV-OP-LIVENESS",
            f"operation {revision}/{state} disagrees with liveness {live_revision}/{live_state}",
        )

    recovery_binding = recovery.get("binding_sha256", "")
    latch_binding = latch.get("binding_sha256", "")
    if recovery_binding:
        _sha256(recovery_binding, "recovery.binding_sha256")
    if latch_binding:
        _sha256(latch_binding, "error_latch.binding_sha256")
    if latch.get("active") is True and recovery.get("status") == "applied":
        if recovery_binding and recovery_binding == latch_binding:
            _fail("SIM-INV-LATCH-RETIRED", "applied recovery left its matching latch active")

    head_boundary = _mapping(value.get("head_boundary", {}), "head_boundary")
    if head_boundary:
        reviewed = head_boundary.get("reviewed_head_sha")
        resolved = head_boundary.get("resolved_head_sha")
        if (
            not isinstance(reviewed, str)
            or GIT_SHA.fullmatch(reviewed) is None
            or not isinstance(resolved, str)
            or GIT_SHA.fullmatch(resolved) is None
            or head_boundary.get("attempt_terminal") not in {True, False}
            or head_boundary.get("continuation_requested") not in {True, False}
        ):
            _fail("SIM-INV-SCHEMA", "head boundary evidence is invalid")
        if (
            reviewed != resolved
            and head_boundary["attempt_terminal"] is True
            and head_boundary["continuation_requested"] is True
        ):
            _fail(
                "SIM-INV-TERMINAL-MONOTONIC",
                "a terminal exact-HEAD attempt requested cross-HEAD continuation",
            )

    attention_reason = operation.get("attention_reason")
    accepted_callback_id = operation.get("accepted_callback_id", "")
    _identifier(accepted_callback_id, "operation.accepted_callback_id", optional=True)
    if (
        state == "attention-required"
        and attention_reason == "callback-timeout"
        and accepted_callback_id
    ):
        _fail(
            "SIM-INV-CALLBACK-DEADLINE",
            "callback acceptance and callback-timeout attention both won",
        )

    artifacts = value.get("artifacts", [])
    if not isinstance(artifacts, list):
        _fail("SIM-INV-SCHEMA", "artifacts must be an array")
    for index, raw_artifact in enumerate(artifacts):
        artifact = _mapping(raw_artifact, f"artifacts[{index}]")
        _identifier(artifact.get("kind"), f"artifacts[{index}].kind")
        _sha256(artifact.get("identity_sha256"), f"artifacts[{index}].identity_sha256")
        status = _identifier(artifact.get("status"), f"artifacts[{index}].status")
        if status not in {"absent", "published"}:
            _fail(
                "SIM-INV-ATOMIC-PUBLICATION",
                f"{artifact['kind']} exposed a non-atomic {status} state",
            )

    effects = value.get("effects", [])
    if not isinstance(effects, list):
        _fail("SIM-INV-SCHEMA", "effects must be an array")
    delivered: dict[str, int] = {}
    effect_ids: set[str] = set()
    for index, raw_effect in enumerate(effects):
        effect = _mapping(raw_effect, f"effects[{index}]")
        effect_id = _identifier(effect.get("effect_id"), f"effects[{index}].effect_id")
        key = _identifier(
            effect.get("idempotency_key"), f"effects[{index}].idempotency_key"
        )
        deliveries = effect.get("deliveries")
        if type(deliveries) is not int or deliveries < 0:
            _fail("SIM-INV-SCHEMA", "effect deliveries must be non-negative integers")
        if effect_id in effect_ids:
            _fail("SIM-INV-EFFECT-ONCE", f"effect {effect_id} appears more than once")
        effect_ids.add(effect_id)
        delivered[key] = delivered.get(key, 0) + deliveries
        if delivered[key] > 1:
            _fail("SIM-INV-EFFECT-ONCE", f"idempotency key {key} was delivered more than once")

    callbacks = value.get("callbacks", [])
    if not isinstance(callbacks, list):
        _fail("SIM-INV-SCHEMA", "callbacks must be an array")
    accepted: set[str] = set()
    for index, raw_callback in enumerate(callbacks):
        callback = _mapping(raw_callback, f"callbacks[{index}]")
        _identifier(callback.get("callback_id"), f"callbacks[{index}].callback_id")
        identity = _sha256(
            callback.get("identity_sha256"), f"callbacks[{index}].identity_sha256"
        )
        expected = callback.get("expected_identity_sha256", identity)
        _sha256(expected, f"callbacks[{index}].expected_identity_sha256")
        if callback.get("accepted") not in {True, False}:
            _fail("SIM-INV-SCHEMA", "callback accepted must be boolean")
        if callback.get("accepted") is True:
            if identity != expected:
                _fail("SIM-INV-IDENTITY", "wrong-identity callback was consumed")
            if identity in accepted:
                _fail("SIM-INV-CALLBACK-ONCE", "callback identity was accepted more than once")
            accepted.add(identity)

    history = value.get("terminal_history", [])
    if not isinstance(history, list) or any(item not in TERMINAL for item in history):
        _fail("SIM-INV-SCHEMA", "terminal_history must contain terminal states only")
    if history and (state not in TERMINAL or state != history[-1]):
        _fail("SIM-INV-TERMINAL-MONOTONIC", "terminal operation was resurrected or rewritten")

    resources = _mapping(operation.get("resources", {}), "operation.resources")
    pending_effect = operation.get("pending_effect", "")
    _identifier(pending_effect, "operation.pending_effect", optional=True)
    callback_binding = liveness.get("callback_submit_binding", "")
    _sha256(callback_binding, "liveness.callback_submit_binding", optional=True)
    callback_status = liveness.get("callback_submit_status", "")
    _identifier(callback_status, "liveness.callback_submit_status", optional=True)
    if state in TERMINAL:
        owned = any(
            resources.get(key) not in {"", 0, None}
            for key in (
                "surface_id",
                "process_group",
                "supervisor_pid",
                "process_identity",
                "supervisor_identity",
            )
        )
        if owned or pending_effect or latch.get("active") is True or callback_binding:
            _fail("SIM-INV-RESOURCE-FREE", "terminal operation retained owned or pending state")

    receipts = value.get("resource_receipts", [])
    if not isinstance(receipts, list):
        _fail("SIM-INV-SCHEMA", "resource_receipts must be an array")
    closed: set[str] = set()
    for index, raw_receipt in enumerate(receipts):
        receipt = _mapping(raw_receipt, f"resource_receipts[{index}]")
        identity = _identifier(receipt.get("identity"), f"resource_receipts[{index}].identity")
        if receipt.get("status") == "closed":
            if identity in closed:
                _fail("SIM-INV-RESOURCE-CLOSE-ONCE", "resource close was published twice")
            closed.add(identity)
    if state == "exiting":
        owned = any(
            resources.get(key) not in {"", 0, None}
            for key in (
                "surface_id",
                "process_group",
                "supervisor_pid",
                "process_identity",
                "supervisor_identity",
            )
        )
        if not owned and not closed:
            _fail(
                "SIM-INV-CLEANUP-RECEIPT",
                "resource-free exiting state lacks a durable close receipt",
            )
