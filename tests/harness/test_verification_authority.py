#!/usr/bin/env python3
"""One schema-v2 authority validates every pipeline verification receipt."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    EffectOutcome,
    OperationSpec,
    RuntimeRoute,
    VerificationEvidence,
    to_dict,
)
from harness.dashboard_receipts import verification_receipt_status  # noqa: E402
from harness.runtime_worker_contracts import RuntimeWorkerError  # noqa: E402
from harness.runtime_worker_verification import (  # noqa: E402
    RuntimeWorkerVerificationMixin,
)
from harness.store import OperationStore  # noqa: E402
from harness.verification import (  # noqa: E402
    VerificationAuthority,
    VerificationAuthorityError,
    load_profiles,
)
from harness.verification_attempt import (  # noqa: E402
    VerificationAttempt,
    VERIFICATION_STEP_SCHEMA_VERSION,
    pipeline_verify_effect_id,
    pipeline_verify_identity,
    verification_input_sha256,
)
from task_review_verification_resubmit import (  # noqa: E402
    _accepted_response_receipt,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="verification-authority.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")
    owner = "verification-parent"
    definition_sha256 = "3" * 64
    failed_head = "f" * 40
    profile = load_profiles(ROOT / "config/verification-profiles.toml")[
        "scoped"
    ]
    input_sha256 = verification_input_sha256(
        definition_sha256,
        failed_head,
        profile.sha256,
        VERIFICATION_STEP_SCHEMA_VERSION,
    )
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "executor", "7" * 64
    )
    parent = store.create(
        OperationSpec(
            owner,
            "verification-parent-key",
            "dispatch",
            owner,
            route,
            "packets/task.json",
            profile.name,
            contract_sha256=definition_sha256,
        ),
        lane_id="parent-lane",
        run_id="parent-run",
    )
    attempt = VerificationAttempt(
        owner, profile.name, profile.sha256, failed_head, 0
    )
    child_spec, lane_id, run_id = pipeline_verify_identity(
        parent.spec,
        definition_sha256=definition_sha256,
        input_sha256=input_sha256,
        profile=profile.name,
        attempt_index=attempt.attempt_index,
    )
    store.create(child_spec, lane_id=lane_id, run_id=run_id)
    for state in ("preflight", "starting", "running", "verifying"):
        store.transition(owner, child_spec.operation_id, state)
    effect_id = pipeline_verify_effect_id(input_sha256, 0)
    store.begin_effect(owner, child_spec.operation_id, effect_id)
    store.resolve_effect(
        owner, child_spec.operation_id, EffectOutcome.SUCCEEDED
    )
    store.transition(owner, child_spec.operation_id, "failed")
    runtime_root = store.root / "owners" / owner / "runtime" / owner
    receipt_path = (
        runtime_root
        / "pipeline-verification"
        / child_spec.operation_id
        / "receipt.json"
    )
    output_path = receipt_path.parent / "evidence" / "scoped-1.log"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"failed\n")
    evidence = VerificationEvidence(
        profile.name,
        profile.sha256,
        failed_head,
        "scoped-1",
        ".",
        1,
        "1",
        "2",
        output_path.relative_to(runtime_root).as_posix(),
        hashlib.sha256(output_path.read_bytes()).hexdigest(),
        len(output_path.read_bytes()),
        2,
    )
    receipt = {
        "schema_version": 2,
        "operation_id": child_spec.operation_id,
        "parent_operation_id": owner,
        "lane_id": lane_id,
        "run_id": run_id,
        "definition_sha256": definition_sha256,
        "step_id": "verify",
        "head_sha": failed_head,
        "input_sha256": input_sha256,
        "profile": profile.name,
        "profile_sha256": profile.sha256,
        "effect_id": effect_id,
        "status": "failed",
        "evidence": [to_dict(evidence)],
        "verification_attempt": attempt.as_dict(),
        "verification_attempt_sha256": attempt.sha256,
    }
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    authority = VerificationAuthority.load(
        receipt_path,
        store=store,
        parent=parent,
        runtime_root=runtime_root,
        expected_definition_sha256=definition_sha256,
        expected_profile=profile.name,
        expected_profile_sha256=profile.sha256,
        expected_head_sha=failed_head,
        allowed_statuses=("failed",),
        child_states=("failed",),
        require_released=True,
        require_effect_succeeded=True,
    )
    issued = VerificationAuthority.issue(
        store=store,
        parent=parent,
        runtime_root=runtime_root,
        definition_sha256=definition_sha256,
        input_sha256=input_sha256,
        profile=profile.name,
        profile_sha256=profile.sha256,
        attempt=attempt,
        evidence=(evidence,),
        expected_command_ids=tuple(
            f"{profile.name}-{index + 1}"
            for index in range(len(profile.commands))
        ),
    )
    check(
        "schema-v2 receipt is one immutable verification authority",
        authority.to_dict() == receipt
        and issued == authority
        and authority.attempt == attempt,
    )

    arbitrary_input = "4" * 64
    arbitrary_spec, arbitrary_lane, arbitrary_run = pipeline_verify_identity(
        parent.spec,
        definition_sha256=definition_sha256,
        input_sha256=arbitrary_input,
        profile=profile.name,
        attempt_index=0,
    )
    store.create(
        arbitrary_spec, lane_id=arbitrary_lane, run_id=arbitrary_run
    )
    for state in ("preflight", "starting", "running", "verifying"):
        store.transition(owner, arbitrary_spec.operation_id, state)
    arbitrary_effect = pipeline_verify_effect_id(arbitrary_input, 0)
    store.begin_effect(owner, arbitrary_spec.operation_id, arbitrary_effect)
    store.resolve_effect(
        owner, arbitrary_spec.operation_id, EffectOutcome.SUCCEEDED
    )
    store.transition(owner, arbitrary_spec.operation_id, "failed")
    self_consistent_arbitrary = {
        **receipt,
        "operation_id": arbitrary_spec.operation_id,
        "lane_id": arbitrary_lane,
        "run_id": arbitrary_run,
        "input_sha256": arbitrary_input,
        "effect_id": arbitrary_effect,
    }
    try:
        VerificationAuthority.validate(
            self_consistent_arbitrary,
            store=store,
            parent=parent,
            runtime_root=runtime_root,
            allowed_statuses=("failed",),
            child_states=("failed",),
            require_released=True,
            require_effect_succeeded=True,
        )
    except VerificationAuthorityError:
        pass
    else:
        raise AssertionError("arbitrary verification input digest was authoritative")
    check("canonical verification input digest rejects a self-consistent fork", True)

    fake_worker = SimpleNamespace(
        store=store,
        operation=parent,
        spec={"owner_id": owner, "operation_id": owner},
        spec_path=runtime_root / "launch.json",
        pipeline=SimpleNamespace(definition_sha256=definition_sha256),
        profile=profile,
        pipeline_extra_commands=(),
    )
    fake_worker.load_verification_receipt = lambda path: (
        RuntimeWorkerVerificationMixin.load_verification_receipt(
            fake_worker, path
        )
    )
    fake_worker.verification_response_accepted = lambda value: (
        RuntimeWorkerVerificationMixin.verification_response_accepted(
            fake_worker, value
        )
    )
    loaded = RuntimeWorkerVerificationMixin.load_verification_receipt(
        fake_worker, receipt_path
    )
    check(
        "runtime and dashboard use the same current-schema authority",
        loaded == receipt
        and verification_receipt_status(
            store, parent, runtime_root, receipt_path
        )
        == "failed",
    )

    child_before = store.read(owner, child_spec.operation_id)
    legacy = dict(receipt)
    legacy["schema_version"] = 1
    legacy.pop("verification_attempt")
    legacy.pop("verification_attempt_sha256")
    receipt_path.write_text(
        json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        VerificationAuthority.load(
            receipt_path,
            store=store,
            parent=parent,
            runtime_root=runtime_root,
        )
    except VerificationAuthorityError as exc:
        check(
            "schema-v1 fails explicitly at the single authority ingress",
            "schema version 2" in str(exc),
        )
    else:
        raise AssertionError("schema-v1 verification authority was accepted")
    try:
        RuntimeWorkerVerificationMixin.load_verification_receipt(
            fake_worker, receipt_path
        )
    except RuntimeWorkerError:
        pass
    else:
        raise AssertionError("runtime accepted schema-v1 verification")
    check(
        "legacy rejection has zero operation effect",
        store.read(owner, child_spec.operation_id) == child_before
        and verification_receipt_status(
            store, parent, runtime_root, receipt_path
        )
        == "invalid",
    )
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )

    stale_attempt = VerificationAttempt(
        owner, profile.name, profile.sha256, "e" * 40, 0
    )
    mutations = {
        "attempt digest": dict(
            receipt, verification_attempt_sha256="0" * 64
        ),
        "attempt HEAD": dict(
            receipt,
            verification_attempt=stale_attempt.as_dict(),
            verification_attempt_sha256=stale_attempt.sha256,
        ),
        "operation": dict(receipt, operation_id="foreign"),
        "lane": dict(receipt, lane_id="foreign"),
        "run": dict(receipt, run_id="foreign"),
        "effect": dict(receipt, effect_id="foreign"),
        "definition": dict(receipt, definition_sha256="0" * 64),
        "profile digest": dict(receipt, profile_sha256="0" * 64),
        "false exit code": {
            **receipt,
            "evidence": [{**receipt["evidence"][0], "exit_code": False}],
        },
        "true exit code": {
            **receipt,
            "evidence": [{**receipt["evidence"][0], "exit_code": True}],
        },
    }
    for label, mutated in mutations.items():
        try:
            VerificationAuthority.validate(
                mutated,
                store=store,
                parent=parent,
                runtime_root=runtime_root,
                expected_definition_sha256=definition_sha256,
                expected_profile=profile.name,
                expected_profile_sha256=profile.sha256,
                expected_head_sha=failed_head,
            )
        except VerificationAuthorityError:
            pass
        else:
            raise AssertionError(f"{label} authority mutation was accepted")
        check(f"{label} authority mutation fails closed", True)

    for boolean_exit in (False, True):
        mutated = {
            **receipt,
            "evidence": [
                {**receipt["evidence"][0], "exit_code": boolean_exit}
            ],
        }
        receipt_path.write_text(
            json.dumps(mutated, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            RuntimeWorkerVerificationMixin.load_verification_receipt(
                fake_worker, receipt_path
            )
        except RuntimeWorkerError:
            pass
        else:
            raise AssertionError("runtime accepted a boolean verification exit code")
        check(
            f"boolean exit code {boolean_exit!r} is invalid for every consumer",
            verification_receipt_status(
                store, parent, runtime_root, receipt_path
            )
            == "invalid",
        )

    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    changed_response = receipt_path.with_name("response-receipt.json")
    changed_response.write_text(
        json.dumps(
            _accepted_response_receipt(
                owner,
                child_spec.operation_id,
                failed_head,
                "b" * 40,
                "c" * 64,
            ),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    check(
        "the finalizing producer's schema-v2 receipt consumes the resubmit budget",
        RuntimeWorkerVerificationMixin.changed_head_resubmit_count(fake_worker)
        == 1,
    )
    check(
        "a second changed-HEAD resubmit is unavailable after that receipt",
        not RuntimeWorkerVerificationMixin.changed_head_resubmit_available(
            SimpleNamespace(
                changed_head_resubmit_count=lambda: (
                    RuntimeWorkerVerificationMixin.changed_head_resubmit_count(
                        fake_worker
                    )
                )
            )
        ),
    )
    malformed = json.loads(changed_response.read_text(encoding="utf-8"))
    malformed["schema_version"] = 1
    changed_response.write_text(
        json.dumps(malformed, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        RuntimeWorkerVerificationMixin.changed_head_resubmit_count(fake_worker)
    except RuntimeWorkerError:
        pass
    else:
        raise AssertionError("schema-v1 changed-HEAD budget receipt was accepted")
    check("malformed resubmit budget evidence fails closed", True)

    changed_response.unlink()
    foreign_response = (
        runtime_root
        / "pipeline-verification"
        / "foreign-child"
        / "response-receipt.json"
    )
    foreign_response.parent.mkdir(parents=True)
    foreign_response.write_text(
        json.dumps(
            _accepted_response_receipt(
                "foreign-parent",
                "foreign-child",
                failed_head,
                "b" * 40,
                "c" * 64,
            ),
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    try:
        RuntimeWorkerVerificationMixin.changed_head_resubmit_count(fake_worker)
    except RuntimeWorkerError:
        pass
    else:
        raise AssertionError("foreign changed-HEAD receipt was counted")
    check("foreign changed-HEAD budget evidence fails closed", True)

print("verification authority matrix: ok")
