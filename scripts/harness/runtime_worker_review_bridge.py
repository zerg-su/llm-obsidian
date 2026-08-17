"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations
from .runtime_worker import *
from types import SimpleNamespace
from .runtime_worker import (
    _atomic_json,
    _callback_target,
    _contain_provider_start_failure,
    _current_callback_receipt_sha256,
    _envelope,
    _normalize_fetch_errors_at_provider_boundary,
    _pipeline_verify_identity,
    _research_input_provenance,
    _review_resolution_handoff_ready,
)
from . import runtime_callback_io
from .runtime_worker_verification import _verification_candidate_is_current
from .review_continuation_recovery import (
    RecoveryDecision,
    RecoveryDisposition,
    RecoveryIdentity,
    RecoveryReason,
    RecoverySnapshot,
    classify_review_continuation,
)
from .review_continuation_observation import observe_review_continuation
from review_contract import ReviewContractError, axis_finding_id


def _review_drive_failure_code(stderr: str) -> str:
    """Reduce runner stderr to one content-free repository-owned reason code."""

    value = stderr.casefold().strip()
    stable_prefix = "task-review-runner:"
    if value.startswith(stable_prefix):
        value = value[len(stable_prefix) :].lstrip()
    if "runtime preflight failed" in value:
        return "runtime-preflight-failed"
    if "runtime callback" in value:
        return "runtime-callback-contract"
    if "runtime prompt" in value:
        return "runtime-prompt-contract"
    if "routing" in value or "model" in value:
        return "runtime-route-contract"
    if "surface" in value or "cmux" in value:
        return "runtime-surface-contract"
    if "review" in value:
        return "review-contract-rejected"
    return "runner-exit-nonzero"


def _review_drive_failure_receipt(
    result: subprocess.CompletedProcess[str], *, drive_sha256: str
) -> dict[str, object]:
    """Describe a failed runner without persisting prompts or error text."""

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    return {
        "schema_version": 1,
        "status": "review-drive-failed",
        "reason_code": _review_drive_failure_code(stderr),
        "returncode": result.returncode,
        "drive_sha256": drive_sha256,
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
    }


def publish_review_resolution_transport(
    *,
    gate_state: dict[str, object],
    gate_root: Path,
    worktree: Path,
    operation_id: str,
    surface_id: str,
    summary_sha256: str,
    runtime_spec_path: Path,
    cmux_adapter: object,
) -> None:
    """Publish one existing review decision through the worker-owned transport."""

    publisher = RuntimeWorkerReviewBridgeMixin()
    publisher.spec = {
        "cwd": worktree.expanduser().resolve(),
        "operation_id": operation_id,
        "surface_id": surface_id,
    }
    publisher.spec_path = runtime_spec_path.expanduser().resolve()
    publisher.review = SimpleNamespace(
        gate_root=gate_root.expanduser().resolve()
    )
    publisher.digest = summary_sha256
    publisher.cmux_adapter = cmux_adapter
    publisher.notify_review_resolution(gate_state)


class RuntimeWorkerReviewBridgeMixin:

    def callback_timeout_rearm_receipt(
        self,
        *,
        generation: int,
        envelope: CallbackEnvelope,
        status: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generation": generation,
            "parent_operation_id": self.spec["operation_id"],
            "parent_run_id": self.spec["run_id"],
            "round_operation_id": envelope.operation_id,
            "round_run_id": envelope.run_id,
            "callback_id": envelope.callback_id,
            "callback_sha256": envelope.payload_sha256,
            "status": status,
        }

    def finalize_callback_timeout_rearm(
        self,
        *,
        generation: int,
        envelope: CallbackEnvelope,
    ) -> None:
        """Repair the receipt-only crash window after callback acceptance."""

        marker = self.spec_path.parent / "callback-timeout-rearm.json"
        if not marker.exists():
            return
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeWorkerError("callback timeout rearm receipt is invalid")
        existing = json.loads(marker.read_text(encoding="utf-8"))
        prepared = self.callback_timeout_rearm_receipt(
            generation=generation, envelope=envelope, status="prepared"
        )
        accepted = {**prepared, "status": "accepted"}
        if existing not in (prepared, accepted):
            raise RuntimeWorkerError("callback timeout rearm receipt changed")
        if existing == prepared:
            _atomic_json(marker, accepted)

    def accept_callback_after_timeout(
        self,
        *,
        generation: int,
        envelope: CallbackEnvelope,
    ) -> object:
        """Rearm only to ingest one already stable exact reviewer callback."""

        parent = self.store.read(
            self.spec["owner_id"], self.spec["operation_id"]
        )
        child = self.store.read(self.spec["owner_id"], envelope.operation_id)
        metadata_path = (
            self.store.root
            / "owners"
            / self.spec["owner_id"]
            / "runtime"
            / self.spec["operation_id"]
            / "session.json"
        )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError(
                "callback timeout rearm metadata is unavailable"
            ) from exc
        time_budget = metadata.get("time_budget_seconds")
        if (
            parent.state != "attention-required"
            or parent.attention_reason != AttentionReason.CALLBACK_TIMEOUT
            or parent.spec.route.profile != "reviewer-callback"
            or child.state != "awaiting-callback"
            or child.run_id != envelope.run_id
            or child.lane_id != parent.lane_id
            or envelope.kind != "review"
            or envelope.payload.get("parent_session_operation_id")
            != parent.spec.operation_id
            or metadata.get("schema_version") != 1
            or metadata.get("operation_id") != parent.spec.operation_id
            or metadata.get("run_id") != parent.run_id
            or not isinstance(time_budget, (int, float))
            or isinstance(time_budget, bool)
            or time_budget <= 0
        ):
            raise RuntimeWorkerError(
                "callback timeout rearm identity is invalid"
            )
        marker = self.spec_path.parent / "callback-timeout-rearm.json"
        prepared = self.callback_timeout_rearm_receipt(
            generation=generation, envelope=envelope, status="prepared"
        )
        if marker.is_file() and not marker.is_symlink():
            existing = json.loads(marker.read_text(encoding="utf-8"))
            if existing not in (prepared, {**prepared, "status": "accepted"}):
                raise RuntimeWorkerError(
                    "callback timeout rearm receipt changed"
                )
        elif marker.exists() or marker.is_symlink():
            raise RuntimeWorkerError(
                "callback timeout rearm receipt is invalid"
            )
        else:
            _atomic_json(marker, prepared)
        now = self.clock() if hasattr(self, "clock") else time.time()
        self.store.rearm_callback_timeout(
            self.spec["owner_id"],
            self.spec["operation_id"],
            deadline_at=now + float(time_budget),
        )
        acceptance = CallbackBroker(
            self.store, self.spec["owner_id"]
        ).accept(
            envelope,
            deadline_operation_id=self.spec["operation_id"],
        )
        _atomic_json(marker, {**prepared, "status": "accepted"})
        return acceptance

    def inspect_callback(self) -> None:
        """Ingest stable review input/callback artifacts without a model call."""

        try:
            target = _callback_target(self.spec)
        except RuntimeWorkerError:
            if not self.registration_invalid:
                self.registration_invalid = True
                try:
                    self.store.transition(
                        self.spec["owner_id"],
                        self.spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.CALLBACK_INVALID,
                    )
                except Exception:
                    pass
                self.publish_error_latch("callback-target-invalid")
            return
        self.registration_invalid = False
        if target != self.active_target:
            if self.active_target is not None and target[0] <= self.active_target[0]:
                return
            self.active_target = target
            self.last_digest = ""
            self.stable_reads = 0
            self.review_input_digest = ""
            self.review_input_stable_reads = 0
            self.callback_handled = False
        if self.callback_handled:
            return
        generation, operation_id, run_id, callback_path = target
        review_input = callback_path.with_name(".review-input.json")
        input_evidence, self.review_input_digest, self.review_input_stable_reads = (
            observe_review_artifact(
                review_input,
                self.review_input_digest,
                self.review_input_stable_reads,
            )
        )
        if input_evidence.state in {"symlink", "oversize", "malformed"}:
            self.summary_attention("review-input-invalid")
            return
        if input_evidence.state == "stable" and not callback_path.exists():
            # A provider-authenticated turn-complete signal does not exist in
            # either supported runtime.  Stable input is therefore evidence for
            # attention/recovery diagnosis only and cannot authorize Enter.
            return
        callback_evidence, self.last_digest, self.stable_reads = (
            observe_review_artifact(
                callback_path,
                self.last_digest,
                self.stable_reads,
            )
        )
        if callback_evidence.state in {"symlink", "oversize", "malformed"}:
            self.summary_attention("callback-artifact-invalid")
            return
        if callback_evidence.state != "stable":
            return
        try:
            raw = callback_path.read_bytes()
        except OSError:
            return
        try:
            envelope = _envelope(json.loads(raw))
            if envelope.operation_id != operation_id or envelope.run_id != run_id:
                raise RuntimeWorkerError("callback identity mismatches runtime launch")
            try:
                acceptance = CallbackBroker(
                    self.store, self.spec["owner_id"]
                ).accept(
                    envelope,
                    deadline_operation_id=self.spec["operation_id"],
                )
            except CallbackTimeoutError:
                acceptance = self.accept_callback_after_timeout(
                    generation=generation,
                    envelope=envelope,
                )
            self.record_provider_result(generation, envelope.payload_sha256)
            self.callback_handled = True
            self.finalize_callback_timeout_rearm(
                generation=generation,
                envelope=envelope,
            )
            _atomic_json(
                self.spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "generation": generation,
                    "callback_id": envelope.callback_id,
                    "operation_id": operation_id,
                    "run_id": envelope.run_id,
                    "payload_sha256": envelope.payload_sha256,
                    "status": "duplicate" if acceptance.duplicate else "accepted",
                },
            )
            if not runtime_callback_io.publish_callback_wake(
                self.spec,
                self.spec_path.parent,
                envelope.callback_id,
                self.cmux_adapter,
                resume_uncertain=runtime_callback_io.wake_resume_once(self, envelope.callback_id),
            ):
                self.summary_attention("callback-wake-effect-uncertain")
                return
        except (
            CallbackError,
            RuntimeWorkerError,
            StoreError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            self.summary_attention("callback-invalid")

    def review_drive_sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(str(getattr(self, "digest", "")).encode())
        gate_state = self.review.gate_root / "review-gate.json"
        if gate_state.is_file():
            if gate_state.is_symlink():
                raise RuntimeWorkerError("review gate state cannot be a symlink")
            digest.update(gate_state.read_bytes())
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        if head.returncode:
            raise RuntimeWorkerError("automatic review cannot resolve product HEAD")
        digest.update(head.stdout.strip().encode())
        callback_root = (
            self.trusted_vault
            / ".vault-meta"
            / "harness"
            / "review-runtime"
            / self.spec["operation_id"]
            / "callbacks"
        )
        if callback_root.is_dir():
            for callback in sorted(callback_root.rglob(".review-callback.json")):
                if callback.is_symlink():
                    raise RuntimeWorkerError("review callback cannot be a symlink")
                digest.update(callback.relative_to(callback_root).as_posix().encode())
                digest.update(callback.read_bytes())
        return digest.hexdigest()

    def wait_for_summary_refresh_after_verification(
        self, verification: dict[str, object]
    ) -> bool:
        """Hold initial review until v4 summary changes after verification."""

        receipt_path = self.verification_receipt_path
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise RuntimeWorkerError(
                "verification summary binding receipt is unavailable"
            )
        receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        verification_operation_id = str(
            verification.get("operation_id") or ""
        )
        gap_authority = getattr(self, "verification_gap_authority", None)
        if (
            verification.get("status")
            != ("failed" if gap_authority is not None else "complete")
            or not re.fullmatch(
                "[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                verification_operation_id,
            )
            or not re.fullmatch("[0-9a-f]{64}", receipt_sha256)
            or not re.fullmatch("[0-9a-f]{40,64}", self.verification_head)
        ):
            raise RuntimeWorkerError(
                "verification summary binding is invalid"
            )
        notify_path = self.spec_path.parent / "pipeline-summary-refresh-notify.json"
        identity = {
            "schema_version": 1,
            "purpose": "post-verification",
            "operation_id": self.spec["operation_id"],
            "approved_head_sha": self.verification_head,
            "verification_operation_id": verification_operation_id,
            "verification_receipt_sha256": receipt_sha256,
            "verification_gap_authority_sha256": (
                hashlib.sha256(
                    json.dumps(
                        gap_authority,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if isinstance(gap_authority, dict)
                else ""
            ),
        }
        if notify_path.is_file():
            if notify_path.is_symlink():
                raise RuntimeWorkerError(
                    "summary refresh notification cannot be a symlink"
                )
            existing = json.loads(notify_path.read_text(encoding="utf-8"))
            if all(existing.get(key) == value for key, value in identity.items()):
                initial = str(existing.get("summary_sha256") or "")
                refreshed = str(existing.get("refreshed_summary_sha256") or "")
                if (
                    not re.fullmatch("[0-9a-f]{64}", initial)
                    or existing.get("status") not in {"sent", "accepted"}
                ):
                    raise RuntimeWorkerError(
                        "summary refresh notification is invalid"
                    )
                if existing.get("status") == "accepted":
                    if refreshed != self.digest:
                        raise RuntimeWorkerError(
                            "accepted summary refresh identity drifted"
                        )
                    return False
                if self.digest == initial:
                    return True
                _atomic_json(
                    notify_path,
                    {
                        **identity,
                        "summary_sha256": initial,
                        "refreshed_summary_sha256": self.digest,
                        "status": "accepted",
                    },
                )
                return False
        marker = {
            **identity,
            "summary_sha256": self.digest,
            "refreshed_summary_sha256": "",
        }
        _atomic_json(notify_path, {**marker, "status": "pending"})
        message = (
            "Exact-HEAD verification completed. Refresh .task-summary.json "
            "before review so its body and outcome evidence cover verified HEAD "
            f"{self.verification_head}; preserve all code-owned fields."
        )
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        _atomic_json(notify_path, {**marker, "status": "sent"})
        return True

    def _resolved_head_verification_ready(self) -> bool:
        """Gate every bounded review launch on the resolved exact-HEAD receipt.

        A resolved changed HEAD must obtain a successful exact-HEAD
        verification receipt before any bounded review iteration can launch.
        When a review resolution moved the product HEAD past the reviewed
        HEAD, this re-binds the verification identity to the current HEAD and
        drives the pipeline verification owner instead of the review; only a
        complete receipt whose evidence names exactly the current HEAD lets
        the review drive proceed.  Corridors without a changed-HEAD
        resolution, and pipelines without a verification step, are untouched.
        """

        resolution_path = (
            self.spec_path.parent / "pipeline-review-resolution-notify.json"
        )
        if resolution_path.is_symlink():
            raise RuntimeWorkerError(
                "review resolution notification cannot be a symlink"
            )
        if not resolution_path.is_file():
            return True
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        reviewed_head = str(resolution.get("reviewed_head_sha") or "")
        if not re.fullmatch("[0-9a-f]{40,64}", reviewed_head):
            return True
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        current_head = head_result.stdout.strip()
        if head_result.returncode or not re.fullmatch(
            "[0-9a-f]{40,64}", current_head
        ):
            raise RuntimeWorkerError("pipeline product HEAD is unavailable")
        if current_head == reviewed_head:
            return True
        if not any(
            step.primitive_id == "verify"
            for step in self.pipeline.definition.steps
        ):
            # Pipelines without a verification step own no exact-HEAD
            # receipts; their resolution path is untouched.
            return True
        if getattr(self, "profile", None) is None:
            # The verification contract is not bound yet (early drive); hold
            # the changed-HEAD launch until the summary pipeline binds it,
            # never launching unverified.
            return False
        return self._current_head_verification_ready(current_head)

    def _current_head_verification_ready(self, current_head: str) -> bool:
        """Drive the code-owned verification/rebind path for one exact HEAD."""

        if self.verification_head != current_head:
            self.verification_head = current_head
            self._bind_verification_attempt(0)
        if not self.adopt_invalidated_verification_successor():
            return False
        receipt = self.verification_receipt()
        if receipt is None:
            self.run_verification()
            return False
        evidence = receipt.get("evidence")
        return (
            receipt.get("status") == "complete"
            and isinstance(evidence, list)
            and bool(evidence)
            and isinstance(evidence[0], dict)
            and evidence[0].get("head_sha") == current_head
        )

    def _review_drive_candidate_is_current(self) -> bool:
        """Reject current-HEAD drift or dirt immediately before review launch.

        Stale verification authority stays immutable evidence for its own
        HEAD: a launch after a clean commit or over a dirty tree re-enters
        the existing code-owned verification/rebind path instead of
        releasing a provider effect on it.
        """

        bound_head = getattr(self, "verification_head", None)
        if getattr(self, "profile", None) is None or not bound_head:
            # Without a bound verification contract there is no receipt whose
            # currency could drift; the existing gates own that launch.
            return True
        if not any(
            step.primitive_id == "verify"
            for step in self.pipeline.definition.steps
        ):
            return True
        if _verification_candidate_is_current(self.spec["cwd"], bound_head):
            return True
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        current_head = head_result.stdout.strip()
        if head_result.returncode or not re.fullmatch(
            "[0-9a-f]{40,64}", current_head
        ):
            raise RuntimeWorkerError("pipeline product HEAD is unavailable")
        if current_head == bound_head:
            # Same-HEAD dirt is wait-only: no launch, no rebind side effect;
            # the next wake re-observes a settled tree.
            return False
        if not self._current_head_verification_ready(current_head):
            return False
        return _verification_candidate_is_current(self.spec["cwd"], current_head)

    def _review_continuation_snapshot(self) -> RecoverySnapshot:
        return observe_review_continuation(self)

    def review_continuation_decision(self) -> RecoveryDecision:
        try:
            return classify_review_continuation(
                self._review_continuation_snapshot()
            )
        except (
            OSError,
            RuntimeWorkerError,
            StoreError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            json.JSONDecodeError,
        ):
            return RecoveryDecision(
                RecoveryDisposition.REFUSE,
                RecoveryReason.MALFORMED_EVIDENCE,
            )

    def _durable_review_in_progress(self) -> bool:
        """Delegate the pre-latch durable predicate to the shared policy."""

        return (
            self.review_continuation_decision().disposition
            is RecoveryDisposition.REVIEW_IN_PROGRESS
        )

    def execute_review_continuation(
        self, decision: RecoveryDecision
    ) -> bool:
        if (
            decision.receipt is None
            or decision.disposition
            not in {
                RecoveryDisposition.REVIEW_DRIVE_REARM,
                RecoveryDisposition.ACCEPTED_CALLBACK_INGEST,
            }
        ):
            return False
        return self.drive_review()

    def review_continuation_recovery_completed(
        self, identity: RecoveryIdentity
    ) -> bool:
        gate_state = self.review_gate_state()
        raw_attempt = gate_state.get("attempt")
        attempt_identity = (
            raw_attempt.get("identity")
            if isinstance(raw_attempt, dict)
            else None
        )
        if identity.recovery_class == "accepted-callback":
            lanes = gate_state.get("lanes")
            results = gate_state.get("round_results")
            if not isinstance(lanes, list) or not isinstance(results, dict):
                return False
            axes = {
                str(lane.get("axis") or "")
                for lane in lanes
                if isinstance(lane, dict)
                and lane.get("lane_id") == identity.lane_id
            }
            return len(axes) == 1 and next(iter(axes)) in results
        if not isinstance(attempt_identity, dict):
            return False
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        return (
            head_result.returncode == 0
            and attempt_identity.get("attempt_id") != identity.attempt_id
            and attempt_identity.get("exact_head_sha")
            == head_result.stdout.strip()
        )

    def _publish_review_launch_admission(self) -> bool:
        """Hand the launch the exact durable receipt/HEAD pair, not a boolean.

        The drive's earlier currency observations are never carried into the
        launch: immediately before releasing it, the durable verification
        receipt is re-read and its exact HEAD re-observed as the clean
        current candidate, and only that admitted pair — published where the
        exact-HEAD runner verifies it against the actual review context — can
        admit a provider effect.  Any receipt, HEAD, or clean-state mismatch
        refuses the launch with zero effect and leaves prior evidence
        untouched.  A drive without a bound verification contract keeps its
        existing gates and any previously admitted pair.
        """

        bound_head = getattr(self, "verification_head", "")
        if getattr(self, "profile", None) is None or not bound_head:
            return True
        if not any(
            step.primitive_id == "verify"
            for step in self.pipeline.definition.steps
        ):
            return True
        receipt = self.verification_receipt()
        gap_authority = getattr(self, "verification_gap_authority", None)
        expected_status = "failed" if gap_authority is not None else "complete"
        if (
            receipt is None
            or receipt.get("status") != expected_status
            or receipt.get("head_sha") != bound_head
        ):
            return False
        if not _verification_candidate_is_current(
            self.spec["cwd"], str(receipt["head_sha"])
        ):
            return False
        receipt_sha256 = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt_pointer = (
            self.spec_path.parent
            / "pipeline-verification"
            / str(receipt["operation_id"])
            / "receipt.json"
        ).resolve()
        admission_path = (
            self.trusted_vault
            / ".vault-meta"
            / "harness"
            / "review-runtime"
            / self.spec["operation_id"]
            / "review-launch-admission.json"
        )
        if admission_path.is_symlink():
            raise RuntimeWorkerError("review launch admission is invalid")
        admission_path.parent.mkdir(parents=True, exist_ok=True)
        admission_path.parent.chmod(0o700)
        admission = {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "verification_operation_id": str(receipt["operation_id"]),
                "verification_lane_id": str(receipt["lane_id"]),
                "verification_run_id": str(receipt["run_id"]),
                "receipt_sha256": receipt_sha256,
                "receipt_pointer": str(receipt_pointer),
                "head_sha": str(receipt["head_sha"]),
                "status": (
                    "admitted-with-gap"
                    if gap_authority is not None
                    else "admitted"
                ),
            }
        if gap_authority is not None:
            gap_path = (
                self.spec_path.parent
                / "pipeline-verification-gap-authority.json"
            ).resolve()
            admission.update(
                {
                    "gap_authority_pointer": str(gap_path),
                    "gap_authority_sha256": hashlib.sha256(
                        json.dumps(
                            gap_authority,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "decision_record_id": gap_authority[
                        "decision_record_id"
                    ],
                    "decision_record_sha256": gap_authority[
                        "decision_record_sha256"
                    ],
                }
            )
        _atomic_json(admission_path, admission)
        return True

    def _review_drive_started_marker(self, input_sha256: str) -> None:
        _atomic_json(
            self.marker_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "definition_sha256": self.pipeline.definition_sha256,
                "status": "started",
                "drive_sha256": input_sha256,
            },
        )

    def drive_review(self) -> bool:
        if not self._resolved_head_verification_ready():
            return False
        if not self._review_drive_candidate_is_current():
            return False
        if not self._publish_review_launch_admission():
            return False
        input_sha256 = self.review_drive_sha256()
        _atomic_json(
            self.marker_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "definition_sha256": self.pipeline.definition_sha256,
                "status": "pending",
                "drive_sha256": input_sha256,
            },
        )
        try:
            if self.review_launcher is not None:
                self.review_launcher(self.trusted_vault, self.spec["cwd"])
            else:
                runner = self.trusted_vault / "scripts" / "task-review-runner.py"
                if not runner.is_file() or runner.is_symlink():
                    raise RuntimeWorkerError(
                        "trusted task review runner is unavailable"
                    )
                launched = subprocess.run(
                    [
                        sys.executable,
                        str(runner),
                        "run",
                        "--worktree",
                        str(self.spec["cwd"]),
                    ],
                    cwd=self.trusted_vault,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                if launched.returncode != 0:
                    receipt = _review_drive_failure_receipt(
                        launched, drive_sha256=input_sha256
                    )
                    runtime_callback_io.record_review_drive_failure(
                        self.spec_path.parent,
                        receipt,
                    )
                    if (
                        receipt["reason_code"] == "review-contract-rejected"
                        and self._durable_review_in_progress()
                    ):
                        # The rejected drive raced a reviewer it already
                        # launched; the durable records prove the exact bound
                        # review is running, so the root keeps its normal
                        # waiting state instead of a false attention latch.
                        self._review_drive_started_marker(input_sha256)
                        return True
                    raise RuntimeWorkerError("automatic task review drive failed")
        except (OSError, RuntimeWorkerError, subprocess.TimeoutExpired) as exc:
            if (
                _review_drive_failure_code(str(exc))
                == "review-contract-rejected"
                and self._durable_review_in_progress()
            ):
                self._review_drive_started_marker(input_sha256)
                return True
            self.summary_attention(
                "review-drive-failed", AttentionReason.ATTENTION_REQUIRED
            )
            return False
        _atomic_json(
            self.marker_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "definition_sha256": self.pipeline.definition_sha256,
                "status": "started",
                "drive_sha256": input_sha256,
            },
        )
        return True

    def review_gate_state(self) -> dict[str, object]:
        gate_path = self.review.gate_root / "review-gate.json"
        if not gate_path.is_file() or gate_path.is_symlink():
            return {}
        state = json.loads(gate_path.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or state.get("dispatch_operation_id") != self.spec["operation_id"]
        ):
            raise RuntimeWorkerError("review gate state is invalid")
        return state

    def build_review_resolution_packet(
        self, gate_state: dict[str, object]
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        attempt = gate_state.get("attempt")
        exact_terminal = (
            isinstance(attempt, dict)
            and attempt.get("status") == "terminal"
        )
        awaiting = gate_state.get(
            "review_notification_evidence"
            if exact_terminal
            else "awaiting_resolution"
        )
        if not isinstance(awaiting, dict) or not awaiting:
            raise RuntimeWorkerError("review resolution evidence is unavailable")
        findings: list[dict[str, object]] = []
        reviewed_heads: set[str] = set()
        review_operation_ids: set[str] = set()
        review_callbacks: list[dict[str, object]] = []
        raw_lanes = gate_state.get("lanes")
        multi_lane = isinstance(raw_lanes, list) and len(raw_lanes) > 1
        for axis in sorted(awaiting):
            evidence = awaiting[axis]
            if not isinstance(evidence, dict):
                raise RuntimeWorkerError("review resolution evidence is invalid")
            pointer = Path(str(evidence.get("pointer") or ""))
            result_path = (self.review.gate_root / pointer).resolve()
            if (
                pointer.is_absolute()
                or self.review.gate_root not in result_path.parents
                or (not result_path.is_file())
                or result_path.is_symlink()
            ):
                raise RuntimeWorkerError("review result pointer is invalid")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            rows = result.get("findings") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("axis") != axis
                or (not isinstance(rows, list))
            ):
                raise RuntimeWorkerError("review result evidence is invalid")
            try:
                material_ids = [
                    axis_finding_id(
                        axis, str(finding.get("finding_id") or "")
                    )
                    if multi_lane
                    else str(finding.get("finding_id") or "")
                    for finding in rows
                    if isinstance(finding, dict)
                    and finding.get("severity") in MATERIAL_SEVERITIES
                ]
            except ReviewContractError as exc:
                raise RuntimeWorkerError(
                    "review finding identity is invalid"
                ) from exc
            callback = {
                "axis": axis,
                "round_operation_id": str(evidence.get("round_operation_id") or ""),
                "round_run_id": str(evidence.get("round_run_id") or ""),
                "callback_id": str(evidence.get("callback_id") or ""),
                "callback_sha256": str(evidence.get("callback_sha256") or ""),
            }
            if (
                evidence.get("material_finding_ids") != material_ids
                or any((not str(value) for value in callback.values()))
                or re.fullmatch("[0-9a-f]{64}", str(callback["callback_sha256"]))
                is None
            ):
                raise RuntimeWorkerError("review callback identity is invalid")
            review_operation_ids.add(str(evidence.get("review_operation_id") or ""))
            review_callbacks.append(callback)
            for finding in rows:
                if not isinstance(finding, dict):
                    raise RuntimeWorkerError("review finding evidence is invalid")
                qualified = dict(finding)
                if multi_lane:
                    try:
                        qualified["finding_id"] = axis_finding_id(
                            axis, str(finding.get("finding_id") or "")
                        )
                    except ReviewContractError as exc:
                        raise RuntimeWorkerError(
                            "review finding identity is invalid"
                        ) from exc
                findings.append(qualified)
            reviewed_heads.add(str(evidence.get("reviewed_head_sha") or ""))
        active_review_operation_id = str(
            gate_state.get("active_review_operation_id") or ""
        )
        if (
            not findings
            or len(findings) > 50
            or len(reviewed_heads) != 1
            or ("" in reviewed_heads)
            or (review_operation_ids != {active_review_operation_id})
            or (not active_review_operation_id)
        ):
            raise RuntimeWorkerError("review decision packet is invalid")
        try:
            review_identity_sha256 = review_transport_identity_sha256(
                active_review_operation_id, review_callbacks
            )
        except ResolutionError as exc:
            raise RuntimeWorkerError(
                "review decision packet identity is invalid"
            ) from exc
        material_findings = [
            finding
            for finding in findings
            if finding.get("severity") in MATERIAL_SEVERITIES
        ]
        if not material_findings:
            raise RuntimeWorkerError("review decision packet has no material findings")
        material_ids = [
            str(finding.get("finding_id") or "") for finding in material_findings
        ]
        if "" in material_ids or len(material_ids) != len(set(material_ids)):
            raise RuntimeWorkerError(
                "review decision packet finding identities are invalid"
            )
        reviewed_head = next(iter(reviewed_heads))
        packet = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "review_operation_id": active_review_operation_id,
            "review_callbacks": review_callbacks,
            "review_identity_sha256": review_identity_sha256,
            "reviewed_head_sha": reviewed_head,
            "allowed_dispositions": sorted(DISPOSITIONS),
            "resolution_path": ".task-review-resolution.json",
            "material_finding_ids": material_ids,
            "findings": findings,
        }
        packet["resolution_template"] = runtime_callback_io.review_resolution_template(
            packet=packet,
            material_findings=material_findings,
        )
        encoded = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_OUTBOX_BYTES:
            raise RuntimeWorkerError("review decision packet exceeds size cap")
        return packet, material_findings

    def validate_existing_review_packet(
        self,
        packet_path: Path,
        notified: object,
        packet: dict[str, object],
    ) -> None:
        if packet_path.is_symlink():
            raise RuntimeWorkerError("review decision packet cannot be a symlink")
        if not packet_path.exists():
            return
        current = json.loads(packet_path.read_text(encoding="utf-8"))
        stable_task_identity = (
            isinstance(current, dict)
            and current.get("schema_version") == 1
            and current.get("operation_id") == self.spec["operation_id"]
        )
        review_generation_changed = (
            isinstance(current, dict)
            and current.get("review_operation_id")
            != packet["review_operation_id"]
        )
        callbacks_changed = (
            isinstance(current, dict)
            and current.get("review_callbacks") != packet["review_callbacks"]
        )
        prior_packet_sha256 = ""
        if isinstance(current, dict):
            prior_packet_sha256 = hashlib.sha256(
                json.dumps(current, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        prior_generation_is_durable = (
            (callbacks_changed or review_generation_changed)
            and isinstance(notified, dict)
            and notified.get("status") == "sent"
            and notified.get("packet_sha256") == prior_packet_sha256
            and notified.get("reviewed_head_sha") == current.get("reviewed_head_sha")
        )
        if not stable_task_identity or (
            (callbacks_changed or review_generation_changed)
            and not prior_generation_is_durable
        ):
            raise RuntimeWorkerError("review decision packet identity changed")

    def ensure_review_resolution_template(
        self,
        *,
        packet: dict[str, object],
        material_findings: list[dict[str, object]],
    ) -> Path:
        return runtime_callback_io.ensure_review_resolution(
            self, packet=packet, material_findings=material_findings
        )

    def load_review_notification(self, notify_path: Path) -> object:
        if not notify_path.is_file() or notify_path.is_symlink():
            return None
        notified = json.loads(notify_path.read_text(encoding="utf-8"))
        if (
            not isinstance(notified, dict)
            or notified.get("schema_version") != 1
            or notified.get("operation_id") != self.spec["operation_id"]
        ):
            raise RuntimeWorkerError("review resolution notification is invalid")
        return notified

    def send_review_resolution_notification(
        self,
        *,
        packet: dict[str, object],
        packet_path: Path,
        resolution_path: Path,
        notify_path: Path,
        notified: object,
    ) -> None:
        encoded = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
        packet_sha256 = hashlib.sha256(encoded).hexdigest()
        drifted_head = ""
        if isinstance(notified, dict):
            if (
                notified.get("packet_sha256") == packet_sha256
                and notified.get("status") == "sent"
            ):
                try:
                    resolution = json.loads(
                        resolution_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    resolution = None
                resolved_head = (
                    str(resolution.get("resolved_head_sha") or "")
                    if isinstance(resolution, dict)
                    else ""
                )
                if re.fullmatch("[0-9a-f]{40,64}", resolved_head):
                    head_result = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=self.spec["cwd"],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    current_head = head_result.stdout.strip()
                    if (
                        head_result.returncode == 0
                        and re.fullmatch("[0-9a-f]{40,64}", current_head)
                        and current_head != resolved_head
                    ):
                        drifted_head = current_head
                if not drifted_head:
                    return
        if drifted_head:
            message = (
                "Coordinator-owned mechanism repair advanced the exact task HEAD "
                f"to {drifted_head} after the current review resolution was written. "
                "Keep the existing finding dispositions, inspect the intervening commits, "
                "then refresh resolved_head_sha and .task-summary.json for this exact HEAD. "
                "Do not relaunch review or replay verification/provider effects."
            )
            wake_id = hashlib.sha256(
                f"{packet_sha256}:{drifted_head}".encode()
            ).hexdigest()
            wake_spec = {
                "origin_surface": self.spec["surface_id"],
                "callback_wake": message,
            }
            wake_root = self.spec_path.parent / "review-resolution-wake"
            wake_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not runtime_callback_io.publish_callback_wake(
                wake_spec,
                wake_root,
                wake_id,
                self.cmux_adapter,
                resume_uncertain=runtime_callback_io.wake_resume_once(self, wake_id),
            ):
                raise RuntimeWorkerError(
                    "review resolution rebind notification effect is uncertain"
                )
            return
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "packet_sha256": packet_sha256,
                "reviewed_head_sha": packet["reviewed_head_sha"],
                "summary_sha256": self.digest,
                "status": "pending",
            },
        )
        message = f"Typed review findings are ready in {packet_path.name}. Resolve every material finding in {resolution_path.name} as applied, rejected, or out-of-scope; include bounded rationale, and a durable follow-up pointer for out-of-scope. Commit a new HEAD and set resolved_head_sha; for a material fork use the task_escalation.py raise contract. Do not launch review. Refresh .task-summary.json after the commit so it covers the final HEAD. Then end the current model turn while keeping this session open. The code-owned observer owns healthy waiting; act again in this same session only on the next typed callback wake, typed escalation, or explicit coordinator request."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("review resolution notification is too large")
        wake_spec = {
            "origin_surface": self.spec["surface_id"],
            "callback_wake": message,
        }
        wake_root = self.spec_path.parent / "review-resolution-wake"
        wake_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not runtime_callback_io.publish_callback_wake(
            wake_spec,
            wake_root,
            packet_sha256,
            self.cmux_adapter,
            resume_uncertain=runtime_callback_io.wake_resume_once(self, packet_sha256),
        ):
            raise RuntimeWorkerError(
                "review resolution notification effect is uncertain"
            )
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "packet_sha256": packet_sha256,
                "reviewed_head_sha": packet["reviewed_head_sha"],
                "summary_sha256": self.digest,
                "status": "sent",
            },
        )

    def notify_review_resolution(self, gate_state: dict[str, object]) -> None:
        packet, material_findings = self.build_review_resolution_packet(gate_state)
        packet_path = self.spec["cwd"] / ".task-review.json"
        notify_path = self.spec_path.parent / "pipeline-review-resolution-notify.json"
        notified = self.load_review_notification(notify_path)
        self.validate_existing_review_packet(packet_path, notified, packet)
        _atomic_json(packet_path, packet)
        resolution_path = self.ensure_review_resolution_template(
            packet=packet, material_findings=material_findings
        )
        if self.review_resolution_correction_sent:
            return
        self.send_review_resolution_notification(
            packet=packet,
            packet_path=packet_path,
            resolution_path=resolution_path,
            notify_path=notify_path,
            notified=notified,
        )

    def wait_for_summary_refresh_after_resolution(
        self, gate_state: dict[str, object], *, target_head: str = ""
    ) -> bool:
        resolution_path = (
            self.spec_path.parent / "pipeline-review-resolution-notify.json"
        )
        if not resolution_path.is_file():
            return False
        if resolution_path.is_symlink():
            raise RuntimeWorkerError(
                "review resolution notification cannot be a symlink"
            )
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        reviewed_head = str(resolution.get("reviewed_head_sha") or "")
        initial_summary = str(resolution.get("summary_sha256") or "")
        context = gate_state.get("context")
        approved_head = target_head or (
            str(context.get("head_sha") or "") if isinstance(context, dict) else ""
        )
        if (
            resolution.get("schema_version") != 1
            or resolution.get("operation_id") != self.spec["operation_id"]
            or resolution.get("status") != "sent"
            or (not re.fullmatch("[0-9a-f]{40,64}", reviewed_head))
            or (not re.fullmatch("[0-9a-f]{64}", initial_summary))
            or (not re.fullmatch("[0-9a-f]{40,64}", approved_head))
        ):
            raise RuntimeWorkerError("review resolution summary binding is invalid")
        if approved_head == reviewed_head or self.digest != initial_summary:
            return False
        notify_path = self.spec_path.parent / "pipeline-summary-refresh-notify.json"
        marker = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "approved_head_sha": approved_head,
            "summary_sha256": self.digest,
        }
        if notify_path.is_file():
            if notify_path.is_symlink():
                raise RuntimeWorkerError(
                    "summary refresh notification cannot be a symlink"
                )
            existing = json.loads(notify_path.read_text(encoding="utf-8"))
            if (
                all((existing.get(field) == value for field, value in marker.items()))
                and existing.get("status") == "sent"
            ):
                return True
        _atomic_json(notify_path, {**marker, "status": "pending"})
        message = f"Refresh .task-summary.json before review finalization: its body still describes the pre-resolution HEAD. Preserve the exact schema/type/title/session, cover every applied or rejected finding, and summarize final HEAD {approved_head}."
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        _atomic_json(notify_path, {**marker, "status": "sent"})
        return True
