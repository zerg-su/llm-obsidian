"""Custom pipeline snapshots and compiled dispatch policy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from outcome_contract import OutcomeContractError, extract_from_bytes
from harness.contracts import ContractError as HarnessContractError
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenCustomPipeline,
    PipelineSpec,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    pipeline_spec_payload,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry, compiled_builtin
from harness.pipelines import CompiledPipeline
from harness.workflows.dispatch import ReviewPolicy
from dispatch_contracts import COMPLETION_PASS_LIMITS
from dispatch_io import (
    DispatchError,
    atomic_text,
    ensure_owned_dir,
    exclusive_json,
    read_object,
    sha256_file,
)


def custom_pipeline_for_request(
    request: dict[str, Any],
) -> FrozenCustomPipeline | None:
    """Freeze a custom contract only from explicit bound approval evidence."""

    if request.get("pipeline") != "custom":
        return None
    approval = request.get("_custom_approval")
    if not isinstance(approval, ExplicitPipelineApproval):
        raise DispatchError("custom pipeline requires exact approval evidence")
    spec, compiled, policy, card = custom_contract_for_request(request)
    try:
        return freeze_custom_pipeline(spec, compiled, approval, card)
    except (HarnessContractError, OSError, ValueError) as exc:
        raise DispatchError(f"custom pipeline changed after approval: {exc}") from exc


def custom_contract_for_request(
    request: dict[str, Any],
) -> tuple[PipelineSpec, CompiledPipeline, CustomPipelinePolicy, str]:
    """Compile the effect-free custom contract for preview or approval."""

    frozen = request.get("_approved_custom_contract")
    if isinstance(frozen, tuple) and len(frozen) == 4:
        return frozen
    path = request.get("custom_pipeline_spec")
    if request.get("pipeline") != "custom":
        raise DispatchError("custom pipeline contract requires pipeline=custom")
    if not isinstance(path, Path):
        raise DispatchError("custom pipeline spec is unavailable")
    try:
        spec = parse_pipeline_spec(path.read_text(encoding="utf-8"))
        policy = CustomPipelinePolicy.default()
        compiled = compile_custom_spec(
            spec,
            builtin_registry(),
            policy=policy,
            capabilities=("route:resolved",),
        )
        card = render_custom_approval(spec, compiled, policy=policy)
        return spec, compiled, policy, card
    except (HarnessContractError, OSError, ValueError) as exc:
        raise DispatchError(f"custom pipeline changed after validation: {exc}") from exc


def custom_approval_card_for_request(request: dict[str, Any]) -> str:
    """Render model-declared plus unavoidable inherited harness authority."""

    _spec, _compiled, _policy, base = custom_contract_for_request(request)
    return base + "\n".join(
        (
            "Inherited harness permissions: cmux-target:policy-only",
            "Inherited harness side effects: cmux-surface:policy-only",
            "Coordinator target: "
            f"surface={request['origin_surface']}; "
            f"session={request['origin_session']}; "
            f"placement={request['placement']}",
            "",
        )
    )


def custom_approval_challenge(
    request: dict[str, Any],
    *,
    request_sha256: str,
    effective: dict[str, Any],
    review: ReviewPolicy,
    prompt: str,
) -> dict[str, Any]:
    """Bind one exact pre-effect validation result to later approval."""

    spec, compiled, _policy, _base_card = custom_contract_for_request(request)
    card = custom_approval_card_for_request(request)
    path = request["custom_pipeline_spec"]
    payload = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "custom_spec_sha256": sha256_file(path),
        "pipeline_spec_sha256": hashlib.sha256(
            json.dumps(
                pipeline_spec_payload(spec),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "definition_sha256": compiled.definition_sha256,
        "approval_card_sha256": hashlib.sha256(card.encode()).hexdigest(),
        "plan_sha256": sha256_file(request["plan_file"]),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "coordinator": {
            "origin_surface": request["origin_surface"],
            "origin_session": request["origin_session"],
            "placement": request["placement"],
        },
        "route": effective,
        "review": {
            "mode": review.mode,
            "cross_model": review.cross_model,
            "runtime": review.runtime,
            "model": review.model,
            "effort": review.effort,
            "max_verify_iterations": review.max_verify_iterations,
            "verification_profile": review.verification_profile,
            "verification_profile_sha256": (
                review.verification_profile_sha256
            ),
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**payload, "challenge_sha256": digest}


def custom_approval_path(request: dict[str, Any]) -> Path:
    return (
        request["vault_root"]
        / ".vault-meta"
        / "dispatch-approval-challenges"
        / f"{request['request_id']}.json"
    )


def custom_approval_plan_path(request: dict[str, Any]) -> Path:
    return custom_approval_path(request).with_suffix(".plan.md")


def approved_plan_file(request: dict[str, Any]) -> Path:
    value = request.get("_approved_plan_file")
    return value if isinstance(value, Path) else request["plan_file"]


def approved_plan_sha256(request: dict[str, Any]) -> str:
    value = request.get("_approved_plan_sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return sha256_file(request["plan_file"])


def approved_outcome_contract_sha256(request: dict[str, Any]) -> str:
    try:
        return extract_from_bytes(approved_plan_file(request).read_bytes()).sha256
    except (OSError, OutcomeContractError) as exc:
        raise DispatchError(f"approved plan Outcome Contract is invalid: {exc}") from exc


def _review_snapshot(review: ReviewPolicy) -> dict[str, Any]:
    return {
        "mode": review.mode,
        "cross_model": review.cross_model,
        "runtime": review.runtime,
        "model": review.model,
        "effort": review.effort,
        "max_verify_iterations": review.max_verify_iterations,
        "verification_profile": review.verification_profile,
        "verification_profile_sha256": review.verification_profile_sha256,
    }


def _review_from_snapshot(value: dict[str, Any]) -> ReviewPolicy:
    return ReviewPolicy(
        depth="simple" if value["mode"] == "skip" else value["mode"],
        cross_model=value["cross_model"],
        enabled=value["mode"] != "skip",
        runtime=value["runtime"],
        model=value["model"],
        effort=value["effort"],
        verification_profile=value["verification_profile"],
        verification_profile_sha256=value["verification_profile_sha256"],
    )


def custom_approval_snapshot(
    request: dict[str, Any],
    challenge: dict[str, Any],
    *,
    session: dict[str, Any],
    effective: dict[str, Any],
    review: ReviewPolicy,
    prompt: str,
) -> dict[str, Any]:
    spec, _compiled, _policy, _card = custom_contract_for_request(request)
    return {
        "schema_version": 1,
        "pipeline_spec": pipeline_spec_payload(spec),
        "approval_card": custom_approval_card_for_request(request),
        "prompt": prompt,
        "plan_sha256": challenge["plan_sha256"],
        "session": session,
        "effective": effective,
        "review": _review_snapshot(review),
    }


def persist_custom_approval_challenge(
    request: dict[str, Any],
    challenge: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    path = custom_approval_path(request)
    ensure_owned_dir(path.parent)
    plan_path = custom_approval_plan_path(request)
    plan_text = request["plan_file"].read_text(encoding="utf-8")
    if hashlib.sha256(plan_text.encode()).hexdigest() != challenge["plan_sha256"]:
        raise DispatchError("custom approval plan changed during validation")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise DispatchError("custom approval challenge is not owner-only")
        existing = read_object(path)
        if (
            existing.get("challenge") != challenge
            or existing.get("snapshot") != snapshot
            or not plan_path.is_file()
            or sha256_file(plan_path) != challenge["plan_sha256"]
        ):
            raise DispatchError(
                "custom approval challenge changed; use a fresh request_id"
            )
        return
    atomic_text(plan_path, plan_text)
    exclusive_json(
        path,
        {
            "schema_version": 1,
            "request_id": request["request_id"],
            "status": "pending",
            "decision": "",
            "actor": "",
            "approval_token_sha256": "",
            "challenge": challenge,
            "snapshot": snapshot,
        },
    )


def compiled_pipeline_for_request(request: dict[str, Any]):
    if request.get("pipeline") == "custom":
        return custom_contract_for_request(request)[1]
    return compiled_builtin(request["pipeline"])


def execution_pipeline_for_request(request: dict[str, Any]) -> str:
    if request.get("pipeline") == "custom":
        return custom_contract_for_request(request)[0].baseline_pipeline
    return request["pipeline"]


def task_pipeline_policy(request: dict[str, Any]) -> dict[str, object]:
    """Render honest task metadata without exposing the raw custom spec."""

    policy: dict[str, object] = {
        "name": request["pipeline"],
        "definition_sha256": compiled_pipeline_for_request(
            request
        ).definition_sha256,
        "completion_policy": request["completion_policy"],
        "total_pass_limit": COMPLETION_PASS_LIMITS[
            request["completion_policy"]
        ],
    }
    if request["pipeline"] == "custom":
        policy.update(
            {
                "source": "custom",
                "baseline": execution_pipeline_for_request(request),
            }
        )
    return policy
