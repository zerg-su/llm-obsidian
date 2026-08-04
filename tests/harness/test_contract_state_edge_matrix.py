#!/usr/bin/env python3
"""Mutation-sensitive edge matrix for pure harness contracts and transitions."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    CallbackEnvelope,
    CapabilityReport,
    ContextPacketManifest,
    ContractError,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    TransitionResult,
    VerificationEvidence,
    operation_record_from_dict,
    operation_spec_from_dict,
    runtime_route_from_dict,
    to_dict,
)
from harness.state_machine import (  # noqa: E402
    TERMINAL,
    TRANSITIONS,
    begin_effect,
    resolve_effect,
    transition,
)
from harness.callback_submit_recovery import (  # noqa: E402
    ArtifactEvidence,
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    classify_callback_submit,
)
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
)


DIGEST = "a" * 64
ROUTE = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", DIGEST)
SPEC = OperationSpec(
    "edge-operation",
    "edge-key",
    "dispatch",
    "edge-owner",
    ROUTE,
    "packets/edge.json",
    "scoped",
)


EXPECTED_TRANSITIONS = {
    source: frozenset(targets)
    for source, targets in json.loads(
        (Path(__file__).with_name("state_transition_oracle.json")).read_text(
            encoding="utf-8"
        )
    ).items()
}


def record(state: str = "running", **changes: object) -> OperationRecord:
    base = OperationRecord(SPEC, state, 2, "edge-lane", "edge-run")
    return replace(base, **changes)


def expect_error(label: str, expected: str, call: Callable[[], object]) -> None:
    try:
        call()
    except ContractError as exc:
        assert expected in str(exc), (label, str(exc))
    else:
        raise AssertionError(f"{label}: invalid value was accepted")


def contract_primitive_matrix() -> None:
    for label, value in (
        ("non-string identifier", None),
        ("empty identifier", ""),
        ("identifier punctuation", "bad value"),
        ("oversized identifier", "x" * 129),
    ):
        expect_error(
            label,
            "model must be a bounded identifier",
            lambda value=value: replace(ROUTE, model=value),
        )
    for label, value in (
        ("non-string digest", None),
        ("uppercase digest", "A" * 64),
        ("short digest", "a" * 63),
    ):
        expect_error(
            label,
            "routing_sha256 must be a lowercase sha256",
            lambda value=value: replace(ROUTE, routing_sha256=value),
        )
    expect_error("unknown runtime", "runtime must be", lambda: replace(ROUTE, runtime="other"))

    for label, value in (
        ("non-string path", None),
        ("empty path", ""),
        ("backslash path", "packets\\edge.json"),
        ("absolute path", "/packets/edge.json"),
        ("parent path", "packets/../edge.json"),
    ):
        expected = "non-empty POSIX path" if label in {"non-string path", "empty path", "backslash path"} else "owner-relative"
        expect_error(label, expected, lambda value=value: replace(SPEC, context_manifest=value))
    expect_error(
        "operation schema",
        "unsupported OperationSpec schema",
        lambda: replace(SPEC, schema_version=2),
    )
    expect_error(
        "optional contract digest",
        "contract_sha256 must be a lowercase sha256",
        lambda: replace(SPEC, contract_sha256="bad"),
    )

    for label, call in (
        (
            "manifest schema",
            lambda: ContextPacketManifest("packet", SPEC.operation_id, (), DIGEST, 0, 2),
        ),
        (
            "manifest byte count",
            lambda: ContextPacketManifest("packet", SPEC.operation_id, (), DIGEST, -1),
        ),
        (
            "manifest normalization",
            lambda: ContextPacketManifest("packet", SPEC.operation_id, ("a//b",), DIGEST, 1),
        ),
        (
            "manifest uniqueness",
            lambda: ContextPacketManifest("packet", SPEC.operation_id, ("a", "a"), DIGEST, 1),
        ),
    ):
        expect_error(label, "ContextPacketManifest" if "manifest" in label and label in {"manifest schema", "manifest byte count"} else "manifest files", call)


def resource_and_record_matrix() -> None:
    for label, expected, call in (
        ("surface id", "surface_id", lambda: OwnedResources(surface_id="bad value")),
        ("negative group", "cannot be negative", lambda: OwnedResources(process_group=-1)),
        ("negative supervisor", "cannot be negative", lambda: OwnedResources(supervisor_pid=-1)),
        ("process digest", "process_identity", lambda: OwnedResources(process_group=2, process_identity="bad")),
        ("supervisor digest", "supervisor_identity", lambda: OwnedResources(supervisor_pid=2, supervisor_identity="bad")),
        ("group identity binding", "requires a process group", lambda: OwnedResources(process_group=1, process_identity=DIGEST)),
        ("supervisor identity binding", "requires a supervisor PID", lambda: OwnedResources(supervisor_pid=1, supervisor_identity=DIGEST)),
    ):
        expect_error(label, expected, call)
    owned = OwnedResources("surface-1", 2, 3, DIGEST, "b" * 64)
    assert owned.process_group == 2 and owned.supervisor_pid == 3

    invalid_records: tuple[tuple[str, str, dict[str, object]], ...] = (
        ("record schema", "metadata", {"schema_version": 2}),
        ("negative revision", "metadata", {"revision": -1}),
        ("negative attempt", "metadata", {"attempt": -1}),
        ("boolean attempt limit", "budget", {"attempt_limit": True}),
        ("zero attempt limit", "budget", {"attempt_limit": 0}),
        ("attempt overflow", "budget", {"attempt": 4, "attempt_limit": 3}),
        ("negative restart count", "budget", {"model_restarts": -1}),
        ("restart overflow", "budget", {"model_restarts": 2, "model_restart_limit": 1}),
        ("negative restart limit", "budget", {"model_restart_limit": -1}),
        ("zero token limit", "budget", {"token_limit": 0}),
        ("negative token use", "budget", {"tokens_used": -1}),
        ("token overflow", "budget", {"token_limit": 1, "tokens_used": 2}),
        ("deadline type", "budget", {"deadline_at": "later"}),
        ("deadline bool", "budget", {"deadline_at": True}),
        ("deadline infinity", "budget", {"deadline_at": math.inf}),
        ("deadline negative", "budget", {"deadline_at": -0.1}),
        ("state identifier", "state", {"state": "bad state"}),
        ("lane identifier", "lane_id", {"lane_id": "bad lane"}),
        ("run identifier", "run_id", {"run_id": "bad run"}),
        ("resume identifier", "resume_state", {"state": "attention-required", "resume_state": "bad state"}),
        ("resume state scope", "only while attention", {"resume_state": "running"}),
        ("pending identifier", "pending_effect", {"pending_effect": "bad effect", "effect_id": "bad effect", "effect_outcome": EffectOutcome.PENDING}),
        ("effect identifier", "effect_id", {"effect_id": "bad effect", "effect_outcome": EffectOutcome.SUCCEEDED}),
        ("outcome type", "bounded outcome", {"effect_outcome": "none"}),
        ("none with identity", "requires an outcome", {"effect_id": "effect"}),
        ("pending missing identity", "must agree", {"effect_outcome": EffectOutcome.PENDING}),
        ("pending mismatch", "must agree", {"pending_effect": "effect-a", "effect_id": "effect-b", "effect_outcome": EffectOutcome.PENDING}),
        ("resolved missing identity", "must agree", {"effect_outcome": EffectOutcome.FAILED}),
        ("resolved still pending", "must agree", {"pending_effect": "effect", "effect_id": "effect", "effect_outcome": EffectOutcome.SUCCEEDED}),
        ("callback partial", "must be complete", {"accepted_callback_id": "callback"}),
        ("callback id", "accepted_callback_id", {"accepted_callback_id": "bad callback", "accepted_callback_kind": "review", "accepted_callback_sha256": DIGEST}),
        ("callback kind", "accepted_callback_kind", {"accepted_callback_id": "callback", "accepted_callback_kind": "bad kind", "accepted_callback_sha256": DIGEST}),
        ("callback digest", "accepted_callback_sha256", {"accepted_callback_id": "callback", "accepted_callback_kind": "review", "accepted_callback_sha256": "bad"}),
    )
    for label, expected, changes in invalid_records:
        expect_error(label, expected, lambda changes=changes: record(**changes))
    attention = record(
        "attention-required",
        resume_state="running",
        attention_reason=AttentionReason.ATTENTION_REQUIRED,
    )
    accepted = record(
        accepted_callback_id="callback",
        accepted_callback_kind="review",
        accepted_callback_sha256=DIGEST,
    )
    assert attention.resume_state == "running" and accepted.accepted_callback_kind == "review"


def remaining_contract_matrix() -> None:
    for label, call in (
        ("invalid capability name", lambda: CapabilityReport(ROUTE, True, ("bad value",))),
        ("compatible reason", lambda: CapabilityReport(ROUTE, True, (), AttentionReason.CAPABILITY_MISMATCH)),
        ("missing incompatible reason", lambda: CapabilityReport(ROUTE, False, ())),
    ):
        expect_error(label, "capability", call)
    assert CapabilityReport(ROUTE, False, ("shell",), AttentionReason.CAPABILITY_MISMATCH).reason

    payload = {"nested": {"values": [1, True, None]}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    callback = CallbackEnvelope("callback", SPEC.operation_id, "edge-run", "review", payload, hashlib.sha256(encoded).hexdigest())
    assert dict(callback.payload) == payload
    callback_cases = (
        ("callback schema", "schema", {"schema_version": 2}),
        ("callback identifier", "callback_id", {"callback_id": "bad id"}),
        ("callback non-json", "JSON serializable", {"payload": {"bad": {1}}}),
        ("callback digest shape", "lowercase sha256", {"payload_sha256": "bad"}),
        ("callback digest binding", "digest mismatch", {"payload_sha256": DIGEST}),
    )
    for label, expected, changes in callback_cases:
        expect_error(label, expected, lambda changes=changes: replace(callback, **changes))
    large = {"value": "x" * CallbackEnvelope.MAX_PAYLOAD_BYTES}
    large_digest = hashlib.sha256(json.dumps(large, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expect_error("payload cap", "size cap", lambda: replace(callback, payload=large, payload_sha256=large_digest))

    evidence = VerificationEvidence("scoped", DIGEST, "b" * 40, "verify", ".", 0, "start", "finish", "outputs/result.txt")
    for label, expected, changes in (
        ("evidence profile", "profile", {"profile": "bad value"}),
        ("evidence profile digest", "profile_sha256", {"profile_sha256": "bad"}),
        ("evidence git id", "git object", {"head_sha": "bad"}),
        ("evidence command", "command_id", {"command_id": "bad value"}),
        ("evidence cwd", "owner-relative", {"cwd": "/tmp"}),
        ("evidence output", "owner-relative", {"output_pointer": "../result"}),
        ("evidence exit", "integer", {"exit_code": "0"}),
        ("evidence started", "timestamps", {"started_at": ""}),
        ("evidence finished", "timestamps", {"finished_at": ""}),
    ):
        expect_error(label, expected, lambda changes=changes: replace(evidence, **changes))
    result = TransitionResult(SPEC.operation_id, "running", "finalizing", 3, True)
    for field, value in (("operation_id", "bad id"), ("previous_state", "bad state"), ("state", "bad state")):
        expect_error(field, field, lambda field=field, value=value: replace(result, **{field: value}))
    expect_error("negative result revision", "negative", lambda: replace(result, revision=-1))


def serialization_and_hydration_matrix() -> None:
    active = record(
        resources=OwnedResources("surface", 2, 3, DIGEST, "b" * 64),
        effect_id="effect",
        effect_outcome=EffectOutcome.SUCCEEDED,
        accepted_callback_id="callback",
        accepted_callback_kind="review",
        accepted_callback_sha256=DIGEST,
    )
    serialized = to_dict(active)
    assert serialized["effect_outcome"] == "succeeded"
    assert serialized["resources"]["process_group"] == 2
    assert isinstance(serialized["spec"], dict)
    assert to_dict(CapabilityReport(ROUTE, True, ("shell",)))["capabilities"] == ["shell"]
    expect_error("serialize non-contract", "dataclass", lambda: to_dict({"record": active}))

    assert runtime_route_from_dict(to_dict(ROUTE)) == ROUTE
    assert operation_spec_from_dict(to_dict(SPEC)) == SPEC
    hydrated = operation_record_from_dict(serialized)
    assert hydrated == active and hydrated.resources == active.resources
    legacy = dict(to_dict(record()))
    legacy.pop("lane_id")
    legacy.pop("run_id")
    legacy.pop("effect_outcome")
    legacy["pending_effect"] = "legacy-effect"
    legacy["effect_id"] = ""
    legacy_record = operation_record_from_dict(legacy)
    assert legacy_record.lane_id == "legacy-lane"
    assert legacy_record.run_id == "legacy-run"
    assert legacy_record.effect_id == "legacy-effect"
    assert legacy_record.effect_outcome == EffectOutcome.PENDING
    clean = dict(to_dict(record()))
    clean.pop("effect_outcome")
    assert operation_record_from_dict(clean).effect_outcome == EffectOutcome.NONE


def state_machine_matrix() -> None:
    same = record("running", attention_reason=AttentionReason.CALLBACK_TIMEOUT)
    unchanged, result = transition(same, "running", reason=AttentionReason.CONTRACT_DRIFT)
    assert unchanged is same and not result.changed
    assert result.attention_reason == AttentionReason.CALLBACK_TIMEOUT

    assert {
        source: frozenset(targets) for source, targets in TRANSITIONS.items()
    } == EXPECTED_TRANSITIONS
    for source, targets in EXPECTED_TRANSITIONS.items():
        for target in targets:
            reason = AttentionReason.ATTENTION_REQUIRED if target == "attention-required" else None
            updated, result = transition(record(source), target, reason=reason)
            assert updated.state == target and updated.revision == 3 and result.changed
            if target == "attention-required":
                assert updated.resume_state == source and updated.attention_reason == reason
            else:
                assert updated.resume_state == "" and updated.attention_reason is None
    expect_error("unknown source", "illegal transition", lambda: transition(record("unknown"), "running"))
    expect_error("illegal target", "illegal transition", lambda: transition(record("created"), "complete"))
    expect_error("missing attention reason", "needs a reason", lambda: transition(record("created"), "attention-required"))
    expect_error("stray attention reason", "only valid", lambda: transition(record("created"), "preflight", reason=AttentionReason.CONTRACT_DRIFT))

    pending = begin_effect(record("running"), "effect-a")
    expect_error("pending transition barrier", "unresolved external effect", lambda: transition(pending, "verifying"))
    attention, _ = transition(pending, "attention-required", reason=AttentionReason.ATTENTION_REQUIRED)
    assert attention.pending_effect == "effect-a" and attention.resume_state == "running"
    assert begin_effect(pending, "effect-a") is pending
    expect_error("different pending effect", "already pending", lambda: begin_effect(pending, "effect-b"))
    expect_error("empty effect", "identifier is required", lambda: begin_effect(record(), ""))
    expect_error("invalid effect id", "pending_effect", lambda: begin_effect(record(), "bad effect"))
    for terminal in TERMINAL:
        expect_error(f"terminal {terminal}", "terminal operation", lambda terminal=terminal: begin_effect(record(terminal), "effect"))

    for outcome in (EffectOutcome.SUCCEEDED, EffectOutcome.FAILED):
        resolved = resolve_effect(pending, outcome)
        assert resolved.pending_effect == "" and resolved.effect_outcome == outcome
        assert resolved.revision == pending.revision + 1
        assert resolve_effect(resolved, outcome) is resolved
        assert begin_effect(resolved, "effect-a") is resolved
        expect_error("conflicting resolution replay", "no external effect", lambda resolved=resolved, outcome=outcome: resolve_effect(resolved, EffectOutcome.FAILED if outcome == EffectOutcome.SUCCEEDED else EffectOutcome.SUCCEEDED))
    for outcome in (EffectOutcome.NONE, EffectOutcome.PENDING):
        expect_error("invalid resolution", "must be succeeded or failed", lambda outcome=outcome: resolve_effect(pending, outcome))
    fresh = begin_effect(resolve_effect(pending, EffectOutcome.SUCCEEDED), "effect-b")
    assert fresh.effect_id == "effect-b" and fresh.effect_outcome == EffectOutcome.PENDING


def callback_recovery_transition_matrix() -> None:
    artifact = ArtifactEvidence
    base = CallbackSubmitEvidence(
        observed_at=1_000,
        generation_progress_at=100,
        callback_deadline_at=1_300,
        operation_id="review-round",
        run_id="review-run",
        lane_id="openai-holistic",
        generation=3,
        expected_operation_id="review-round",
        expected_run_id="review-run",
        expected_lane_id="openai-holistic",
        expected_generation=3,
        target_sha256=DIGEST,
        expected_target_sha256=DIGEST,
        operation_state="awaiting-callback",
        process_status="alive",
        surface_status="alive",
        prompt_class="idle-prompt",
        stable_idle_observations=2,
    )
    policy = CallbackSubmitPolicy.default()
    cases = (
        (
            "callback before reservation",
            {"callback_artifact": artifact("stable", "b" * 64)},
            "accept-callback",
            "",
        ),
        (
            "callback between reservation and send",
            {
                "callback_artifact": artifact("stable", "b" * 64),
                "nudge_count": 1,
                "recovery_status": "reserved",
            },
            "accept-callback",
            "",
        ),
        (
            "receipt after send",
            {
                "receipt_artifact": artifact("stable", "c" * 64),
                "nudge_count": 1,
                "recovery_status": "sent",
            },
            "none",
            "",
        ),
        (
            "deadline expires after reservation",
            {
                "callback_deadline_at": 1_059,
                "nudge_count": 1,
                "recovery_status": "reserved",
            },
            "attention-required",
            "callback-submit-deadline-insufficient",
        ),
        (
            "terminal after reservation",
            {
                "operation_state": "complete",
                "nudge_count": 1,
                "recovery_status": "reserved",
            },
            "attention-required",
            "callback-submit-terminal",
        ),
        (
            "generation changes after reservation",
            {
                "expected_generation": 4,
                "nudge_count": 1,
                "recovery_status": "reserved",
            },
            "attention-required",
            "callback-submit-stale-generation",
        ),
        (
            "ownership becomes unknown after reservation",
            {
                "surface_status": "unknown",
                "nudge_count": 1,
                "recovery_status": "reserved",
            },
            "attention-required",
            "callback-submit-ownership-lost",
        ),
        (
            "send result is uncertain",
            {"nudge_count": 1, "recovery_status": "uncertain"},
            "attention-required",
            "callback-submit-effect-uncertain",
        ),
    )
    for label, changes, expected_action, expected_reason in cases:
        decision = classify_callback_submit(replace(base, **changes), policy)
        assert (decision.action, decision.reason) == (
            expected_action,
            expected_reason,
        ), (label, decision)

    with tempfile.TemporaryDirectory(prefix="callback-shared-ceiling.") as raw:
        generic_first = LivenessController(Path(raw) / "generic-first")
        initial = LivenessEvidence(
            0, "alive", 1, "awaiting-callback", prompt_state="non-interactive"
        )
        generic_first.observe(initial, LivenessPolicy.default())
        generic = generic_first.observe(
            replace(initial, observed_at=1_000), LivenessPolicy.default()
        )
        assert generic.action == "nudge"
        assert not generic_first.reserve_callback_submit("d" * 64)

        submit_first = LivenessController(Path(raw) / "submit-first")
        submit_first.observe(initial, LivenessPolicy.default())
        assert submit_first.reserve_callback_submit("e" * 64)
        generic = submit_first.observe(
            replace(initial, observed_at=1_000), LivenessPolicy.default()
        )
        assert generic.action == "suspected-idle"
        assert submit_first.current_state().nudge_count == 1


contract_primitive_matrix()
resource_and_record_matrix()
remaining_contract_matrix()
serialization_and_hydration_matrix()
state_machine_matrix()
callback_recovery_transition_matrix()
print("contract/state edge matrix passed")
