"""Exactly-once orchestration for the bounded structural-pivot review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol

from model_routing_config import RoutingConfig, RoutingError

from review_contract import (
    require_unique_finding_ids,
    require_unqualified_finding_ids,
    validate_finding,
)

from ..contracts import CallbackEnvelope, OperationRecord, OperationSpec, RuntimeRoute, to_dict
from ..finalization_pivot import (
    FinalizationPivotError,
    MAX_RECOMMENDATION_BYTES,
    PIVOT_ROUTE_ALIAS,
    compile_pivot_packet,
    load_accepted_pivot_receipt,
    pivot_packet_sha256,
    pivot_receipt_path,
    pivot_required,
    validate_pivot_receipt,
)
from ..review_submit import publish_review_input_template, round_schema_lines
from ..runtime_session_contracts import RuntimeSessionRequest
from ..review_workspace import ReviewWorkspaceBinding
from ..state_machine import TERMINAL
from ..store import OperationStore, StoreError
from .review import (
    ReviewFinding,
    ReviewLaneSession,
    ReviewResult,
    ReviewRound,
    prepare_review_round,
)


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PACKET_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "lineage_id",
        "origin_task_id",
        "plan_sha256",
        "outcome_contract_sha256",
        "material_cycles",
    }
)


class StructuralPivotError(RuntimeError):
    """Durable pivot evidence is absent, conflicting, or ambiguous."""


@dataclass(frozen=True)
class StructuralPivotResult:
    """One typed reconciliation result with no inferred authority."""

    status: Literal[
        "not-required", "reserved", "in-flight", "accepted", "attention"
    ]
    operation: OperationRecord | None = None
    packet_path: Path | None = None
    packet_sha256: str = ""
    reason: str = ""


class StructuralPivotRuntime(Protocol):
    """Existing runtime/session operations used by this workflow."""

    def start(
        self,
        request: RuntimeSessionRequest,
        *,
        on_surface_opened: Callable[[object], None] | None = None,
    ) -> object: ...

    def status(self, owner_id: str, operation_id: str) -> object: ...

    def accept_callback(self, envelope: CallbackEnvelope) -> object: ...

    def request_exit(self, owner_id: str, operation_id: str) -> object: ...

    def cleanup(self, owner_id: str, operation_id: str) -> object: ...


def _canonical_packet(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    packet = compile_pivot_packet(snapshot)
    if set(packet) != PACKET_FIELDS:
        raise StructuralPivotError("structural pivot packet shape is invalid")
    try:
        lineage = str(uuid.UUID(str(packet["lineage_id"])))
        origin = str(uuid.UUID(str(packet["origin_task_id"])))
    except (ValueError, TypeError, AttributeError) as exc:
        raise StructuralPivotError("structural pivot lineage is invalid") from exc
    if (
        lineage != packet["lineage_id"]
        or origin != packet["origin_task_id"]
        or not SHA256.fullmatch(str(packet["plan_sha256"]))
        or not SHA256.fullmatch(str(packet["outcome_contract_sha256"]))
    ):
        raise StructuralPivotError("structural pivot authority binding is invalid")
    encoded = (
        json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return packet, encoded


def _operation_id(packet: Mapping[str, Any]) -> str:
    digest = pivot_packet_sha256(packet)
    return str(
        uuid.uuid5(
            uuid.UUID(packet["lineage_id"]),
            f"structural-pivot:{digest}:{PIVOT_ROUTE_ALIAS}",
        )
    )


def pivot_operation_id(snapshot: Mapping[str, Any]) -> str:
    """Derive the sole operation identity from lineage, packet, and route."""

    packet, _encoded = _canonical_packet(snapshot)
    return _operation_id(packet)


def _atomic_publish(path: Path, expected: bytes) -> None:
    directory = path.parent
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise StructuralPivotError("structural pivot packet directory is invalid")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise StructuralPivotError("structural pivot packet must be a regular file")
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise StructuralPivotError("structural pivot packet is unreadable") from exc
        if actual != expected:
            raise StructuralPivotError("structural pivot packet binding changed")
        return
    descriptor, raw = tempfile.mkstemp(prefix=".packet.", dir=directory)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class StructuralPivotWorkflow:
    """Reserve and reconcile one structural review on existing Harness owners."""

    def __init__(
        self,
        store: OperationStore,
        config: RoutingConfig,
        *,
        verification_profile: str,
        verification_profile_sha256: str,
        ledger_root: Path | None = None,
        fault_observer: Callable[[str], None] | None = None,
    ) -> None:
        if not SHA256.fullmatch(verification_profile_sha256):
            raise StructuralPivotError(
                "structural pivot verification profile digest is invalid"
            )
        self.store = store
        self.config = config
        self.verification_profile = verification_profile
        self.verification_profile_sha256 = verification_profile_sha256
        self.ledger_root = (
            Path(ledger_root).expanduser().resolve()
            if ledger_root is not None
            else self.store.root / "finalization-ledger"
        )
        self._fault_observer = fault_observer

    def _observe(self, boundary: str) -> None:
        if self._fault_observer is not None:
            self._fault_observer(boundary)

    def _spec(
        self,
        packet: Mapping[str, Any],
        *,
        packet_path: Path,
        root_operation_id: str,
    ) -> tuple[OperationSpec, str, str]:
        try:
            route = self.config.finalization_route(PIVOT_ROUTE_ALIAS)
        except RoutingError as exc:
            raise StructuralPivotError(
                "registered structural pivot route is unavailable"
            ) from exc
        packet_sha256 = pivot_packet_sha256(packet)
        operation_id = _operation_id(packet)
        identity = json.dumps(
            {
                "lineage_id": packet["lineage_id"],
                "packet_sha256": packet_sha256,
                "route_alias": PIVOT_ROUTE_ALIAS,
                "operation_id": operation_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        idempotency_key = hashlib.sha256(identity).hexdigest()
        spec = OperationSpec(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            kind="structural-pivot",
            owner_id=str(packet["lineage_id"]),
            route=RuntimeRoute(
                route["runtime"],
                route["model"],
                route["effort"],
                "reviewer-callback",
                self.config.fingerprint,
            ),
            context_manifest=packet_path.relative_to(self.store.root).as_posix(),
            verification_profile=self.verification_profile,
            contract_sha256=packet_sha256,
            parent_operation_id=root_operation_id,
            root_operation_id=root_operation_id,
        )
        lane_id = hashlib.sha256(f"{idempotency_key}:lane".encode()).hexdigest()[:32]
        run_id = hashlib.sha256(f"{idempotency_key}:run".encode()).hexdigest()[:32]
        return spec, lane_id, run_id

    def reserve(
        self,
        snapshot: Mapping[str, Any],
        *,
        root_operation_id: str,
    ) -> StructuralPivotResult:
        """Publish/reuse the frozen packet and reserve before provider effect."""

        try:
            if not pivot_required(snapshot):
                return StructuralPivotResult("not-required")
            packet, encoded = _canonical_packet(snapshot)
            if root_operation_id != packet["lineage_id"]:
                raise StructuralPivotError(
                    "structural pivot root does not match its ledger lineage"
                )
            packet_path = (
                self.store.root
                / "structural-pivots"
                / str(packet["lineage_id"])
                / "packet.json"
            )
            _atomic_publish(packet_path, encoded)
            self._observe("packet-published")
            spec, lane_id, run_id = self._spec(
                packet,
                packet_path=packet_path,
                root_operation_id=root_operation_id,
            )
            record = self.store.create(spec, lane_id=lane_id, run_id=run_id)
            self._observe("operation-reserved")
            return StructuralPivotResult(
                "reserved" if record.state == "created" else "in-flight",
                record,
                packet_path,
                pivot_packet_sha256(packet),
            )
        except (
            FinalizationPivotError,
            OSError,
            StoreError,
            StructuralPivotError,
            ValueError,
        ) as exc:
            return StructuralPivotResult("attention", reason=str(exc))

    def _pivot_root(self, lineage_id: str) -> Path:
        return self.store.root / "structural-pivots" / lineage_id

    @staticmethod
    def _record(value: object) -> OperationRecord:
        record = value if isinstance(value, OperationRecord) else getattr(value, "record", None)
        if not isinstance(record, OperationRecord):
            raise StructuralPivotError("pivot runtime returned no operation record")
        return record

    def _round(self, parent: OperationRecord) -> ReviewRound:
        lane = ReviewLaneSession(
            axis="openai-holistic",
            owner_id=parent.spec.owner_id,
            operation_id=parent.spec.operation_id,
            lane_id=parent.lane_id,
            run_id=parent.run_id,
            surface_id="",
            checkpoint="",
            spec=parent.spec,
            verification_iteration=0,
            max_verify_iterations=0,
            # This synthetic terminal view is used only to derive the existing
            # deterministic child-round contract before any provider effect.
            state="complete",
        )
        return prepare_review_round(self.store, lane)

    def _prepare_review_artifacts(
        self,
        parent: OperationRecord,
        round_: ReviewRound,
        *,
        exact_head: str,
        worktree: Path,
    ) -> tuple[str, str]:
        pivot_root = self._pivot_root(parent.spec.owner_id)
        callback_dir = pivot_root / "callbacks"
        callback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        callback_dir.chmod(0o700)
        callback_path = callback_dir / ".review-callback.json"
        input_path = callback_dir / ".review-input.json"
        meta = publish_review_input_template(
            state_root=pivot_root,
            state_dir=callback_dir,
            worktree=worktree,
            meta={
                "schema_version": 1,
                "transport": "review-round",
                "operation_id": round_.operation_id,
                "run_id": round_.run_id,
                "review_id": parent.spec.operation_id,
                "parent_session_operation_id": parent.spec.operation_id,
                "review_mode": "simple",
                "axis": round_.axis,
                "verification_iteration": 0,
                "worktree": str(worktree),
                "task_name": parent.spec.owner_id,
                "head_sha": exact_head,
                "review_purpose": "implementation",
                "review_boundary_input_sha256": parent.spec.contract_sha256,
                "verification_profile": {
                    "name": self.verification_profile,
                    "sha256": self.verification_profile_sha256,
                },
                "route": to_dict(parent.spec.route),
            },
        )
        meta_bytes = (
            json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        _atomic_publish(callback_dir / ".review-meta.json", meta_bytes)
        submit = shlex.join(
            (
                str(Path(sys.executable).resolve()),
                str(worktree / "scripts/harness/review_submit.py"),
                "--worktree",
                str(worktree),
                "--state-dir",
                str(callback_dir),
                "--input-file",
                str(input_path),
            )
        )
        prompt = "\n".join(
            (
                "# Structural pivot review",
                "",
                "Analyze the frozen third-failure packet and the product read-only.",
                f"Frozen packet: `{self.store.root / parent.spec.context_manifest}`.",
                f"Product worktree (read-only): `{worktree}`.",
                "Do not edit files, create commits, resolve findings, or advance Git HEAD.",
                "Return only structural analysis through the existing review-input contract.",
                *round_schema_lines(verification_iteration=0),
                f"Write that exact JSON to `{input_path}`.",
                "Then submit it through this exact command:",
                "",
                f"`{submit}`",
                "",
            )
        ).encode()
        prompt_path = pivot_root / "prompt.md"
        _atomic_publish(prompt_path, prompt)
        return (
            prompt_path.relative_to(self.store.root).as_posix(),
            callback_path.relative_to(self.store.root).as_posix(),
        )

    def start(
        self,
        snapshot: Mapping[str, Any],
        *,
        root_operation_id: str,
        runtime: StructuralPivotRuntime,
        origin_surface: str,
        review_workspace: ReviewWorkspaceBinding,
        worktree: Path,
        callback_wake: str = "",
    ) -> StructuralPivotResult:
        """Launch or observe the sole registered reviewer without replay."""

        reserved = self.reserve(snapshot, root_operation_id=root_operation_id)
        if reserved.status not in {"reserved", "in-flight"} or reserved.operation is None:
            return reserved
        try:
            worktree = Path(worktree).expanduser().resolve()
            if worktree.is_symlink() or not worktree.is_dir():
                raise StructuralPivotError("structural pivot product root is invalid")
            parent = self.store.read(root_operation_id, reserved.operation.spec.operation_id)
            if parent.state in TERMINAL:
                return self.reconcile(
                    snapshot, root_operation_id=root_operation_id, runtime=runtime
                )
            round_ = self._round(parent)
            self._observe("round-prepared")
            packet = compile_pivot_packet(snapshot)
            prompt_pointer, callback_pointer = self._prepare_review_artifacts(
                parent,
                round_,
                exact_head=str(packet["material_cycles"][-1]["exact_head"]),
                worktree=worktree,
            )
            request = RuntimeSessionRequest(
                spec=parent.spec,
                lane_id=parent.lane_id,
                run_id=parent.run_id,
                origin_surface=review_workspace.anchor_surface_id,
                cwd=self.store.root,
                prompt_pointer=prompt_pointer,
                callback_pointer=callback_pointer,
                placement="split",
                product_root=worktree,
                initial_callback_operation_id=round_.operation_id,
                initial_callback_run_id=round_.run_id,
                callback_wake=callback_wake,
                model_restart_limit=0,
            )
            observed = self._record(
                runtime.start(
                    request,
                    on_surface_opened=review_workspace.validate_member,
                )
            )
            self._observe("runtime-started")
            if observed.spec != parent.spec:
                raise StructuralPivotError("pivot runtime operation identity changed")
            return StructuralPivotResult(
                "in-flight",
                observed,
                reserved.packet_path,
                reserved.packet_sha256,
            )
        except Exception as exc:
            return StructuralPivotResult(
                "attention",
                reserved.operation,
                reserved.packet_path,
                reserved.packet_sha256,
                str(exc),
            )

    def _parse_callback(
        self, path: Path, round_: ReviewRound
    ) -> tuple[CallbackEnvelope, ReviewResult]:
        if (
            path.is_symlink()
            or path.parent.is_symlink()
            or not path.is_file()
            or path.stat().st_size > CallbackEnvelope.MAX_PAYLOAD_BYTES + 10_000
        ):
            raise StructuralPivotError("structural pivot callback must be a regular file")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StructuralPivotError("structural pivot callback is unreadable") from exc
        expected_envelope_fields = {
            "schema_version",
            "callback_id",
            "operation_id",
            "run_id",
            "kind",
            "payload",
            "payload_sha256",
        }
        if not isinstance(raw, dict) or set(raw) != expected_envelope_fields:
            raise StructuralPivotError("structural pivot callback shape is invalid")
        envelope = CallbackEnvelope(**raw)
        payload = dict(envelope.payload)
        if set(payload) != {
            "schema_version",
            "parent_session_operation_id",
            "axis",
            "verification_iteration",
            "verdict",
            "findings",
        }:
            raise StructuralPivotError("structural pivot review payload shape is invalid")
        if (
            envelope.operation_id != round_.operation_id
            or envelope.run_id != round_.run_id
            or envelope.kind != "review"
            or payload.get("schema_version") != 1
            or payload.get("parent_session_operation_id") != round_.parent_operation_id
            or payload.get("axis") != round_.axis
            or payload.get("verification_iteration") != 0
            or not isinstance(payload.get("findings"), list)
            or len(payload["findings"]) > 50
        ):
            raise StructuralPivotError("structural pivot callback identity changed")
        normalized = [
            validate_finding(item, f"structural pivot findings[{index}]")
            for index, item in enumerate(payload["findings"])
        ]
        require_unqualified_finding_ids(
            round_.axis, (item["finding_id"] for item in normalized)
        )
        require_unique_finding_ids(
            (item["finding_id"] for item in normalized),
            "structural pivot finding_id values",
        )
        findings = tuple(
            ReviewFinding(axis=round_.axis, **item) for item in normalized
        )
        result = ReviewResult(
            round_.axis,
            str(payload.get("verdict") or ""),
            findings,
            0,
        )
        return envelope, result

    @staticmethod
    def _recommendation(result: ReviewResult, accepted_sha256: str) -> str:
        rows = [
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity,
                "file": finding.file,
                "line": finding.line,
                "summary": finding.summary,
                "evidence": finding.evidence,
                "recommendation": finding.recommendation,
            }
            for finding in result.findings
        ]

        def encode(included: list[dict[str, Any]]) -> str:
            return json.dumps(
                {
                    "verdict": result.verdict,
                    "accepted_review_sha256": accepted_sha256,
                    "finding_count": len(rows),
                    "findings": included,
                    "omitted_findings": len(rows) - len(included),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        included: list[dict[str, Any]] = []
        for row in rows:
            candidate = encode([*included, row])
            if len(candidate.encode()) > MAX_RECOMMENDATION_BYTES:
                break
            included.append(row)
        rendered = encode(included)
        if not rendered or len(rendered.encode()) > MAX_RECOMMENDATION_BYTES:
            raise StructuralPivotError("structural pivot recommendation is not total")
        return rendered

    def _finish_child(self, child: OperationRecord) -> None:
        current = child
        if current.state in TERMINAL:
            return
        sequence = ("finalizing", "exiting", "complete")
        remaining = (
            sequence[sequence.index(current.state) + 1 :]
            if current.state in sequence
            else sequence
        )
        for target in remaining:
            self.store.transition(
                current.spec.owner_id, current.spec.operation_id, target
            )
            current = self.store.read(
                current.spec.owner_id, current.spec.operation_id
            )
            self._observe(f"child-{target}")

    def _receipt(
        self,
        snapshot: Mapping[str, Any],
        result: ReviewResult,
        accepted_sha256: str,
    ) -> tuple[Path, dict[str, Any]]:
        packet = compile_pivot_packet(snapshot)
        receipt = {
            "schema_version": 1,
            "kind": "structural-pivot-receipt",
            "lineage_id": packet["lineage_id"],
            "packet_sha256": pivot_packet_sha256(packet),
            "route_alias": PIVOT_ROUTE_ALIAS,
            "read_only": True,
            "structural_recommendation": self._recommendation(
                result, accepted_sha256
            ),
            "status": "accepted",
        }
        validate_pivot_receipt(receipt, snapshot=snapshot)
        path = pivot_receipt_path(self.ledger_root, str(packet["lineage_id"]))
        encoded = (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        _atomic_publish(path, encoded)
        return path, receipt

    @staticmethod
    def _validate_durable_acceptance(
        receipt: Mapping[str, Any], child: OperationRecord
    ) -> None:
        try:
            recommendation = json.loads(str(receipt["structural_recommendation"]))
        except (KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StructuralPivotError(
                "structural pivot recommendation evidence is invalid"
            ) from exc
        if (
            child.spec.kind != "review-round"
            or child.state != "complete"
            or not child.accepted_callback_id
            or child.accepted_callback_kind != "review"
            or not child.accepted_callback_sha256
            or not isinstance(recommendation, dict)
            or set(recommendation)
            != {
                "verdict",
                "accepted_review_sha256",
                "finding_count",
                "findings",
                "omitted_findings",
            }
            or recommendation.get("accepted_review_sha256")
            != child.accepted_callback_sha256
            or recommendation.get("verdict")
            not in {"approve", "changes-requested", "blocked"}
            or type(recommendation.get("finding_count")) is not int
            or type(recommendation.get("omitted_findings")) is not int
            or not isinstance(recommendation.get("findings"), list)
            or len(recommendation["findings"])
            + recommendation["omitted_findings"]
            != recommendation["finding_count"]
        ):
            raise StructuralPivotError(
                "structural pivot receipt lacks its accepted callback operation"
            )

    @staticmethod
    def _resource_free(record: OperationRecord) -> bool:
        resources = record.resources
        return record.state == "complete" and not any(
            (
                resources.surface_id,
                resources.process_group,
                resources.supervisor_pid,
                resources.process_identity,
                resources.supervisor_identity,
            )
        )

    def reconcile(
        self,
        snapshot: Mapping[str, Any],
        *,
        root_operation_id: str,
        runtime: StructuralPivotRuntime,
    ) -> StructuralPivotResult:
        """Accept one exact callback, publish its receipt, and prove cleanup."""

        reserved = self.reserve(snapshot, root_operation_id=root_operation_id)
        if reserved.status not in {"reserved", "in-flight"} or reserved.operation is None:
            return reserved
        try:
            parent = self.store.read(root_operation_id, reserved.operation.spec.operation_id)
            round_ = self._round(parent)
            child = self.store.read(root_operation_id, round_.operation_id)
            existing_receipt = load_accepted_pivot_receipt(
                self.ledger_root, snapshot=snapshot
            )
            if existing_receipt is not None:
                self._validate_durable_acceptance(existing_receipt, child)
                if self._resource_free(parent):
                    return StructuralPivotResult(
                        "accepted", parent, reserved.packet_path, reserved.packet_sha256
                    )
            callback_path = self._pivot_root(root_operation_id) / "callbacks" / ".review-callback.json"
            if existing_receipt is None:
                if not callback_path.exists() and not callback_path.is_symlink():
                    if parent.state in TERMINAL or parent.state == "attention-required":
                        raise StructuralPivotError(
                            "pivot reviewer terminated without an accepted callback"
                        )
                    return StructuralPivotResult(
                        "reserved" if parent.state == "created" else "in-flight",
                        parent,
                        reserved.packet_path,
                        reserved.packet_sha256,
                    )
                envelope, result = self._parse_callback(callback_path, round_)
                runtime.accept_callback(envelope)
                self._observe("callback-accepted")
                child = self.store.read(root_operation_id, round_.operation_id)
                self._finish_child(child)
                self._receipt(snapshot, result, envelope.payload_sha256)
                self._observe("receipt-published")
            exit_result = self._record(
                runtime.request_exit(root_operation_id, parent.spec.operation_id)
            )
            cleaned = (
                exit_result
                if self._resource_free(exit_result)
                else self._record(
                    runtime.cleanup(root_operation_id, parent.spec.operation_id)
                )
            )
            if self._resource_free(cleaned):
                self._observe("cleanup-complete")
                return StructuralPivotResult(
                    "accepted",
                    cleaned,
                    reserved.packet_path,
                    reserved.packet_sha256,
                )
            if cleaned.state == "attention-required" or cleaned.state in TERMINAL:
                raise StructuralPivotError("structural pivot cleanup requires attention")
            return StructuralPivotResult(
                "in-flight",
                cleaned,
                reserved.packet_path,
                reserved.packet_sha256,
            )
        except Exception as exc:
            return StructuralPivotResult(
                "attention",
                reserved.operation,
                reserved.packet_path,
                reserved.packet_sha256,
                str(exc),
            )


__all__ = [
    "StructuralPivotError",
    "StructuralPivotResult",
    "StructuralPivotWorkflow",
    "pivot_operation_id",
]
