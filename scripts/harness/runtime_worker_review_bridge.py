"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations
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
from review_contract import ReviewContractError, axis_finding_id


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
                _atomic_json(
                    self.spec_path.parent / "callback-error.json",
                    {"schema_version": 1, "status": "callback-target-invalid"},
                )
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
            try:
                submitted = submit_stable_review_input(
                    vault_root=self.trusted_vault,
                    worktree=self.spec["product_root"],
                    callback_path=callback_path,
                )
            except (OSError, RuntimeWorkerError, subprocess.TimeoutExpired):
                submitted = subprocess.CompletedProcess((), 3, "", "")
            if _submit_failure_requires_attention(submitted, callback_path):
                self.summary_attention("review-input-invalid")
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
        self.callback_handled = True
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
            if not publish_callback_wake(
                self.spec,
                self.spec_path.parent,
                envelope.callback_id,
                self.cmux_adapter,
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

    def drive_review(self) -> bool:
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
                    raise RuntimeWorkerError("automatic task review drive failed")
        except (OSError, RuntimeWorkerError, subprocess.TimeoutExpired):
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
        awaiting = gate_state.get("awaiting_resolution")
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
        stable_identity = (
            isinstance(current, dict)
            and current.get("schema_version") == 1
            and current.get("operation_id") == self.spec["operation_id"]
            and current.get("review_operation_id") == packet["review_operation_id"]
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
            callbacks_changed
            and isinstance(notified, dict)
            and notified.get("status") == "sent"
            and notified.get("packet_sha256") == prior_packet_sha256
            and notified.get("reviewed_head_sha") == current.get("reviewed_head_sha")
        )
        if not stable_identity or (
            callbacks_changed and not prior_generation_is_durable
        ):
            raise RuntimeWorkerError("review decision packet identity changed")

    def ensure_review_resolution_template(
        self,
        *,
        packet: dict[str, object],
        material_findings: list[dict[str, object]],
    ) -> Path:
        resolution_path = self.spec["cwd"] / ".task-review-resolution.json"
        if resolution_path.is_symlink():
            raise RuntimeWorkerError("review resolution response cannot be a symlink")
        write_template = True
        if resolution_path.exists():
            current = json.loads(resolution_path.read_text(encoding="utf-8"))
            if (
                not isinstance(current, dict)
                or current.get("schema_version") != 1
                or current.get("operation_id") != self.spec["operation_id"]
            ):
                raise RuntimeWorkerError("review resolution response identity changed")
            write_template = (
                current.get("reviewed_head_sha") != packet["reviewed_head_sha"]
                or current.get("review_identity_sha256")
                != packet["review_identity_sha256"]
            )
        if write_template:
            _atomic_json(
                resolution_path,
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "review_identity_sha256": packet["review_identity_sha256"],
                    "reviewed_head_sha": packet["reviewed_head_sha"],
                    "resolved_head_sha": "",
                    "resolutions": [
                        {
                            "finding_id": str(finding.get("finding_id") or ""),
                            "disposition": "",
                            "rationale": "",
                            "follow_up": "",
                        }
                        for finding in material_findings
                    ],
                },
            )
        return resolution_path

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
        if isinstance(notified, dict):
            if (
                notified.get("packet_sha256") == packet_sha256
                and notified.get("status") == "sent"
            ):
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
        message = f"Typed review findings are ready in {packet_path.name}. Resolve every material finding in {resolution_path.name} as applied, rejected, or out-of-scope; include bounded rationale, and a durable follow-up pointer for out-of-scope. Commit a new HEAD and set resolved_head_sha; for a material fork use the task_escalation.py raise contract. Do not launch review. Refresh .task-summary.json after the commit so it covers the final HEAD. Remain available for same-session verification."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("review resolution notification is too large")
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
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
