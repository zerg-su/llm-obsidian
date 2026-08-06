"""Exact dual-provenance policy for the retained fresh-review boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from task_escalation_records import DecisionRecord


COORDINATOR_PROVENANCE_OPERATION_ID = "75ff063d-d388-46a7-915d-0eed20392da4"
COORDINATOR_PROVENANCE_RECORD_ID = "resolution-c18780860b35bf087f1ab9c5c44d9b67"
COORDINATOR_PROVENANCE_SHA256 = (
    "19b7353968b7b7ee91043a604f10a4f7471b99a6b268a643a2f24cf41285aa4b"
)
SCOPED_VERIFICATION_OPERATION_ID = (
    "ad97826c-0651-4014-a113-72518e6fceea-verify-23e835bbe5984523"
)
SCOPED_VERIFICATION_RECEIPT_SHA256 = (
    "c2037564a8a77f384fe012df4aa009882a124ad1297ab9f218cd90106278638b"
)
FRESH_BOUNDARY_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "dispatch_operation_id",
        "kind",
        "previous_context_sha256",
        "next_context_sha256",
        "reason",
        "authorization_provenance",
        "verification_operation_id",
        "verification_receipt_sha256",
        "status",
    }
)
ZERO_EFFECT_FIELDS = (
    "os_signals_sent",
    "cmux_signals_sent",
    "callback_effects_replayed",
    "provider_effects_replayed",
)
_INITIAL_FRESH_BOUNDARY_PROVENANCE_PREFIX = (
    "Classified as an eligible repository-owned fresh-boundary authorization "
    "provenance compatibility failure."
)
_FINAL_EXACT_TAIL_PREFIX = (
    "Classified as coordinator-authored exact commit-identity drift."
)
_FINAL_SELECTOR_PREFIX = (
    "Classified as an eligible repository-owned exact authorization-selector/"
    "latest-resolution routing mechanism failure."
)
FRESH_BOUNDARY_PROVENANCE_PREFIX = (
    _INITIAL_FRESH_BOUNDARY_PROVENANCE_PREFIX,
    _FINAL_EXACT_TAIL_PREFIX,
    _FINAL_SELECTOR_PREFIX,
)
_FRESH_BOUNDARY_PROVENANCE_DECISION = (
    f"{_INITIAL_FRESH_BOUNDARY_PROVENANCE_PREFIX} Authorize one narrow "
    "regression-backed "
    "fail-closed repair: preserve the immutable fresh-boundary artifact byte-"
    "for-byte; validate its exact coordinator escalation provenance "
    "verification_operation_id 75ff063d-d388-46a7-915d-0eed20392da4 and "
    "verification_receipt_sha256 "
    "19b7353968b7b7ee91043a604f10a4f7471b99a6b268a643a2f24cf41285aa4b; "
    "separately validate the exact bound scoped-verification operation "
    "ad97826c-0651-4014-a113-72518e6fceea-verify-23e835bbe5984523 and receipt "
    "digest c2037564a8a77f384fe012df4aa009882a124ad1297ab9f218cd90106278638b "
    "through the existing continuation/dispatch/gate identity chain; and accept "
    "only when both provenance layers, record/file digests, operation, dispatch, "
    "context, kind, reason, unchanged gate/progress state, clean descendant "
    "ancestry, and zero replay/signal counters are exact and unambiguous. Reject "
    "any missing, rewritten, mismatched, duplicated, unrelated, or broadened "
    "provenance. Add focused positive/negative and idempotency regressions, run "
    "clean focused/full/coverage/quality plus a fresh exact-HEAD release-final "
    "gate, then authorize exactly one replacement supported reconcile because "
    "the consumed attempt failed before post-fresh-publication-sync receipt or "
    "any lifecycle/provider/callback/reviewer effect. Preserve all historical "
    "evidence and prior prohibitions: no reviewer/provider relaunch, callback/"
    "effect replay, signals, cmux or manual store/gate edits, push, publish, tag, "
    "release, or reap. Escalate on any further identity, ownership, or lifecycle "
    "drift."
)
_CORRECTED_PROVENANCE_IDENTITY_DECISION = (
    "Classified as coordinator-authored exact-identity contract drift. The "
    "intended coordinator provenance verification_receipt_sha256 is the actual "
    "immutable 64-character record/artifact-bound value "
    "19b7353968b7b7ee91043a604f10a4f7471b99a6b268a643a2f24cf41285aa4b; "
    "the prior 63-character value ending aa4 is superseded only at that literal "
    "and must never be accepted. Explicitly adopt clean repair commit "
    "8addce2c2e06ed54d4353d8908938e32df460933 and its focused fail-closed "
    "regressions. Preserve the still-unused authorization for exactly one "
    "replacement supported reconcile after one clean focused/full/coverage/"
    "quality and exact-HEAD release-final gate. Apply the user's minimum-release "
    "boundary: make no further architectural refactor, completeness-certificate "
    "work, optional enhancement, reviewer/provider relaunch, callback/effect "
    "replay, signal, cmux or manual store/gate edit, push, publish, tag, release, "
    "or reap. If the corrected exact immutable identity chain does not compile "
    "or any new lifecycle/identity drift appears, fail closed and escalate once."
)
_FINAL_EXACT_TAIL_DECISION = (
    f"{_FINAL_EXACT_TAIL_PREFIX} The intended and adopted clean existing Git "
    "commit is exactly 8addce2df1bd3d7cae3b6f586b7fb43d9bb13733; the "
    "nonexistent 8addce2c2e06ed54d4353d8908938e32df460933 value is superseded "
    "and must be rejected. Preserve the already-authorized final minimal literal "
    "exact-tail extension, now through raise "
    "4ad73b06-7526-439d-82f3-a014ce1e178b with sha256 "
    "1cd799cdb36c15eafd187de79f933392c8094a60869643000d54e1f8749718a1 and "
    "this exact resolution record, all predecessor identities, the corrected "
    "64-character coordinator provenance digest "
    "19b7353968b7b7ee91043a604f10a4f7471b99a6b268a643a2f24cf41285aa4b, "
    "and the still-unused authorization for exactly one replacement supported "
    "reconcile. Permit only the focused exact-tail regression and minimal "
    "existing-validator change required to compile this immutable correction; "
    "then one clean focused/full/coverage/quality and exact-HEAD release-final "
    "gate followed by that single reconcile. Preserve the user's minimum-release "
    "boundary and every prior prohibition: no completeness certificate, optional "
    "refactor, generic decision language, new abstraction/public interface/"
    "dependency/migration, reviewer/provider relaunch, replay, signal, cmux or "
    "manual store/gate edit, push, publish, tag, release, or reap. Escalate once "
    "and stop on any further identity or lifecycle drift."
)
_DEFAULT_BINDING_DECISION = (
    "Classified as an eligible repository-owned supported-facade default "
    "store/owner binding mechanism failure. The zero-effect invocation against "
    "store=.vault-meta/harness and owner=local did not address or mutate the "
    "canonical task boundary, created no sync receipt or lifecycle effect, and "
    "therefore did not consume the one replacement supported reconcile grant. "
    "Authorize exactly one correctly bound supported reconcile using store "
    "/private/tmp/llm-obsidian-265-simulator-coordinator.cVqYPn/.vault-meta/"
    "harness and owner ad97826c-0651-4014-a113-72518e6fceea. Preserve the exact "
    "compiled authorization resolution-5834db241204d59c5e2c5c5610a1ea65, all "
    "immutable evidence, zero callback/provider replay, zero manual gate/store "
    "edits, zero unrelated OS/cmux signals, and all minimum-release "
    "prohibitions. Do not repeat the correctly bound command regardless of "
    "result; inspect the resulting lifecycle boundary read-only and escalate "
    "once on any further identity or routing drift."
)
_FINAL_SELECTOR_DECISION = (
    f"{_FINAL_SELECTOR_PREFIX} Authorize only the minimal regression-backed "
    "fail-closed compatibility needed for the supported facade to validate one "
    "immutable exact chain rooted in compiled resolution-"
    "5834db241204d59c5e2c5c5610a1ea65, continuing through zero-effect default-"
    "binding resolution-12fdf06b8e93bb56b2dc4749a589597a, and ending at this "
    "exact coordinator decision record. The compatibility must reject missing, "
    "reordered, mutated, ambiguous, or unrelated records; it must not broaden "
    "the public DSL, lifecycle state machine, permissions, or generic decision "
    "grammar. Preserve the still-unused single correctly bound reconcile using "
    "store /private/tmp/llm-obsidian-265-simulator-coordinator.cVqYPn/.vault-"
    "meta/harness and owner ad97826c-0651-4014-a113-72518e6fceea, all immutable "
    "evidence, zero callback/provider replay, zero manual gate/store edits, zero "
    "unrelated OS/cmux signals, and all minimum-release prohibitions. After "
    "focused regressions, clean full, coverage, quality, and one fresh exact-HEAD "
    "release-final receipt, invoke that correctly bound reconcile exactly once "
    "and inspect the boundary read-only. On any further identity, selector, "
    "routing, or lifecycle drift, stop this task without another compatibility "
    "patch."
)
_EXACT_PROVENANCE_TAIL = (
    (
        "resolution-b28b1e20822edf26a9c4ffa399abf305",
        "resolution",
        "0f4c34f780989a8921741390b872aa0d5b0b0ecfb2fbcc7ba4f33a8520074ce9",
        "2f0718a4-fe30-4f97-97a6-1c5faa3fccd6",
        "98d4e69168e0a7b4cc3c68b815c74425eb5ce8f544432894e29b5a375383e079",
    ),
    (
        "50949589-9803-4b88-9e2c-e86e89da73a9",
        "raise",
        "a3aeac897b3db2c29f5a8d82bf9a140a689ad7924dffcfc113977388c5e95e26",
        "resolution-b28b1e20822edf26a9c4ffa399abf305",
        "0f4c34f780989a8921741390b872aa0d5b0b0ecfb2fbcc7ba4f33a8520074ce9",
    ),
    (
        "resolution-133515ccf480a8d5266eb125582f7f5b",
        "resolution",
        "85416c36635bb3d4cfac65de0463d83b96c61744b32a825a97c6306dc0744479",
        "50949589-9803-4b88-9e2c-e86e89da73a9",
        "a3aeac897b3db2c29f5a8d82bf9a140a689ad7924dffcfc113977388c5e95e26",
    ),
)
_EXACT_CORRECTION_TAIL = (
    (
        "06a3444c-c03f-4eb7-bee5-381ed69032fb",
        "raise",
        "778c27d45d5a0337aa91c89706fff45797e395da2e0e88319151224a43c17ce7",
        "resolution-133515ccf480a8d5266eb125582f7f5b",
        "85416c36635bb3d4cfac65de0463d83b96c61744b32a825a97c6306dc0744479",
        "contract-drift",
    ),
    (
        "resolution-10939a9719a80eb4eb5dd91059b097cc",
        "resolution",
        "3055116cbd7f829529e4f3f105edd70f3baed9c290eb67c919580431b3ce5b37",
        "06a3444c-c03f-4eb7-bee5-381ed69032fb",
        "778c27d45d5a0337aa91c89706fff45797e395da2e0e88319151224a43c17ce7",
        "contract-drift",
    ),
    (
        "3d775da5-2d50-4754-aba9-2cddb2d1715f",
        "raise",
        "807c81a499239bd27d6330de05fd7a34df685b2b2781a488fc96d9a363447baf",
        "resolution-10939a9719a80eb4eb5dd91059b097cc",
        "3055116cbd7f829529e4f3f105edd70f3baed9c290eb67c919580431b3ce5b37",
        "mechanism-failure",
    ),
    (
        "resolution-1671bc7ae72328b503d14860fd98415d",
        "resolution",
        "5786f93a726c5df73c95273dc14cd2fc7e40b7f28f66e01a9dce00c8c6d6b019",
        "3d775da5-2d50-4754-aba9-2cddb2d1715f",
        "807c81a499239bd27d6330de05fd7a34df685b2b2781a488fc96d9a363447baf",
        "mechanism-failure",
    ),
    (
        "4ad73b06-7526-439d-82f3-a014ce1e178b",
        "raise",
        "1cd799cdb36c15eafd187de79f933392c8094a60869643000d54e1f8749718a1",
        "resolution-1671bc7ae72328b503d14860fd98415d",
        "5786f93a726c5df73c95273dc14cd2fc7e40b7f28f66e01a9dce00c8c6d6b019",
        "contract-drift",
    ),
    (
        "resolution-5834db241204d59c5e2c5c5610a1ea65",
        "resolution",
        "16aa04f969d4d0e0ce5a335b26d4b953ea0d235d542d14ce25c9b5a93dbebfdc",
        "4ad73b06-7526-439d-82f3-a014ce1e178b",
        "1cd799cdb36c15eafd187de79f933392c8094a60869643000d54e1f8749718a1",
        "contract-drift",
    ),
)
_EXACT_SELECTOR_TAIL = (
    (
        "0819b5ff-abf4-4fba-aa0a-393cafd3cc93",
        "raise",
        "6ec6c5a6d39df65118773ced05ec2f2fcbea9d6aae17b53e76b1bbf82762d69a",
        "resolution-5834db241204d59c5e2c5c5610a1ea65",
        "16aa04f969d4d0e0ce5a335b26d4b953ea0d235d542d14ce25c9b5a93dbebfdc",
        "mechanism-failure",
    ),
    (
        "resolution-12fdf06b8e93bb56b2dc4749a589597a",
        "resolution",
        "ae35956ec23ac4ff43752d6e01be78a104559a70c945e99c0ae167313d0fd09e",
        "0819b5ff-abf4-4fba-aa0a-393cafd3cc93",
        "6ec6c5a6d39df65118773ced05ec2f2fcbea9d6aae17b53e76b1bbf82762d69a",
        "mechanism-failure",
    ),
    (
        "ce042952-220d-4a50-a35e-c38fb2038546",
        "raise",
        "fda8e41e5e4d9bc38c90e4f65d1354b246f5c45388cd7eb690b7650d650162d8",
        "resolution-12fdf06b8e93bb56b2dc4749a589597a",
        "ae35956ec23ac4ff43752d6e01be78a104559a70c945e99c0ae167313d0fd09e",
        "mechanism-failure",
    ),
    (
        "resolution-77faf7709dfaa1bd2d11ac3d60d324ee",
        "resolution",
        "8796db7b834844e5d19452e018472b2ff20ff7f7cd79c6aa3f43489e8be7c72e",
        "ce042952-220d-4a50-a35e-c38fb2038546",
        "fda8e41e5e4d9bc38c90e4f65d1354b246f5c45388cd7eb690b7650d650162d8",
        "mechanism-failure",
    ),
)


def coordinator_provenance_is_exact(
    authorization: Mapping[str, object], continuation: Mapping[str, object]
) -> bool:
    """Bind immutable coordinator provenance to its scoped verification."""

    return bool(
        set(authorization) == FRESH_BOUNDARY_AUTHORIZATION_FIELDS
        and authorization.get("authorization_provenance")
        == "coordinator-approved"
        and authorization.get("verification_operation_id")
        == COORDINATOR_PROVENANCE_OPERATION_ID
        and authorization.get("verification_receipt_sha256")
        == COORDINATOR_PROVENANCE_SHA256
        and continuation.get("authorization_record_id")
        == COORDINATOR_PROVENANCE_RECORD_ID
        and continuation.get("authorization_record_sha256")
        == COORDINATOR_PROVENANCE_SHA256
        and continuation.get("source_verification_operation_id")
        == SCOPED_VERIFICATION_OPERATION_ID
        and continuation.get("source_verification_receipt_sha256")
        == SCOPED_VERIFICATION_RECEIPT_SHA256
        and all(continuation.get(field) == 0 for field in ZERO_EFFECT_FIELDS)
    )


def _selector_tail_is_exact(
    chain: list[DecisionRecord], index: int, root_index: int, worktree: Path
) -> bool:
    if root_index < 0 or not provenance_tail_is_exact(chain, root_index, worktree):
        return False
    selector = chain[root_index + 1 : index + 1]
    expected_ids = tuple(row[0] for row in _EXACT_SELECTOR_TAIL)
    if tuple(record.record_id for record in selector) != expected_ids or any(
        sum(record.record_id == record_id for record in chain) != 1
        for record_id in expected_ids
    ):
        return False
    for record, expected in zip(selector, _EXACT_SELECTOR_TAIL, strict=True):
        if (
            (
                record.record_id,
                record.record_type,
                record.sha256,
                record.previous_record_id,
                record.previous_record_sha256,
                record.payload.get("category"),
            )
            != expected
        ):
            return False
    scope = {
        key: chain[root_index].payload.get(key)
        for key in ("worktree", "task_name", "task_surface")
    }
    return bool(
        selector[1].payload.get("decision") == _DEFAULT_BINDING_DECISION
        and selector[-1].payload.get("status") == "resolved"
        and selector[-1].payload.get("decision") == _FINAL_SELECTOR_DECISION
        and str(selector[-1].payload.get("worktree") or "") == str(worktree)
        and all(
            {
                key: record.payload.get(key)
                for key in ("worktree", "task_name", "task_surface")
            }
            == scope
            for record in selector
        )
        and [
            row_index
            for row_index, record in enumerate(chain)
            if record.record_type == "resolution"
            and str(record.payload.get("decision") or "").startswith(
                _FINAL_SELECTOR_PREFIX
            )
        ]
        == [index]
    )


def provenance_tail_is_exact(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> bool:
    """Accept only the literal replacement-reconcile correction tails."""

    if index < 2:
        return False
    if chain[index].record_id == _EXACT_SELECTOR_TAIL[-1][0]:
        return _selector_tail_is_exact(
            chain, index, index - len(_EXACT_SELECTOR_TAIL), worktree
        )
    if chain[index].record_id == _EXACT_CORRECTION_TAIL[-1][0]:
        initial_index = index - len(_EXACT_CORRECTION_TAIL)
        if initial_index < 2:
            return False
        initial_tail = chain[initial_index - 2 : initial_index + 1]
        initial_ids = tuple(row[0] for row in _EXACT_PROVENANCE_TAIL)
        if tuple(record.record_id for record in initial_tail) != initial_ids or any(
            sum(record.record_id == record_id for record in chain) != 1
            for record_id in initial_ids
        ):
            return False
        for record, expected in zip(
            initial_tail, _EXACT_PROVENANCE_TAIL, strict=True
        ):
            if (
                (
                    record.record_id,
                    record.record_type,
                    record.sha256,
                    record.previous_record_id,
                    record.previous_record_sha256,
                )
                != expected
            ):
                return False
        correction = chain[initial_index + 1 : index + 1]
        expected_ids = tuple(row[0] for row in _EXACT_CORRECTION_TAIL)
        if tuple(record.record_id for record in correction) != expected_ids or any(
            sum(record.record_id == record_id for record in chain) != 1
            for record_id in expected_ids
        ):
            return False
        for record, expected in zip(
            correction, _EXACT_CORRECTION_TAIL, strict=True
        ):
            if (
                (
                    record.record_id,
                    record.record_type,
                    record.sha256,
                    record.previous_record_id,
                    record.previous_record_sha256,
                    record.payload.get("category"),
                )
                != expected
            ):
                return False
        scope = {
            key: initial_tail[-1].payload.get(key)
            for key in ("worktree", "task_name", "task_surface")
        }
        return bool(
            initial_tail[-1].payload.get("status") == "resolved"
            and initial_tail[-1].payload.get("category") == "mechanism-failure"
            and all(
                {
                    key: record.payload.get(key)
                    for key in ("worktree", "task_name", "task_surface")
                }
                == scope
                for record in initial_tail
            )
            and correction[1].payload.get("decision")
            == _CORRECTED_PROVENANCE_IDENTITY_DECISION
            and correction[-1].payload.get("status") == "resolved"
            and correction[-1].payload.get("decision")
            == _FINAL_EXACT_TAIL_DECISION
            and str(correction[-1].payload.get("worktree") or "")
            == str(worktree)
            and all(
                {
                    key: record.payload.get(key)
                    for key in ("worktree", "task_name", "task_surface")
                }
                == scope
                for record in correction
            )
            and [
                row_index
                for row_index, record in enumerate(chain)
                if record.record_type == "resolution"
                and str(record.payload.get("decision") or "").startswith(
                    _FINAL_EXACT_TAIL_PREFIX
                )
            ]
            == [index]
        )
    tail = chain[index - 2 : index + 1]
    expected_ids = tuple(row[0] for row in _EXACT_PROVENANCE_TAIL)
    if tuple(record.record_id for record in tail) != expected_ids or any(
        sum(record.record_id == record_id for record in chain) != 1
        for record_id in expected_ids
    ):
        return False
    for record, expected in zip(tail, _EXACT_PROVENANCE_TAIL, strict=True):
        if (
            (
                record.record_id,
                record.record_type,
                record.sha256,
                record.previous_record_id,
                record.previous_record_sha256,
            )
            != expected
        ):
            return False
    latest = tail[-1]
    scope = {
        key: latest.payload.get(key)
        for key in ("category", "worktree", "task_name", "task_surface")
    }
    return bool(
        latest.payload.get("status") == "resolved"
        and latest.payload.get("category") == "mechanism-failure"
        and str(latest.payload.get("worktree") or "") == str(worktree)
        and str(latest.payload.get("decision") or "")
        == _FRESH_BOUNDARY_PROVENANCE_DECISION
        and all(
            {
                key: record.payload.get(key)
                for key in ("category", "worktree", "task_name", "task_surface")
            }
            == scope
            for record in tail
        )
        and [
            row_index
            for row_index, record in enumerate(chain)
            if record.record_type == "resolution"
            and str(record.payload.get("decision") or "").startswith(
                _INITIAL_FRESH_BOUNDARY_PROVENANCE_PREFIX
            )
        ]
        == [index]
    )
