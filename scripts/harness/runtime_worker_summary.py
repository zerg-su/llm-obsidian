"""Task-summary validation, pipeline advancement, and callback publication."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("task-summary",)

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from task_plan_authority import PlanAuthorityError, resolve_plan_authority

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import (
    _atomic_json,
    _pipeline_verify_effect_id,
    _pipeline_verify_identity,
    _review_resolution_handoff_ready,
)
from .artifact_repair import (
    ArtifactRepairError,
    ContractArtifactOwner,
    CorrectionBudgetExhausted,
    CorrectionNotificationUncertain,
)
from .contracts import CanonicalContractTemplate, ContractFamily
from .runtime_worker_verification import (
    _review_resolution_drift_in_flight,
    _verification_candidate_is_current,
)
from .verification_attempt import verification_input_sha256
from .fresh_artifact_repair import (
    FreshArtifactRepair,
    FreshRepairError,
    FreshRepairInvalid,
    launch_fresh_repair_for_worker,
)


def task_summary_contract_template(
    meta: Mapping[str, object], attempt_id: str
) -> CanonicalContractTemplate:
    """Derive the blank model-editable summary from dispatch authority."""

    version = meta.get("version")
    reap = meta.get("reap_policy")
    if version not in {3, 4} or not isinstance(reap, Mapping):
        raise RuntimeWorkerError("task-summary template requires current metadata")
    allowed = reap.get("allowed_types")
    title = reap.get("title")
    session = meta.get("origin_session")
    plan = meta.get("plan_file")
    mode = reap.get("mode")
    if (
        not isinstance(allowed, list)
        or len(allowed) != 1
        or not isinstance(allowed[0], str)
        or not allowed[0]
        or not isinstance(title, str)
        or not title
        or not isinstance(session, str)
        or not session
        or not isinstance(plan, str)
        or not plan
        or mode not in {"shared", "final"}
    ):
        raise RuntimeWorkerError("task-summary template authority is invalid")
    if version == 3:
        return CanonicalContractTemplate.create(
            ContractFamily.TASK_SUMMARY,
            attempt_id=attempt_id,
            target_pointer=".task-summary.json",
            value={
                "schema_version": 1,
                "type": allowed[0],
                "title": title,
                "session": session,
                "body": "",
            },
            code_owned_fields={"schema_version", "type", "session"},
            model_owned_fields={"title", "body"},
        )
    if mode == "final":
        try:
            worktree = Path(str(meta.get("worktree") or "")).expanduser().resolve()
            resolve_plan_authority(meta, worktree)
        except (OSError, PlanAuthorityError) as exc:
            raise RuntimeWorkerError(
                "task-summary outcome authority is invalid"
            ) from exc
    return CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id=attempt_id,
        target_pointer=".task-summary.json",
        value={
            "schema_version": 2,
            "type": allowed[0],
            "title": title,
            "session": session,
            "body": "",
            "outcome_disposition": "",
            "outcome_evidence_ids": [],
            "residual_gap_pointers": [],
        },
        code_owned_fields={"schema_version", "type", "session"},
        model_owned_fields={
            "title",
            "body",
            "outcome_disposition",
            "outcome_evidence_ids",
            "residual_gap_pointers",
        },
    )


@dataclass
class SummaryPipelineState:
    summary: dict[str, object]
    marker: dict[str, object] | None
    steps: tuple[object, ...]
    verify_step: object | None
    existing_verification: dict[str, object] | None


class RuntimeWorkerSummaryMixin:
    def mark_failed_task_summary_correction_runtime(self) -> None:
        owner = getattr(self, "task_summary_artifact_owner", None)
        if (
            self.provider_exited
            and not self.callback_handled
            and isinstance(owner, ContractArtifactOwner)
            and owner.has_sent_correction
        ):
            self.summary_attention(
                "wiki-summary-correction-session-dead",
                AttentionReason.ATTENTION_REQUIRED,
            )

    def publish_task_summary_contract(self) -> None:
        meta_path = self.spec["cwd"] / ".task-meta.json"
        if meta_path.is_symlink():
            raise RuntimeWorkerError("task-summary metadata cannot be a symlink")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError("task-summary metadata is unreadable") from exc
        if not isinstance(meta, dict):
            raise RuntimeWorkerError("task-summary metadata is invalid")
        template = task_summary_contract_template(
            meta, str(self.spec["operation_id"])
        )
        self.task_summary_artifact_owner = ContractArtifactOwner.publish(
            state_root=self.spec_path.parent,
            worktree=self.spec["cwd"],
            template=template,
            actual_target=self.spec["task_summary_pointer"],
        )

    def request_task_summary_correction(self, invalid_sha256: str) -> None:
        owner = getattr(self, "task_summary_artifact_owner", None)
        if not isinstance(owner, ContractArtifactOwner):
            self.summary_attention("wiki-summary-template-unavailable")
            return
        if owner.awaiting_semantic_edit(invalid_sha256):
            return
        try:
            reservation = owner.reserve_correction(invalid_sha256)
            owner.restore_template()
            message = (
                "The task summary was rejected before callback acceptance. "
                "Harness restored the exact identity-bound template in "
                ".task-summary.json. Edit that existing object in place; fill "
                "only the model-owned title, body, outcome_disposition, "
                "outcome_evidence_ids, and residual_gap_pointers fields. Keep "
                "schema_version, type, and session unchanged. This is the only "
                "same-session correction; do not relaunch work, review, or reap."
            )

            def send(wake: str) -> None:
                self.cmux_adapter.send(self.spec["surface_id"], wake)
                self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")

            owner.deliver_correction(
                reservation,
                message,
                send,
                fault_observer=getattr(self, "fault_observer", None),
            )
            self.summary_digest = ""
            self.summary_stable_reads = 0
        except CorrectionBudgetExhausted:
            try:
                launch_fresh_repair_for_worker(self, owner, invalid_sha256)
                self.summary_digest = ""
                self.summary_stable_reads = 0
            except FreshRepairError:
                self.summary_attention(
                    "wiki-summary-correction-exhausted",
                    AttentionReason.RETRY_EXHAUSTED,
                )
        except CorrectionNotificationUncertain:
            self.summary_attention(
                "wiki-summary-correction-notification-uncertain",
                AttentionReason.ATTENTION_REQUIRED,
            )
        except ArtifactRepairError:
            self.summary_attention("wiki-summary-template-invalid")

    def summary_is_stable(self, raw: bytes) -> bool:
        self.digest = hashlib.sha256(raw).hexdigest()
        if self.digest != self.summary_digest:
            self.summary_digest = self.digest
            self.summary_stable_reads = 1
            return False
        self.summary_stable_reads += 1
        return self.summary_stable_reads >= 2

    def load_summary_contract(self, raw: bytes) -> dict[str, object]:
        raw_summary = json.loads(raw)
        meta_path = self.spec["cwd"] / ".task-meta.json"
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(self.meta, dict) or self.meta.get("version") not in {3, 4}:
            raise RuntimeWorkerError("task summary requires v3 or v4 metadata")
        summary = validate_summary_for_task(
            raw_summary,
            self.meta,
            allow_missing_session=True,
            require_schema=True,
        )
        if (
            self.meta.get("task_id") != self.spec["operation_id"]
            or Path(str(self.meta.get("worktree") or "")).resolve() != self.spec["cwd"]
            or self.meta.get("task_surface") != self.spec["surface_id"]
        ):
            raise RuntimeWorkerError(
                "task summary metadata mismatches the runtime owner"
            )
        validate_handoff(self.meta, summary, str(self.meta.get("origin_session") or ""))
        self.review = task_review_status(
            self.meta,
            self.spec["cwd"],
            expected_vault=self.trusted_vault,
            expected_operation_id=self.spec["operation_id"],
        )
        self.operation = self.store.read(
            self.spec["owner_id"], self.spec["operation_id"]
        )
        return summary

    def load_review_marker(self) -> dict[str, object] | None:
        self.marker_path = self.spec_path.parent / "pipeline-review-start.json"
        if not self.marker_path.is_file() or self.marker_path.is_symlink():
            return None
        marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        if (
            marker.get("schema_version") != 1
            or marker.get("operation_id") != self.spec["operation_id"]
            or marker.get("definition_sha256") != self.pipeline.definition_sha256
            or marker.get("status") not in {"pending", "started"}
        ):
            raise RuntimeWorkerError("pipeline review launch receipt is invalid")
        return marker

    def bind_verification_contract(self, verify_step: object | None) -> None:
        self.verification_controller_receipt_path = (
            self.spec_path.parent / "pipeline-step-verify.json"
        )
        review_policy = self.meta.get("review_policy")
        if not isinstance(review_policy, dict):
            raise RuntimeWorkerError("task verification policy is unavailable")
        profiles = load_profiles(
            self.trusted_vault / "config" / "verification-profiles.toml"
        )
        profile_name = str(review_policy.get("verification_profile") or "")
        self.profile = profiles.get(profile_name)
        if self.profile is None or self.profile.sha256 != review_policy.get(
            "verification_profile_sha256"
        ):
            raise RuntimeWorkerError("task verification profile binding is stale")
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.verification_head = head_result.stdout.strip()
        if head_result.returncode or not re.fullmatch(
            "[0-9a-f]{40,64}", self.verification_head
        ):
            raise RuntimeWorkerError("pipeline product HEAD is unavailable")
        self.verification_step_schema_version = (
            verify_step.schema_version if verify_step is not None else 1
        )
        self._bind_verification_attempt(0)

    def _bind_verification_attempt(self, attempt_index: int) -> None:
        self.verification_attempt = VerificationAttempt(
            parent_operation_id=self.operation.spec.operation_id,
            profile=self.profile.name,
            profile_sha256=self.profile.sha256,
            exact_head_sha=self.verification_head,
            attempt_index=attempt_index,
        )
        self.verification_input_sha256 = verification_input_sha256(
            self.pipeline.definition_sha256,
            self.verification_head,
            self.profile.sha256,
            self.verification_step_schema_version,
        )
        self.verification_effect_id = _pipeline_verify_effect_id(
            self.verification_input_sha256, attempt_index
        )
        (
            self.verification_spec,
            self.verification_lane_id,
            self.verification_run_id,
        ) = _pipeline_verify_identity(
            self.operation.spec,
            definition_sha256=self.pipeline.definition_sha256,
            input_sha256=self.verification_input_sha256,
            profile=self.profile.name,
            attempt_index=attempt_index,
        )
        self.verification_root = (
            self.spec_path.parent
            / "pipeline-verification"
            / self.verification_spec.operation_id
        )
        self.verification_receipt_path = self.verification_root / "receipt.json"

    def handle_prior_failed_verification(
        self, previous: dict[str, object] | None
    ) -> bool:
        if previous is None or previous["status"] != "failed":
            return True
        self.reconcile_failed_verification_child(previous)
        if self._pipeline_name == "engineering/fix":
            return (
                True
                if previous["head_sha"] == self.verification_head
                else self.accept_fix_retry_resubmission(previous)
            )
        if previous["head_sha"] == self.verification_head:
            failed_attempt = self.verification_attempt_from_receipt(previous)
            allow_changed_head = (
                self.changed_head_resubmit_count()
                < MAX_PIPELINE_VERIFY_RESUBMITS
            )
            allow_same_head = failed_attempt.attempt_index == 0
            self.notify_verification_attention(
                previous,
                allow_resubmit=allow_changed_head,
                allow_same_head_retry=allow_same_head,
            )
            if not allow_same_head:
                self.summary_attention(
                    "pipeline-verification-same-head-retry-exhausted",
                    AttentionReason.RETRY_EXHAUSTED,
                )
                return False
            if not self.accept_same_head_verification_retry(previous):
                return False
            self._bind_verification_attempt(1)
            return True
        allow_resubmit = (
            self.changed_head_resubmit_count() < MAX_PIPELINE_VERIFY_RESUBMITS
        )
        self.notify_verification_attention(
            previous,
            allow_resubmit=allow_resubmit,
            allow_same_head_retry=(
                self.verification_attempt_from_receipt(previous).attempt_index
                == 0
            ),
        )
        if not allow_resubmit:
            self.summary_attention(
                "pipeline-verification-retry-exhausted",
                AttentionReason.RETRY_EXHAUSTED,
            )
            return False
        return self.accept_verification_resubmission(previous)

    def resolve_current_verification(
        self, verify_step: object | None
    ) -> tuple[dict[str, object] | None, bool]:
        existing = self.verification_receipt() if verify_step is not None else None
        if existing is not None:
            self.run_verification()
            existing = self.verification_receipt()
            if existing is not None and existing["status"] == "failed":
                if self._pipeline_name == "engineering/fix":
                    self.schedule_fix_retry(existing)
                else:
                    allow_resubmit = (
                        self.changed_head_resubmit_count()
                        < MAX_PIPELINE_VERIFY_RESUBMITS
                    )
                    self.notify_verification_attention(
                        existing,
                        allow_resubmit=allow_resubmit,
                        allow_same_head_retry=(
                            self.verification_attempt.attempt_index == 0
                        ),
                    )
                    if not allow_resubmit:
                        self.summary_attention(
                            "pipeline-verification-retry-exhausted",
                            AttentionReason.RETRY_EXHAUSTED,
                        )
                return None, True
        if existing is not None and existing["status"] == "complete":
            evidence = existing["evidence"]
            if (
                not isinstance(evidence, list)
                or self.verification_head != evidence[0]["head_sha"]
            ):
                self.summary_attention(
                    "pipeline-verification-head-drift",
                    AttentionReason.CONTRACT_DRIFT,
                )
                return None, True
            if not _verification_candidate_is_current(
                self.spec["cwd"], str(existing["head_sha"])
            ):
                # The receipt stays immutable evidence for its own exact
                # HEAD; a moved or dirty candidate can never consume it.  An
                # identity-exact resolution in flight only suppresses the
                # attention latch (the resolution machinery re-verifies at
                # the resolved HEAD); it never restores currency.
                if not _review_resolution_drift_in_flight(
                    self, str(existing["head_sha"])
                ):
                    self.summary_attention(
                        "pipeline-verification-stale-authority",
                        AttentionReason.CONTRACT_DRIFT,
                    )
                return None, True
        return existing, False

    def build_summary_pipeline_state(
        self, raw: bytes, *, summary: dict[str, object] | None = None
    ) -> SummaryPipelineState | None:
        summary = summary if summary is not None else self.load_summary_contract(raw)
        if (
            self.pipeline is None
            or self.operation.spec.contract_sha256 != self.pipeline.definition_sha256
        ):
            self.summary_attention(
                "pipeline-contract-drift", AttentionReason.CONTRACT_DRIFT
            )
            return None
        marker = self.load_review_marker()
        steps = self.pipeline.definition.steps
        primitive_shape = tuple(step.primitive_id for step in steps)
        supported_shapes = {
            ("model_step", "review"),
            ("model_step", "verify", "review"),
            (
                "model_step",
                "model_step",
                "model_step",
                "model_step",
                "verify",
                "review",
            ),
        }
        if not self.is_custom_pipeline and primitive_shape not in supported_shapes:
            raise RuntimeWorkerError(
                "compiled production pipeline shape is unsupported"
            )
        verify_step = next(
            (step for step in steps if step.primitive_id == "verify"), None
        )
        self.bind_verification_contract(verify_step)
        if verify_step is not None and (
            not self.adopt_invalidated_verification_successor()
        ):
            return None
        previous = (
            self.controller_verification_receipt() if verify_step is not None else None
        )
        if not self.handle_prior_failed_verification(previous):
            return None
        existing, halted = self.resolve_current_verification(verify_step)
        if halted:
            return None
        return SummaryPipelineState(summary, marker, steps, verify_step, existing)

    def advance_review_boundary(
        self, state: SummaryPipelineState, verification_complete: bool
    ) -> bool:
        if not verification_complete:
            return False
        gate_state = self.review_gate_state()
        awaiting_resolution = gate_state.get("awaiting_resolution")
        notification_evidence = gate_state.get("review_notification_evidence")
        raw_attempt = gate_state.get("attempt")
        terminal = (
            raw_attempt.get("terminal")
            if isinstance(raw_attempt, dict)
            else None
        )
        exact_attempt_resolution = (
            gate_state.get("status") == "changes-requested"
            and isinstance(raw_attempt, dict)
            and raw_attempt.get("status") == "terminal"
            and isinstance(terminal, dict)
            and terminal.get("result") == "changes-requested"
            and isinstance(notification_evidence, dict)
            and bool(notification_evidence)
        )
        if (
            gate_state.get("status") == "awaiting-resolution"
            or exact_attempt_resolution
        ):
            self.notify_review_resolution(gate_state)
            if self.review.status == "stale":
                if not _review_resolution_handoff_ready(
                    worktree=self.spec["cwd"],
                    operation_id=self.spec["operation_id"],
                    gate_state=gate_state,
                    current_head=self.verification_head,
                ):
                    return True
                if self.wait_for_summary_refresh_after_resolution(
                    gate_state, target_head=self.verification_head
                ):
                    return True
                self.drive_review()
                return True
            _atomic_json(
                self.marker_path,
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "definition_sha256": self.pipeline.definition_sha256,
                    "status": "started",
                    "drive_sha256": self.review_drive_sha256(),
                },
            )
            return True
        if state.marker is not None and self.review.status in {"reviewing", "stale"}:
            current_drive_sha256 = self.review_drive_sha256()
            if (
                state.marker["status"] == "pending"
                or state.marker.get("drive_sha256") != current_drive_sha256
            ):
                self.drive_review()
            return True
        if (
            state.marker is not None
            and self.review.status == "attention"
            and gate_state.get("status") == "attention-required"
        ):
            # The gate owns recovery classification for its own attention
            # boundary (for example a zero-effect attempt whose provider
            # session never started).  One code-owned re-drive per distinct
            # durable drive input lets the flow supersede or re-arm; an
            # unchanged input falls through to the real attention latch.
            current_drive_sha256 = self.review_drive_sha256()
            if (
                state.marker["status"] == "pending"
                or state.marker.get("drive_sha256") != current_drive_sha256
            ):
                self.drive_review()
                return True
        return False

    def advance_compiled_pipeline(self, state: SummaryPipelineState) -> bool:
        verification_complete = state.verify_step is None or (
            state.existing_verification is not None
            and state.existing_verification["status"] == "complete"
        )
        if self.advance_review_boundary(state, verification_complete):
            return False
        review_observation = {
            "missing": "pending",
            "reviewing": "running",
            "approved": "complete",
            "skipped": "complete",
            "attention": "attention",
            "stale": "attention",
        }[self.review.status]
        observations: dict[str, str] = {}
        for step in state.steps:
            if step.primitive_id == "model_step":
                observations[step.step_id] = "complete"
            elif step.primitive_id == "verify":
                observations[step.step_id] = (
                    "pending"
                    if state.existing_verification is None
                    else (
                        "complete"
                        if state.existing_verification["status"] == "complete"
                        else "attention"
                    )
                )
            else:
                observations[step.step_id] = (
                    review_observation if verification_complete else "pending"
                )
        progress = reconcile_pipeline(self.pipeline, observations)
        if progress.action == "start":
            step = next(row for row in state.steps if row.step_id == progress.step_id)
            if step.primitive_id == "verify":
                self.run_verification()
            elif state.marker is None or state.marker["status"] != "started":
                self.drive_review()
            return False
        if progress.action == "wait":
            return False
        if progress.action == "attention":
            if progress.step_id == (
                state.verify_step.step_id if state.verify_step else ""
            ):
                self.summary_attention(
                    "pipeline-verification-failed",
                    AttentionReason.ATTENTION_REQUIRED,
                )
            else:
                self.summary_attention(f"review-finalization-{self.review.status}")
            return False
        if progress.action != "reap-ready":
            raise RuntimeWorkerError(
                "compiled pipeline returned an invalid finalization action"
            )
        return not self.wait_for_summary_refresh_after_resolution(
            self.review_gate_state()
        )

    def publish_summary_callback(self, summary: dict[str, object]) -> None:
        encoded = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
        payload_sha256 = hashlib.sha256(encoded).hexdigest()
        generation = self.initial_generation
        envelope = CallbackEnvelope(
            callback_id=f"wiki-summary-{payload_sha256[:24]}",
            operation_id=self.spec["operation_id"],
            run_id=self.spec["run_id"],
            kind="wiki-summary",
            payload=summary,
            payload_sha256=payload_sha256,
        )
        acceptance = CallbackBroker(self.store, self.spec["owner_id"]).accept(envelope)
        self.record_provider_result(generation, payload_sha256)
        self.callback_handled = True
        emit_compiled_pipeline_event(
            self.spec["cwd"],
            event="terminal",
            pipeline_id=self.pipeline.definition.pipeline_id,
            pipeline_version=self.pipeline.definition.version,
            profile=self.pipeline.definition.profile,
            compiler_outcome=(
                "custom-resolved"
                if self.pipeline.definition.pipeline_id == "custom"
                else "resolved"
            ),
            definition_sha=self.pipeline.definition_sha256,
            primitive_count=len(self.pipeline.definition.steps),
            loop_iteration=0,
            terminal_category="complete",
        )
        _atomic_json(
            self.spec_path.parent / "callback-receipt.json",
            {
                "schema_version": 1,
                "callback_id": envelope.callback_id,
                "operation_id": envelope.operation_id,
                "run_id": envelope.run_id,
                "payload_sha256": envelope.payload_sha256,
                "status": "duplicate" if acceptance.duplicate else "accepted",
            },
        )
        self.notify_summary_reap(envelope)

    def notify_summary_reap(self, envelope: CallbackEnvelope) -> None:
        notify_path = self.spec_path.parent / "task-summary-notify.json"
        if notify_path.exists():
            marker = json.loads(notify_path.read_text(encoding="utf-8"))
            if (
                marker.get("schema_version") != 1
                or marker.get("callback_id") != envelope.callback_id
            ):
                raise RuntimeWorkerError("task summary notification marker is invalid")
            if marker.get("status") == "sent":
                return
            if marker.get("status") != "pending":
                raise RuntimeWorkerError(
                    "task summary notification marker state is invalid"
                )
            # A torn reap wake resumes once per generation: the wake
            # instructs the idempotent reap runner, so re-sending the exact
            # message converges. Live retries stay fail-closed.
            if not wake_resume_once(self, envelope.callback_id):
                self.mark_attention(AttentionReason.ATTENTION_REQUIRED)
                return
        vault_root = Path(str(self.meta.get("vault_root") or "")).resolve()
        reap_runner = vault_root / "scripts" / "reap-runner.py"
        if (
            not reap_runner.is_file()
            or reap_runner.is_symlink()
            or not (vault_root / "wiki").is_dir()
        ):
            raise RuntimeWorkerError("trusted reap runner is unavailable")
        command = shlex.join(
            [
                "python3",
                str(reap_runner),
                "--vault-root",
                str(vault_root),
                "--worktree",
                str(self.spec["cwd"]),
            ]
        )
        wake = (
            "Typed final task summary callback was accepted. "
            f"Run this exact command now: {command}"
        )
        if len(wake.encode()) > 4096:
            raise RuntimeWorkerError("task summary wake message is too large")
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "callback_id": envelope.callback_id,
                "status": "pending",
            },
        )
        self.cmux_adapter.send(self.spec["origin_surface"], wake)
        self.cmux_adapter.send_key(self.spec["origin_surface"], "Enter")
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "callback_id": envelope.callback_id,
                "status": "sent",
            },
        )

    def finish_task_summary(self, raw: bytes) -> None:
        owner = getattr(self, "task_summary_artifact_owner", None)
        if isinstance(owner, ContractArtifactOwner):
            try:
                fresh = FreshArtifactRepair.load(owner=owner)
                fresh_record = self.store.read(
                    self.spec["owner_id"],
                    str(fresh.reservation["operation_id"]),
                )
                meta = json.loads(
                    (self.spec["cwd"] / ".task-meta.json").read_text(
                        encoding="utf-8"
                    )
                )
                reconciliation = fresh.reconcile(
                    fresh_record,
                    lambda value: validate_summary_for_task(
                        value,
                        meta,
                        allow_missing_session=True,
                        require_schema=True,
                    )
                )
                if reconciliation.status == "adopted":
                    self.summary_digest = ""
                    self.summary_stable_reads = 0
                    return
                if reconciliation.status == "pending":
                    return
            except FreshRepairInvalid:
                self.summary_attention(
                    "wiki-summary-fresh-repair-invalid",
                    AttentionReason.RETRY_EXHAUSTED,
                )
                return
            except (FreshRepairError, StoreError, OSError, json.JSONDecodeError):
                pass
        if not self.summary_is_stable(raw):
            return
        if not isinstance(owner, ContractArtifactOwner):
            self.summary_attention("wiki-summary-template-unavailable")
            return
        try:
            repaired = owner.repair(authoritative_fields={})
        except ArtifactRepairError:
            self.request_task_summary_correction(hashlib.sha256(raw).hexdigest())
            return
        if repaired.changed:
            self.summary_digest = repaired.output_sha256
            self.summary_stable_reads = 0
            return
        try:
            summary = self.load_summary_contract(raw)
        except (WikiSummaryError, json.JSONDecodeError, TypeError, ValueError):
            self.request_task_summary_correction(repaired.output_sha256)
            return
        try:
            state = self.build_summary_pipeline_state(raw, summary=summary)
            if state is None or not self.advance_compiled_pipeline(state):
                return
            self.publish_summary_callback(state.summary)
        except (
            CallbackError,
            ContractError,
            RuntimeWorkerError,
            VerificationError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            self.summary_attention("wiki-summary-invalid")
