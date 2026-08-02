#!/usr/bin/env python3
"""Fast exhaustive transition matrix for the 2.6 release contracts."""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker, CallbackError
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)
from harness.pipeline_builtins import compiled_builtin
from harness.pipelines import reconcile_pipeline
from harness.runtime_worker import _review_resolution_handoff_ready
from harness.state_machine import TRANSITIONS, begin_effect, resolve_effect, transition
from review_resolution import review_transport_identity_sha256
from wiki_summary_contract import WikiSummaryError, validate_summary


def transition_oracle() -> dict[str, frozenset[str]]:
    raw = json.loads(
        (Path(__file__).with_name("state_transition_oracle.json")).read_text(
            encoding="utf-8"
        )
    )
    return {source: frozenset(targets) for source, targets in raw.items()}


EXPECTED_TRANSITIONS = transition_oracle()


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def operation(state: str) -> OperationRecord:
    return OperationRecord(
        OperationSpec(
            "matrix-operation",
            "matrix-key",
            "dispatch",
            "matrix-owner",
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "executor",
                "a" * 64,
            ),
            "packets/matrix.json",
            "scoped",
        ),
        state,
        0,
        "matrix-lane",
        "matrix-run",
    )


def require_transition_contract(actual: dict[str, set[str]]) -> None:
    normalized = {source: frozenset(targets) for source, targets in actual.items()}
    if normalized != EXPECTED_TRANSITIONS:
        raise AssertionError("production transition table drifted from test oracle")


require_transition_contract(TRANSITIONS)
for label, mutation in (
    (
        "added edge",
        {**TRANSITIONS, "created": {*TRANSITIONS["created"], "complete"}},
    ),
    (
        "removed edge",
        {**TRANSITIONS, "created": TRANSITIONS["created"] - {"preflight"}},
    ),
):
    try:
        require_transition_contract(mutation)
    except AssertionError:
        pass
    else:
        raise AssertionError(f"transition oracle missed {label}")
check("operation transition oracle is independent and mutation-sensitive", True)

states = tuple(sorted(EXPECTED_TRANSITIONS))
state_cases = 0
for source, target in itertools.product(states, repeat=2):
    record = operation(source)
    allowed = target == source or target in EXPECTED_TRANSITIONS[source]
    reason = (
        AttentionReason.ATTENTION_REQUIRED
        if target == "attention-required" and target != source
        else None
    )
    try:
        updated, result = transition(record, target, reason=reason)
    except ContractError:
        if allowed:
            raise AssertionError(f"allowed transition rejected: {source} -> {target}")
    else:
        if not allowed:
            raise AssertionError(f"illegal transition accepted: {source} -> {target}")
        if target == source:
            assert not result.changed and updated is record
        else:
            assert result.changed and updated.state == target
            assert updated.revision == record.revision + 1
            if target == "attention-required":
                assert updated.resume_state == source
                assert updated.attention_reason == reason
            else:
                assert not updated.resume_state
                assert updated.attention_reason is None
    state_cases += 1
check(
    "operation state matrix covers every source/target pair",
    state_cases == len(states) ** 2,
)

for target, reason in (
    ("attention-required", None),
    ("preflight", AttentionReason.ATTENTION_REQUIRED),
):
    try:
        transition(operation("created"), target, reason=reason)
    except ContractError:
        pass
    else:
        raise AssertionError(f"invalid transition reason accepted: {target}, {reason}")

effect_record = begin_effect(operation("created"), "matrix-effect")
assert begin_effect(effect_record, "matrix-effect") is effect_record
for invalid_effect in ("", "different-effect"):
    try:
        begin_effect(effect_record if invalid_effect else operation("created"), invalid_effect)
    except ContractError:
        pass
    else:
        raise AssertionError(f"invalid effect start accepted: {invalid_effect!r}")
try:
    transition(effect_record, "preflight")
except ContractError:
    pass
else:
    raise AssertionError("state advance accepted with unresolved effect")
try:
    resolve_effect(effect_record, EffectOutcome.PENDING)
except ContractError:
    pass
else:
    raise AssertionError("pending effect accepted as a resolution")
resolved_effect = resolve_effect(effect_record, EffectOutcome.SUCCEEDED)
assert resolve_effect(resolved_effect, EffectOutcome.SUCCEEDED) is resolved_effect
assert begin_effect(resolved_effect, "matrix-effect") is resolved_effect
try:
    resolve_effect(resolved_effect, EffectOutcome.FAILED)
except ContractError:
    pass
else:
    raise AssertionError("different replayed effect outcome accepted")
try:
    begin_effect(operation("complete"), "matrix-effect")
except ContractError:
    pass
else:
    raise AssertionError("terminal operation accepted a new effect")
check("effect lifecycle matrix covers idempotent and fail-closed branches", True)

for kind, payload in (
    ("review", {"verdict": "unknown", "findings": []}),
    ("unknown", {}),
):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = CallbackEnvelope(
        f"matrix-{kind}",
        "matrix-operation",
        "matrix-run",
        kind,
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    try:
        CallbackBroker._next_state(envelope)
    except CallbackError:
        pass
    else:
        raise AssertionError(f"unknown callback routing accepted: {kind}, {payload}")
check("callback routing rejects unknown kinds and review verdicts", True)


pipeline_cases = 0
for pipeline_name in (
    "lifecycle/default",
    "engineering/change",
    "engineering/fix",
):
    pipeline = compiled_builtin(pipeline_name)
    step_ids = tuple(step.step_id for step in pipeline.definition.steps)
    for statuses in itertools.product(
        ("pending", "running", "complete", "attention"),
        repeat=len(step_ids),
    ):
        observations = dict(zip(step_ids, statuses, strict=True))
        first_non_complete = next(
            (
                index
                for index, status in enumerate(statuses)
                if status != "complete"
            ),
            len(statuses),
        )
        valid = (
            first_non_complete == len(statuses)
            or all(
                status == "pending"
                for status in statuses[first_non_complete + 1 :]
            )
        )
        try:
            progress = reconcile_pipeline(pipeline, observations)
        except ContractError:
            if valid:
                raise AssertionError(
                    f"valid {pipeline_name} observations rejected: {statuses}"
                )
        else:
            if not valid:
                raise AssertionError(
                    f"out-of-order {pipeline_name} observations accepted: {statuses}"
                )
            expected_completed = step_ids[:first_non_complete]
            if first_non_complete == len(statuses):
                assert progress.action == "reap-ready"
                assert progress.step_id == ""
            else:
                assert progress.action == {
                    "pending": "start",
                    "running": "wait",
                    "attention": "attention",
                }[statuses[first_non_complete]]
                assert progress.step_id == step_ids[first_non_complete]
            assert progress.completed_steps == expected_completed
        pipeline_cases += 1
check(
    "all built-in pipeline observation combinations reconcile deterministically",
    pipeline_cases == 4**2 + 4**3 + 4**6,
)


def review_boundary(
    axes: tuple[str, ...], material_by_axis: dict[str, list[str]]
) -> tuple[dict[str, object], list[dict[str, str]]]:
    callbacks = [
        {
            "axis": axis,
            "round_operation_id": f"round-{index}",
            "round_run_id": f"run-{index}",
            "callback_id": f"callback-{index}",
            "callback_sha256": f"{index + 1:x}" * 64,
        }
        for index, axis in enumerate(axes)
    ]
    gate = {
        "active_review_operation_id": "review-operation",
        "awaiting_resolution": {
            callback["axis"]: {
                "reviewed_head_sha": "b" * 40,
                "material_finding_ids": material_by_axis[callback["axis"]],
                "review_operation_id": "review-operation",
                **{
                    key: value
                    for key, value in callback.items()
                    if key != "axis"
                },
            }
            for callback in callbacks
        },
    }
    return gate, callbacks


review_cases = (
    (("holistic",), {"holistic": []}, False),
    (("holistic",), {"holistic": ["H-1"]}, True),
    (
        ("spec", "standards-correctness-architecture-security"),
        {"spec": [], "standards-correctness-architecture-security": []},
        False,
    ),
    (
        ("spec", "standards-correctness-architecture-security"),
        {"spec": ["S-1"], "standards-correctness-architecture-security": []},
        True,
    ),
    (
        ("spec", "standards-correctness-architecture-security"),
        {"spec": [], "standards-correctness-architecture-security": ["C-1"]},
        True,
    ),
    (
        ("spec", "standards-correctness-architecture-security"),
        {"spec": ["S-1"], "standards-correctness-architecture-security": ["C-1"]},
        True,
    ),
    (
        ("spec", "standards-correctness-architecture-security"),
        {"spec": ["DUP-1"], "standards-correctness-architecture-security": ["DUP-1"]},
        False,
    ),
)
with tempfile.TemporaryDirectory(prefix="release-transition-matrix.") as raw:
    worktree = Path(raw)
    for axes, material_by_axis, expected in review_cases:
        gate, callbacks = review_boundary(axes, material_by_axis)
        finding_ids = [
            finding_id
            for axis in sorted(axes)
            for finding_id in material_by_axis[axis]
        ]
        resolution = {
            "schema_version": 1,
            "operation_id": "matrix-operation",
            "review_identity_sha256": review_transport_identity_sha256(
                "review-operation", callbacks
            ),
            "reviewed_head_sha": "b" * 40,
            "resolved_head_sha": "c" * 40,
            "resolutions": [
                {
                    "finding_id": finding_id,
                    "disposition": "applied",
                    "rationale": "The exact matrix correction is present.",
                    "follow_up": "",
                }
                for finding_id in finding_ids
            ],
        }
        (worktree / ".task-review-resolution.json").write_text(
            json.dumps(resolution, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        actual = _review_resolution_handoff_ready(
            worktree=worktree,
            operation_id="matrix-operation",
            gate_state=gate,
            current_head="c" * 40,
        )
        assert actual is expected, (axes, material_by_axis, actual)
check("review resolution matrix covers simple and every deep mixed verdict", True)


declared = {"evidence-a", "evidence-b"}
summary_cases = 0
for disposition, evidence_count, has_gap in itertools.product(
    ("achieved", "partially-achieved", "not-achieved"),
    range(3),
    (False, True),
):
    evidence = sorted(declared)[:evidence_count]
    payload = {
        "schema_version": 2,
        "type": "repo-touch",
        "title": "Release transition matrix",
        "session": "matrix-session",
        "body": "The exact outcome classification is recorded.",
        "outcome_disposition": disposition,
        "outcome_evidence_ids": evidence,
        "residual_gap_pointers": ["[[Matrix follow-up]]"] if has_gap else [],
    }
    expected = (
        disposition == "achieved"
        and evidence_count == len(declared)
        and not has_gap
    ) or (
        disposition != "achieved" and has_gap
    )
    try:
        validate_summary(
            payload,
            declared_evidence_ids=declared,
            require_schema=True,
        )
    except WikiSummaryError:
        if expected:
            raise AssertionError(
                f"valid summary disposition rejected: {disposition}, "
                f"evidence={evidence_count}, gap={has_gap}"
            )
    else:
        if not expected:
            raise AssertionError(
                f"invalid summary disposition accepted: {disposition}, "
                f"evidence={evidence_count}, gap={has_gap}"
            )
    summary_cases += 1
check("Wiki Summary disposition matrix covers every evidence/gap shape", summary_cases == 18)

print(
    "release transition matrix passed: "
    f"{state_cases + pipeline_cases + len(review_cases) + summary_cases} cases"
)
