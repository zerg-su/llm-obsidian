"""Durable pipeline verification and finalizing resubmit recovery."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import (
    ContractError as HarnessContractError,
    EffectOutcome,
    OwnedResources,
    to_dict,
)
from harness.custom_pipelines import CustomPipelinePolicy, resolve_custom_executable
from harness.pipeline_builtins import builtin_registry
from harness.runtime_worker import _pipeline_verify_identity
from harness.store import OperationStore, StoreError
from harness.verification import VerificationError, compose_commands, load_profiles
from harness.workflows.review import ReviewContext, review_round_envelope
from harness.workflows.review_gate import ReviewGateController, ReviewGateRun
from review_resolution import ResolutionError, validate_resolution_evidence
from task_review_context import _callback_path, _canonical_sha256, _context, _envelope
from task_review_resolution_bundle import _recovery_resolution_bundle
from task_review_shared import (
    FinalizingRecovery,
    TaskReviewError,
    _atomic_json,
    _read_json,
)
from task_review_verification_resubmit import _durable_verification_resubmit
from task_review_verification_recovery import _finalizing_resubmit_recovery




def _successful_verification_receipt(
    meta: Mapping[str, Any],
    vault: Path,
    task_id: str,
    current_head: str,
) -> tuple[Path, Path, dict[str, Any], Mapping[str, Any], object, list[object], str] | None:
    owner_runtime = (
        vault / ".vault-meta" / "harness"
        / "owners"
        / task_id
        / "runtime"
        / task_id
    ).resolve()
    linked_path = owner_runtime / "pipeline-step-verify.json"
    if not linked_path.exists():
        return None
    if not linked_path.is_file() or linked_path.is_symlink():
        raise TaskReviewError(
            "successful review recovery verification link is invalid"
        )
    receipt = _read_json(linked_path, "successful verification receipt")
    operation_id = str(receipt.get("operation_id") or "")
    receipt_path = (
        owner_runtime
        / "pipeline-verification"
        / operation_id
        / "receipt.json"
    ).resolve()
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id
        )
        or not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _read_json(receipt_path, "successful verification receipt")
        != receipt
    ):
        return None
    policy = meta.get("pipeline_policy")
    review_policy = meta.get("review_policy")
    evidence = receipt.get("evidence")
    input_sha256 = str(receipt.get("input_sha256") or "")
    profile = str(receipt.get("profile") or "")
    configured_profile = load_profiles(
        vault / "config/verification-profiles.toml"
    ).get(profile)
    if (
        set(receipt)
        != {
            "schema_version",
            "operation_id",
            "parent_operation_id",
            "lane_id",
            "run_id",
            "definition_sha256",
            "step_id",
            "head_sha",
            "input_sha256",
            "profile",
            "profile_sha256",
            "effect_id",
            "status",
            "evidence",
        }
        or receipt.get("schema_version") != 1
        or receipt.get("parent_operation_id") != task_id
        or not isinstance(policy, Mapping)
        or receipt.get("definition_sha256")
        != policy.get("definition_sha256")
        or receipt.get("step_id") != "verify"
        or receipt.get("head_sha") != current_head
        or not re.fullmatch(r"[0-9a-f]{64}", input_sha256)
        or not isinstance(review_policy, Mapping)
        or configured_profile is None
        or profile != review_policy.get("verification_profile")
        or receipt.get("profile_sha256")
        != review_policy.get("verification_profile_sha256")
        or configured_profile.sha256 != receipt.get("profile_sha256")
        or receipt.get("effect_id")
        != f"pipeline-verify-{input_sha256[:32]}"
        or receipt.get("status") != "complete"
        or not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 100
    ):
        return None
    return (
        owner_runtime,
        receipt_path,
        receipt,
        policy,
        configured_profile,
        evidence,
        input_sha256,
    )


def _validate_successful_verification_operation(
    store: OperationStore,
    task_id: str,
    receipt: Mapping[str, Any],
    input_sha256: str,
) -> None:
    operation_id = str(receipt["operation_id"])
    try:
        parent = store.read(task_id, task_id)
        expected_spec, expected_lane, expected_run = _pipeline_verify_identity(
            parent.spec,
            definition_sha256=str(receipt["definition_sha256"]),
            input_sha256=input_sha256,
            profile=str(receipt["profile"]),
        )
        child = store.read(task_id, operation_id)
    except (StoreError, ValueError) as exc:
        raise TaskReviewError(
            "successful review recovery verification operation is unavailable"
        ) from exc
    if (
        expected_spec.operation_id != operation_id
        or receipt.get("lane_id") != expected_lane
        or receipt.get("run_id") != expected_run
        or child.spec != expected_spec
        or child.lane_id != expected_lane
        or child.run_id != expected_run
        or child.state != "complete"
        or child.resources != OwnedResources()
        or child.pending_effect
        or child.effect_id != receipt.get("effect_id")
        or child.effect_outcome != EffectOutcome.SUCCEEDED
    ):
        raise TaskReviewError(
            "successful review recovery verification operation changed"
        )


def _validate_successful_verification_evidence(
    store: OperationStore,
    task_id: str,
    owner_runtime: Path,
    receipt: Mapping[str, Any],
    policy: Mapping[str, Any],
    configured_profile: object,
    evidence: list[object],
    current_head: str,
) -> None:
    profile = str(receipt["profile"])
    extra_commands: tuple[str, ...] = ()
    if policy.get("name") == "custom":
        try:
            baseline, compiled, extra_commands, _custom_spec = (
                resolve_custom_executable(
                    store_root=owner_runtime.parent,
                    operation_id=task_id,
                    definition_sha256=str(receipt["definition_sha256"]),
                    registry=builtin_registry(),
                    policy=CustomPipelinePolicy.default(),
                    capabilities=("route:resolved",),
                )
            )
        except (HarnessContractError, OSError, ValueError) as exc:
            raise TaskReviewError(
                "successful review recovery custom pipeline is unavailable"
            ) from exc
        if (
            baseline != policy.get("baseline")
            or compiled.definition_sha256
            != receipt.get("definition_sha256")
        ):
            raise TaskReviewError(
                "successful review recovery custom pipeline changed"
            )
    try:
        commands = compose_commands(configured_profile, extra_commands)
    except VerificationError as exc:
        raise TaskReviewError(
            "successful review recovery verification commands are invalid"
        ) from exc
    expected_command_ids = [
        f"{profile}-{index + 1}"
        for index in range(len(commands))
    ]
    if not all(isinstance(row, dict) for row in evidence):
        raise TaskReviewError(
            "successful review recovery verification evidence is invalid"
        )
    if [str(row.get("command_id") or "") for row in evidence] != (
        expected_command_ids
    ):
        raise TaskReviewError(
            "successful review recovery verification command set is incomplete"
        )
    evidence_root = (owner_runtime / "pipeline-verification").resolve()
    for row in evidence:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "command_id",
                "cwd",
                "exit_code",
                "started_at",
                "finished_at",
                "head_sha",
                "profile",
                "profile_sha256",
                "output_pointer",
            }
            or type(row.get("exit_code")) is not int
            or row.get("exit_code") != 0
            or row.get("head_sha") != current_head
            or row.get("profile") != profile
            or row.get("profile_sha256")
            != receipt.get("profile_sha256")
        ):
            raise TaskReviewError(
                "successful review recovery verification evidence is invalid"
            )
        pointer = Path(str(row.get("output_pointer") or ""))
        output = (owner_runtime / pointer).resolve()
        if (
            pointer.is_absolute()
            or evidence_root not in output.parents
            or not output.is_file()
            or output.is_symlink()
        ):
            raise TaskReviewError(
                "successful review recovery verification evidence is unavailable"
            )


def _durable_successful_verification(
    meta: Mapping[str, Any],
    vault: Path,
    store: OperationStore,
    task_id: str,
    current_head: str,
) -> tuple[str, str] | None:
    """Prove the coordinator reran the exact configured profile at HEAD."""

    loaded = _successful_verification_receipt(meta, vault, task_id, current_head)
    if loaded is None:
        return None
    owner_runtime, receipt_path, receipt, policy, profile, evidence, input_sha256 = loaded
    _validate_successful_verification_operation(store, task_id, receipt, input_sha256)
    _validate_successful_verification_evidence(
        store,
        task_id,
        owner_runtime,
        receipt,
        policy,
        profile,
        evidence,
        current_head,
    )
    return str(receipt["operation_id"]), hashlib.sha256(receipt_path.read_bytes()).hexdigest()
