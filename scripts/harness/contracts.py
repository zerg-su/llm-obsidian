"""Strict, stdlib-only JSON contracts used by the runtime harness."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, TypeVar


ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
RUNTIMES = frozenset({"claude", "codex"})


class ContractError(ValueError):
    """A typed harness value violates its public contract."""


class AttentionReason(str, enum.Enum):
    RUNTIME_UNAVAILABLE = "runtime-unavailable"
    CAPABILITY_MISMATCH = "capability-mismatch"
    SURFACE_OPEN_FAILED = "surface-open-failed"
    PROCESS_START_FAILED = "process-start-failed"
    PROMPT_UNKNOWN = "prompt-unknown"
    CALLBACK_INVALID = "callback-invalid"
    CALLBACK_TIMEOUT = "callback-timeout"
    VERIFICATION_FAILED = "verification-failed"
    RETRY_EXHAUSTED = "retry-exhausted"
    SURFACE_LOST = "surface-lost"
    PROCESS_ORPHANED = "process-orphaned"
    CLEANUP_INCOMPLETE = "cleanup-incomplete"
    CONTRACT_DRIFT = "contract-drift"
    ATTENTION_REQUIRED = "attention-required"


class EffectOutcome(str, enum.Enum):
    NONE = "none"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded identifier")
    return value


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase sha256")
    return value


def _relative_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ContractError(f"{label} must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ContractError(f"{label} must remain owner-relative")
    return path.as_posix()


def _json_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    try:
        canonical = json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be JSON serializable") from exc
    if not isinstance(canonical, dict):
        raise ContractError(f"{label} must be an object")
    return MappingProxyType(canonical)


@dataclass(frozen=True)
class RuntimeRoute:
    runtime: str
    model: str
    effort: str
    profile: str
    routing_sha256: str

    def __post_init__(self) -> None:
        if self.runtime not in RUNTIMES:
            raise ContractError("runtime must be claude or codex")
        _identifier(self.model, "model")
        _identifier(self.effort, "effort")
        _identifier(self.profile, "profile")
        _sha256(self.routing_sha256, "routing_sha256")


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    idempotency_key: str
    kind: str
    owner_id: str
    route: RuntimeRoute
    context_manifest: str
    verification_profile: str
    keep_open: bool = False
    contract_sha256: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported OperationSpec schema")
        for value, label in (
            (self.operation_id, "operation_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.kind, "kind"),
            (self.owner_id, "owner_id"),
            (self.verification_profile, "verification_profile"),
        ):
            _identifier(value, label)
        _relative_path(self.context_manifest, "context_manifest")
        if self.contract_sha256:
            _sha256(self.contract_sha256, "contract_sha256")


@dataclass(frozen=True)
class ContextPacketManifest:
    packet_id: str
    operation_id: str
    files: tuple[str, ...]
    content_sha256: str
    byte_count: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.byte_count < 0:
            raise ContractError("invalid ContextPacketManifest metadata")
        _identifier(self.packet_id, "packet_id")
        _identifier(self.operation_id, "operation_id")
        _sha256(self.content_sha256, "content_sha256")
        normalized = tuple(_relative_path(path, "files item") for path in self.files)
        if normalized != self.files or len(set(normalized)) != len(normalized):
            raise ContractError("manifest files must be normalized and unique")


@dataclass(frozen=True)
class OwnedResources:
    surface_id: str = ""
    process_group: int = 0
    supervisor_pid: int = 0
    process_identity: str = ""
    supervisor_identity: str = ""

    def __post_init__(self) -> None:
        if self.surface_id:
            _identifier(self.surface_id, "surface_id")
        if self.process_group < 0 or self.supervisor_pid < 0:
            raise ContractError("resource process identifiers cannot be negative")
        if self.process_identity:
            _sha256(self.process_identity, "process_identity")
        if self.supervisor_identity:
            _sha256(self.supervisor_identity, "supervisor_identity")
        if self.process_identity and self.process_group <= 1:
            raise ContractError("process identity requires a process group")
        if self.supervisor_identity and self.supervisor_pid <= 1:
            raise ContractError("supervisor identity requires a supervisor PID")


@dataclass(frozen=True)
class OperationRecord:
    spec: OperationSpec
    state: str
    revision: int
    lane_id: str
    run_id: str
    resources: OwnedResources = field(default_factory=OwnedResources)
    attempt: int = 0
    attempt_limit: int = 3
    model_restarts: int = 0
    model_restart_limit: int = 1
    deadline_at: float = 0.0
    token_limit: int = 200_000
    tokens_used: int = 0
    attention_reason: AttentionReason | None = None
    resume_state: str = ""
    pending_effect: str = ""
    effect_id: str = ""
    effect_outcome: EffectOutcome = EffectOutcome.NONE
    accepted_callback_id: str = ""
    accepted_callback_kind: str = ""
    accepted_callback_sha256: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.revision < 0 or self.attempt < 0:
            raise ContractError("invalid OperationRecord metadata")
        integer_budgets = (
            self.attempt_limit,
            self.model_restarts,
            self.model_restart_limit,
            self.token_limit,
            self.tokens_used,
        )
        if (
            any(type(value) is not int or value < 0 for value in integer_budgets)
            or self.attempt_limit < 1
            or self.model_restart_limit < 0
            or self.token_limit < 1
            or self.attempt > self.attempt_limit
            or self.model_restarts > self.model_restart_limit
            or self.tokens_used > self.token_limit
            or not isinstance(self.deadline_at, (int, float))
            or isinstance(self.deadline_at, bool)
            or not math.isfinite(float(self.deadline_at))
            or self.deadline_at < 0
        ):
            raise ContractError("invalid persisted operation budget")
        _identifier(self.state, "state")
        _identifier(self.lane_id, "lane_id")
        _identifier(self.run_id, "run_id")
        if self.resume_state:
            _identifier(self.resume_state, "resume_state")
            if self.state != "attention-required":
                raise ContractError(
                    "resume_state is valid only while attention is required"
                )
        if self.pending_effect:
            _identifier(self.pending_effect, "pending_effect")
        if self.effect_id:
            _identifier(self.effect_id, "effect_id")
        if not isinstance(self.effect_outcome, EffectOutcome):
            raise ContractError("effect_outcome must be a bounded outcome")
        if self.effect_outcome == EffectOutcome.NONE:
            if self.effect_id or self.pending_effect:
                raise ContractError("effect identity requires an outcome")
        elif self.effect_outcome == EffectOutcome.PENDING:
            if not self.effect_id or self.pending_effect != self.effect_id:
                raise ContractError("pending effect identity and outcome must agree")
        elif not self.effect_id or self.pending_effect:
            raise ContractError("resolved effect identity and outcome must agree")
        callback_fields = (
            self.accepted_callback_id,
            self.accepted_callback_kind,
            self.accepted_callback_sha256,
        )
        if any(callback_fields) != all(callback_fields):
            raise ContractError("accepted callback identity must be complete")
        if self.accepted_callback_id:
            _identifier(self.accepted_callback_id, "accepted_callback_id")
            _identifier(self.accepted_callback_kind, "accepted_callback_kind")
            _sha256(self.accepted_callback_sha256, "accepted_callback_sha256")


@dataclass(frozen=True)
class CapabilityReport:
    route: RuntimeRoute
    compatible: bool
    capabilities: tuple[str, ...]
    reason: AttentionReason | None = None

    def __post_init__(self) -> None:
        for value in self.capabilities:
            _identifier(value, "capability")
        if self.compatible == (self.reason is not None):
            raise ContractError("capability reason must be present exactly when incompatible")


@dataclass(frozen=True)
class CallbackEnvelope:
    callback_id: str
    operation_id: str
    run_id: str
    kind: str
    payload: Mapping[str, Any]
    payload_sha256: str
    schema_version: int = 1
    MAX_PAYLOAD_BYTES: ClassVar[int] = 65_536

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported CallbackEnvelope schema")
        for value, label in (
            (self.callback_id, "callback_id"),
            (self.operation_id, "operation_id"),
            (self.run_id, "run_id"),
            (self.kind, "kind"),
        ):
            _identifier(value, label)
        frozen = _json_mapping(self.payload, "payload")
        encoded = json.dumps(dict(frozen), sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > self.MAX_PAYLOAD_BYTES:
            raise ContractError("callback payload exceeds size cap")
        _sha256(self.payload_sha256, "payload_sha256")
        if hashlib.sha256(encoded).hexdigest() != self.payload_sha256:
            raise ContractError("callback payload digest mismatch")
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True)
class VerificationEvidence:
    profile: str
    profile_sha256: str
    head_sha: str
    command_id: str
    cwd: str
    exit_code: int
    started_at: str
    finished_at: str
    output_pointer: str

    def __post_init__(self) -> None:
        _identifier(self.profile, "profile")
        _sha256(self.profile_sha256, "profile_sha256")
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.head_sha):
            raise ContractError("head_sha must be a git object id")
        _identifier(self.command_id, "command_id")
        _relative_path(self.cwd, "cwd")
        _relative_path(self.output_pointer, "output_pointer")
        if not isinstance(self.exit_code, int):
            raise ContractError("exit_code must be an integer")
        if not self.started_at or not self.finished_at:
            raise ContractError("verification timestamps are required")


@dataclass(frozen=True)
class TransitionResult:
    operation_id: str
    previous_state: str
    state: str
    revision: int
    changed: bool
    attention_reason: AttentionReason | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.previous_state, "previous_state")
        _identifier(self.state, "state")
        if self.revision < 0:
            raise ContractError("revision cannot be negative")


T = TypeVar("T")


def to_dict(value: Any) -> dict[str, Any]:
    """Return a stable JSON-compatible mapping for a contract dataclass."""
    if not dataclasses.is_dataclass(value):
        raise ContractError("contract value must be a dataclass instance")

    def normalize(item: Any) -> Any:
        if isinstance(item, enum.Enum):
            return item.value
        if dataclasses.is_dataclass(item):
            return {
                field.name: normalize(getattr(item, field.name))
                for field in dataclasses.fields(item)
            }
        if isinstance(item, Mapping):
            return {key: normalize(val) for key, val in sorted(item.items())}
        if isinstance(item, tuple):
            return [normalize(val) for val in item]
        return item

    return normalize(value)


def runtime_route_from_dict(value: Mapping[str, Any]) -> RuntimeRoute:
    return RuntimeRoute(
        runtime=value.get("runtime", ""),
        model=value.get("model", ""),
        effort=value.get("effort", ""),
        profile=value.get("profile", ""),
        routing_sha256=value.get("routing_sha256", ""),
    )


def operation_spec_from_dict(value: Mapping[str, Any]) -> OperationSpec:
    return OperationSpec(
        operation_id=value.get("operation_id", ""),
        idempotency_key=value.get("idempotency_key", ""),
        kind=value.get("kind", ""),
        owner_id=value.get("owner_id", ""),
        route=runtime_route_from_dict(value.get("route", {})),
        context_manifest=value.get("context_manifest", ""),
        verification_profile=value.get("verification_profile", ""),
        keep_open=value.get("keep_open", False),
        contract_sha256=value.get("contract_sha256", ""),
        schema_version=value.get("schema_version", 0),
    )


def operation_record_from_dict(value: Mapping[str, Any]) -> OperationRecord:
    reason = value.get("attention_reason")
    pending_effect = value.get("pending_effect", "")
    effect_id = value.get("effect_id", "") or pending_effect
    raw_outcome = value.get("effect_outcome")
    effect_outcome = (
        EffectOutcome(raw_outcome)
        if raw_outcome
        else EffectOutcome.PENDING if pending_effect else EffectOutcome.NONE
    )
    return OperationRecord(
        spec=operation_spec_from_dict(value.get("spec", {})),
        state=value.get("state", ""),
        revision=value.get("revision", -1),
        lane_id=value.get("lane_id", "legacy-lane"),
        run_id=value.get("run_id", "legacy-run"),
        resources=OwnedResources(**value.get("resources", {})),
        attempt=value.get("attempt", 0),
        attempt_limit=value.get("attempt_limit", 3),
        model_restarts=value.get("model_restarts", 0),
        model_restart_limit=value.get("model_restart_limit", 1),
        deadline_at=value.get("deadline_at", 0.0),
        token_limit=value.get("token_limit", 200_000),
        tokens_used=value.get("tokens_used", 0),
        attention_reason=AttentionReason(reason) if reason else None,
        resume_state=value.get("resume_state", ""),
        pending_effect=pending_effect,
        effect_id=effect_id,
        effect_outcome=effect_outcome,
        accepted_callback_id=value.get("accepted_callback_id", ""),
        accepted_callback_kind=value.get("accepted_callback_kind", ""),
        accepted_callback_sha256=value.get("accepted_callback_sha256", ""),
        schema_version=value.get("schema_version", 0),
    )
