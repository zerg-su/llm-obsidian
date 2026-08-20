"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("verification-escalation",)
from .artifact_repair import (
    publish_verification_escalation,
    verification_gap_resolution_authorizes,
    verification_resolution_authorizes,
)
from .runtime_worker import *
from .runtime_worker import (
    _atomic_json,
    _bounded_file_sha256,
    _callback_target,
    _contain_provider_start_failure,
    _current_callback_receipt_sha256,
    _envelope,
    _normalize_fetch_errors_at_provider_boundary,
    _pipeline_verify_identity,
    _research_input_provenance,
    _review_resolution_handoff_ready,
    _submit_failure_requires_attention,
)
from .contracts import OwnedResources
from .store import StoreError
from .verification import (
    _authority_path_is_safe,
    VerificationAuthority,
    VerificationAuthorityError,
)
from .verification_attempt import MAX_SAME_HEAD_ATTEMPT_INDEX
from .runtime_session_continuation import paste_editor_text


def _verification_candidate_is_current(cwd: Path, expected_head_sha: str) -> bool:
    """Re-observe the exact current HEAD and full tree cleanliness.

    Verification authority is consumed (controller linking, summary
    acceptance, review drive) only against a fresh bracketed observation:
    the exact expected HEAD both before and after an empty tracked-and-
    untracked tree observation.  Re-reading the HEAD after the tree
    observation closes the schedule where a clean concurrent commit lands
    between the two reads, so a moved candidate invalidates the whole
    observation.  An unavailable observation is never currency.
    """

    if not expected_head_sha:
        return False

    def observed_head() -> str:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        return "" if head.returncode else head.stdout.strip()

    if observed_head() != expected_head_sha:
        return False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode or status.stdout.strip():
        return False
    return observed_head() == expected_head_sha


def _review_resolution_drift_in_flight(
    worker: object, stale_head_sha: str
) -> bool:
    """One identity-exact resolution notification names this stale HEAD.

    While the executor commits a review resolution, the product tree
    legitimately moves past the reviewed HEAD; that drift is owned
    end-to-end by the existing resolution machinery
    (`_resolved_head_verification_ready` and the summary refresh path).
    This marker is wait-only: callers may suppress a typed attention latch
    over exactly the notified reviewed HEAD, but the marker never
    authorizes linking, consuming, or review-releasing stale authority.
    Anything short of the exact sent, own-operation, packet-bound
    notification for exactly this stale HEAD is not a resolution in flight.
    """

    if not re.fullmatch("[0-9a-f]{40,64}", str(stale_head_sha or "")):
        return False
    notify_path = (
        worker.spec_path.parent / "pipeline-review-resolution-notify.json"
    )
    if notify_path.is_symlink() or not notify_path.is_file():
        return False
    try:
        notified = json.loads(notify_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(notified, dict)
        and notified.get("schema_version") == 1
        and notified.get("operation_id") == worker.spec["operation_id"]
        and notified.get("status") == "sent"
        and bool(
            re.fullmatch(
                "[0-9a-f]{64}", str(notified.get("packet_sha256") or "")
            )
        )
        and notified.get("reviewed_head_sha") == stale_head_sha
    )


def _orphaned_predecessor_lineage(worker: object) -> bool:
    """Read-only census of one bounded attempt lineage.

    A missing bound-attempt record classifies as a fresh run only when the
    entire deterministic identity space is empty: no successor record and no
    receipt, response, or invalidation trace for either bounded identity.
    Any surviving trace proves the predecessor record was lost rather than
    never created, so the lineage is orphaned and must stay typed attention
    instead of minting a replacement attempt.
    """

    try:
        worker.store.read(
            worker.spec["owner_id"], worker.verification_spec.operation_id
        )
    except StoreError:
        pass
    else:
        return False
    verification_root = worker.spec_path.parent / "pipeline-verification"
    evidence_roots = [verification_root / worker.verification_spec.operation_id]
    attempt_index = worker.verification_attempt.attempt_index
    if attempt_index < MAX_SAME_HEAD_ATTEMPT_INDEX:
        successor_spec, _successor_lane, _successor_run = _pipeline_verify_identity(
            worker.operation.spec,
            definition_sha256=worker.pipeline.definition_sha256,
            input_sha256=worker.verification_input_sha256,
            profile=worker.profile.name,
            attempt_index=attempt_index + 1,
        )
        try:
            worker.store.read(worker.spec["owner_id"], successor_spec.operation_id)
        except StoreError:
            pass
        else:
            return True
        evidence_roots.append(verification_root / successor_spec.operation_id)
    return any(
        trace.is_symlink() or trace.exists()
        for evidence_root in evidence_roots
        for trace in (
            evidence_root / "invalidation.json",
            evidence_root / "receipt.json",
            evidence_root / "response-receipt.json",
        )
    )


class RuntimeWorkerVerificationMixin:

    def verification_attempt_from_receipt(
        self, receipt: dict[str, object]
    ) -> VerificationAttempt:
        try:
            attempt = VerificationAuthority.attempt_from(receipt)
        except VerificationAuthorityError as exc:
            raise RuntimeWorkerError(
                "pipeline verification attempt identity is invalid"
            ) from exc
        if (
            attempt.parent_operation_id != self.spec["operation_id"]
            or attempt.profile != self.profile.name
            or attempt.profile_sha256 != self.profile.sha256
            or attempt.exact_head_sha != receipt.get("head_sha")
        ):
            raise RuntimeWorkerError(
                "pipeline verification attempt identity is invalid"
            )
        return attempt

    def load_verification_receipt(self, receipt_path: Path) -> dict[str, object] | None:
        if not receipt_path.exists():
            return None
        expected_command_ids = [
            f"{self.profile.name}-{index + 1}"
            for index in range(
                len(compose_commands(self.profile, self.pipeline_extra_commands))
            )
        ]
        try:
            parent = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            authority = VerificationAuthority.load(
                receipt_path,
                store=self.store,
                parent=parent,
                runtime_root=self.spec_path.parent,
                expected_definition_sha256=self.pipeline.definition_sha256,
                expected_profile=self.profile.name,
                expected_profile_sha256=self.profile.sha256,
                expected_command_ids=expected_command_ids,
            )
        except VerificationAuthorityError as exc:
            raise RuntimeWorkerError(
                "pipeline verification receipt is invalid"
            ) from exc
        return authority.to_dict()

    def controller_verification_receipt(self) -> dict[str, object] | None:
        linked: dict[str, object] | None = None
        if self.verification_controller_receipt_path.exists():
            if (
                not self.verification_controller_receipt_path.is_file()
                or self.verification_controller_receipt_path.is_symlink()
            ):
                raise RuntimeWorkerError(
                    "pipeline verification controller receipt is invalid"
                )
            raw_linked = json.loads(
                self.verification_controller_receipt_path.read_text(encoding="utf-8")
            )
            if not isinstance(raw_linked, dict):
                raise RuntimeWorkerError(
                    "pipeline verification controller receipt is invalid"
                )
            linked_operation_id = str(raw_linked.get("operation_id") or "")
            if not IDENTIFIER.fullmatch(linked_operation_id):
                raise RuntimeWorkerError(
                    "pipeline verification controller receipt is invalid"
                )
            child_path = (
                self.spec_path.parent
                / "pipeline-verification"
                / linked_operation_id
                / "receipt.json"
            )
            linked = self.load_verification_receipt(child_path)
            if linked != raw_linked:
                raise RuntimeWorkerError(
                    "pipeline verification controller linkage is invalid"
                )
        receipts_root = self.spec_path.parent / "pipeline-verification"
        receipts = (
            [
                receipt
                for path in receipts_root.glob("*/receipt.json")
                if (receipt := self.load_verification_receipt(path)) is not None
            ]
            if receipts_root.is_dir()
            else []
        )
        unresolved_failures = [
            receipt
            for receipt in receipts
            if receipt["status"] == "failed"
            and (not self.verification_response_accepted(receipt))
        ]
        if len(unresolved_failures) > 1:
            raise RuntimeWorkerError(
                "multiple failed verification children need reconciliation"
            )
        if unresolved_failures:
            recovered = unresolved_failures[0]
            if recovered != linked:
                self.link_verification_receipt(recovered)
            return recovered
        current_receipts = [
            receipt
            for receipt in receipts
            if receipt["operation_id"] == self.verification_spec.operation_id
        ]
        if len(current_receipts) > 1:
            raise RuntimeWorkerError(
                "duplicate verification child receipts are invalid"
            )
        if current_receipts:
            recovered = current_receipts[0]
            if recovered != linked:
                self.link_verification_receipt(recovered)
            return recovered
        return linked

    def verification_receipt(self) -> dict[str, object] | None:
        receipt = self.load_verification_receipt(self.verification_receipt_path)
        if receipt is None:
            return None
        if (
            receipt["head_sha"] != self.verification_head
            or receipt["operation_id"] != self.verification_spec.operation_id
        ):
            return None
        return receipt

    def verification_response_accepted(self, receipt: dict[str, object]) -> bool:
        response_receipt_path = (
            self.spec_path.parent
            / "pipeline-verification"
            / str(receipt["operation_id"])
            / "response-receipt.json"
        )
        if not response_receipt_path.exists():
            return False
        if not response_receipt_path.is_file() or response_receipt_path.is_symlink():
            raise RuntimeWorkerError("verification response receipt is invalid")
        accepted = json.loads(response_receipt_path.read_text(encoding="utf-8"))
        if (
            not isinstance(accepted, dict)
            or accepted.get("schema_version") != 2
            or accepted.get("operation_id") != self.spec["operation_id"]
            or (accepted.get("verification_operation_id") != receipt["operation_id"])
            or (accepted.get("failed_head_sha") != receipt["head_sha"])
            or (accepted.get("status") != "accepted")
            or (
                not re.fullmatch(
                    "[0-9a-f]{40,64}", str(accepted.get("resubmitted_head_sha") or "")
                )
            )
            or (
                not re.fullmatch(
                    "[0-9a-f]{64}", str(accepted.get("response_sha256") or "")
                )
            )
        ):
            raise RuntimeWorkerError("verification response receipt is invalid")
        if "next_attempt" not in accepted:
            if accepted.get("resubmitted_head_sha") == receipt["head_sha"]:
                raise RuntimeWorkerError("verification response receipt is invalid")
            return True
        failed_attempt = self.verification_attempt_from_receipt(receipt)
        try:
            next_attempt = VerificationAttempt.from_dict(
                accepted.get("next_attempt")
            )
        except VerificationAttemptError as exc:
            raise RuntimeWorkerError(
                "verification response receipt is invalid"
            ) from exc
        if (
            failed_attempt.attempt_index != 0
            or accepted.get("resubmitted_head_sha") != receipt["head_sha"]
            or accepted.get("failed_attempt_sha256") != failed_attempt.sha256
            or accepted.get("next_attempt_sha256") != next_attempt.sha256
            or next_attempt != failed_attempt.same_head_retry()
            or not IDENTIFIER.fullmatch(
                str(accepted.get("mechanism_flake_decision_id") or "")
            )
            or not re.fullmatch(
                "[0-9a-f]{64}",
                str(accepted.get("mechanism_flake_decision_sha256") or ""),
            )
        ):
            raise RuntimeWorkerError("verification response receipt is invalid")
        return True

    def link_verification_receipt(self, receipt: dict[str, object]) -> bool:
        """Admit the durable controller link by exact receipt/HEAD identity.

        A completed receipt becomes linked authority only while its exact
        HEAD is the clean current candidate, re-observed immediately before
        the durable write and once more after it; a post-write mismatch
        retracts the link.  A prior candidate observation is therefore never
        carried into the publication: a clean commit landing after any
        earlier read leaves zero durable stale authority.  A failed receipt
        is failure evidence for the existing attention/resubmit machinery,
        not authority, and stays linkable regardless of currency.
        """

        if self.verification_controller_receipt_path.is_symlink():
            raise RuntimeWorkerError(
                "pipeline verification controller receipt is invalid"
            )
        admit_by_identity = receipt["status"] != "failed"
        if admit_by_identity and not _verification_candidate_is_current(
            self.spec["cwd"], str(receipt["head_sha"])
        ):
            return False
        _atomic_json(self.verification_controller_receipt_path, receipt)
        if admit_by_identity and not _verification_candidate_is_current(
            self.spec["cwd"], str(receipt["head_sha"])
        ):
            self.verification_controller_receipt_path.unlink(missing_ok=True)
            return False
        return True

    def failed_verification_count(self) -> int:
        count = 0
        receipts_root = self.spec_path.parent / "pipeline-verification"
        if not receipts_root.is_dir():
            return 0
        for path in receipts_root.glob("*/receipt.json"):
            receipt = self.load_verification_receipt(path)
            if receipt is not None and receipt["status"] == "failed":
                count += 1
        return count

    def changed_head_resubmit_count(self) -> int:
        count = 0
        receipts_root = self.spec_path.parent / "pipeline-verification"
        if not receipts_root.is_dir():
            return 0
        for path in receipts_root.glob("*/response-receipt.json"):
            if path.is_symlink() or not path.is_file():
                raise RuntimeWorkerError("verification response receipt is invalid")
            receipt_path = path.with_name("receipt.json")
            receipt = self.load_verification_receipt(receipt_path)
            if (
                receipt is None
                or path.parent.name != receipt.get("operation_id")
                or not self.verification_response_accepted(receipt)
            ):
                raise RuntimeWorkerError("verification response receipt is invalid")
            value = json.loads(path.read_text(encoding="utf-8"))
            changed_head_fields = {
                "schema_version",
                "operation_id",
                "verification_operation_id",
                "failed_head_sha",
                "resubmitted_head_sha",
                "response_sha256",
                "status",
            }
            if not isinstance(value, dict):
                raise RuntimeWorkerError("verification response receipt is invalid")
            if (
                value.get("schema_version") != 2
                or value.get("status") != "accepted"
                or value.get("operation_id") != self.spec["operation_id"]
                or value.get("verification_operation_id")
                != receipt["operation_id"]
                or value.get("failed_head_sha") != receipt["head_sha"]
                or not re.fullmatch(
                    "[0-9a-f]{40,64}",
                    str(value.get("resubmitted_head_sha") or ""),
                )
                or not re.fullmatch(
                    "[0-9a-f]{64}", str(value.get("response_sha256") or "")
                )
            ):
                raise RuntimeWorkerError("verification response receipt is invalid")
            if "next_attempt" in value:
                same_head_fields = changed_head_fields | {
                    "failed_attempt_sha256",
                    "next_attempt",
                    "next_attempt_sha256",
                    "mechanism_flake_decision_id",
                    "mechanism_flake_decision_sha256",
                }
                if (
                    set(value) != same_head_fields
                    or value["resubmitted_head_sha"] != value["failed_head_sha"]
                ):
                    raise RuntimeWorkerError(
                        "verification response receipt is invalid"
                    )
                continue
            if (
                set(value) != changed_head_fields
                or value["resubmitted_head_sha"] == value["failed_head_sha"]
            ):
                raise RuntimeWorkerError("verification response receipt is invalid")
            count += 1
        return count

    def changed_head_resubmit_available(self) -> bool:
        return self.changed_head_resubmit_count() < MAX_PIPELINE_VERIFY_RESUBMITS

    def fix_retry_policy(self) -> tuple[str, int]:
        raw_policy = self.meta.get("pipeline_policy")
        if not isinstance(raw_policy, dict):
            raise RuntimeWorkerError("engineering/fix completion policy is unavailable")
        completion = str(raw_policy.get("completion_policy") or "")
        limit = raw_policy.get("total_pass_limit")
        if (
            completion not in {"attention", "autonomous"}
            or type(limit) is not int
            or limit != {"attention": 2, "autonomous": 3}[completion]
        ):
            raise RuntimeWorkerError("engineering/fix completion policy is invalid")
        return (completion, limit)

    def schedule_fix_retry(self, failed: dict[str, object]) -> None:
        completion, total_limit = self.fix_retry_policy()
        completed_passes = self.failed_verification_count()
        if completed_passes < 1:
            raise RuntimeWorkerError("engineering/fix failed-pass count is invalid")
        verification_sha256 = hashlib.sha256(
            json.dumps(failed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if completed_passes >= total_limit:
            if completion == "attention":
                self.summary_attention(
                    "pipeline-verification-retry-exhausted",
                    AttentionReason.RETRY_EXHAUSTED,
                    write_error=False,
                )
                return
            terminal_path = (
                self.spec_path.parent / "pipeline-fix" / "terminal-exhausted.json"
            )
            self.write_immutable_json(
                terminal_path,
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "completion_policy": completion,
                    "total_pass_limit": total_limit,
                    "completed_passes": completed_passes,
                    "verification_operation_id": failed["operation_id"],
                    "verification_sha256": verification_sha256,
                    "failed_head_sha": failed["head_sha"],
                    "status": "retry-exhausted",
                },
            )
            current_parent = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            if current_parent.state not in TERMINAL:
                self.store.transition(
                    self.spec["owner_id"], self.spec["operation_id"], "failed"
                )
            return
        reproduction_path = (
            self.spec_path.parent
            / "pipeline-fix"
            / "pass-0"
            / "reproduce"
            / "receipt.json"
        )
        reproduction = load_receipt(reproduction_path)
        iteration = completed_passes
        intent_path = (
            self.spec_path.parent
            / "pipeline-fix"
            / f"pass-{iteration}"
            / "retry-intent.json"
        )
        self.write_immutable_json(
            intent_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "definition_sha256": self.pipeline.definition_sha256,
                "iteration": iteration,
                "completion_policy": completion,
                "total_pass_limit": total_limit,
                "reproduction_receipt_sha256": reproduction.receipt_sha256,
                "verification_operation_id": failed["operation_id"],
                "verification_sha256": verification_sha256,
                "failed_head_sha": failed["head_sha"],
                "current_head_sha": self.git_head(),
                "status": "pending",
            },
        )
        self.fix_transport_complete = False
        emit_compiled_pipeline_event(
            self.spec["cwd"],
            event="fix-retry-scheduled",
            pipeline_id=self.pipeline.definition.pipeline_id,
            pipeline_version=self.pipeline.definition.version,
            profile=self.pipeline.definition.profile,
            compiler_outcome="resolved",
            definition_sha=self.pipeline.definition_sha256,
            primitive_count=len(self.pipeline.definition.steps),
            loop_iteration=iteration,
            terminal_category="verification-failed",
        )

    def accept_fix_retry_resubmission(self, failed: dict[str, object]) -> bool:
        matching_intents: list[dict[str, object]] = []
        for intent_path in sorted(
            (self.spec_path.parent / "pipeline-fix").glob("pass-*/retry-intent.json")
        ):
            if intent_path.is_symlink():
                raise RuntimeWorkerError("fix retry intent cannot be a symlink")
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if (
                isinstance(intent, dict)
                and intent.get("verification_operation_id") == failed["operation_id"]
            ):
                matching_intents.append(intent)
        if len(matching_intents) != 1:
            raise RuntimeWorkerError("failed verification has no exact fix retry")
        intent = matching_intents[0]
        iteration = intent.get("iteration")
        if type(iteration) is not int:
            raise RuntimeWorkerError("fix retry iteration is invalid")
        receipt_root = self.spec_path.parent / "pipeline-fix" / f"pass-{iteration}"
        if not all(
            (
                (receipt_root / step / "receipt.json").is_file()
                for step in ("root-cause", "regression-test", "minimal-fix")
            )
        ):
            return False
        response_receipt_path = (
            self.spec_path.parent
            / "pipeline-verification"
            / str(failed["operation_id"])
            / "response-receipt.json"
        )
        response_sha256 = hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        response_receipt = {
            "schema_version": 2,
            "operation_id": self.spec["operation_id"],
            "verification_operation_id": failed["operation_id"],
            "failed_head_sha": failed["head_sha"],
            "resubmitted_head_sha": self.verification_head,
            "response_sha256": response_sha256,
            "status": "accepted",
        }
        self.write_immutable_json(response_receipt_path, response_receipt)
        failed_record = self.store.read(
            self.spec["owner_id"], str(failed["operation_id"])
        )
        if failed_record.state == "attention-required":
            self.store.transition(
                self.spec["owner_id"], failed_record.spec.operation_id, "failed"
            )
        elif failed_record.state != "failed":
            raise RuntimeWorkerError("failed verification operation cannot resume")
        return True

    def verification_attention_packet(
        self,
        receipt: dict[str, object],
        *,
        allow_resubmit: bool,
        allow_same_head_retry: bool = False,
    ) -> tuple[dict[str, object], str]:
        raw_evidence = receipt.get("evidence")
        if not isinstance(raw_evidence, list):
            raise RuntimeWorkerError("verification attention evidence is invalid")
        packet_evidence = [
            {
                "command_id": str(row["command_id"]),
                "exit_code": int(row["exit_code"]),
                "output_pointer": str(
                    (self.spec_path.parent / str(row["output_pointer"])).resolve()
                ),
            }
            for row in raw_evidence
            if isinstance(row, dict)
        ]
        if len(packet_evidence) != len(raw_evidence):
            raise RuntimeWorkerError("verification attention evidence is invalid")
        attempt = self.verification_attempt_from_receipt(receipt)
        allowed = [
            *(["fix-and-resubmit"] if allow_resubmit else []),
            *(
                ["retry-mechanism-flake"]
                if allow_same_head_retry and attempt.attempt_index == 0
                else []
            ),
            "escalate",
        ]
        packet = {
            "schema_version": 2,
            "operation_id": self.spec["operation_id"],
            "verification_operation_id": str(receipt["operation_id"]),
            "verification_lane_id": str(receipt["lane_id"]),
            "verification_run_id": str(receipt["run_id"]),
            "definition_sha256": self.pipeline.definition_sha256,
            "step_id": "verify",
            "head_sha": str(receipt["head_sha"]),
            "status": "attention-required",
            "reason": "verification-failed",
            "safe_boundary": "tdd-slices-complete",
            "allowed_responses": allowed,
            "response_pointer": ".task-verification-response.json",
            "receipt_pointer": str(
                (
                    self.spec_path.parent
                    / "pipeline-verification"
                    / str(receipt["operation_id"])
                    / "receipt.json"
                ).resolve()
            ),
            "evidence": packet_evidence,
            "verification_attempt": attempt.as_dict(),
            "verification_attempt_sha256": attempt.sha256,
        }
        encoded = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_OUTBOX_BYTES:
            raise RuntimeWorkerError("verification attention packet is too large")
        return (packet, hashlib.sha256(encoded).hexdigest())

    def accept_verification_gap_disposition(
        self, failed: dict[str, object]
    ) -> bool:
        """Consume one exact coordinator grant without changing failed truth."""

        try:
            decision_record = load_latest_escalation(self.spec["cwd"])
        except EscalationRecordError as exc:
            raise RuntimeWorkerError(
                "verification baseline-gap decision is invalid"
            ) from exc
        if decision_record is None or decision_record.record_type != "resolution":
            return False
        payload = decision_record.payload
        evidence = failed.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise RuntimeWorkerError("verification baseline-gap evidence is invalid")
        command_ids = tuple(
            str(row.get("command_id") or "")
            for row in evidence
            if isinstance(row, dict)
        )
        if len(command_ids) != len(evidence):
            raise RuntimeWorkerError("verification baseline-gap evidence is invalid")
        failed_receipt_sha256 = hashlib.sha256(
            json.dumps(
                failed, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        meta_path = self.spec["cwd"] / ".task-meta.json"
        if meta_path.is_symlink():
            raise RuntimeWorkerError("task metadata cannot be a symlink")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError("task metadata is invalid") from exc
        origin_session = str(meta.get("origin_session") or "")
        attempt = self.verification_attempt_from_receipt(failed)
        if (
            payload.get("status") != "resolved"
            or payload.get("category") != "pipeline-decision"
            or payload.get("decision") != "continue-unrelated-baseline-gap"
            or Path(str(payload.get("worktree") or "")).expanduser().resolve()
            != self.spec["cwd"]
            or not verification_gap_resolution_authorizes(
                payload.get("verification_resolution"),
                attempt,
                str(failed.get("operation_id") or ""),
                failed_receipt_sha256=failed_receipt_sha256,
                command_ids=command_ids,
                origin_session=origin_session,
            )
        ):
            return False
        authority = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "verification_operation_id": failed["operation_id"],
            "failed_head_sha": failed["head_sha"],
            "failed_attempt_sha256": attempt.sha256,
            "failed_receipt_sha256": failed_receipt_sha256,
            "command_ids": list(command_ids),
            "origin_session": origin_session,
            "decision_record_id": decision_record.record_id,
            "decision_record_sha256": decision_record.sha256,
            "status": "review-admitted-with-gap",
        }
        self.write_immutable_json(
            self.spec_path.parent / "pipeline-verification-gap-authority.json",
            authority,
        )
        self.verification_gap_authority = authority
        return True

    def notify_verification_attention(
        self,
        receipt: dict[str, object],
        *,
        allow_resubmit: bool,
        allow_same_head_retry: bool = False,
    ) -> str:
        packet, packet_sha256 = self.verification_attention_packet(
            receipt,
            allow_resubmit=allow_resubmit,
            allow_same_head_retry=allow_same_head_retry,
        )
        if allow_same_head_retry:
            publish_verification_escalation(
                state_root=self.spec_path.parent,
                worktree=self.spec["cwd"],
                failed_attempt=self.verification_attempt_from_receipt(receipt),
                verification_operation_id=str(receipt["operation_id"]),
            )
        packet_path = self.spec["cwd"] / ".task-verification.json"
        if packet_path.is_symlink():
            raise RuntimeWorkerError(
                "verification attention packet cannot be a symlink"
            )
        _atomic_json(packet_path, packet)
        notify_path = (
            self.spec_path.parent / "pipeline-verification-attention-notify.json"
        )
        if notify_path.is_file():
            if notify_path.is_symlink():
                raise RuntimeWorkerError(
                    "verification attention notification is invalid"
                )
            notified = json.loads(notify_path.read_text(encoding="utf-8"))
            if (
                not isinstance(notified, dict)
                or notified.get("schema_version") != 1
                or notified.get("operation_id") != self.spec["operation_id"]
            ):
                raise RuntimeWorkerError(
                    "verification attention notification is invalid"
                )
            if (
                notified.get("packet_sha256") == packet_sha256
                and notified.get("status") == "sent"
            ):
                return packet_sha256
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "packet_sha256": packet_sha256,
                "status": "pending",
            },
        )
        verification_raise = shlex.join((
            "python3", str(self.trusted_vault / "scripts" / "task_escalation.py"),
            "raise", "--worktree", str(self.spec["cwd"]), "--category",
            "mechanism-failure", "--verification-mechanism-flake", "--reason",
            "Isolated rerun established a verification mechanism flake.", "--question",
            "Authorize one exact same-HEAD verification retry?"))
        baseline_raise = shlex.join((
            "python3", str(self.trusted_vault / "scripts" / "task_escalation.py"),
            "raise", "--worktree", str(self.spec["cwd"]), "--category",
            "pipeline-decision", "--verification-baseline-gap", "--reason",
            "Isolated evidence established an unrelated baseline verification gap.",
            "--question", "Admit review with the exact failed receipt preserved?"))
        paste_editor_text(
            self.cmux_adapter,
            surface_id=self.spec["surface_id"],
            text=f"Typed pipeline verification attention is ready in .task-verification.json. For changed-HEAD fix-and-resubmit, commit the fix and run `python3 {self.trusted_vault}/scripts/pipeline-verification-resubmit.py --worktree {self.spec['cwd']}`. If isolated evidence establishes a mechanism flake, run this exact typed raise command: `{verification_raise}`. If it instead proves an unrelated baseline gap, run: `{baseline_raise}`. A same-HEAD retry or gap admission happens only through its exact coordinator decision. Do not create an empty commit, launch review, or invoke reap.",
        )
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "packet_sha256": packet_sha256,
                "status": "sent",
            },
        )
        return packet_sha256

    def accept_verification_resubmission(self, failed: dict[str, object]) -> bool:
        if self.verification_head == failed["head_sha"]:
            return False
        _, packet_sha256 = self.verification_attention_packet(
            failed,
            allow_resubmit=True,
            allow_same_head_retry=(
                self.verification_attempt_from_receipt(failed).attempt_index == 0
            ),
        )
        response_path = self.spec["cwd"] / ".task-verification-response.json"
        try:
            raw = response_path.read_bytes()
        except FileNotFoundError:
            return False
        if response_path.is_symlink() or not raw or len(raw) > MAX_OUTBOX_BYTES:
            raise RuntimeWorkerError("verification resubmission response is invalid")
        response = json.loads(raw)
        expected_keys = {
            "schema_version",
            "operation_id",
            "verification_operation_id",
            "failed_head_sha",
            "packet_sha256",
            "response",
            "resubmitted_head_sha",
        }
        if (
            not isinstance(response, dict)
            or set(response) != expected_keys
            or response.get("schema_version") != 1
            or (response.get("operation_id") != self.spec["operation_id"])
            or (response.get("verification_operation_id") != failed["operation_id"])
            or (response.get("failed_head_sha") != failed["head_sha"])
            or (response.get("packet_sha256") != packet_sha256)
            or (response.get("response") != "fix-and-resubmit")
            or (response.get("resubmitted_head_sha") != self.verification_head)
        ):
            raise RuntimeWorkerError("verification resubmission response is invalid")
        response_sha256 = hashlib.sha256(
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        response_receipt_path = (
            self.spec_path.parent
            / "pipeline-verification"
            / str(failed["operation_id"])
            / "response-receipt.json"
        )
        response_receipt = {
            "schema_version": 2,
            "operation_id": self.spec["operation_id"],
            "verification_operation_id": failed["operation_id"],
            "failed_head_sha": failed["head_sha"],
            "resubmitted_head_sha": self.verification_head,
            "response_sha256": response_sha256,
            "status": "accepted",
        }
        if response_receipt_path.is_file():
            if response_receipt_path.is_symlink():
                raise RuntimeWorkerError("verification response receipt is invalid")
            existing = json.loads(response_receipt_path.read_text(encoding="utf-8"))
            if existing != response_receipt:
                raise RuntimeWorkerError("verification response receipt is invalid")
        else:
            _atomic_json(response_receipt_path, response_receipt)
        failed_record = self.store.read(
            self.spec["owner_id"], str(failed["operation_id"])
        )
        if failed_record.state == "attention-required":
            self.store.transition(
                self.spec["owner_id"], failed_record.spec.operation_id, "failed"
            )
        elif failed_record.state != "failed":
            raise RuntimeWorkerError("failed verification operation cannot resume")
        return True

    def accept_same_head_verification_retry(
        self, failed: dict[str, object]
    ) -> bool:
        failed_attempt = self.verification_attempt_from_receipt(failed)
        if (
            failed_attempt.exact_head_sha != self.verification_head
            or failed_attempt.attempt_index != 0
        ):
            return False
        _, packet_sha256 = self.verification_attention_packet(
            failed,
            allow_resubmit=self.changed_head_resubmit_available(),
            allow_same_head_retry=True,
        )
        response_path = self.spec["cwd"] / ".task-verification-response.json"
        try:
            raw = response_path.read_bytes()
        except FileNotFoundError:
            return False
        if response_path.is_symlink() or not raw or len(raw) > MAX_OUTBOX_BYTES:
            raise RuntimeWorkerError("verification resubmission response is invalid")
        response = json.loads(raw)
        changed_head_keys = {
            "schema_version",
            "operation_id",
            "verification_operation_id",
            "failed_head_sha",
            "packet_sha256",
            "response",
            "resubmitted_head_sha",
        }
        resubmitted_head = (
            str(response.get("resubmitted_head_sha") or "")
            if isinstance(response, dict)
            else ""
        )
        if (
            isinstance(response, dict)
            and set(response) == changed_head_keys
            and response.get("schema_version") == 1
            and response.get("operation_id") == self.spec["operation_id"]
            and response.get("verification_operation_id")
            == failed["operation_id"]
            and response.get("failed_head_sha") == failed["head_sha"]
            and response.get("packet_sha256") == packet_sha256
            and response.get("response") == "fix-and-resubmit"
            and re.fullmatch("[0-9a-f]{40,64}", resubmitted_head)
            and resubmitted_head != self.verification_head
            and _verification_candidate_is_current(
                self.spec["cwd"], resubmitted_head
            )
        ):
            # The executor may commit and atomically publish a changed-HEAD
            # response after this worker bound the failed HEAD for the current
            # reconciliation pass.  This exact response belongs to the normal
            # changed-HEAD path, so wait for the next pass to rebind HEAD
            # instead of misclassifying valid authority as a malformed
            # same-HEAD retry.
            return False
        expected_keys = {
            "schema_version",
            "operation_id",
            "verification_operation_id",
            "failed_head_sha",
            "packet_sha256",
            "response",
            "resubmitted_head_sha",
            "failed_attempt_sha256",
            "next_attempt",
            "next_attempt_sha256",
            "mechanism_flake_decision_id",
            "mechanism_flake_decision_sha256",
        }
        try:
            next_attempt = VerificationAttempt.from_dict(
                response.get("next_attempt") if isinstance(response, dict) else None
            )
        except VerificationAttemptError as exc:
            raise RuntimeWorkerError(
                "verification resubmission response is invalid"
            ) from exc
        if (
            not isinstance(response, dict)
            or set(response) != expected_keys
            or response.get("schema_version") != 2
            or response.get("operation_id") != self.spec["operation_id"]
            or response.get("verification_operation_id")
            != failed["operation_id"]
            or response.get("failed_head_sha") != failed["head_sha"]
            or response.get("packet_sha256") != packet_sha256
            or response.get("response") != "retry-mechanism-flake"
            or response.get("resubmitted_head_sha") != self.verification_head
            or response.get("failed_attempt_sha256") != failed_attempt.sha256
            or response.get("next_attempt_sha256") != next_attempt.sha256
            or next_attempt != failed_attempt.same_head_retry()
            or not IDENTIFIER.fullmatch(
                str(response.get("mechanism_flake_decision_id") or "")
            )
            or not re.fullmatch(
                "[0-9a-f]{64}",
                str(response.get("mechanism_flake_decision_sha256") or ""),
            )
        ):
            raise RuntimeWorkerError("verification resubmission response is invalid")
        try:
            decision_record = load_latest_escalation(self.spec["cwd"])
        except EscalationRecordError as exc:
            raise RuntimeWorkerError(
                "verification mechanism-flake decision is invalid"
            ) from exc
        payload = decision_record.payload if decision_record is not None else {}
        if (
            decision_record is None
            or decision_record.record_type != "resolution"
            or decision_record.sha256
            != response["mechanism_flake_decision_sha256"]
            or payload.get("status") != "resolved"
            or payload.get("category") != "mechanism-failure"
            or payload.get("id") != response["mechanism_flake_decision_id"]
            or Path(str(payload.get("worktree") or "")).expanduser().resolve()
            != self.spec["cwd"]
            or not verification_resolution_authorizes(
                payload.get("verification_resolution"),
                failed_attempt,
                str(failed["operation_id"]),
            )
        ):
            raise RuntimeWorkerError(
                "verification mechanism-flake decision is invalid"
            )
        response_sha256 = hashlib.sha256(
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        response_receipt_path = (
            self.spec_path.parent
            / "pipeline-verification"
            / str(failed["operation_id"])
            / "response-receipt.json"
        )
        response_receipt = {
            "schema_version": 2,
            "operation_id": self.spec["operation_id"],
            "verification_operation_id": failed["operation_id"],
            "failed_head_sha": failed["head_sha"],
            "resubmitted_head_sha": self.verification_head,
            "failed_attempt_sha256": failed_attempt.sha256,
            "next_attempt": next_attempt.as_dict(),
            "next_attempt_sha256": next_attempt.sha256,
            "mechanism_flake_decision_id": response[
                "mechanism_flake_decision_id"
            ],
            "mechanism_flake_decision_sha256": decision_record.sha256,
            "response_sha256": response_sha256,
            "status": "accepted",
        }
        self.write_immutable_json(response_receipt_path, response_receipt)
        failed_record = self.store.read(
            self.spec["owner_id"], str(failed["operation_id"])
        )
        if failed_record.state == "attention-required":
            self.store.transition(
                self.spec["owner_id"], failed_record.spec.operation_id, "failed"
            )
        elif failed_record.state != "failed":
            raise RuntimeWorkerError("failed verification operation cannot resume")
        return True

    def reconcile_failed_verification_child(self, failed: dict[str, object]) -> None:
        failed_operation_id = str(failed["operation_id"])
        failed_record = self.store.read(self.spec["owner_id"], failed_operation_id)
        if failed_record.pending_effect:
            if failed_record.pending_effect != failed["effect_id"]:
                raise RuntimeWorkerError("failed verification effect is uncertain")
            self.store.resolve_effect(
                self.spec["owner_id"], failed_operation_id, EffectOutcome.SUCCEEDED
            )
            failed_record = self.store.read(self.spec["owner_id"], failed_operation_id)
        if failed_record.state == "verifying":
            self.store.transition(
                self.spec["owner_id"],
                failed_operation_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        elif failed_record.state not in {"attention-required", "failed"}:
            raise RuntimeWorkerError("failed verification operation state is invalid")

    def product_tree_is_clean(self) -> bool:
        """Observe exact tracked-and-untracked product cleanliness at HEAD."""

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        return status.returncode == 0 and not status.stdout.strip()

    def invalidated_verification_attempt(self) -> object | None:
        """Return the bound attempt's record when its authority is invalid.

        A settled succeeded own-identity effect without a persisted receipt
        proves the probes ran to completion once but can never prove
        verification authority for the current candidate: the receipt is the
        only durable outcome, and settlement closes the resumable window.
        The record must carry the exact derived identity — spec, lane, run,
        own effect — with released resources and no symlinked or unsafe
        receipt path. Pending, foreign, unsettled, receipt-bearing, or
        identity-drifted attempts are never classified here — they stay with
        their existing fail-closed owners.
        """

        try:
            record = self.store.read(
                self.spec["owner_id"], self.verification_spec.operation_id
            )
        except StoreError:
            return None
        if (
            self.verification_receipt_path.is_symlink()
            or not _authority_path_is_safe(self.verification_receipt_path)
            or self.verification_receipt_path.exists()
            or record.spec != self.verification_spec
            or record.lane_id != self.verification_lane_id
            or record.run_id != self.verification_run_id
            or record.resources != OwnedResources()
            or record.pending_effect
            or record.effect_id != self.verification_effect_id
            or record.effect_outcome != EffectOutcome.SUCCEEDED
            or record.state not in {"verifying", "attention-required", "failed"}
        ):
            return None
        return record

    def adopt_invalidated_verification_successor(self) -> bool:
        """Hand one invalidated attempt to its exact-current-HEAD successor.

        The stale attempt is durably terminalized and linked to exactly one
        predecessor-bound fresh attempt through the existing identity
        constructors; repeated wakes and crash re-entry converge on the same
        successor. A dirty product tree or an exhausted successor identity
        space stays typed attention with no mutation, no replacement, and no
        probe replay.
        """

        stale = self.invalidated_verification_attempt()
        if stale is None:
            if not _orphaned_predecessor_lineage(self):
                return True
            self.summary_attention(
                "pipeline-verification-orphaned-lineage",
                AttentionReason.ATTENTION_REQUIRED,
            )
            return False
        if not self.product_tree_is_clean():
            self.summary_attention(
                "pipeline-verification-dirty-tree",
                AttentionReason.ATTENTION_REQUIRED,
            )
            return False
        if self.verification_attempt.attempt_index >= MAX_SAME_HEAD_ATTEMPT_INDEX:
            self.summary_attention(
                "pipeline-verification-retry-exhausted",
                AttentionReason.RETRY_EXHAUSTED,
            )
            return False
        stale_operation_id = stale.spec.operation_id
        predecessor_attempt_sha256 = self.verification_attempt.sha256
        predecessor_effect_id = self.verification_effect_id
        if stale.state not in TERMINAL:
            self.store.transition(
                self.spec["owner_id"], stale_operation_id, "failed"
            )
        self._bind_verification_attempt(
            self.verification_attempt.attempt_index + 1
        )
        self.write_immutable_json(
            self.spec_path.parent
            / "pipeline-verification"
            / stale_operation_id
            / "invalidation.json",
            {
                "schema_version": 1,
                "operation_id": stale_operation_id,
                "parent_operation_id": self.spec["operation_id"],
                "profile_sha256": self.verification_attempt.profile_sha256,
                "predecessor_attempt_sha256": predecessor_attempt_sha256,
                "predecessor_effect_id": predecessor_effect_id,
                "successor_operation_id": self.verification_spec.operation_id,
                "successor_attempt_sha256": self.verification_attempt.sha256,
                "successor_effect_id": self.verification_effect_id,
                "current_head_sha": self.verification_head,
                "status": "invalidated",
            },
        )
        return self.adopt_invalidated_verification_successor()

    def run_verification(self) -> None:
        from .dashboard_facade import launch_bound_facade_dashboard

        launch_bound_facade_dashboard(
            worktree=self.spec["cwd"],
            facade="verify",
            root_operation_id=self.verification_spec.root_operation_id,
        )
        existing = self.verification_receipt()
        current = self.store.create(
            self.verification_spec,
            lane_id=self.verification_lane_id,
            run_id=self.verification_run_id,
        )
        supervisor = OperationSupervisor(
            self.store, self.spec["owner_id"], self.verification_spec.operation_id
        )
        supervisor.configure_budget(
            attempt_limit=1,
            model_restart_limit=0,
            time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
            token_limit=DEFAULT_TOKEN_LIMIT,
        )
        current = supervisor.read()
        if current.state == "created":
            supervisor.transition("preflight")
            supervisor.transition("starting")
            supervisor.transition("running")
            supervisor.transition("verifying")
            supervisor.consume_attempt()
            current = supervisor.read()
        # An interrupted own-identity effect without a receipt resumes: the
        # probes are process-local and the receipt is their only outcome.
        pending = current.pending_effect
        if pending and pending != self.verification_effect_id:
            self.summary_attention("pipeline-verification-effect-uncertain")
            return
        ran_effect = False
        resume_pending = bool(pending) and existing is None
        if pending and existing is not None:
            self.store.resolve_effect(
                self.spec["owner_id"],
                self.verification_spec.operation_id,
                EffectOutcome.SUCCEEDED,
            )
        if existing is None:
            current = supervisor.read()
            if current.state != "verifying":
                raise RuntimeWorkerError("pipeline verification state is invalid")

            def execute_verification(_record: object) -> list[object]:
                evidence = list(
                    run_profile(
                        self.profile,
                        root=self.spec["cwd"],
                        evidence_dir=self.verification_root / "evidence",
                        runner=self.verification_runner or subprocess.run,
                        extra_commands=self.pipeline_extra_commands,
                        pointer_root=self.spec_path.parent,
                    )
                )
                verified_heads = {str(item.head_sha) for item in evidence}
                current_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.spec["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if (
                    current_head.returncode
                    or current_head.stdout.strip() != self.verification_head
                    or verified_heads != {self.verification_head}
                ):
                    raise VerificationError(
                        "verification HEAD changed during execution"
                    )
                if (
                    self.verification_attempt.attempt_index > 0
                    and not self.product_tree_is_clean()
                ):
                    # A replacement attempt attests the exact clean HEAD, so
                    # bytes mutated during its probes can never be receipted.
                    raise VerificationError(
                        "verification tree became dirty during execution"
                    )
                return evidence

            def persist_verification(_record: object, evidence: list[object]) -> None:
                expected_command_ids = tuple(
                    f"{self.profile.name}-{index + 1}"
                    for index in range(
                        len(
                            compose_commands(
                                self.profile, self.pipeline_extra_commands
                            )
                        )
                    )
                )
                try:
                    authority = VerificationAuthority.issue(
                        store=self.store,
                        parent=self.operation,
                        runtime_root=self.spec_path.parent,
                        definition_sha256=self.pipeline.definition_sha256,
                        input_sha256=self.verification_input_sha256,
                        profile=self.profile.name,
                        profile_sha256=self.profile.sha256,
                        attempt=self.verification_attempt,
                        evidence=tuple(evidence),
                        expected_command_ids=expected_command_ids,
                    )
                except VerificationAuthorityError as exc:
                    raise RuntimeWorkerError(
                        "pipeline verification authority is invalid"
                    ) from exc
                _atomic_json(
                    self.verification_receipt_path,
                    authority.to_dict(),
                )
                persisted = json.loads(
                    self.verification_receipt_path.read_text(encoding="utf-8")
                )
                # The receipt stays immutable evidence for its own exact
                # HEAD; the controller link admits it as authority only by
                # exact-current-candidate identity at its own boundary.
                self.link_verification_receipt(persisted)

            supervisor.effect(
                self.verification_effect_id,
                execute_verification,
                persist_result=persist_verification,
                resume_pending=resume_pending,
            )
            ran_effect = True
            existing = self.verification_receipt()
        if existing is None:
            raise RuntimeWorkerError("pipeline verification produced no receipt")
        if existing["status"] == "failed":
            current = supervisor.read()
            if current.state == "verifying":
                self.store.transition(
                    self.spec["owner_id"],
                    self.verification_spec.operation_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            return
        current = supervisor.read()
        if current.state == "verifying":
            supervisor.transition("finalizing")
            supervisor.transition("exiting")
            supervisor.transition("complete")
        if (
            ran_effect
            and not _verification_candidate_is_current(
                self.spec["cwd"], str(existing["head_sha"])
            )
            and not _review_resolution_drift_in_flight(
                self, str(existing["head_sha"])
            )
        ):
            # A clean commit or dirt landed after the in-effect observation:
            # the receipt survives as immutable evidence for its own HEAD but
            # is never consumed as current authority.  A resolution in flight
            # only suppresses the latch; it never restores currency.
            self.summary_attention(
                "pipeline-verification-stale-authority",
                AttentionReason.CONTRACT_DRIFT,
            )
