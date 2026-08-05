"""Provider-neutral contracts for one-input, schema-producing executions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .provider_events import (
    ProviderEvent,
    ProviderEventIdentity,
    validate_event_stream,
)


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LOGICAL_PROVIDERS = frozenset({"anthropic", "openai"})
CAPABILITIES = frozenset({"read-context", "schema-output"})
AUTH_PROFILES = MappingProxyType(
    {"anthropic": "native-subscription", "openai": "chatgpt"}
)
DISPOSITIONS = frozenset(
    {
        "succeeded",
        "auth-expired",
        "usage-exhausted",
        "schema-invalid",
        "policy-denied",
        "timeout",
        "transport-failed",
    }
)
MAX_PACKET_BYTES = 1_048_576
MAX_PROCESS_OUTPUT_BYTES = 1_048_576


class EphemeralProviderError(ValueError):
    """An ephemeral adapter would violate its frozen provider-neutral contract."""


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise EphemeralProviderError(f"{label} must be a bounded identifier")


def _owned_path(
    value: Path,
    cwd: Path,
    label: str,
    *,
    require_file: bool = False,
    require_directory: bool = False,
) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise EphemeralProviderError(f"{label} must be an absolute path")
    if value.is_symlink():
        raise EphemeralProviderError(f"{label} must not be a symlink")
    resolved = value.resolve(strict=False)
    try:
        resolved.relative_to(cwd)
    except ValueError as exc:
        raise EphemeralProviderError(f"{label} escapes ephemeral scratch") from exc
    if require_file and (not resolved.is_file() or resolved.is_symlink()):
        raise EphemeralProviderError(f"{label} must be a regular file")
    if require_directory and (not resolved.is_dir() or resolved.is_symlink()):
        raise EphemeralProviderError(f"{label} must be a directory")
    return resolved


def _bounded_bytes(path: Path, label: str) -> bytes:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_PACKET_BYTES:
        raise EphemeralProviderError(f"{label} must be non-empty and bounded")
    return raw


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_bounded_bytes(path, "output schema"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EphemeralProviderError("output schema must be valid JSON") from exc
    if not isinstance(value, dict):
        raise EphemeralProviderError("output schema must be an object")
    _validate_schema_shape(value, root=True)
    return value


def _validate_schema_shape(schema: object, *, root: bool = False) -> None:
    if not isinstance(schema, dict):
        raise EphemeralProviderError("output schema node must be an object")
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
    }
    if set(schema) - allowed:
        raise EphemeralProviderError("output schema uses an unsupported keyword")
    kind = schema.get("type")
    if kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise EphemeralProviderError("output schema type is unsupported")
    if root and (kind != "object" or schema.get("additionalProperties") is not False):
        raise EphemeralProviderError(
            "root output schema must be a closed object"
        )
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise EphemeralProviderError("output schema enum must be a non-empty list")
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise EphemeralProviderError(
                "object schema requires properties and required"
            )
        if (
            any(not isinstance(key, str) or not key for key in properties)
            or any(not isinstance(key, str) or key not in properties for key in required)
            or len(set(required)) != len(required)
            or schema.get("additionalProperties") not in {None, False}
        ):
            raise EphemeralProviderError("object schema keys are invalid")
        for child in properties.values():
            _validate_schema_shape(child)
    elif kind == "array":
        if "items" not in schema:
            raise EphemeralProviderError("array schema requires items")
        _validate_schema_shape(schema["items"])


def validate_output_instance(value: object, schema: Mapping[str, Any]) -> bool:
    """Validate the deliberately small schema subset accepted by the adapter."""

    kind = schema["type"]
    valid_type = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: type(item) is int,
        "number": lambda item: (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
        ),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }[kind]
    if not valid_type(value):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "object":
        assert isinstance(value, dict)
        properties = schema["properties"]
        if any(key not in value for key in schema["required"]):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(
            key not in value or validate_output_instance(value[key], child)
            for key, child in properties.items()
        )
    if kind == "array":
        assert isinstance(value, list)
        return all(validate_output_instance(item, schema["items"]) for item in value)
    return True


@dataclass(frozen=True)
class EphemeralRunSpec:
    """One logical route, one bounded input, and one schema-valid result."""

    logical_provider: str
    model: str
    effort: str
    context_packet: Path
    output_schema: Path
    result_path: Path
    runtime_home: Path
    cwd: Path
    capabilities: tuple[str, ...]
    auth_profile: str
    turn_budget: int
    wall_clock_deadline: float
    operation_id: str
    run_id: str
    generation: int
    effect_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.logical_provider not in LOGICAL_PROVIDERS:
            raise EphemeralProviderError("ephemeral logical provider is invalid")
        for value, label in (
            (self.model, "model"),
            (self.effort, "effort"),
            (self.operation_id, "operation_id"),
            (self.run_id, "run_id"),
            (self.effect_id, "effect_id"),
        ):
            _identifier(value, label)
        if self.auth_profile != AUTH_PROFILES[self.logical_provider]:
            raise EphemeralProviderError("ephemeral auth profile changed billing premise")
        if (
            not isinstance(self.capabilities, tuple)
            or frozenset(self.capabilities) != CAPABILITIES
            or len(self.capabilities) != len(CAPABILITIES)
        ):
            raise EphemeralProviderError("ephemeral capabilities must be exact")
        if type(self.turn_budget) is not int or not 1 <= self.turn_budget <= 4:
            raise EphemeralProviderError("ephemeral turn budget is invalid")
        if (
            not isinstance(self.wall_clock_deadline, (int, float))
            or isinstance(self.wall_clock_deadline, bool)
            or not math.isfinite(float(self.wall_clock_deadline))
            or not 1 <= self.wall_clock_deadline <= 1800
        ):
            raise EphemeralProviderError("ephemeral deadline is invalid")
        if type(self.generation) is not int or self.generation < 1:
            raise EphemeralProviderError("ephemeral generation is invalid")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise EphemeralProviderError("ephemeral cwd must be absolute")
        cwd = self.cwd.resolve()
        if not cwd.is_dir() or cwd.is_symlink():
            raise EphemeralProviderError("ephemeral cwd must be a directory")
        object.__setattr__(self, "cwd", cwd)
        for field, require_file, require_directory in (
            ("context_packet", True, False),
            ("output_schema", True, False),
            ("result_path", False, False),
            ("runtime_home", False, True),
        ):
            normalized = _owned_path(
                getattr(self, field),
                cwd,
                field,
                require_file=require_file,
                require_directory=require_directory,
            )
            object.__setattr__(self, field, normalized)
        if self.result_path.parent != cwd:
            raise EphemeralProviderError("ephemeral result must stay at scratch root")
        _bounded_bytes(self.context_packet, "context packet")
        _load_schema(self.output_schema)

    @property
    def schema(self) -> dict[str, Any]:
        return _load_schema(self.output_schema)


@dataclass(frozen=True)
class EphemeralCommand:
    argv: tuple[str, ...]
    stdin: bytes
    environment: Mapping[str, str]
    transport: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not self.argv
            or not Path(self.argv[0]).is_absolute()
            or not self.stdin
        ):
            raise EphemeralProviderError("ephemeral command is invalid")
        _identifier(self.transport, "transport")
        if not isinstance(self.environment, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise EphemeralProviderError("ephemeral environment is invalid")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class AuthProbeCommand:
    """A bounded local account-status check that cannot invoke a model."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]
    timeout_seconds: float = 8.0
    model_effect: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not self.argv
            or not Path(self.argv[0]).is_absolute()
            or not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 30
            or self.model_effect
        ):
            raise EphemeralProviderError("auth probe command is invalid")
        if not isinstance(self.environment, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise EphemeralProviderError("auth probe environment is invalid")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class AuthPreflightResult:
    logical_provider: str
    auth_profile: str
    status: str
    reason: str
    model_effect_allowed: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.logical_provider not in LOGICAL_PROVIDERS
            or self.auth_profile != AUTH_PROFILES[self.logical_provider]
            or self.status not in {"ready", "billing-profile-unverified"}
            or self.model_effect_allowed != (self.status == "ready")
        ):
            raise EphemeralProviderError("ephemeral auth preflight is invalid")
        _identifier(self.reason, "auth preflight reason")


@dataclass(frozen=True)
class NativeAccountProbe:
    preflight: AuthPreflightResult
    command: EphemeralCommand | None
    max_model_effects: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.max_model_effects != 1
            or (self.command is not None) != self.preflight.model_effect_allowed
        ):
            raise EphemeralProviderError("native account probe is not bounded")


@dataclass(frozen=True)
class EphemeralProcessResult:
    provider_session_id: str
    process_identity: str
    source_id: str
    stdout: bytes
    stderr: bytes
    result_bytes: bytes
    returncode: int
    input_accepted: bool = True
    timed_out: bool = False
    resource_closed: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value, label in (
            (self.provider_session_id, "provider_session_id"),
            (self.source_id, "source_id"),
        ):
            _identifier(value, label)
        if (
            self.schema_version != 1
            or not SHA256.fullmatch(self.process_identity)
            or type(self.returncode) is not int
            or not all(
                isinstance(value, bytes)
                and len(value) <= MAX_PROCESS_OUTPUT_BYTES
                for value in (self.stdout, self.stderr, self.result_bytes)
            )
            or not all(
                isinstance(value, bool)
                for value in (
                    self.input_accepted,
                    self.timed_out,
                    self.resource_closed,
                )
            )
        ):
            raise EphemeralProviderError("ephemeral process result is invalid")


@dataclass(frozen=True)
class EphemeralRunResult:
    logical_provider: str
    disposition: str
    events: tuple[ProviderEvent, ...]
    result: Mapping[str, Any] | None
    transport: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.logical_provider not in LOGICAL_PROVIDERS
            or self.disposition not in DISPOSITIONS
            or not self.events
        ):
            raise EphemeralProviderError("ephemeral result is invalid")
        _identifier(self.transport, "transport")
        cursor = validate_event_stream("ephemeral", self.events)
        if self.disposition == "succeeded":
            if not cursor.result_published or self.result is None:
                raise EphemeralProviderError("successful ephemeral result is incomplete")
        elif cursor.result_published or self.result is not None:
            raise EphemeralProviderError("failed ephemeral result published business output")
        if self.result is not None:
            canonical = json.loads(json.dumps(dict(self.result), sort_keys=True))
            if not isinstance(canonical, dict):
                raise EphemeralProviderError("ephemeral business result is invalid")
            object.__setattr__(self, "result", MappingProxyType(canonical))


def normalized_run_result(
    spec: EphemeralRunSpec,
    process: EphemeralProcessResult,
    *,
    transport: str,
    disposition: str,
    result: dict[str, Any] | None = None,
    gap_reason: str = "",
) -> EphemeralRunResult:
    """Build the shared closed event subset from provider-owned parsing."""

    identity = ProviderEventIdentity(
        operation_id=spec.operation_id,
        run_id=spec.run_id,
        generation=spec.generation,
        provider_session_id=process.provider_session_id,
        process_identity=process.process_identity,
        source_id=process.source_id,
    )
    events: list[ProviderEvent] = []

    def append(kind: str, **values: object) -> None:
        events.append(
            ProviderEvent(
                kind,
                identity,
                len(events) + 1,
                **values,
            )
        )

    append("provider-started")
    if process.input_accepted:
        append("input-accepted", effect_id=spec.effect_id)
    if result is not None:
        encoded = json.dumps(
            result, sort_keys=True, separators=(",", ":")
        ).encode()
        append(
            "result-published",
            result_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    elif gap_reason:
        append("event-gap", reason=gap_reason)
    append("process-exited", exit_code=process.returncode)
    if process.resource_closed:
        append("resource-closed", reason="owned-resources-gone")
    return EphemeralRunResult(
        logical_provider=spec.logical_provider,
        disposition=disposition,
        events=tuple(events),
        result=result,
        transport=transport,
    )


class EphemeralAdapter(Protocol):
    logical_provider: str

    def compile(
        self, spec: EphemeralRunSpec, *, env: Mapping[str, str]
    ) -> EphemeralCommand: ...

    def auth_command(
        self, spec: EphemeralRunSpec, *, env: Mapping[str, str]
    ) -> AuthProbeCommand: ...

    def preflight(
        self,
        spec: EphemeralRunSpec,
        *,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> AuthPreflightResult: ...

    def normalize(
        self, spec: EphemeralRunSpec, process: EphemeralProcessResult
    ) -> EphemeralRunResult: ...


class EphemeralTransportRegistry:
    """Bind logical providers to replaceable internal transports exactly once."""

    def __init__(self, adapters: Mapping[str, EphemeralAdapter]):
        if set(adapters) != LOGICAL_PROVIDERS:
            raise EphemeralProviderError("ephemeral registry must bind both providers")
        if any(
            key != getattr(adapter, "logical_provider", "")
            for key, adapter in adapters.items()
        ):
            raise EphemeralProviderError("ephemeral registry provider binding changed")
        self._adapters = MappingProxyType(dict(adapters))

    def _adapter(self, spec: EphemeralRunSpec) -> EphemeralAdapter:
        try:
            return self._adapters[spec.logical_provider]
        except KeyError as exc:  # pragma: no cover - spec closes this vocabulary.
            raise EphemeralProviderError("ephemeral provider is unregistered") from exc

    def compile(
        self, spec: EphemeralRunSpec, *, env: Mapping[str, str]
    ) -> EphemeralCommand:
        return self._adapter(spec).compile(spec, env=env)

    def auth_command(
        self, spec: EphemeralRunSpec, *, env: Mapping[str, str]
    ) -> AuthProbeCommand:
        return self._adapter(spec).auth_command(spec, env=env)

    def preflight(
        self,
        spec: EphemeralRunSpec,
        *,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> AuthPreflightResult:
        return self._adapter(spec).preflight(
            spec, stdout=stdout, stderr=stderr, returncode=returncode
        )

    def bounded_probe(
        self,
        spec: EphemeralRunSpec,
        preflight: AuthPreflightResult,
        *,
        env: Mapping[str, str],
    ) -> NativeAccountProbe:
        if (
            preflight.logical_provider != spec.logical_provider
            or preflight.auth_profile != spec.auth_profile
        ):
            raise EphemeralProviderError("auth preflight identity changed")
        command = self.compile(spec, env=env) if preflight.model_effect_allowed else None
        return NativeAccountProbe(preflight, command)

    def normalize(
        self, spec: EphemeralRunSpec, process: EphemeralProcessResult
    ) -> EphemeralRunResult:
        return self._adapter(spec).normalize(spec, process)
