"""Public harness CLI; diagnostics work without a model session."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
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
from .reconciliation import ReconcileDecision, reconcile
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
            print(f"{row['operation_id']}\t{row['state']}\t{row['kind']}")
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
    return store.transition(owner, operation_id, "cancelled")


def _resume(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
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
    if initial.state == "attention-required":
        if not initial.resume_state:
            return store.transition(owner, operation_id, initial.state)
        store.transition(owner, operation_id, initial.resume_state)
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
                            action = "cancel-complete"
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
                    action = "cancel-complete"
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
            publish_status(args.store, terminal_owner=args.owner)
        return 0
    except (ContractError, RuntimeSessionError, StoreError) as exc:
        _emit({"status": "error", "reason": str(exc)}, json_mode=True)
        return 2
