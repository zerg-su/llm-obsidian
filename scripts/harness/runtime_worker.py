"""Short-lived supervisor for one provider process inside an owned cmux surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters.cmux import CmuxAdapter
from .adapters.process import ProcessAdapter, ProcessError, ProcessHandle
from .callbacks import (
    REVIEWER_PROFILES,
    CallbackBroker,
    CallbackError,
    CallbackTimeoutError,
)
from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError as HarnessContractError,
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    to_dict,
)
from .prompts import PromptDecision, classify
from .pipeline_builtins import compiled_executable_for_contract
from .pipeline_builtins import builtin_registry
from .custom_pipelines import (
    CustomPipelinePolicy,
    resolve_custom_executable,
)
from .liveness import (
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
)
from .callback_submit_recovery import (
    ArtifactEvidence,
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    callback_submit_binding_identity,
    callback_submit_binding_sha256,
    classify_callback_prompt,
    classify_callback_submit,
)
from .pipelines import reconcile_pipeline
from .review_finalization import task_review_status
from .state_machine import TERMINAL
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor, SupervisorError
from .runtime_worker_contracts import (
    IDENTIFIER,
    SURFACE_UUID,
    RuntimeWorkerError,
)
from .runtime_worker_spec import load_spec
from .verification import (
    VerificationError,
    compose_commands,
    load_profiles,
    run_profile,
)
from .verification_attempt import (
    VerificationAttempt,
    VerificationAttemptError,
    pipeline_verify_effect_id,
    pipeline_verify_identity,
    verification_input_sha256,
)
from .workflows.engineering_fix import (
    FixStepReceipt,
    FixWorkflowError,
    accept_phase,
    load_receipt,
    prepare_next_phase,
    prepare_retry_phase,
    reconcile_fix,
    reconcile_retry_fix,
)
from .workflows.custom_sequence import (
    CustomSequenceError,
    CustomStepReceipt,
    accept_custom_step,
    custom_step_request,
    load_custom_receipt,
    prepare_custom_step,
    reconcile_custom_sequence,
)
from research_contract import (
    ResearchContractError,
    load_artifact,
    validate_result_artifact,
)
from lifecycle_telemetry import emit_compiled_pipeline_event, emit_lifecycle_event
from review_resolution import (
    DISPOSITIONS,
    MATERIAL_SEVERITIES,
    ResolutionError,
    review_transport_identity_sha256,
)
from task_contract import ContractError, validate_handoff
from task_escalation_records import (
    EscalationRecordError,
    load_latest as load_latest_escalation,
)
from wiki_summary_contract import WikiSummaryError, validate_summary_for_task

from .runtime_callback_io import (
    MAX_OUTBOX_BYTES,
    _atomic_json,
    _bounded_file_sha256,
    _callback_target,
    _current_callback_receipt_sha256,
    _envelope,
    _normalize_fetch_errors_at_provider_boundary,
    _research_input_provenance,
    _submit_failure_requires_attention,
    _write_once_json,
    observe_review_artifact,
    publish_callback_wake,
    submit_stable_review_input,
    wake_resume_once,
)
from .runtime_provider import (
    RESEARCH_PATH,
    _contain_provider_start_failure,
    _pin_env_shebang,
    _reap_child,
    automate_prompt,
    provider_argv,
    provider_environment,
    provider_resume_argv,
)
from .runtime_provider_events import (
    RuntimeProviderEventError,
    RuntimeProviderEventStream,
)


MAX_SCREEN_BYTES = 70_000
MAX_PIPELINE_VERIFY_RESUBMITS = 1
CALLBACK_WAIT_STATES = frozenset(
    {"running", "awaiting-callback", "verifying"}
)
#: Mechanism/transport failure classes a restarted worker generation may
#: resume once from their durable resume boundary.  Policy and budget
#: terminals (retry-exhausted, contract drift, budget ceilings) stay with the
#: coordinator.
RESUMABLE_SUMMARY_ATTENTION = frozenset(
    {
        "wiki-summary-invalid",
        "review-drive-failed",
        "callback-wake-effect-uncertain",
        "pipeline-verification-effect-uncertain",
    }
)


def _review_resolution_handoff_ready(
    *,
    worktree: Path,
    operation_id: str,
    gate_state: Mapping[str, object],
    current_head: str,
) -> bool:
    """Return true only after the executor publishes one complete fix handoff."""

    awaiting = gate_state.get("awaiting_resolution")
    if not isinstance(awaiting, dict) or not awaiting:
        attempt = gate_state.get("attempt")
        terminal = (
            attempt.get("terminal") if isinstance(attempt, dict) else None
        )
        if (
            isinstance(attempt, dict)
            and attempt.get("status") == "terminal"
            and isinstance(terminal, dict)
            and terminal.get("result") == "changes-requested"
        ):
            awaiting = gate_state.get("review_notification_evidence")
    if not isinstance(awaiting, dict) or not awaiting:
        return False
    reviewed_heads = {
        str(boundary.get("reviewed_head_sha") or "")
        for boundary in awaiting.values()
        if isinstance(boundary, dict)
    }
    expected_finding_ids: list[str] = []
    review_operation_ids: set[str] = set()
    review_callbacks: list[dict[str, object]] = []
    for axis in sorted(awaiting):
        boundary = awaiting[axis]
        if not isinstance(boundary, dict):
            return False
        material_ids = boundary.get("material_finding_ids")
        if (
            not isinstance(material_ids, list)
            or any(
                not isinstance(finding_id, str) or not finding_id
                for finding_id in material_ids
            )
        ):
            return False
        expected_finding_ids.extend(material_ids)
        review_operation_ids.add(
            str(boundary.get("review_operation_id") or "")
        )
        review_callbacks.append(
            {
                "axis": axis,
                "round_operation_id": str(
                    boundary.get("round_operation_id") or ""
                ),
                "round_run_id": str(
                    boundary.get("round_run_id") or ""
                ),
                "callback_id": str(boundary.get("callback_id") or ""),
                "callback_sha256": str(
                    boundary.get("callback_sha256") or ""
                ),
            }
        )
    active_review_operation_id = str(
        gate_state.get("active_review_operation_id") or ""
    )
    try:
        review_identity_sha256 = review_transport_identity_sha256(
            active_review_operation_id, review_callbacks
        )
    except ResolutionError:
        return False
    if (
        len(reviewed_heads) != 1
        or "" in reviewed_heads
        or not expected_finding_ids
        or len(expected_finding_ids) != len(set(expected_finding_ids))
        or review_operation_ids != {active_review_operation_id}
    ):
        return False
    resolution_path = worktree / ".task-review-resolution.json"
    if not resolution_path.is_file() or resolution_path.is_symlink():
        return False
    try:
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    items = resolution.get("resolutions") if isinstance(resolution, dict) else None
    if (
        resolution.get("schema_version") != 1
        or resolution.get("operation_id") != operation_id
        or resolution.get("reviewed_head_sha") != next(iter(reviewed_heads))
        or resolution.get("resolved_head_sha") != current_head
        or resolution.get("review_identity_sha256")
        != review_identity_sha256
        or not isinstance(items, list)
        or not items
        or [
            item.get("finding_id")
            for item in items
            if isinstance(item, dict)
        ]
        != expected_finding_ids
    ):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("finding_id"), str)
        and bool(item["finding_id"])
        and item.get("disposition") in DISPOSITIONS
        and isinstance(item.get("rationale"), str)
        and bool(item["rationale"])
        and isinstance(item.get("follow_up"), str)
        and (
            item["disposition"] != "out-of-scope"
            or bool(item["follow_up"])
        )
        for item in items
    )


def _pipeline_verify_identity(
    parent: OperationSpec,
    *,
    definition_sha256: str,
    input_sha256: str,
    profile: str,
    attempt_index: int = 0,
) -> tuple[OperationSpec, str, str]:
    """Derive one immutable verify operation from its exact pipeline input."""

    try:
        return pipeline_verify_identity(
            parent,
            definition_sha256=definition_sha256,
            input_sha256=input_sha256,
            profile=profile,
            attempt_index=attempt_index,
        )
    except VerificationAttemptError as exc:
        raise RuntimeWorkerError(str(exc)) from exc


def _pipeline_verify_effect_id(input_sha256: str, attempt_index: int = 0) -> str:
    """Keep attempt-zero compatibility while separating the one retry effect."""

    try:
        return pipeline_verify_effect_id(input_sha256, attempt_index)
    except VerificationAttemptError as exc:
        raise RuntimeWorkerError(str(exc)) from exc


def provider_exit_is_final(
    *,
    provider_exited: bool,
    callback_mode: str,
    callback_handled: bool,
    operation_state: str,
    operation_profile: str,
    callback_deadline_at: float,
) -> bool:
    """Keep callback transports alive until handled or durably stopped."""

    if not provider_exited:
        return False
    if callback_handled:
        return True
    if (
        callback_mode in {"task-summary", "artifact-repair"}
        or (
            operation_profile in REVIEWER_PROFILES
            and callback_deadline_at > 0
        )
    ):
        return operation_state in {
            "attention-required",
            "cancelling",
            "exiting",
            *TERMINAL,
        }
    return True


def enforce_callback_deadline(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    *,
    callback_handled: bool,
    now: float | None = None,
) -> bool:
    """Turn an expired live reviewer wait into durable typed attention."""

    record = store.read(owner_id, operation_id)
    if (
        callback_handled
        or record.spec.route.profile not in REVIEWER_PROFILES
        or record.state not in CALLBACK_WAIT_STATES
        or not record.deadline_at
    ):
        return False
    try:
        OperationSupervisor(
            store, owner_id, operation_id
        ).check_budget(
            now=now,
            timeout_reason=AttentionReason.CALLBACK_TIMEOUT,
        )
    except SupervisorError:
        current = store.read(owner_id, operation_id)
        return (
            current.state == "attention-required"
            and current.attention_reason
            == AttentionReason.CALLBACK_TIMEOUT
        )
    return False


def run(spec_path: Path, *, poll_seconds: float=0.1, checkpoint_probe: Callable[[str, str], str] | None=None, cmux_adapter: object | None=None, review_launcher: Callable[[Path, Path], None] | None=None, verification_runner: Callable[..., subprocess.CompletedProcess[str]] | None=None, callback_submit_policy: CallbackSubmitPolicy | None=None, clock: Callable[[], float] | None=None, wall_clock: Callable[[], float] | None=None, monotonic_clock: Callable[[], float] | None=None, sleeper: Callable[[float], None] | None=None, wake_source: object | None=None, initial_start_observation_limit: int | None=None) -> int:
    from .runtime_worker_execution import RuntimeWorkerExecution
    worker = RuntimeWorkerExecution()
    worker.contain_provider_start_failure = _contain_provider_start_failure
    return worker.execute(spec_path, poll_seconds=poll_seconds, checkpoint_probe=checkpoint_probe, cmux_adapter=cmux_adapter, review_launcher=review_launcher, verification_runner=verification_runner, callback_submit_policy=callback_submit_policy, clock=clock, wall_clock=wall_clock, monotonic_clock=monotonic_clock, sleeper=sleeper, wake_source=wake_source, initial_start_observation_limit=initial_start_observation_limit)


def _publish_early_failure(spec_path: Path, reason: str) -> None:
    """Leave a bounded diagnostic when failure precedes worker initialization."""

    try:
        lexical = spec_path.expanduser()
        if lexical.is_symlink() or lexical.name != "launch.json":
            return
        launch = lexical.resolve(strict=True)
        parent = launch.parent
        stat = parent.stat()
        if not parent.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o022:
            return
        value = json.loads(launch.read_text(encoding="utf-8"))
        raw_ready = value.get("ready_path") if isinstance(value, dict) else None
        if not isinstance(raw_ready, str) or not raw_ready:
            return
        ready = Path(raw_ready).expanduser().resolve(strict=False)
        if ready != parent / "ready.json":
            return
        _atomic_json(
            ready,
            {
                "schema_version": 1,
                "status": "failed",
                "reason": reason[:200],
            },
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.spec)
    except RuntimeWorkerError as exc:
        _publish_early_failure(args.spec, str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
