"""Typed pipeline catalog, compiler, and state-free progress reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

from .contracts import ContractError, ID_RE, to_dict


SCHEMA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
SESSION_MODES = frozenset({"parent-child", "review", "verification", "worktree"})
COMPILER_VERSION = "1.0.0"


def _require_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded identifier")


def _require_version(value: str, label: str) -> None:
    if not isinstance(value, str) or not SEMVER_RE.fullmatch(value):
        raise ContractError(f"{label} must be a semantic version")


def _require_schema(value: str, label: str) -> None:
    if not isinstance(value, str) or not SCHEMA_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded schema identifier")


def _normalized_identifiers(
    values: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ContractError(f"{label} must be a tuple")
    for value in values:
        _require_identifier(value, label)
    normalized = tuple(sorted(values))
    if len(set(normalized)) != len(normalized):
        raise ContractError(f"{label} must be unique")
    return normalized


@dataclass(frozen=True)
class PrimitiveDefinition:
    """Versioned descriptor for one existing harness operation class."""

    primitive_id: str
    version: str
    session_modes: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PrimitiveDefinition schema")
        _require_identifier(self.primitive_id, "primitive_id")
        _require_version(self.version, "primitive version")
        modes = _normalized_identifiers(self.session_modes, "primitive session mode")
        if not modes or not set(modes).issubset(SESSION_MODES):
            raise ContractError("primitive session modes are invalid")
        object.__setattr__(self, "session_modes", modes)
        object.__setattr__(
            self,
            "required_capabilities",
            _normalized_identifiers(
                self.required_capabilities,
                "primitive required capability",
            ),
        )

    @property
    def identity(self) -> str:
        return f"{self.primitive_id}@{self.version}"


@dataclass(frozen=True)
class PipelineStep:
    step_id: str
    primitive_id: str
    primitive_version: str
    input_schema: str
    output_schema: str
    session_mode: str
    semantic_skills: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineStep schema")
        _require_identifier(self.step_id, "step_id")
        _require_identifier(self.primitive_id, "step primitive_id")
        _require_version(self.primitive_version, "step primitive_version")
        _require_schema(self.input_schema, "step input_schema")
        _require_schema(self.output_schema, "step output_schema")
        if self.session_mode not in SESSION_MODES:
            raise ContractError("step session_mode is invalid")
        object.__setattr__(
            self,
            "semantic_skills",
            _normalized_identifiers(self.semantic_skills, "step semantic skill"),
        )


@dataclass(frozen=True)
class PipelineDefinition:
    """Code-owned immutable semantic shape; not an executable DSL."""

    pipeline_id: str
    version: str
    profile: str
    input_schema: str
    output_schema: str
    steps: tuple[PipelineStep, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineDefinition schema")
        _require_identifier(self.pipeline_id, "pipeline_id")
        _require_version(self.version, "pipeline version")
        _require_identifier(self.profile, "pipeline profile")
        _require_schema(self.input_schema, "pipeline input_schema")
        _require_schema(self.output_schema, "pipeline output_schema")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ContractError("pipeline must contain at least one step")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ContractError("pipeline step ids must be unique")


@dataclass(frozen=True)
class PrimitiveRegistry:
    """Single code-owned catalog for compiled semantic contracts."""

    primitives: tuple[PrimitiveDefinition, ...]
    semantic_skills: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PrimitiveRegistry schema")
        if not isinstance(self.primitives, tuple) or not self.primitives:
            raise ContractError("primitive registry cannot be empty")
        identities = tuple(item.identity for item in self.primitives)
        if len(set(identities)) != len(identities):
            raise ContractError("primitive identities must be unique")
        object.__setattr__(
            self,
            "semantic_skills",
            _normalized_identifiers(
                self.semantic_skills,
                "registry semantic skill",
            ),
        )

    def resolve(self, primitive_id: str, version: str) -> PrimitiveDefinition:
        identity = f"{primitive_id}@{version}"
        for primitive in self.primitives:
            if primitive.identity == identity:
                return primitive
        raise ContractError(f"unknown primitive: {identity}")


@dataclass(frozen=True)
class CompiledPipeline:
    definition: PipelineDefinition
    compiler_version: str
    canonical_definition: str
    definition_sha256: str
    resolved_primitives: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    schema_version: int = 1


@dataclass(frozen=True)
class PipelineProgress:
    """One action derived from durable step observations, never stored itself."""

    action: str
    step_id: str = ""
    completed_steps: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineProgress schema")
        if self.action not in {"start", "wait", "attention", "reap-ready"}:
            raise ContractError("pipeline progress action is invalid")
        if self.action == "reap-ready":
            if self.step_id:
                raise ContractError("reap-ready cannot carry a step")
        else:
            _require_identifier(self.step_id, "pipeline progress step_id")
        for step_id in self.completed_steps:
            _require_identifier(step_id, "completed pipeline step")


def reconcile_pipeline(
    compiled: CompiledPipeline,
    observations: Mapping[str, str],
) -> PipelineProgress:
    """Derive the next action from existing operation/gate observations.

    The compiled definition owns ordering. Callers own durable operations and
    translate their typed states into pending/running/complete/attention.
    """

    if not isinstance(observations, Mapping):
        raise ContractError("pipeline observations must be a mapping")
    steps = compiled.definition.steps
    step_ids = tuple(step.step_id for step in steps)
    if set(observations) != set(step_ids):
        raise ContractError("pipeline observations must cover the exact definition")
    statuses = tuple(observations[step_id] for step_id in step_ids)
    if any(
        not isinstance(status, str)
        or status not in {"pending", "running", "complete", "attention"}
        for status in statuses
    ):
        raise ContractError("pipeline observation status is invalid")

    completed: list[str] = []
    for index, (step_id, status) in enumerate(zip(step_ids, statuses, strict=True)):
        if status == "complete":
            if index != len(completed):
                raise ContractError(
                    "completed pipeline steps must form an ordered prefix"
                )
            completed.append(step_id)
            continue
        if any(later != "pending" for later in statuses[index + 1 :]):
            raise ContractError(
                "completed pipeline steps must form an ordered prefix"
            )
        action = {
            "pending": "start",
            "running": "wait",
            "attention": "attention",
        }[status]
        return PipelineProgress(action, step_id, tuple(completed))
    return PipelineProgress("reap-ready", completed_steps=tuple(completed))


def compile_pipeline(
    definition: PipelineDefinition,
    registry: PrimitiveRegistry,
    *,
    capabilities: tuple[str, ...] = (),
) -> CompiledPipeline:
    """Validate and canonicalize a built-in descriptor without executing it."""

    available = set(
        _normalized_identifiers(capabilities, "available capability")
    )
    resolved: list[PrimitiveDefinition] = []
    required_capabilities: set[str] = set()
    previous: PipelineStep | None = None
    for step in definition.steps:
        primitive = registry.resolve(step.primitive_id, step.primitive_version)
        if step.session_mode not in primitive.session_modes:
            raise ContractError(
                f"{primitive.identity} does not support session mode "
                f"{step.session_mode}"
            )
        missing_skills = set(step.semantic_skills) - set(
            registry.semantic_skills
        )
        if missing_skills:
            raise ContractError(
                f"step {step.step_id} uses unregistered semantic skills: "
                f"{','.join(sorted(missing_skills))}"
            )
        if previous and previous.output_schema != step.input_schema:
            raise ContractError(
                f"schema mismatch before step {step.step_id}: "
                f"{previous.output_schema} != {step.input_schema}"
            )
        missing = set(primitive.required_capabilities) - available
        if missing:
            raise ContractError(
                f"step {step.step_id} lacks capabilities: "
                f"{','.join(sorted(missing))}"
            )
        resolved.append(primitive)
        required_capabilities.update(primitive.required_capabilities)
        previous = step

    if definition.steps[0].input_schema != definition.input_schema:
        raise ContractError("pipeline input schema does not match the first primitive")
    if definition.steps[-1].output_schema != definition.output_schema:
        raise ContractError(
            "pipeline output schema does not match the terminal primitive"
        )
    canonical = json.dumps(
        {
            "compiler_version": COMPILER_VERSION,
            "definition": to_dict(definition),
            "resolved_primitives": [to_dict(item) for item in resolved],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CompiledPipeline(
        definition=definition,
        compiler_version=COMPILER_VERSION,
        canonical_definition=canonical,
        definition_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        resolved_primitives=tuple(item.identity for item in resolved),
        required_capabilities=tuple(sorted(required_capabilities)),
    )


def render_contract(compiled: CompiledPipeline) -> str:
    """Render a bounded, explicitly non-executable semantic contract."""

    definition = compiled.definition
    lines = [
        f"Pipeline: {definition.pipeline_id}/{definition.profile}@{definition.version}",
        f"Definition: {compiled.definition_sha256}",
        "Steps: "
        + " -> ".join(
            f"{step.step_id}:{identity}"
            for step, identity in zip(
                definition.steps,
                compiled.resolved_primitives,
                strict=True,
            )
        ),
        "Required capabilities: "
        + (", ".join(compiled.required_capabilities) or "none"),
        "Execution: existing harness supervisor with state-free reconciliation",
    ]
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode()) > 8_192:
        raise ContractError("compiled contract exceeds size cap")
    return rendered
