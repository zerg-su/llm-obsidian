#!/usr/bin/env python3
"""Drive the automatic review gate for one exact v3/v4 dispatch worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

from harness.context import ContextBuilder, ContextInput, outcome_contract_input
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError as HarnessContractError,
    EffectOutcome,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    resolve_custom_executable,
)
from harness.pipeline_builtins import builtin_registry
from harness.runtime_worker import _pipeline_verify_identity
from harness.review_program import (
    PURPOSES as REVIEW_PURPOSES,
    QUESTIONS as REVIEW_QUESTIONS,
    ReviewBoundaryInput,
)
from harness.review_program_authority import stale_resolution_boundary
from harness.review_submit import round_schema_lines
from harness.runtime_sessions import RuntimeSessionManager
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError
from harness.verification import (
    VerificationError,
    compose_commands,
    load_profiles,
)
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    review_round_envelope,
    review_session_specs,
    runtime_status_is_live,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewPreset,
    ReviewScopeBoundary,
    review_context_sha256,
)
from model_routing import (
    load_config,
    resolve,
    routing_from_environment,
    session_from_meta,
)
from review_resolution import (
    MATERIAL_SEVERITIES,
    MAX_FIX_DELTA_BYTES,
    ResolutionError,
    ReviewResolution,
    ReviewResolutionEvidence,
    build_resolution_evidence,
    review_transport_identity_sha256,
    validate_resolution,
    validate_resolution_evidence,
)
from review_telemetry import emit_review_event
from task_contract import normalize
from task_review_shared import (
    ActiveReviewRound,
    FinalizingRecovery,
    ResolutionBundle,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_bytes,
    _atomic_json,
    _atomic_text,
    _git,
    _git_bytes,
    _load_review_boundary_input,
    _read_json,
)
from task_review_resolution_bundle import (
    _bounded_input,
    _recovery_resolution_bundle,
    _resolution_bundle,
)
from task_review_context import (
    _callback_path,
    _canonical_sha256,
    _context,
    _current_review_is_quiescent,
    _current_runtime_root,
    _envelope,
    _gate_root,
    _prompt,
    _purpose_boundary_inputs,
    _request,
    _route,
    _runtime_root,
    _validate_task,
)
from task_review_verification import (
    _durable_successful_verification,
    _durable_verification_resubmit,
    _finalizing_resubmit_recovery,
)
from task_review_flow import (
    _emit_round_telemetry,
    _pending_replay_is_safe,
    _receipt,
    _run_review as _drive_review,
    _write_round_meta,
    load_active_round,
)
from task_review_current import (
    _current_policy,
    _same_requested_policy,
    _validate_current_checkout,
    run_current_review as _run_current_review,
)
from task_review_plan import run_plan_review as _run_plan_review
from task_review_finalizing import (
    _apply_finalizing_recovery,
    _dispatched_review_is_quiescent,
    _launch_authorized_task_review,
    recover_finalizing_review,
)


















































def _run_review(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Keep the public facade while the flow owns review orchestration."""

    return _drive_review(
        meta,
        vault,
        worktree,
        task_id,
        runtime_root,
        runtime_manager=runtime_manager,
        apply_finalizing_recovery=_apply_finalizing_recovery,
    )












def run_task_review(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    return _run_review(
        meta,
        vault,
        worktree,
        task_id,
        _runtime_root(vault, task_id),
        runtime_manager=runtime_manager,
    )












def run_current_review(
    worktree: Path,
    *,
    deep: bool = False,
    full: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    no_review: bool = False,
    purpose: str = "implementation",
    boundary_input_file: Path | None = None,
    artifact_root: Path | None = None,
    plan_file: Path | None = None,
    origin_surface: str = "",
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    return _run_current_review(
        worktree,
        deep=deep,
        full=full,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
        purpose=purpose,
        boundary_input_file=boundary_input_file,
        artifact_root=artifact_root,
        plan_file=plan_file,
        origin_surface=origin_surface,
        scratch_root=scratch_root,
        runtime_manager=runtime_manager,
        apply_finalizing_recovery=_apply_finalizing_recovery,
    )


def run_plan_review(
    worktree: Path,
    *,
    plan_file: Path,
    base: str = "",
    capability_dispositions: str = "",
    success_evidence_map: str = "",
    deep: bool = False,
    full: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    origin_surface: str = "",
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    return _run_plan_review(
        worktree,
        plan_file=plan_file,
        base=base,
        capability_dispositions=capability_dispositions,
        success_evidence_map=success_evidence_map,
        deep=deep,
        full=full,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        origin_surface=origin_surface,
        scratch_root=scratch_root,
        runtime_manager=runtime_manager,
        apply_finalizing_recovery=_apply_finalizing_recovery,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--worktree", type=Path, required=True)
    current = sub.add_parser("current")
    current.add_argument("--worktree", type=Path, required=True)
    current.add_argument("--deep", action="store_true")
    current.add_argument("--full", action="store_true")
    current.add_argument("--cross-model", action="store_true")
    current.add_argument("--runtime", choices=("claude", "codex"), default="")
    current.add_argument("--model", default="")
    current.add_argument("--effort", default="")
    current.add_argument("--no-review", action="store_true")
    current.add_argument(
        "--purpose", choices=REVIEW_PURPOSES, default="implementation"
    )
    current.add_argument("--boundary-input", type=Path)
    current.add_argument("--artifact-root", type=Path)
    current.add_argument("--plan", type=Path)
    current.add_argument("--origin-surface", default="")
    plan = sub.add_parser("plan")
    plan.add_argument("--worktree", type=Path, required=True)
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--base", default="")
    plan.add_argument("--capability-dispositions", default="")
    plan.add_argument("--success-evidence-map", default="")
    plan.add_argument("--deep", action="store_true")
    plan.add_argument("--full", action="store_true")
    plan.add_argument("--cross-model", action="store_true")
    plan.add_argument("--runtime", choices=("claude", "codex"), default="")
    plan.add_argument("--model", default="")
    plan.add_argument("--effort", default="")
    plan.add_argument("--origin-surface", default="")
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_manager: object | None = None,
) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_task_review(
                args.worktree, runtime_manager=runtime_manager
            )
        elif args.command == "current":
            if (
                args.plan is not None
                and args.purpose == "implementation"
                and args.boundary_input is None
            ):
                raise TaskReviewError(
                    "legacy current --plan is ambiguous; use the plan facade "
                    "or pass an explicit compatible purpose and boundary"
                )
            result = run_current_review(
                args.worktree,
                deep=args.deep,
                full=args.full,
                cross_model=args.cross_model,
                runtime=args.runtime,
                model=args.model,
                effort=args.effort,
                no_review=args.no_review,
                purpose=args.purpose,
                boundary_input_file=args.boundary_input,
                artifact_root=args.artifact_root,
                plan_file=args.plan,
                origin_surface=args.origin_surface,
                runtime_manager=runtime_manager,
            )
        else:
            result = run_plan_review(
                args.worktree,
                plan_file=args.plan,
                base=args.base,
                capability_dispositions=args.capability_dispositions,
                success_evidence_map=args.success_evidence_map,
                deep=args.deep,
                full=args.full,
                cross_model=args.cross_model,
                runtime=args.runtime,
                model=args.model,
                effort=args.effort,
                origin_surface=args.origin_surface,
                runtime_manager=runtime_manager,
            )
    except (OSError, TaskReviewError, ValueError, RuntimeError) as exc:
        print(f"task-review-runner: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
