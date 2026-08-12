"""Durable pipeline verification and finalizing resubmit recovery."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import ContractError as HarnessContractError
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    resolve_custom_executable,
)
from harness.pipeline_builtins import builtin_registry
from harness.store import OperationStore, StoreError
from harness.verification import (
    VerificationAuthority,
    VerificationAuthorityError,
    VerificationError,
    compose_commands,
    load_profiles,
)
from task_review_shared import TaskReviewError, _read_json
from task_review_verification_recovery import _finalizing_resubmit_recovery
from task_review_verification_resubmit import _durable_verification_resubmit


def _durable_successful_verification(
    meta: Mapping[str, Any],
    vault: Path,
    store: OperationStore,
    task_id: str,
    current_head: str,
) -> tuple[str, str] | None:
    """Prove one complete schema-v2 verification at the exact current HEAD."""

    owner_runtime = (
        vault
        / ".vault-meta"
        / "harness"
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
    linked = _read_json(linked_path, "successful verification receipt")
    operation_id = str(linked.get("operation_id") or "")
    receipt_path = (
        owner_runtime
        / "pipeline-verification"
        / operation_id
        / "receipt.json"
    ).resolve()
    policy = meta.get("pipeline_policy")
    review_policy = meta.get("review_policy")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id)
        or not isinstance(policy, Mapping)
        or not isinstance(review_policy, Mapping)
    ):
        raise TaskReviewError(
            "successful review recovery verification authority is invalid"
        )
    profile_name = str(review_policy.get("verification_profile") or "")
    profile_sha256 = str(
        review_policy.get("verification_profile_sha256") or ""
    )
    configured_profile = load_profiles(
        vault / "config/verification-profiles.toml"
    ).get(profile_name)
    if configured_profile is None or configured_profile.sha256 != profile_sha256:
        raise TaskReviewError(
            "successful review recovery verification profile is invalid"
        )
    extra_commands: tuple[str, ...] = ()
    if policy.get("name") == "custom":
        try:
            baseline, compiled, extra_commands, _custom_spec = (
                resolve_custom_executable(
                    store_root=owner_runtime.parent,
                    operation_id=task_id,
                    definition_sha256=str(
                        policy.get("definition_sha256") or ""
                    ),
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
            != policy.get("definition_sha256")
        ):
            raise TaskReviewError(
                "successful review recovery custom pipeline changed"
            )
    try:
        command_ids = [
            f"{profile_name}-{index + 1}"
            for index in range(
                len(compose_commands(configured_profile, extra_commands))
            )
        ]
        parent = store.read(task_id, task_id)
        authority = VerificationAuthority.load(
            receipt_path,
            store=store,
            parent=parent,
            runtime_root=owner_runtime,
            expected_definition_sha256=str(
                policy.get("definition_sha256") or ""
            ),
            expected_profile=profile_name,
            expected_profile_sha256=profile_sha256,
            expected_head_sha=current_head,
            allowed_statuses=("complete",),
            expected_command_ids=command_ids,
            child_states=("complete",),
            require_released=True,
            require_effect_succeeded=True,
        )
    except (
        StoreError,
        VerificationError,
        VerificationAuthorityError,
    ) as exc:
        raise TaskReviewError(
            "successful review recovery verification authority is invalid"
        ) from exc
    if linked != authority.to_dict() or authority.operation_id != operation_id:
        raise TaskReviewError(
            "successful review recovery verification link changed"
        )
    return authority.operation_id, hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
