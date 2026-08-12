#!/usr/bin/env python3
"""Deterministic post-approval runner for one dispatch task split.

The coordinator still owns natural-language parsing, context selection, and the
single user approval. This runner owns route capture, worktree creation,
prompt/metadata rendering, and the dispatch log entry. The generic provider
runtime owns cmux and provider lifecycle mechanics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from model_routing import (  # noqa: E402
    RoutingError,
    capture_session,
    load_config,
    resolve,
    routing_from_environment,
)
from task_contract import ContractError, normalize as normalize_task_contract  # noqa: E402
from outcome_contract import OutcomeContractError, extract_from_bytes  # noqa: E402
from lifecycle_telemetry import (  # noqa: E402
    emit_compiled_pipeline_event,
    emit_lifecycle_event,
)
from harness.contracts import (  # noqa: E402
    ContractError as HarnessContractError,
    RuntimeRoute,
)
from harness.context import ContextBuilder, outcome_contract_input  # noqa: E402
from harness.git_ops import GitAdapter, GitError  # noqa: E402
from harness.pipeline_builtins import (  # noqa: E402
    EXECUTABLE_BUILTINS,
    builtin_registry,
    compiled_builtin,
)
from harness.custom_pipelines import (  # noqa: E402
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
from harness.pipelines import CompiledPipeline, render_contract  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.runtime_sessions import (  # noqa: E402
    RuntimeSessionError,
    RuntimeSessionManager,
    RuntimeSessionResult,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.dispatch import (  # noqa: E402
    DispatchRequest as HarnessDispatchRequest,
    ReviewPolicy,
    start_dispatch,
)
from dispatch_contracts import (  # noqa: E402
    COMPLETION_PASS_LIMITS,
    COORDINATOR_ACTION,
    DEFAULT_DISPATCH,
    REVIEW_MODES,
    RUN_STATES,
    TASK_LOCAL_GIT_EXCLUDES,
    DispatchError,
    _approval_lock,
    absolute_dir,
    atomic_json,
    atomic_text,
    ensure_owned_dir,
    exclusive_json,
    read_object,
    require_string,
    sha256_file,
    utc_now,
    validate_request,
)
from dispatch_custom_contracts import (  # noqa: E402
    _review_from_snapshot,
    approved_outcome_contract_sha256,
    approved_plan_file,
    approved_plan_sha256,
    compiled_pipeline_for_request,
    custom_approval_card_for_request,
    custom_approval_challenge,
    custom_approval_path,
    custom_approval_plan_path,
    custom_approval_snapshot,
    custom_contract_for_request,
    custom_pipeline_for_request,
    execution_pipeline_for_request,
    persist_custom_approval_challenge,
    task_pipeline_policy,
)
from dispatch_setup import (  # noqa: E402
    extract_prompt_body,
    keep_plan_branch,
    load_dispatch_config,
    materialize_current_context,
    render_task_prompt,
    resolved_routes,
    review_policy,
    review_topology_preview,
    run_state_path,
)
from dispatch_workspace import (  # noqa: E402
    create_worktree,
    dispatch_log,
    ensure_task_git_excludes,
    initialize_task,
    observer_command,
    run_command,
    sync_codex_profile,
    write_task_files,
)
from dispatch_approval import (  # noqa: E402
    authorize_custom_request,
    host_custom_approval_decision,
    record_custom_approval_decision,
)
from dispatch_execution import start  # noqa: E402
from dispatch_lifecycle import (  # noqa: E402
    _child_identity,
    begin_run,
    completed_replay,
    harness_request,
    lifecycle_contract,
    lifecycle_contract_for_request,
    mark_failed,
)


HOST_APPROVAL_PROGRAM = Path("/usr/bin/osascript")




def die(message: str, code: int = 3) -> NoReturn:
    print(f"dispatch-runner: {message}", file=sys.stderr)
    raise SystemExit(code)














































































































def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--spec", type=Path, required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--spec", type=Path, required=True)
    approve.add_argument("--challenge-sha256", required=True)
    launch = sub.add_parser("start")
    launch.add_argument("--spec", type=Path, required=True)
    launch.add_argument("--approval-token", default="")
    args = parser.parse_args()
    try:
        spec_path = args.spec.expanduser().resolve()
        spec_sha256 = sha256_file(spec_path)
        raw = read_object(spec_path)
        if args.command == "start":
            replay = completed_replay(raw, spec_sha256)
            if replay is not None:
                print(json.dumps(replay, ensure_ascii=False, sort_keys=True))
                return 0
        request = validate_request(materialize_current_context(raw))
        if args.command in {"validate", "approve"}:
            config = load_dispatch_config(request["vault_root"], request["target_repo"])
            session, effective = resolved_routes(request, persist=False)
            review = review_policy(request, config)
            topology = review_topology_preview(request, review)
            prompt_request = request
            if request["pipeline"] == "custom":
                prompt_request = dict(request)
                prompt_request["_approved_plan_file"] = (
                    custom_approval_plan_path(request)
                )
            prompt = render_task_prompt(prompt_request, config)
            result = {
                "schema_version": 1,
                "status": "valid",
                "request_id": request["request_id"],
                "runtime": effective["runtime"],
                "model": effective["model"],
                "effort": effective["effort"],
                "plan_sha256": sha256_file(request["plan_file"]),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "session_source": session["source"],
                "placement": request["placement"],
                "observer": {
                    "temporary": request["request_id"],
                    "argv": observer_command(
                        request["vault_root"], request["request_id"]
                    ),
                },
                "pipeline": lifecycle_contract_for_request(request, review),
                "review": {
                    "mode": review.mode,
                    "cross_model": review.cross_model,
                    "runtime": review.runtime,
                    "model": review.model,
                    "effort": review.effort,
                    "max_verify_iterations": (
                        review.max_verify_iterations
                    ),
                    "verification_profile": (
                        review.verification_profile
                    ),
                    "verification_profile_sha256": (
                        review.verification_profile_sha256
                    ),
                    **topology,
                },
            }
            challenge = None
            if request["pipeline"] == "custom":
                challenge = custom_approval_challenge(
                    request,
                    request_sha256=spec_sha256,
                    effective=effective,
                    review=review,
                    prompt=prompt,
                )
                if args.command == "validate":
                    persist_custom_approval_challenge(
                        request,
                        challenge,
                        custom_approval_snapshot(
                            request,
                            challenge,
                            session=session,
                            effective=effective,
                            review=review,
                            prompt=prompt,
                        ),
                    )
                result["challenge_sha256"] = challenge["challenge_sha256"]
            if args.command == "approve":
                if challenge is None:
                    raise DispatchError(
                        "approve is available only for custom pipelines"
                    )
                result = record_custom_approval_decision(
                    request,
                    challenge,
                    args.challenge_sha256,
                )
            print(json.dumps(result, sort_keys=True))
            return 0
        if request["pipeline"] == "custom":
            request = authorize_custom_request(
                request,
                spec_sha256,
                args.approval_token,
            )
        print(json.dumps(start(request, spec_sha256), ensure_ascii=False, sort_keys=True))
        return 0
    except (
        DispatchError,
        RoutingError,
        ContractError,
        RuntimeSessionError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
