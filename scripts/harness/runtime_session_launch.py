"""Launch and callback-target lifecycle for one provider session."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from time import time
from typing import Callable

from .callbacks import CallbackBroker
from .liveness import LivenessController
from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
)
from .runtime_session_contracts import (
    IDENTIFIER,
    SURFACE_UUID,
    RuntimeSessionError,
    RuntimeSessionRequest,
    RuntimeSessionResult,
    SurfacePrepared,
    _relative,
    checkpointless_reviewer_route,
)
from .runtime_session_continuation import (
    await_surface_transport_ready,
    deliver_continuation,
)
from .runtime_callback_io import _bounded_file_sha256
from .runtime_provider_input import (
    bound_continuation_effect_id,
    initial_provider_argv,
    reserve_continuation_input,
)
from .store import StoreError
from .supervisor import OperationSupervisor


class RuntimeSessionLaunchMixin:
    """Own provider launch, continuation, and callback registration effects."""

    def _continuation_receipt_path(
        self, record: OperationRecord, effect_id: str
    ) -> Path:
        return (
            self._state_root(record)
            / "continuation-deliveries"
            / f"{effect_id}.json"
        )

    def _continuation_receipt(
        self,
        record: OperationRecord,
        effect_id: str,
        prompt: str,
        target: dict[str, object],
    ) -> tuple[Path, dict[str, object] | None, dict[str, object]]:
        identity = {
            "schema_version": 1,
            "effect_id": effect_id,
            "owner_id": record.spec.owner_id,
            "operation_id": record.spec.operation_id,
            "run_id": record.run_id,
            "lane_id": record.lane_id,
            "generation": int(target["generation"]),
            "callback_operation_id": str(target["operation_id"]),
            "callback_run_id": str(target["run_id"]),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
        path = self._continuation_receipt_path(record, effect_id)
        existing: dict[str, object] | None = None
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise RuntimeSessionError(
                    "continuation delivery receipt is not a regular file"
                )
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeSessionError(
                    "continuation delivery receipt is invalid"
                ) from exc
            if not isinstance(value, dict) or any(
                value.get(key) != expected for key, expected in identity.items()
            ):
                raise RuntimeSessionError(
                    "continuation delivery receipt identity changed"
                )
            existing = value
        return path, existing, identity

    def _continuation_artifact_ready(
        self,
        record: OperationRecord,
        target: dict[str, object],
    ) -> bool:
        operation_id = str(target["operation_id"])
        try:
            child = self.store.read(record.spec.owner_id, operation_id)
        except StoreError:
            return False
        return (
            child.spec.kind == "review-round"
            and child.run_id == str(target["run_id"])
            and child.lane_id == record.lane_id
            and child.state in {"verifying", "finalizing", "complete"}
            and bool(child.accepted_callback_id)
            and child.accepted_callback_kind == "review"
            and bool(child.accepted_callback_sha256)
        )

    def _continuation_ownership_ready(
        self,
        record: OperationRecord,
        target: dict[str, object],
    ) -> bool:
        """Revalidate exact parent resources and callback generation read-only."""

        try:
            current = self.store.read(
                record.spec.owner_id, record.spec.operation_id
            )
            current_target = self._callback_target(current)
            process_status = self.process.process_status(
                current.resources.process_group,
                current.resources.process_identity,
            )
            surface_status = self.cmux.status(current.resources.surface_id)
        except Exception:
            return False
        return (
            current.run_id == record.run_id
            and current.lane_id == record.lane_id
            and current.resources == record.resources
            and current.state in {"running", "awaiting-callback", "verifying"}
            and current_target == target
            and process_status == "alive"
            and surface_status == "alive"
        )

    def _begin_start(self, supervisor: OperationSupervisor) -> None:
        """Publish both durable pre-surface lifecycle boundaries."""

        supervisor.transition("preflight")
        self._notify(supervisor.owner_id, supervisor.operation_id)
        supervisor.transition("starting")
        self._notify(supervisor.owner_id, supervisor.operation_id)

    def _abort_prepared_surface(
        self,
        supervisor: OperationSupervisor,
        opened: object,
        placement: str,
    ) -> None:
        surface_id = str(getattr(opened, "surface_id", ""))
        try:
            if placement == "workspace":
                self.cmux.close_workspace_exact(
                    str(getattr(opened, "workspace_id", "")),
                    str(getattr(opened, "window_id", "")),
                )
            else:
                self.cmux.close_exact(surface_id)
        except Exception as exc:
            record = self._mark_attention(
                supervisor.read(), AttentionReason.CLEANUP_INCOMPLETE
            )
            raise RuntimeSessionError(
                f"prepared surface cleanup requires attention: {record.state}"
            ) from exc
        supervisor.bind_resources(OwnedResources())
        self.store.transition(
            supervisor.owner_id, supervisor.operation_id, "failed"
        )
        self._notify(supervisor.owner_id, supervisor.operation_id)

    def start(
        self,
        request: RuntimeSessionRequest,
        *,
        on_surface_opened: SurfacePrepared | None = None,
    ) -> RuntimeSessionResult:
        """Preflight, bind one exact surface, then launch one provider worker."""

        prompt_path = self._resolve_pointer(
            request.cwd, request.prompt_pointer, must_exist=True
        )
        callback_path = self._resolve_pointer(
            request.cwd, request.callback_pointer, must_exist=False
        )
        callback_outbox = request.cwd / request.callback_pointer
        if not callback_path.parent.is_dir():
            raise RuntimeSessionError("runtime callback directory is unavailable")
        try:
            existing = self.store.read(
                request.spec.owner_id, request.spec.operation_id
            )
        except StoreError:
            existing = None
        exact_replay = (
            existing is not None
            and existing.spec == request.spec
            and existing.lane_id == request.lane_id
            and existing.run_id == request.run_id
            and existing.state != "created"
        )
        callback_exists = callback_outbox.exists() or callback_outbox.is_symlink()
        if callback_exists and (
            not exact_replay
            or callback_outbox.is_symlink()
            or not callback_outbox.is_file()
        ):
            raise RuntimeSessionError(
                "runtime callback pointer must be a fresh owned outbox"
            )
        task_summary_path = (
            self._resolve_pointer(
                request.cwd, request.task_summary_pointer, must_exist=False
            )
            if request.callback_mode == "task-summary"
            else None
        )
        task_summary_source = (
            request.cwd / request.task_summary_pointer
            if task_summary_path is not None
            else None
        )
        if task_summary_source is not None and (
            task_summary_source.exists() or task_summary_source.is_symlink()
        ):
            if (
                not exact_replay
                or task_summary_source.is_symlink()
                or not task_summary_source.is_file()
            ):
                raise RuntimeSessionError(
                    "task summary source must be a fresh owned handoff"
                )
        prompt = self._read_prompt(prompt_path)
        driver = self._driver(request.spec.route)
        argv, deferred_initial_input = initial_provider_argv(
            driver,
            request,
            callback_path=callback_path,
            prompt=prompt,
        )
        report = self.check_route(
            request.spec.route,
            callback_path.parent,
            origin_surface=request.origin_surface,
        )
        if not report.compatible:
            reason = report.reason.value if report.reason else "capability-mismatch"
            raise RuntimeSessionError(f"runtime preflight failed: {reason}")

        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        if record.state != "created":
            metadata = self._metadata(record)
            expected = {
                "cwd": str(request.cwd),
                "origin_surface": request.origin_surface,
                "prompt_pointer": request.prompt_pointer,
                "callback_pointer": request.callback_pointer,
                "placement": request.placement,
                "checkpoint": request.checkpoint,
                "product_root": (
                    str(request.product_root) if request.product_root else ""
                ),
                "callback_mode": request.callback_mode,
                "task_summary_pointer": request.task_summary_pointer,
                "initial_callback_operation_id": (
                    request.initial_callback_operation_id
                ),
                "initial_callback_run_id": (
                    request.initial_callback_run_id
                ),
                "runtime_home": (
                    str(request.runtime_home) if request.runtime_home else ""
                ),
                "research_request_sha256": request.research_request_sha256,
                "callback_wake": request.callback_wake,
                "attempt_limit": request.attempt_limit,
                "model_restart_limit": request.model_restart_limit,
                "time_budget_seconds": request.time_budget_seconds,
                "token_limit": request.token_limit,
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise RuntimeSessionError(
                    "idempotent start request changed runtime session identity"
                )
            observed = self.status(
                request.spec.owner_id, request.spec.operation_id
            )
            return (
                replace(observed, action="already-started")
                if observed.action == "observed"
                else observed
            )
        supervisor = OperationSupervisor(
            self.store, request.spec.owner_id, request.spec.operation_id
        )
        try:
            record = supervisor.configure_budget(
                attempt_limit=request.attempt_limit,
                model_restart_limit=request.model_restart_limit,
                time_budget_seconds=request.time_budget_seconds,
                token_limit=request.token_limit,
            )
            record = supervisor.consume_attempt()
        except Exception as exc:
            raise RuntimeSessionError(
                "runtime operation budget requires attention"
            ) from exc
        self._write_metadata(record, request)
        initial_operation_id = request.spec.operation_id
        initial_run_id = record.run_id
        if request.initial_callback_operation_id:
            child = self.store.read(
                request.spec.owner_id,
                request.initial_callback_operation_id,
            )
            if (
                child.run_id != request.initial_callback_run_id
                or child.lane_id != record.lane_id
                or child.state != "awaiting-callback"
            ):
                raise RuntimeSessionError(
                    "initial callback target must be the exact awaiting "
                    "same-lane child"
                )
            initial_operation_id = child.spec.operation_id
            initial_run_id = child.run_id
        self._write_callback_target(
            record,
            operation_id=initial_operation_id,
            run_id=initial_run_id,
            callback_pointer=request.callback_pointer,
            generation=1,
        )
        self._begin_start(supervisor)
        def bind_surface(_record: OperationRecord, opened: object) -> None:
            surface_id = str(getattr(opened, "surface_id", ""))
            if not SURFACE_UUID.fullmatch(surface_id):
                raise RuntimeSessionError("cmux returned no exact owned surface")
            supervisor.bind_resources(OwnedResources(surface_id=surface_id))

        try:
            opened = supervisor.effect(
                "open-surface",
                lambda _record: (
                    self.cmux.open_split(request.origin_surface)
                    if request.placement == "split"
                    else self.cmux.open_workspace(
                        request.origin_surface, cwd=request.cwd
                    )
                ),
                persist_result=bind_surface,
            ).value
        except Exception as exc:
            current = self._mark_attention(
                supervisor.read(), AttentionReason.SURFACE_OPEN_FAILED
            )
            raise RuntimeSessionError(
                f"surface open requires attention: {current.state}"
            ) from exc
        surface_id = str(getattr(opened, "surface_id", ""))
        self._write_surface_metadata(supervisor.read(), opened)

        if on_surface_opened is not None:
            try:
                on_surface_opened(self._result(supervisor.read(), "surface-opened"))
            except Exception as exc:
                self._abort_prepared_surface(
                    supervisor, opened, request.placement
                )
                raise RuntimeSessionError(
                    "surface preparation failed before provider launch"
                ) from exc

        try:
            launch = self.process.prepare_surface_launch(
                argv=argv,
                cwd=request.cwd,
                state_root=self._state_root(supervisor.read()),
                worker=self.worker,
                callback_pointer=callback_path, product_root=request.product_root,
                reviewer_sandbox=request.spec.route.profile == "reviewer-callback",
                callback_registration=self._callback_target_path(supervisor.read()),
                store_root=self.store.root,
                owner_id=request.spec.owner_id,
                operation_id=request.spec.operation_id,
                run_id=request.run_id,
                surface_id=surface_id,
                runtime=request.spec.route.runtime,
                callback_mode=request.callback_mode,
                task_summary_pointer=task_summary_path,
                origin_surface=request.origin_surface,
                runtime_home=request.runtime_home,
                research_request_sha256=request.research_request_sha256,
                callback_wake=request.callback_wake,
                initial_input_pointer=(
                    prompt_path if deferred_initial_input else None
                ),
            )
        except Exception as exc:
            self._abort_prepared_surface(
                supervisor, opened, request.placement
            )
            raise RuntimeSessionError("provider worker preparation failed") from exc

        def start_provider(_record: OperationRecord) -> object:
            if not await_surface_transport_ready(
                self.cmux,
                surface_id=surface_id,
            ):
                raise RuntimeSessionError(
                    "surface terminal did not become ready"
                )
            self.cmux.send(surface_id, str(getattr(launch, "command")))
            self.cmux.send_key(surface_id, "Enter")
            return self.process.await_surface_handle(
                launch, timeout_seconds=self.start_timeout_seconds
            )

        def bind_process(_record: OperationRecord, handle: object) -> None:
            pid = int(getattr(handle, "pid", 0))
            pgid = int(getattr(handle, "process_group", 0))
            supervisor_pid = int(getattr(handle, "supervisor_pid", 0)) or pid
            process_identity = str(
                getattr(handle, "process_identity", "")
            )
            supervisor_identity = str(
                getattr(handle, "supervisor_identity", "")
            )
            if (
                pid <= 1
                or pgid <= 1
                or pid != pgid
                or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
                or not re.fullmatch(r"[0-9a-f]{64}", supervisor_identity)
            ):
                raise RuntimeSessionError("provider worker returned invalid ownership")
            supervisor.bind_resources(
                OwnedResources(
                    surface_id=surface_id,
                    process_group=pgid,
                    supervisor_pid=supervisor_pid,
                    process_identity=process_identity,
                    supervisor_identity=supervisor_identity,
                )
            )

        try:
            supervisor.effect(
                "start-provider",
                start_provider,
                persist_result=bind_process,
            )
        except Exception as exc:
            current = self._mark_attention(
                supervisor.read(), AttentionReason.PROCESS_START_FAILED
            )
            raise RuntimeSessionError(
                f"provider start requires attention: {current.state}"
            ) from exc
        supervisor.transition("running")
        record = supervisor.transition("awaiting-callback")
        self._notify(record.spec.owner_id, record.spec.operation_id)
        checkpoint = ""
        try:
            checkpoint = self.cmux.resume_checkpoint(
                surface_id, record.spec.route.runtime
            )
        except Exception:
            pass
        return self._result(record, "started", checkpoint=checkpoint)

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> RuntimeSessionResult:
        """Send one bounded prompt to the exact existing provider session."""

        record = self.store.read(owner_id, operation_id)
        checkpointless_claude = (
            not checkpoint
            and checkpointless_reviewer_route(record.spec.route)
        )
        if not checkpointless_claude and (
            not checkpoint or not IDENTIFIER.fullmatch(checkpoint)
        ):
            raise RuntimeSessionError(
                "same-session continuation needs a checkpoint"
            )
        if record.state not in {"running", "awaiting-callback", "verifying"}:
            raise RuntimeSessionError("operation cannot continue from its current state")
        if not record.resources.surface_id or record.resources.process_group <= 1:
            raise RuntimeSessionError("operation has no exact live ownership")
        if checkpointless_claude:
            observed = self.status(owner_id, operation_id)
            if (
                observed.action != "observed"
                or observed.record != record
                or observed.process_status != "alive"
                or observed.surface_status != "alive"
                or observed.checkpoint
            ):
                raise RuntimeSessionError(
                    "checkpointless Claude continuation is not exactly live"
                )
        else:
            actual = self.cmux.resume_checkpoint(
                record.resources.surface_id, record.spec.route.runtime
            )
            if actual != checkpoint:
                raise RuntimeSessionError(
                    "same-session checkpoint identity changed"
                )
        metadata = self._metadata(record)
        cwd = Path(str(metadata.get("cwd") or "")).resolve()
        prompt_path = self._resolve_pointer(cwd, prompt_pointer, must_exist=True)
        prompt = self._read_prompt(prompt_path)
        target = self._callback_target(record)
        effect_id = bound_continuation_effect_id(record, prompt, target)
        receipt_path, receipt, receipt_identity = self._continuation_receipt(
            record, effect_id, prompt, target
        )
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        current = supervisor.read()
        already_acknowledged = bool(
            receipt and receipt.get("status") == "acknowledged"
        )
        effect_succeeded = (
            current.effect_id == effect_id
            and current.effect_outcome == EffectOutcome.SUCCEEDED
        )
        effect_pending = (
            current.effect_id == effect_id
            and current.effect_outcome == EffectOutcome.PENDING
        )
        if (
            not effect_succeeded
            and not effect_pending
            and not already_acknowledged
        ):
            time_budget_seconds = metadata.get("time_budget_seconds")
            if (
                not isinstance(time_budget_seconds, (int, float))
                or isinstance(time_budget_seconds, bool)
                or time_budget_seconds <= 0
            ):
                raise RuntimeSessionError(
                    "same-session continuation has no valid time budget"
                )
            supervisor.begin_continuation(
                time_budget_seconds=float(time_budget_seconds)
            )

        if receipt and receipt.get("status") == "unconfirmed":
            current = self._mark_attention(
                supervisor.read(),
                AttentionReason.CONTINUATION_SUBMIT_UNCONFIRMED,
            )
            raise RuntimeSessionError(
                f"continuation submit requires attention: {current.state}"
            )

        if not already_acknowledged:
            delivery = reserve_continuation_input(
                self._state_root(record) / "provider-events",
                record=record,
                target=target,
                workspace_id=str(metadata.get("workspace_id") or ""),
                prompt=prompt,
                attention_state=lambda: self._mark_attention(
                    supervisor.read(),
                    AttentionReason.CONTINUATION_SUBMIT_UNCONFIRMED,
                ).state,
            )
            if receipt is None:
                self._write_json(
                    receipt_path,
                    {
                        **receipt_identity,
                        "status": "paste-reserved",
                        "submit_count": 0,
                    },
                )
            send_prompt = not effect_succeeded and not (
                receipt
                and receipt.get("status")
                in {
                    "paste-reserved",
                    "transport-accepted",
                    "submit-reserved",
                    "submit-accepted",
                    "submit-retry-reserved",
                    "submit-retried",
                }
            )
            prior_submit_accepted = bool(
                receipt
                and receipt.get("status")
                in {
                    "submit-reserved",
                    "submit-accepted",
                    "submit-retry-reserved",
                    "submit-retried",
                }
            )
            prior_submit_count = (
                int(receipt.get("submit_count") or 0)
                if prior_submit_accepted
                else 0
            )
            prior_pre_send_screen_sha256 = str(
                receipt.get("pre_send_screen_sha256") or ""
            ) if receipt else ""
            prior_pre_send_editor_sha256 = str(
                receipt.get("pre_send_editor_sha256") or ""
            ) if receipt else ""
            prior_paste_screen_sha256 = str(
                receipt.get("paste_screen_sha256") or ""
            ) if receipt else ""
            liveness = LivenessController(
                self._state_root(record) / "liveness"
            )
            target_sha256 = _bounded_file_sha256(
                self._callback_target_path(record)
            )
            if not target_sha256:
                raise RuntimeSessionError(
                    "continuation callback target digest is unavailable"
                )
            retry_identity = {
                "operation_id": str(target["operation_id"]),
                "run_id": str(target["run_id"]),
                "lane_id": record.lane_id,
                "generation": int(target["generation"]),
                "target_sha256": target_sha256,
                "expected_operation_id": str(target["operation_id"]),
                "expected_run_id": str(target["run_id"]),
                "expected_lane_id": record.lane_id,
                "expected_generation": int(target["generation"]),
                "expected_target_sha256": target_sha256,
            }
            retry_binding = hashlib.sha256(
                json.dumps(
                    retry_identity,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()

            def reserve_retry() -> bool:
                try:
                    return liveness.reserve_callback_submit(
                        retry_binding, retry_identity
                    )
                except ContractError:
                    return False

            def observe_stage(
                status: str,
                submit_count: int,
                pre_send_screen_sha256: str,
                pre_send_editor_sha256: str,
                paste_screen_sha256: str,
            ) -> None:
                if status == "submit-retried":
                    # Publish the exact generation effect before the more
                    # advanced delivery receipt. A crash can therefore leave
                    # a replayable reserved receipt, never a submit-retried
                    # receipt paired with a still-reserved liveness binding.
                    liveness.mark_callback_submit_sent(retry_binding)
                self._write_json(
                    receipt_path,
                    {
                        **receipt_identity,
                        "status": status,
                        "submit_count": submit_count,
                        "pre_send_screen_sha256": pre_send_screen_sha256,
                        "pre_send_editor_sha256": pre_send_editor_sha256,
                        "paste_screen_sha256": paste_screen_sha256,
                    },
                )

            class ContinuationUnconfirmed(RuntimeError):
                pass

            def deliver(_record: OperationRecord) -> None:
                result = deliver_continuation(
                    self.cmux,
                    surface_id=record.resources.surface_id,
                    prompt=prompt,
                    runtime=record.spec.route.runtime,
                    artifact_ready=lambda: self._continuation_artifact_ready(
                        record, target
                    ),
                    ownership_ready=lambda: self._continuation_ownership_ready(
                        record, target
                    ),
                    reserve_retry=reserve_retry,
                    observe_stage=observe_stage,
                    send_prompt=send_prompt,
                    submit_already_accepted=prior_submit_accepted,
                    accepted_submit_count=prior_submit_count,
                    pre_send_screen_sha256=prior_pre_send_screen_sha256,
                    pre_send_editor_sha256=prior_pre_send_editor_sha256,
                    paste_screen_sha256=prior_paste_screen_sha256,
                )
                if (
                    result.acknowledged
                    and receipt
                    and receipt.get("status") == "submit-retry-reserved"
                ):
                    try:
                        liveness.mark_callback_submit_sent(retry_binding)
                    except ContractError as exc:
                        raise ContinuationUnconfirmed(
                            "submit-retry-receipt-unconfirmed"
                        ) from exc
                status = "acknowledged" if result.acknowledged else "unconfirmed"
                self._write_json(
                    receipt_path,
                    {
                        **receipt_identity,
                        "status": status,
                        "evidence": result.evidence,
                        "submit_count": result.submit_count,
                    },
                )
                if not result.acknowledged:
                    delivery.ambiguous()
                    raise ContinuationUnconfirmed(result.evidence)
                delivery.accepted()

            try:
                if effect_succeeded:
                    deliver(supervisor.read())
                else:
                    supervisor.effect(
                        effect_id,
                        deliver,
                        resume_pending=(
                            supervisor.read().pending_effect == effect_id
                        ),
                    )
            except ContinuationUnconfirmed as exc:
                pending = supervisor.read()
                if pending.pending_effect == effect_id:
                    self.store.resolve_effect(
                        owner_id, operation_id, EffectOutcome.FAILED
                    )
                current = self._mark_attention(
                    supervisor.read(),
                    AttentionReason.CONTINUATION_SUBMIT_UNCONFIRMED,
                )
                raise RuntimeSessionError(
                    f"continuation submit requires attention: {exc}"
                ) from exc
            except Exception:
                try:
                    delivery.ambiguous()
                except Exception:
                    pass
                raise
        current = supervisor.read()
        if current.state != "running":
            current = supervisor.transition("running")
        self._notify(current.spec.owner_id, current.spec.operation_id)
        return self._result(current, "continued", checkpoint=checkpoint)

    def rearm_callback_timeout(
        self,
        owner_id: str,
        operation_id: str,
    ) -> RuntimeSessionResult:
        """Atomically restore a proven accepted reviewer callback boundary."""

        record = self.store.read(owner_id, operation_id)
        metadata = self._metadata(record)
        time_budget_seconds = metadata.get("time_budget_seconds")
        if (
            not isinstance(time_budget_seconds, (int, float))
            or isinstance(time_budget_seconds, bool)
            or time_budget_seconds <= 0
        ):
            raise RuntimeSessionError(
                "callback timeout rearm has no valid time budget"
            )
        updated = self.store.rearm_callback_timeout(
            owner_id,
            operation_id,
            deadline_at=time() + float(time_budget_seconds),
        )
        self._notify(owner_id, operation_id)
        return self._result(updated, "callback-timeout-rearmed")

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        callback_operation_id: str,
        callback_run_id: str,
        callback_pointer: str,
    ) -> RuntimeSessionResult:
        """Atomically retarget the live worker to one same-lane child receipt."""

        parent = self.store.read(owner_id, parent_operation_id)
        child = self.store.read(owner_id, callback_operation_id)
        if (
            child.run_id != callback_run_id
            or child.lane_id != parent.lane_id
            or child.state != "awaiting-callback"
        ):
            raise RuntimeSessionError(
                "callback target must be the exact awaiting same-lane child"
            )
        metadata = self._metadata(parent)
        cwd = Path(str(metadata.get("cwd") or "")).resolve()
        pointer_path = self._resolve_pointer(
            cwd, callback_pointer, must_exist=False
        )
        if (
            not pointer_path.parent.is_dir()
            or not os.access(pointer_path.parent, os.W_OK)
        ):
            raise RuntimeSessionError("callback target directory is unavailable")
        normalized = _relative(callback_pointer, "callback_pointer")
        with self.store.locked(owner_id):
            current = self._callback_target(parent)
            if (
                current.get("operation_id") == callback_operation_id
                and current.get("run_id") == callback_run_id
                and current.get("callback_pointer") == normalized
            ):
                return self._result(parent, "callback-target-unchanged")
            if (
                parent.spec.route.runtime == "claude"
                and parent.spec.route.profile == "reviewer-callback"
                and normalized != self._metadata(parent).get("callback_pointer")
            ):
                raise RuntimeSessionError(
                    "Claude callback target must reuse its exact allowed outbox"
                )
            if pointer_path.exists() or pointer_path.is_symlink():
                if (
                    normalized != current.get("callback_pointer")
                    or pointer_path.is_symlink()
                    or not pointer_path.is_file()
                ):
                    raise RuntimeSessionError(
                        "callback target is not a reusable owned outbox"
                    )
                pointer_path.unlink()
            self._write_callback_target(
                parent,
                operation_id=callback_operation_id,
                run_id=callback_run_id,
                callback_pointer=normalized,
                generation=int(current["generation"]) + 1,
            )
        self._notify(parent.spec.owner_id, parent.spec.operation_id)
        return self._result(parent, "callback-target-registered")

    def continue_same_session_round(
        self,
        owner_id: str,
        parent_operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
        callback_operation_id: str,
        callback_run_id: str,
        callback_pointer: str,
    ) -> RuntimeSessionResult:
        """Retarget one child, send one prompt, then await its callback.

        The child owns only the typed result receipt. Provider and cmux
        resources remain anchored to the persistent parent operation.
        """

        self.register_callback_target(
            owner_id,
            parent_operation_id,
            callback_operation_id,
            callback_run_id,
            callback_pointer,
        )
        self.continue_session(
            owner_id,
            parent_operation_id,
            checkpoint,
            prompt_pointer,
        )
        parent = self.store.read(owner_id, parent_operation_id)
        if parent.state == "running":
            self.store.transition(
                owner_id,
                parent_operation_id,
                "awaiting-callback",
            )
            parent = self.store.read(owner_id, parent_operation_id)
        elif parent.state != "awaiting-callback":
            raise RuntimeSessionError(
                "same-session round cannot await its callback"
            )
        self._notify(owner_id, parent_operation_id)
        return self._result(
            parent,
            "round-continued",
            checkpoint=checkpoint,
        )

    def accept_callback(
        self, envelope: CallbackEnvelope
    ) -> RuntimeSessionResult:
        # Operation ids are owner-scoped; resolve the only exact durable match
        # rather than accepting a caller-supplied ownership guess.
        owner_id = self._owner_for_operation(envelope.operation_id)
        record = self.store.read(owner_id, envelope.operation_id)
        deadline_operation_id = ""
        if (
            envelope.kind == "review"
            and record.spec.kind == "review-round"
        ):
            parent = envelope.payload.get(
                "parent_session_operation_id"
            )
            if (
                not isinstance(parent, str)
                or not IDENTIFIER.fullmatch(parent)
            ):
                raise RuntimeSessionError(
                    "review round callback has no exact parent session"
                )
            deadline_operation_id = parent
        acceptance = CallbackBroker(
            self.store, record.spec.owner_id
        ).accept(
            envelope,
            deadline_operation_id=deadline_operation_id,
        )
        updated = self.store.read(record.spec.owner_id, envelope.operation_id)
        action = "callback-duplicate" if acceptance.duplicate else "callback-accepted"
        self._notify(record.spec.owner_id, record.spec.operation_id)
        return self._result(updated, action)
