"""Public harness CLI; diagnostics work without a model session."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from pathlib import Path
from time import time
from typing import Sequence

from .adapters.cmux import CmuxAdapter
from .adapters.process import ProcessAdapter
from .contracts import (
    AttentionReason,
    ContractError,
    OperationRecord,
    OwnedResources,
    TransitionResult,
    to_dict,
)
from .diagnostics import observe as observe_diagnostics
from .reconciliation import (
    ReconcileDecision,
    prove_accepted_callback_ownership,
    reconcile,
)
from .state_machine import TERMINAL
from .status_segment import publish as publish_status
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor
from .runtime_sessions import RuntimeSessionError, RuntimeSessionManager


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="harness")
    result.add_argument("--store", type=Path, default=Path(".vault-meta/harness"))
    result.add_argument("--owner", default="local")
    result.add_argument("--json", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("operation_id")
    resume = commands.add_parser("resume")
    resume.add_argument("operation_id")
    commands.add_parser("reconcile")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("operation_id")
    close = commands.add_parser("close")
    close.add_argument("operation_id")
    commands.add_parser("doctor")
    commands.add_parser("diagnose")
    return result


def _emit(value: object, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, list):
        for row in value:
            detail = row.get("kind", row.get("action", ""))
            print(f"{row['operation_id']}\t{row['state']}\t{detail}")
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


def _has_owned_resources(record: OperationRecord) -> bool:
    resources = record.resources
    return bool(
        resources.surface_id
        or resources.process_group
        or resources.supervisor_pid
    )


def _attention(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    reason: AttentionReason,
) -> TransitionResult:
    record = store.read(owner, operation_id)
    if record.state in TERMINAL:
        return store.transition(owner, operation_id, record.state)
    return store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=reason,
    )


def _runtime_workspace(
    store: OperationStore,
    record: OperationRecord,
) -> tuple[str, str] | None:
    path = (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
        / "session.json"
    )
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("operation_id") != record.spec.operation_id
        or value.get("run_id") != record.run_id
    ):
        raise ValueError("runtime session metadata identity is invalid")
    if value.get("placement") != "workspace":
        return None
    workspace_id = str(value.get("workspace_id") or "")
    window_id = str(value.get("window_id") or "")
    if not workspace_id or not window_id:
        raise ValueError("runtime workspace metadata is incomplete")
    return workspace_id, window_id


def _continuation_time_budget(
    store: OperationStore,
    record: OperationRecord,
) -> float | None:
    path = (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
        / "session.json"
    )
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    budget = value.get("time_budget_seconds") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("operation_id") != record.spec.operation_id
        or value.get("run_id") != record.run_id
        or not isinstance(budget, (int, float))
        or isinstance(budget, bool)
        or budget <= 0
    ):
        return None
    return float(budget)


def _reconcile_owned_resources(
    store: OperationStore,
    record: OperationRecord,
    process_adapter: object,
    cmux_adapter: object,
) -> ReconcileDecision:
    """Probe exact ownership and confirm any cleanup with a fresh liveness read."""

    try:
        resources = record.resources
        has_process_group = resources.process_group > 1
        has_surface = bool(resources.surface_id)
        workspace = _runtime_workspace(store, record)
        accepted = prove_accepted_callback_ownership(
            record,
            process_adapter,
        )
        if accepted.applicable:
            process_status = accepted.process_status
            supervisor_status = accepted.supervisor_status
            if "unknown" in {process_status, supervisor_status} or (
                process_status == "alive" and supervisor_status == "dead"
            ):
                return ReconcileDecision(
                    "none", AttentionReason.ATTENTION_REQUIRED
                )
            if "alive" in {process_status, supervisor_status}:
                return ReconcileDecision("continue")
        if resources.supervisor_pid and not has_process_group:
            return ReconcileDecision(
                "none", AttentionReason.ATTENTION_REQUIRED
            )
        if has_surface and not has_process_group:
            surface = (
                cmux_adapter.workspace_status(*workspace)
                if workspace is not None
                else cmux_adapter.status(resources.surface_id)
            )
            if surface == "alive":
                return ReconcileDecision("continue")
            if surface == "missing":
                return ReconcileDecision("complete")
            return ReconcileDecision(
                "none", AttentionReason.ATTENTION_REQUIRED
            )
        if has_process_group and not has_surface:
            process = process_adapter.process_status(
                resources.process_group,
                resources.process_identity,
            )
            if process == "alive":
                return ReconcileDecision("continue")
            if process == "dead":
                return ReconcileDecision("complete")
            return ReconcileDecision(
                "none", AttentionReason.ATTENTION_REQUIRED
            )
        decision = reconcile(
            record,
            process_adapter,
            cmux_adapter,
            workspace=workspace,
        )
        if decision.action in {"close-exact", "terminate-exact"}:
            decision = reconcile(
                record,
                process_adapter,
                cmux_adapter,
                workspace=workspace,
            )
        return decision
    except Exception:
        return ReconcileDecision("none", AttentionReason.ATTENTION_REQUIRED)


def _clear_owned_resources(
    store: OperationStore,
    owner: str,
    operation_id: str,
) -> None:
    OperationSupervisor(store, owner, operation_id).bind_resources(OwnedResources())


def _cancel_or_close(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
) -> TransitionResult:
    record = store.read(owner, operation_id)
    if record.state in TERMINAL:
        return store.transition(owner, operation_id, record.state)
    if record.pending_effect:
        return _attention(
            store,
            owner,
            operation_id,
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
    if (
        record.spec.route.profile == "reviewer-callback"
        and _has_owned_resources(record)
    ):
        runtime = RuntimeSessionManager(
            store,
            cmux_adapter,
            process_adapter,
        )
        try:
            runtime.prove_durable_cleanup_ownership(owner, operation_id)
        except RuntimeSessionError:
            return _attention(
                store,
                owner,
                operation_id,
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        initial = record
        result = (
            runtime.cleanup(owner, operation_id)
            if record.state == "exiting"
            else runtime.request_exit(owner, operation_id)
        )
        current = result.record
        return TransitionResult(
            operation_id,
            initial.state,
            current.state,
            current.revision,
            current.revision != initial.revision,
            current.attention_reason,
        )
    if _has_owned_resources(record):
        decision = _reconcile_owned_resources(
            store, record, process_adapter, cmux_adapter
        )
        if decision.action == "continue":
            requested = RuntimeSessionManager(
                store,
                cmux_adapter,
                process_adapter,
            ).request_exit(owner, operation_id)
            current = requested.record
            return TransitionResult(
                operation_id,
                record.state,
                current.state,
                current.revision,
                current.revision != record.revision,
                current.attention_reason,
            )
        if decision.action != "complete":
            return _attention(
                store,
                owner,
                operation_id,
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        _clear_owned_resources(store, owner, operation_id)
        record = store.read(owner, operation_id)
    if record.state not in {"cancelling", "exiting"}:
        store.transition(owner, operation_id, "cancelling")
        record = store.read(owner, operation_id)
    if record.state == "cancelling":
        store.transition(owner, operation_id, "exiting")
        record = store.read(owner, operation_id)
    terminal_state = (
        "complete"
        if record.state == "exiting"
        and record.accepted_callback_id
        and record.accepted_callback_kind
        and record.accepted_callback_sha256
        else "cancelled"
    )
    return store.transition(owner, operation_id, terminal_state)


def _review_recovery_kind(
    gate: object, response_path: Path
) -> str:
    """Select exact callback recovery before the legacy response boundary."""

    if not isinstance(gate, dict):
        return ""
    attempt = gate.get("attempt")
    if (
        gate.get("status") == "reviewing"
        and gate.get("execution_protocol") == "exact-head-attempt-v1"
        and isinstance(attempt, dict)
        and attempt.get("status") == "awaiting-callback"
    ):
        return "accepted-exact-callbacks"
    if not response_path.is_file():
        return ""
    if gate.get("status") in {
        "verifying",
        "recovery-verification-required",
        "fresh-boundary-authorized",
    }:
        return "legacy-finalizing"
    return ""


def _recover_finalizing_review_if_present(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    runtime_manager: object | None = None,
) -> bool:
    """Recover one exact accepted review callback without starting a model."""

    store_root = store.root.expanduser().resolve()
    vault = store_root.parents[1]
    session_path = (
        store_root
        / "owners"
        / owner
        / "runtime"
        / operation_id
        / "session.json"
    )
    if not session_path.is_file() or session_path.is_symlink():
        return False
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSessionError(
            "dispatch recovery session metadata is invalid"
        ) from exc
    if not isinstance(session, dict):
        raise RuntimeSessionError(
            "dispatch recovery session metadata is invalid"
        )
    worktree = Path(str(session.get("cwd") or "")).expanduser()
    response_path = worktree / ".task-verification-response.json"
    if (
        session.get("operation_id") != operation_id
        or not worktree.is_absolute()
    ):
        return False
    gate_path = (
        vault
        / ".vault-meta"
        / "harness"
        / "review-data"
        / operation_id
        / operation_id
        / "review-gate.json"
    )
    if not gate_path.is_file() or gate_path.is_symlink():
        return False
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSessionError(
            "dispatch recovery review gate is invalid"
        ) from exc
    recovery_kind = _review_recovery_kind(gate, response_path)
    if not recovery_kind:
        return False
    runner_path = Path(__file__).resolve().parents[1] / "task-review-runner.py"
    module_spec = importlib.util.spec_from_file_location(
        "_harness_task_review_recovery",
        runner_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeSessionError(
            "dispatch recovery implementation is unavailable"
        )
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
        recover = getattr(
            module,
            (
                "recover_task_review_for_mechanism"
                if recovery_kind == "accepted-exact-callbacks"
                else "recover_finalizing_review"
            ),
        )
        runtime = runtime_manager or RuntimeSessionManager.for_root(
            vault,
            store_root=store_root,
        )
        receipt = recover(worktree, runtime_manager=runtime)
    except Exception as exc:
        raise RuntimeSessionError(
            "dispatch finalizing review recovery failed"
        ) from exc
    if not isinstance(receipt, dict) or receipt.get("status") not in {
        "approved",
        "changes-requested",
        "verifying",
        "reviewing",
    }:
        raise RuntimeSessionError(
            "dispatch finalizing review recovery did not make bounded progress"
        )
    return True


def _recover_post_verification_review_drive_if_present(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
    runtime_manager: object | None = None,
) -> bool:
    """Continue one exact live dispatch through its authorized failed drive."""

    session_path = (
        store.root
        / "owners"
        / owner
        / "runtime"
        / operation_id
        / "session.json"
    )
    if not session_path.is_file() or session_path.is_symlink():
        return False
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSessionError(
            "post-verification dispatch session metadata is invalid"
        ) from exc
    if (
        not isinstance(session, dict)
        or session.get("operation_id") != operation_id
    ):
        return False
    worktree = Path(str(session.get("cwd") or "")).expanduser()
    if not worktree.is_absolute():
        return False
    runner_path = Path(__file__).resolve().parents[1] / "task-review-runner.py"
    module_spec = importlib.util.spec_from_file_location(
        "_harness_post_verification_review_drive",
        runner_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeSessionError(
            "post-verification review recovery is unavailable"
        )
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
        recover = getattr(module, "recover_post_verification_review_drive")
        receipt = recover(
            worktree,
            process_adapter=process_adapter,
            cmux_adapter=cmux_adapter,
            runtime_manager=runtime_manager,
        )
    except Exception as exc:
        raise RuntimeSessionError(
            "post-verification review-drive recovery failed"
        ) from exc
    if receipt is None:
        return False
    if not isinstance(receipt, dict) or receipt.get("status") != "applied":
        raise RuntimeSessionError(
            "post-verification review-drive recovery did not synchronize"
        )
    return True


def _resume(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
    review_runtime_manager: object | None = None,
) -> TransitionResult:
    """Restore the exact paused phase and drive cleanup without a model."""

    initial = store.read(owner, operation_id)
    if initial.pending_effect:
        return _attention(
            store,
            owner,
            operation_id,
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
    if initial.spec.kind == "dispatch" and (
        _recover_post_verification_review_drive_if_present(
            store,
            owner,
            operation_id,
            process_adapter=process_adapter,
            cmux_adapter=cmux_adapter,
            runtime_manager=review_runtime_manager,
        )
    ):
        current = store.read(owner, operation_id)
        return TransitionResult(
            operation_id,
            initial.state,
            current.state,
            current.revision,
            current.revision != initial.revision,
            current.attention_reason,
        )
    if initial.state == "attention-required":
        if initial.spec.kind == "dispatch":
            _recover_finalizing_review_if_present(
                store,
                owner,
                operation_id,
                runtime_manager=review_runtime_manager,
            )
        if not initial.resume_state:
            return store.transition(owner, operation_id, initial.state)
        store.transition(owner, operation_id, initial.resume_state)
        current = store.read(owner, operation_id)
        if (
            current.state in {"running", "awaiting-callback", "verifying"}
            and current.deadline_at
            and current.deadline_at <= time()
        ):
            time_budget_seconds = _continuation_time_budget(store, current)
            if time_budget_seconds is None:
                return _attention(
                    store,
                    owner,
                    operation_id,
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            OperationSupervisor(
                store, owner, operation_id
            ).begin_continuation(
                time_budget_seconds=time_budget_seconds
            )
    current = store.read(owner, operation_id)
    if current.state == "cancelling":
        return _cancel_or_close(
            store,
            owner,
            operation_id,
            process_adapter=process_adapter,
            cmux_adapter=cmux_adapter,
        )
    if current.state == "exiting":
        runtime = RuntimeSessionManager(
            store,
            cmux_adapter,
            process_adapter,
        )
        final = runtime.cleanup(owner, operation_id).record
        return TransitionResult(
            operation_id,
            initial.state,
            final.state,
            final.revision,
            final.revision != initial.revision,
            final.attention_reason,
        )
    return TransitionResult(
        operation_id,
        initial.state,
        current.state,
        current.revision,
        current.revision != initial.revision,
        current.attention_reason,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    process_adapter: object | None = None,
    cmux_adapter: object | None = None,
    review_runtime_manager: object | None = None,
) -> int:
    args = parser().parse_args(argv)
    store = OperationStore(args.store)
    process_adapter = process_adapter or ProcessAdapter()
    cmux_adapter = cmux_adapter or CmuxAdapter()
    try:
        if args.command == "status":
            value = [
                {
                    "operation_id": row.spec.operation_id,
                    "kind": row.spec.kind,
                    "state": row.state,
                    "revision": row.revision,
                    "lane_id": row.lane_id,
                    "run_id": row.run_id,
                }
                for row in store.list(args.owner)
            ]
        elif args.command == "inspect":
            value = to_dict(store.read(args.owner, args.operation_id))
        elif args.command == "doctor":
            value = {
                "status": "ok" if shutil.which("cmux") else "degraded",
                "cmux": bool(shutil.which("cmux")),
                "claude": bool(shutil.which("claude")),
                "codex": bool(shutil.which("codex")),
            }
        elif args.command == "diagnose":
            value = observe_diagnostics(args.store, args.owner)
        elif args.command == "reconcile":
            value = []
            for row in store.list(args.owner):
                if row.state in TERMINAL:
                    continue
                operation_id = row.spec.operation_id
                if row.pending_effect:
                    _attention(
                        store,
                        args.owner,
                        operation_id,
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                    action = "inspect-pending-effect"
                elif _has_owned_resources(row):
                    decision = _reconcile_owned_resources(
                        store, row, process_adapter, cmux_adapter
                    )
                    if decision.action == "continue":
                        action = "resources-live"
                    elif decision.action == "complete":
                        _clear_owned_resources(store, args.owner, operation_id)
                        if row.state in {"cancelling", "exiting"}:
                            _cancel_or_close(
                                store,
                                args.owner,
                                operation_id,
                                process_adapter=process_adapter,
                                cmux_adapter=cmux_adapter,
                            )
                            action = (
                                "callback-complete"
                                if row.accepted_callback_id
                                else "cancel-complete"
                            )
                        else:
                            _attention(
                                store,
                                args.owner,
                                operation_id,
                                reason=(
                                    decision.reason
                                    or AttentionReason.CLEANUP_INCOMPLETE
                                ),
                            )
                            action = "resources-released"
                    else:
                        _attention(
                            store,
                            args.owner,
                            operation_id,
                            reason=AttentionReason.CLEANUP_INCOMPLETE,
                        )
                        action = "inspect-owned-resources"
                elif row.state in {"cancelling", "exiting"}:
                    _cancel_or_close(
                        store,
                        args.owner,
                        operation_id,
                        process_adapter=process_adapter,
                        cmux_adapter=cmux_adapter,
                    )
                    action = (
                        "callback-complete"
                        if row.accepted_callback_id
                        else "cancel-complete"
                    )
                else:
                    action = "none"
                current = store.read(args.owner, operation_id)
                value.append(
                    {
                        "operation_id": operation_id,
                        "state": current.state,
                        "action": action,
                    }
                )
        else:
            operation_id = args.operation_id
            record = store.read(args.owner, operation_id)
            if args.command == "resume":
                result = _resume(
                    store,
                    args.owner,
                    operation_id,
                    process_adapter=process_adapter,
                    cmux_adapter=cmux_adapter,
                    review_runtime_manager=review_runtime_manager,
                )
            elif args.command in {"cancel", "close"}:
                result = _cancel_or_close(
                    store,
                    args.owner,
                    operation_id,
                    process_adapter=process_adapter,
                    cmux_adapter=cmux_adapter,
                )
            value = to_dict(result)
        _emit(value, json_mode=args.json)
        if args.command in {"resume", "cancel", "close", "reconcile"}:
            publish_status(
                args.store,
                trigger_owner=args.owner,
                trigger_operation=(
                    operation_id if args.command != "reconcile" else ""
                ),
            )
        return 0
    except (ContractError, RuntimeSessionError, StoreError) as exc:
        _emit({"status": "error", "reason": str(exc)}, json_mode=True)
        return 2
