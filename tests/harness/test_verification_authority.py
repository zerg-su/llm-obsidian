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
    import shutil as _shutil

    _shutil.rmtree(foreign_response.parent)

    # Invalidated-handoff identity: the attempt-1 successor of one exact
    # verification input is a distinct predecessor-bound operation, and the
    # identity space ends there.
    from harness.verification_attempt import VerificationAttemptError

    successor_spec, successor_lane, successor_run = pipeline_verify_identity(
        parent.spec,
        definition_sha256=definition_sha256,
        input_sha256=input_sha256,
        profile=profile.name,
        attempt_index=1,
    )
    successor_effect = pipeline_verify_effect_id(input_sha256, 1)
    check(
        "the attempt-1 successor identity is distinct and predecessor-bound",
        successor_spec.operation_id == f"{child_spec.operation_id}-a1"
        and successor_spec.parent_operation_id == parent.spec.operation_id
        and successor_spec.idempotency_key != child_spec.idempotency_key
        and successor_lane != lane_id
        and successor_run != run_id
        and successor_effect != effect_id,
    )
    for factory in (
        lambda: pipeline_verify_identity(
            parent.spec,
            definition_sha256=definition_sha256,
            input_sha256=input_sha256,
            profile=profile.name,
            attempt_index=2,
        ),
        lambda: pipeline_verify_effect_id(input_sha256, 2),
        lambda: attempt.same_head_retry().same_head_retry(),
    ):
        try:
            factory()
        except VerificationAttemptError:
            pass
        else:
            raise AssertionError("an attempt-2 verification identity was minted")
    check("no attempt-2 verification identity exists", True)

    # A successor receipt validates as one immutable authority of its own.
    store.create(
        successor_spec, lane_id=successor_lane, run_id=successor_run
    )
    for state in ("preflight", "starting", "running", "verifying"):
        store.transition(owner, successor_spec.operation_id, state)
    store.begin_effect(owner, successor_spec.operation_id, successor_effect)
    store.resolve_effect(
        owner, successor_spec.operation_id, EffectOutcome.SUCCEEDED
    )
    store.transition(owner, successor_spec.operation_id, "failed")
    successor_attempt = attempt.same_head_retry()
    successor_receipt_path = (
        runtime_root
        / "pipeline-verification"
        / successor_spec.operation_id
        / "receipt.json"
    )
    successor_output = successor_receipt_path.parent / "evidence" / "scoped-1.log"
    successor_output.parent.mkdir(parents=True)
    successor_output.write_bytes(b"failed\n")
    successor_receipt = {
        **receipt,
        "operation_id": successor_spec.operation_id,
        "lane_id": successor_lane,
        "run_id": successor_run,
        "effect_id": successor_effect,
        "verification_attempt": successor_attempt.as_dict(),
        "verification_attempt_sha256": successor_attempt.sha256,
        "evidence": [
            {
                **receipt["evidence"][0],
                "output_pointer": successor_output.relative_to(
                    runtime_root
                ).as_posix(),
            }
        ],
    }
    successor_receipt_path.write_text(
        json.dumps(successor_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    successor_authority = VerificationAuthority.load(
        successor_receipt_path,
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
    check(
        "the attempt-1 successor receipt is one immutable authority",
        successor_authority.to_dict() == successor_receipt
        and successor_authority.attempt == successor_attempt
        and successor_authority.operation_id == successor_spec.operation_id,
    )

    # Stale evidence can never authorize a different HEAD at the ingress.
    try:
        VerificationAuthority.validate(
            receipt,
            store=store,
            parent=parent,
            runtime_root=runtime_root,
            expected_definition_sha256=definition_sha256,
            expected_profile=profile.name,
            expected_profile_sha256=profile.sha256,
            expected_head_sha="b" * 40,
        )
    except VerificationAuthorityError:
        pass
    else:
        raise AssertionError("stale-HEAD evidence authorized a different HEAD")
    check("stale-HEAD receipt cannot authorize the current HEAD", True)

    # Receiptless invalidation classification is identity-exact and
    # symlink-safe: only the derived spec/lane/run with released resources and
    # a settled succeeded own effect qualifies; any drift or a dangling
    # receipt symlink stays with the existing fail-closed owners.
    from harness.runtime_worker_control import RuntimeWorkerControlMixin

    inval_store = OperationStore(base / "store-invalidation")
    inval_owner = "invalidation-parent"
    inval_head = "b" * 40
    inval_input = verification_input_sha256(
        definition_sha256,
        inval_head,
        profile.sha256,
        VERIFICATION_STEP_SCHEMA_VERSION,
    )
    inval_parent = inval_store.create(
        OperationSpec(
            inval_owner,
            "invalidation-parent-key",
            "dispatch",
            inval_owner,
            route,
            "packets/task.json",
            profile.name,
            contract_sha256=definition_sha256,
        ),
        lane_id="parent-lane",
        run_id="parent-run",
    )
    inval_spec, inval_lane, inval_run = pipeline_verify_identity(
        inval_parent.spec,
        definition_sha256=definition_sha256,
        input_sha256=inval_input,
        profile=profile.name,
        attempt_index=0,
    )
    inval_store.create(inval_spec, lane_id=inval_lane, run_id=inval_run)
    for state in ("preflight", "starting", "running", "verifying"):
        inval_store.transition(inval_owner, inval_spec.operation_id, state)
    inval_effect = pipeline_verify_effect_id(inval_input, 0)
    inval_store.begin_effect(inval_owner, inval_spec.operation_id, inval_effect)
    inval_store.resolve_effect(
        inval_owner, inval_spec.operation_id, EffectOutcome.SUCCEEDED
    )
    inval_runtime = (
        inval_store.root / "owners" / inval_owner / "runtime" / inval_owner
    )
    inval_receipt_path = (
        inval_runtime
        / "pipeline-verification"
        / inval_spec.operation_id
        / "receipt.json"
    )
    inval_attempt = VerificationAttempt(
        inval_owner, profile.name, profile.sha256, inval_head, 0
    )
    inval_successor_spec, _s_lane, _s_run = pipeline_verify_identity(
        inval_parent.spec,
        definition_sha256=definition_sha256,
        input_sha256=inval_input,
        profile=profile.name,
        attempt_index=1,
    )

    def make_inval_worker() -> SimpleNamespace:
        worker = SimpleNamespace(
            store=inval_store,
            spec={"owner_id": inval_owner, "operation_id": inval_owner},
            spec_path=inval_runtime / "launch.json",
            verification_spec=inval_spec,
            verification_lane_id=inval_lane,
            verification_run_id=inval_run,
            verification_effect_id=inval_effect,
            verification_attempt=inval_attempt,
            verification_head=inval_head,
            verification_receipt_path=inval_receipt_path,
            attention_calls=[],
            bind_calls=[],
        )
        worker.product_tree_is_clean = lambda: True
        worker.summary_attention = (
            lambda *args, **kwargs: worker.attention_calls.append(args)
        )

        def bind(index: int) -> None:
            worker.bind_calls.append(index)
            worker.verification_spec = inval_successor_spec
            worker.verification_effect_id = pipeline_verify_effect_id(
                inval_input, index
            )
            worker.verification_attempt = inval_attempt.same_head_retry()

        worker._bind_verification_attempt = bind
        worker.invalidated_verification_attempt = (
            lambda: RuntimeWorkerVerificationMixin.invalidated_verification_attempt(
                worker
            )
        )
        worker.adopt_invalidated_verification_successor = (
            lambda: RuntimeWorkerVerificationMixin.adopt_invalidated_verification_successor(
                worker
            )
        )
        worker.write_immutable_json = (
            lambda path, value: RuntimeWorkerControlMixin.write_immutable_json(
                worker, path, value
            )
        )
        return worker

    inval_record_path = (
        inval_store.root
        / "owners"
        / inval_owner
        / "operations"
        / f"{inval_spec.operation_id}.json"
    )
    pristine_record = inval_record_path.read_text(encoding="utf-8")
    check(
        "a pristine receiptless settled attempt classifies as invalidated",
        make_inval_worker().invalidated_verification_attempt() is not None,
    )

    def mutated_record(**changes: object) -> str:
        value = json.loads(pristine_record)
        for key, item in changes.items():
            container, _, field = key.partition(".")
            if field:
                value[container][field] = item
            else:
                value[key] = item
        return json.dumps(value, sort_keys=True)

    identity_mutations = {
        "lane": mutated_record(lane_id="f" * 32),
        "run": mutated_record(run_id="f" * 32),
        "spec": mutated_record(**{"spec.verification_profile": "full"}),
        "resources": mutated_record(**{"resources.surface_id": "S"}),
    }
    for label, encoded in identity_mutations.items():
        inval_record_path.write_text(encoded + "\n", encoding="utf-8")
        check(
            f"receiptless {label} drift is never classified as invalidated",
            make_inval_worker().invalidated_verification_attempt() is None,
        )
    inval_record_path.write_text(pristine_record, encoding="utf-8")

    inval_receipt_path.parent.mkdir(parents=True)
    inval_receipt_path.symlink_to(
        inval_receipt_path.parent / "missing-receipt.json"
    )
    symlinked_worker = make_inval_worker()
    check(
        "a dangling receipt symlink is tamper evidence, not absence",
        symlinked_worker.invalidated_verification_attempt() is None
        and symlinked_worker.adopt_invalidated_verification_successor() is True
        and symlinked_worker.bind_calls == []
        and symlinked_worker.attention_calls == []
        and not (inval_receipt_path.parent / "invalidation.json").exists()
        and inval_record_path.read_text(encoding="utf-8") == pristine_record,
    )
    inval_receipt_path.unlink()

    (inval_receipt_path.parent / "invalidation.json").write_text(
        '{"schema_version": 1, "status": "forged"}\n', encoding="utf-8"
    )
    try:
        make_inval_worker().adopt_invalidated_verification_successor()
    except RuntimeWorkerError:
        pass
    else:
        raise AssertionError(
            "conflicting invalidation bytes were silently overwritten"
        )
    check("conflicting invalidation record bytes fail closed", True)

    # 2.7.4 F273.MISSING_PREDECESSOR_FAIL_OPEN: when the deterministic
    # attempt-0 predecessor record is absent while ANY attempt-1 successor or
    # invalidation evidence exists, the lineage is orphaned.  The verification
    # owner must latch exactly one typed attention and perform no adoption, no
    # successor binding, no store mutation, and no minting.  Only a truly
    # empty attempt identity space may classify as a fresh run.
    from harness.store import StoreError

    def orphan_world(case: str) -> SimpleNamespace:
        world_store = OperationStore(base / f"store-orphan-{case}")
        world_owner = f"orphan-{case}"
        world_head = "b" * 40
        world_input = verification_input_sha256(
            definition_sha256,
            world_head,
            profile.sha256,
            VERIFICATION_STEP_SCHEMA_VERSION,
        )
        world_parent = world_store.create(
            OperationSpec(
                world_owner,
                f"{world_owner}-key",
                "dispatch",
                world_owner,
                route,
                "packets/task.json",
                profile.name,
                contract_sha256=definition_sha256,
            ),
            lane_id="parent-lane",
            run_id="parent-run",
        )
        spec0, lane0, run0 = pipeline_verify_identity(
            world_parent.spec,
            definition_sha256=definition_sha256,
            input_sha256=world_input,
            profile=profile.name,
            attempt_index=0,
        )
        successor_identity = pipeline_verify_identity(
            world_parent.spec,
            definition_sha256=definition_sha256,
            input_sha256=world_input,
            profile=profile.name,
            attempt_index=1,
        )
        world_runtime = (
            world_store.root / "owners" / world_owner / "runtime" / world_owner
        )
        worker = SimpleNamespace(
            store=world_store,
            operation=world_parent,
            spec={"owner_id": world_owner, "operation_id": world_owner},
            spec_path=world_runtime / "launch.json",
            pipeline=SimpleNamespace(definition_sha256=definition_sha256),
            profile=profile,
            verification_spec=spec0,
            verification_lane_id=lane0,
            verification_run_id=run0,
            verification_effect_id=pipeline_verify_effect_id(world_input, 0),
            verification_input_sha256=world_input,
            verification_attempt=VerificationAttempt(
                world_owner, profile.name, profile.sha256, world_head, 0
            ),
            verification_head=world_head,
            verification_receipt_path=(
                world_runtime
                / "pipeline-verification"
                / spec0.operation_id
                / "receipt.json"
            ),
            attention_calls=[],
            bind_calls=[],
        )
        worker.product_tree_is_clean = lambda: True
        worker.summary_attention = (
            lambda *args, **kwargs: worker.attention_calls.append(args)
        )
        worker._bind_verification_attempt = (
            lambda index: worker.bind_calls.append(index)
        )
        worker.invalidated_verification_attempt = (
            lambda: RuntimeWorkerVerificationMixin.invalidated_verification_attempt(
                worker
            )
        )
        worker.adopt_invalidated_verification_successor = (
            lambda: RuntimeWorkerVerificationMixin.adopt_invalidated_verification_successor(
                worker
            )
        )
        worker.write_immutable_json = (
            lambda path, value: RuntimeWorkerControlMixin.write_immutable_json(
                worker, path, value
            )
        )
        return SimpleNamespace(
            store=world_store,
            owner=world_owner,
            head=world_head,
            input=world_input,
            spec0=spec0,
            successor_identity=successor_identity,
            runtime=world_runtime,
            invalidation_path=(
                world_runtime
                / "pipeline-verification"
                / spec0.operation_id
                / "invalidation.json"
            ),
            worker=worker,
        )

    def seed_successor_record(world: SimpleNamespace, target_state: str) -> None:
        successor_spec, successor_lane, successor_run = world.successor_identity
        world.store.create(
            successor_spec, lane_id=successor_lane, run_id=successor_run
        )
        if target_state == "created":
            return
        for step in ("preflight", "starting", "running", "verifying"):
            world.store.transition(world.owner, successor_spec.operation_id, step)
        if target_state == "verifying":
            return
        world.store.begin_effect(
            world.owner,
            successor_spec.operation_id,
            pipeline_verify_effect_id(world.input, 1),
        )
        world.store.resolve_effect(
            world.owner, successor_spec.operation_id, EffectOutcome.SUCCEEDED
        )
        if target_state == "failed":
            world.store.transition(
                world.owner, successor_spec.operation_id, "failed"
            )
            return
        for step in ("finalizing", "exiting", "complete"):
            world.store.transition(world.owner, successor_spec.operation_id, step)

    def seed_successor_receipt(world: SimpleNamespace) -> None:
        seed_successor_record(world, "complete")
        successor_spec = world.successor_identity[0]
        successor_receipt_file = (
            world.runtime
            / "pipeline-verification"
            / successor_spec.operation_id
            / "receipt.json"
        )
        successor_receipt_file.parent.mkdir(parents=True, exist_ok=True)
        successor_receipt_file.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "operation_id": successor_spec.operation_id,
                    "parent_operation_id": world.owner,
                    "head_sha": world.head,
                    "status": "complete",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def valid_invalidation_record(world: SimpleNamespace) -> dict[str, object]:
        successor_spec = world.successor_identity[0]
        return {
            "schema_version": 1,
            "operation_id": world.spec0.operation_id,
            "parent_operation_id": world.owner,
            "predecessor_attempt_sha256": VerificationAttempt(
                world.owner, profile.name, profile.sha256, world.head, 0
            ).sha256,
            "predecessor_effect_id": pipeline_verify_effect_id(world.input, 0),
            "successor_operation_id": successor_spec.operation_id,
            "successor_attempt_sha256": VerificationAttempt(
                world.owner, profile.name, profile.sha256, world.head, 1
            ).sha256,
            "successor_effect_id": pipeline_verify_effect_id(world.input, 1),
            "current_head_sha": world.head,
            "status": "invalidated",
        }

    def write_invalidation(world: SimpleNamespace, value: dict[str, object]) -> None:
        world.invalidation_path.parent.mkdir(parents=True, exist_ok=True)
        world.invalidation_path.write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )

    def seed_pending_successor(world: SimpleNamespace) -> None:
        seed_successor_record(world, "created")

    def seed_verifying_successor(world: SimpleNamespace) -> None:
        seed_successor_record(world, "verifying")

    def seed_failed_successor(world: SimpleNamespace) -> None:
        seed_successor_record(world, "failed")

    def seed_complete_successor(world: SimpleNamespace) -> None:
        seed_successor_record(world, "complete")

    def seed_valid_invalidation(world: SimpleNamespace) -> None:
        write_invalidation(world, valid_invalidation_record(world))

    def seed_malformed_invalidation(world: SimpleNamespace) -> None:
        world.invalidation_path.parent.mkdir(parents=True, exist_ok=True)
        world.invalidation_path.write_text(
            "{malformed invalidation bytes", encoding="utf-8"
        )

    def seed_symlinked_invalidation(world: SimpleNamespace) -> None:
        world.invalidation_path.parent.mkdir(parents=True, exist_ok=True)
        world.invalidation_path.symlink_to(
            world.invalidation_path.parent / "missing-invalidation.json"
        )

    def seed_conflicting_invalidation(world: SimpleNamespace) -> None:
        seed_successor_record(world, "created")
        write_invalidation(
            world,
            {
                **valid_invalidation_record(world),
                "successor_operation_id": "forged-successor",
            },
        )

    def seed_drifted_invalidation(world: SimpleNamespace) -> None:
        seed_successor_record(world, "created")
        write_invalidation(
            world,
            {
                **valid_invalidation_record(world),
                "predecessor_attempt_sha256": "0" * 64,
            },
        )

    orphan_cases = (
        ("pending-successor", seed_pending_successor),
        ("verifying-successor", seed_verifying_successor),
        ("failed-successor", seed_failed_successor),
        ("complete-successor", seed_complete_successor),
        ("receipted-successor", seed_successor_receipt),
        ("valid-invalidation", seed_valid_invalidation),
        ("malformed-invalidation", seed_malformed_invalidation),
        ("symlinked-invalidation", seed_symlinked_invalidation),
        ("conflicting-invalidation", seed_conflicting_invalidation),
        ("drifted-invalidation", seed_drifted_invalidation),
    )
    for orphan_case, seed_orphan_evidence in orphan_cases:
        orphan = orphan_world(orphan_case)
        seed_orphan_evidence(orphan)
        successor_spec = orphan.successor_identity[0]

        def successor_state() -> object:
            try:
                return orphan.store.read(
                    orphan.owner, successor_spec.operation_id
                ).state
            except StoreError:
                return None

        def invalidation_bytes() -> object:
            if orphan.invalidation_path.is_symlink():
                return "symlink"
            if not orphan.invalidation_path.is_file():
                return None
            return orphan.invalidation_path.read_bytes()

        successor_before = successor_state()
        invalidation_before = invalidation_bytes()
        adopted = orphan.worker.adopt_invalidated_verification_successor()
        predecessor_still_absent = False
        try:
            orphan.store.read(orphan.owner, orphan.spec0.operation_id)
        except StoreError:
            predecessor_still_absent = True
        check(
            f"orphaned predecessor ({orphan_case}) latches exactly one typed "
            "attention with no adoption, no binding, and no mutation",
            adopted is False
            and len(orphan.worker.attention_calls) == 1
            and orphan.worker.bind_calls == []
            and predecessor_still_absent
            and successor_state() == successor_before
            and invalidation_bytes() == invalidation_before,
        )

    fresh = orphan_world("fresh-empty")
    check(
        "a truly empty attempt identity space stays the only fresh-run "
        "classification",
        fresh.worker.adopt_invalidated_verification_successor() is True
        and fresh.worker.attention_calls == []
        and fresh.worker.bind_calls == [],
    )

    # 2.7.4 F274.CANDIDATE_PREDICATE_TOCTOU: the current-candidate predicate
    # brackets its tree observation with two exact HEAD observations, so a
    # clean commit landing between any of its reads invalidates the whole
    # observation instead of yielding currency for the predecessor HEAD.
    import subprocess as real_subprocess

    import harness.runtime_worker_verification as rwv

    def toctou_repo(name: str) -> tuple[Path, str]:
        repo = base / f"toctou-{name}"
        repo.mkdir()
        for argv in (
            ("init", "-b", "main"),
            ("config", "user.email", "toctou@example.invalid"),
            ("config", "user.name", "Toctou World"),
        ):
            real_subprocess.run(
                ["git", "-C", str(repo), *argv], check=True, capture_output=True
            )
        (repo / "product.txt").write_text("base\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(repo), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        head = real_subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return repo, head

    def land_toctou_commit(repo: Path) -> None:
        (repo / "product.txt").write_text("moved\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(repo), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "moved"],
            check=True,
            capture_output=True,
        )

    toctou_windows = (
        ("between the HEAD and tree observations", ["git", "rev-parse", "HEAD"]),
        ("between the tree and closing HEAD observations",
         ["git", "status", "--porcelain"]),
    )
    for window_name, trigger_argv in toctou_windows:
        repo, head_b = toctou_repo(window_name.replace(" ", "-")[:24])
        armed = {"armed": True, "reads": 0}

        class CommitBetweenObservations:
            CompletedProcess = real_subprocess.CompletedProcess

            @staticmethod
            def run(argv: list[str], **kwargs: object):
                result = real_subprocess.run(argv, **kwargs)
                armed["reads"] += 1
                if list(argv) == trigger_argv and armed["armed"]:
                    armed["armed"] = False
                    land_toctou_commit(repo)
                return result

        original_subprocess = rwv.subprocess
        rwv.subprocess = CommitBetweenObservations
        try:
            raced = rwv._verification_candidate_is_current(repo, head_b)
        finally:
            rwv.subprocess = original_subprocess
        check(
            f"a clean commit {window_name} never yields predecessor currency",
            raced is False and armed["armed"] is False,
        )

    control_repo, control_head = toctou_repo("control")
    check(
        "an undisturbed exact clean candidate observation stays current",
        rwv._verification_candidate_is_current(control_repo, control_head)
        is True,
    )
    (control_repo / "junk.txt").write_text("dirt\n", encoding="utf-8")
    check(
        "untracked dirt refuses candidate currency",
        rwv._verification_candidate_is_current(control_repo, control_head)
        is False,
    )
    check(
        "an empty expected HEAD is never currency",
        rwv._verification_candidate_is_current(control_repo, "") is False,
    )

    # 2.7.4 F274.RESOLUTION_DRIFT_BYPASS: the resolution notification is
    # wait-only and identity-exact.  Anything short of the exact sent,
    # own-operation, packet-bound marker for exactly the stale HEAD is not a
    # resolution in flight; and even the exact marker never appears at a
    # linking/consumption call site as authorization.
    marker_runtime = base / "resolution-marker-runtime"
    marker_runtime.mkdir()
    marker_worker = SimpleNamespace(
        spec_path=marker_runtime / "launch.json",
        spec={"operation_id": "marker-op", "owner_id": "marker-op"},
    )
    marker_path = marker_runtime / "pipeline-review-resolution-notify.json"
    stale_head = "b" * 40
    exact_marker = {
        "schema_version": 1,
        "operation_id": "marker-op",
        "packet_sha256": "c" * 64,
        "reviewed_head_sha": stale_head,
        "summary_sha256": "d" * 64,
        "status": "sent",
    }

    def write_marker(value: object) -> None:
        if marker_path.is_symlink() or marker_path.exists():
            marker_path.unlink()
        if isinstance(value, str):
            marker_path.write_text(value, encoding="utf-8")
        else:
            marker_path.write_text(
                json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
            )

    check(
        "a missing resolution notification is never a resolution in flight",
        rwv._review_resolution_drift_in_flight(marker_worker, stale_head)
        is False,
    )
    write_marker(exact_marker)
    check(
        "the exact sent own-operation packet-bound marker names its "
        "reviewed HEAD",
        rwv._review_resolution_drift_in_flight(marker_worker, stale_head)
        is True,
    )
    marker_rows = (
        ("foreign operation", {**exact_marker, "operation_id": "foreign-op"}),
        ("pending status", {**exact_marker, "status": "pending"}),
        ("missing packet digest", {**exact_marker, "packet_sha256": ""}),
        ("drifted schema", {**exact_marker, "schema_version": 2}),
        (
            "foreign reviewed HEAD",
            {**exact_marker, "reviewed_head_sha": "e" * 40},
        ),
        ("malformed bytes", "{malformed notification"),
    )
    for marker_case, marker_value in marker_rows:
        write_marker(marker_value)
        check(
            f"a {marker_case} resolution notification is never a resolution "
            "in flight",
            rwv._review_resolution_drift_in_flight(marker_worker, stale_head)
            is False,
        )
    marker_path.unlink()
    marker_path.symlink_to(marker_runtime / "missing-notification.json")
    check(
        "a symlinked resolution notification is never a resolution in flight",
        rwv._review_resolution_drift_in_flight(marker_worker, stale_head)
        is False,
    )
    write_marker(exact_marker)
    check(
        "even the exact marker never restores candidate currency",
        rwv._verification_candidate_is_current(control_repo, control_head)
        is False,
    )

print("verification authority matrix: ok")
