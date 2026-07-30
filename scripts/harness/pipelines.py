"""Pure typed pipeline composition over the existing harness contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .contracts import ContractError, to_dict


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SCHEMA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
ENFORCEMENT = {"policy-only", "sandbox-enforced"}


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ContractError(f"{label} must be a bounded identifier")
    return value


def _version(value: str, label: str) -> str:
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise ContractError(f"{label} must be a semantic version")
    return value


def _schema_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SCHEMA_ID.fullmatch(value):
        raise ContractError(f"{label} must be a bounded schema identifier")
    return value


def _identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ContractError(f"{label} must be a tuple")
    normalized = tuple(sorted(_identifier(value, label) for value in values))
    if len(set(normalized)) != len(normalized):
        raise ContractError(f"{label} must be unique")
    return normalized


@dataclass(frozen=True)
class PipelineBudget:
    """Statically additive worst-case pipeline envelope."""

    model_calls: int = 0
    review_calls: int = 0
    verification_calls: int = 0
    token_limit: int = 0
    deadline_seconds: int = 0
    restart_limit: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.model_calls,
                self.review_calls,
                self.verification_calls,
                self.token_limit,
                self.deadline_seconds,
                self.restart_limit,
            )
        ):
            raise ContractError("pipeline budget values must be non-negative integers")

    def __add__(self, other: object) -> "PipelineBudget":
        if not isinstance(other, PipelineBudget):
            return NotImplemented
        return PipelineBudget(
            model_calls=self.model_calls + other.model_calls,
            review_calls=self.review_calls + other.review_calls,
            verification_calls=(
                self.verification_calls + other.verification_calls
            ),
            token_limit=self.token_limit + other.token_limit,
            deadline_seconds=self.deadline_seconds + other.deadline_seconds,
            restart_limit=self.restart_limit + other.restart_limit,
        )


@dataclass(frozen=True)
class PolicyBinding:
    """Bind one declarable class to a concrete enforcement mechanism."""

    category: str
    class_id: str
    mechanism: str
    enforcement: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PolicyBinding schema")
        if self.category not in {"permission", "side-effect"}:
            raise ContractError("binding category must be permission or side-effect")
        _identifier(self.class_id, "binding class")
        _identifier(self.mechanism, "binding mechanism")
        if self.enforcement not in ENFORCEMENT:
            raise ContractError("binding enforcement is invalid")


@dataclass(frozen=True)
class PrimitiveDefinition:
    """Versioned semantics available to code-owned pipeline definitions."""

    primitive_id: str
    version: str
    input_schema: str
    output_schema: str
    budget: PipelineBudget = field(default_factory=PipelineBudget)
    permissions: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PrimitiveDefinition schema")
        _identifier(self.primitive_id, "primitive_id")
        _version(self.version, "primitive version")
        _schema_id(self.input_schema, "primitive input_schema")
        _schema_id(self.output_schema, "primitive output_schema")
        object.__setattr__(
            self, "permissions", _identifiers(self.permissions, "primitive permission")
        )
        object.__setattr__(
            self,
            "side_effects",
            _identifiers(self.side_effects, "primitive side-effect"),
        )
        object.__setattr__(
            self,
            "required_capabilities",
            _identifiers(
                self.required_capabilities, "primitive required capability"
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
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineStep schema")
        _identifier(self.step_id, "step_id")
        _identifier(self.primitive_id, "step primitive_id")
        _version(self.primitive_version, "step primitive_version")


@dataclass(frozen=True)
class PipelineDefinition:
    """Code-owned immutable pipeline source."""

    pipeline_id: str
    version: str
    profile: str
    steps: tuple[PipelineStep, ...]
    permission_ceiling: tuple[str, ...] = ()
    side_effect_ceiling: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineDefinition schema")
        _identifier(self.pipeline_id, "pipeline_id")
        _version(self.version, "pipeline version")
        _identifier(self.profile, "pipeline profile")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ContractError("pipeline must contain at least one step")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ContractError("pipeline step ids must be unique")
        object.__setattr__(
            self,
            "permission_ceiling",
            _identifiers(self.permission_ceiling, "permission ceiling"),
        )
        object.__setattr__(
            self,
            "side_effect_ceiling",
            _identifiers(self.side_effect_ceiling, "side-effect ceiling"),
        )


@dataclass(frozen=True)
class PrimitiveRegistry:
    """Single code-owned source for primitive semantics and policy bindings."""

    primitives: tuple[PrimitiveDefinition, ...]
    bindings: tuple[PolicyBinding, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PrimitiveRegistry schema")
        if not isinstance(self.primitives, tuple) or not self.primitives:
            raise ContractError("primitive registry cannot be empty")
        identities = tuple(primitive.identity for primitive in self.primitives)
        if len(set(identities)) != len(identities):
            raise ContractError("primitive identities must be unique")
        binding_ids = tuple(
            (binding.category, binding.class_id) for binding in self.bindings
        )
        if len(set(binding_ids)) != len(binding_ids):
            raise ContractError("policy bindings must be unique")

    def resolve(self, primitive_id: str, version: str) -> PrimitiveDefinition:
        identity = f"{primitive_id}@{version}"
        for primitive in self.primitives:
            if primitive.identity == identity:
                return primitive
        raise ContractError(f"unknown primitive: {identity}")

    def binding(self, category: str, class_id: str) -> PolicyBinding:
        for binding in self.bindings:
            if binding.category == category and binding.class_id == class_id:
                return binding
        raise ContractError(f"unbound {category} class: {class_id}")


@dataclass(frozen=True)
class CompiledPipeline:
    definition: PipelineDefinition
    canonical_definition: str
    definition_sha256: str
    resolved_primitives: tuple[str, ...]
    budget: PipelineBudget
    permissions: tuple[str, ...]
    side_effects: tuple[str, ...]
    bindings: tuple[PolicyBinding, ...]
    schema_version: int = 1


def compile_pipeline(
    definition: PipelineDefinition,
    registry: PrimitiveRegistry,
    *,
    capabilities: tuple[str, ...] = (),
) -> CompiledPipeline:
    """Compile one pure definition without launching a runtime or side effect."""

    available = set(_identifiers(capabilities, "available capability"))
    resolved: list[PrimitiveDefinition] = []
    budget = PipelineBudget()
    permissions: set[str] = set()
    side_effects: set[str] = set()
    previous: PrimitiveDefinition | None = None
    for step in definition.steps:
        primitive = registry.resolve(step.primitive_id, step.primitive_version)
        if previous and previous.output_schema != primitive.input_schema:
            raise ContractError(
                f"schema mismatch before step {step.step_id}: "
                f"{previous.output_schema} != {primitive.input_schema}"
            )
        missing = set(primitive.required_capabilities) - available
        if missing:
            raise ContractError(
                f"step {step.step_id} lacks capabilities: {','.join(sorted(missing))}"
            )
        resolved.append(primitive)
        budget = budget + primitive.budget
        permissions.update(primitive.permissions)
        side_effects.update(primitive.side_effects)
        previous = primitive

    ordered_permissions = tuple(sorted(permissions))
    ordered_side_effects = tuple(sorted(side_effects))
    if not permissions.issubset(definition.permission_ceiling):
        raise ContractError("pipeline permissions exceed the code-owned ceiling")
    if not side_effects.issubset(definition.side_effect_ceiling):
        raise ContractError("pipeline side effects exceed the code-owned ceiling")
    bindings = tuple(
        sorted(
            (
                *(
                    registry.binding("permission", class_id)
                    for class_id in ordered_permissions
                ),
                *(
                    registry.binding("side-effect", class_id)
                    for class_id in ordered_side_effects
                ),
            ),
            key=lambda item: (item.category, item.class_id),
        )
    )
    canonical = json.dumps(
        {
            "definition": to_dict(definition),
            "resolved_primitives": [to_dict(primitive) for primitive in resolved],
            "bindings": [to_dict(binding) for binding in bindings],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CompiledPipeline(
        definition=definition,
        canonical_definition=canonical,
        definition_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        resolved_primitives=tuple(primitive.identity for primitive in resolved),
        budget=budget,
        permissions=ordered_permissions,
        side_effects=ordered_side_effects,
        bindings=bindings,
    )


def render_contract(compiled: CompiledPipeline) -> str:
    """Render one bounded approval summary from compiled, not raw, input."""

    definition = compiled.definition
    binding_by_id = {
        (binding.category, binding.class_id): binding
        for binding in compiled.bindings
    }
    lines = [
        f"Pipeline: {definition.pipeline_id}/{definition.profile}@{definition.version}",
        f"Definition: {compiled.definition_sha256}",
        "Steps: "
        + " -> ".join(
            f"{step.step_id}:{identity}"
            for step, identity in zip(
                definition.steps, compiled.resolved_primitives, strict=True
            )
        ),
        (
            "Worst case: "
            f"model={compiled.budget.model_calls} "
            f"review={compiled.budget.review_calls} "
            f"verify={compiled.budget.verification_calls} "
            f"tokens={compiled.budget.token_limit} "
            f"deadline={compiled.budget.deadline_seconds}s "
            f"restarts={compiled.budget.restart_limit}"
        ),
        "Permissions:",
    ]
    lines.extend(
        f"- {class_id} [{binding_by_id[('permission', class_id)].enforcement}:"
        f"{binding_by_id[('permission', class_id)].mechanism}]"
        for class_id in compiled.permissions
    )
    lines.append("Side effects:")
    lines.extend(
        f"- {class_id} [{binding_by_id[('side-effect', class_id)].enforcement}:"
        f"{binding_by_id[('side-effect', class_id)].mechanism}]"
        for class_id in compiled.side_effects
    )
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode()) > 8_192:
        raise ContractError("compiled approval contract exceeds size cap")
    return rendered
