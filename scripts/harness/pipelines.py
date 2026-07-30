"""Pure typed pipeline composition over the existing harness contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .contracts import ContractError, OperationSpec, RuntimeRoute, to_dict


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SCHEMA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
ENFORCEMENT = {"policy-only", "sandbox-enforced"}
SESSION_MODES = {
    "controller",
    "parent-child",
    "review",
    "verification",
    "worktree",
}
COMPILER_VERSION = "1.0.0"


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


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError(f"{label} must be a SHA-256 digest")
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
    input_schema: str
    output_schema: str
    session_mode: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineStep schema")
        _identifier(self.step_id, "step_id")
        _identifier(self.primitive_id, "step primitive_id")
        _version(self.primitive_version, "step primitive_version")
        _schema_id(self.input_schema, "step input_schema")
        _schema_id(self.output_schema, "step output_schema")
        if self.session_mode not in SESSION_MODES:
            raise ContractError("step session_mode is invalid")
        allowed_modes = {
            "bounded_loop": {"controller"},
            "human_gate": {"controller"},
            "model_step": {"parent-child", "worktree"},
            "review": {"review"},
            "verify": {"verification"},
        }.get(self.primitive_id)
        if allowed_modes is not None and self.session_mode not in allowed_modes:
            raise ContractError(
                f"{self.primitive_id} cannot use {self.session_mode} session mode"
            )


@dataclass(frozen=True)
class PipelineDefinition:
    """Code-owned immutable pipeline source."""

    pipeline_id: str
    version: str
    profile: str
    input_schema: str
    output_schema: str
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
        _schema_id(self.input_schema, "pipeline input_schema")
        _schema_id(self.output_schema, "pipeline output_schema")
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
    compiler_version: str
    canonical_definition: str
    definition_sha256: str
    resolved_primitives: tuple[str, ...]
    budget: PipelineBudget
    permissions: tuple[str, ...]
    side_effects: tuple[str, ...]
    bindings: tuple[PolicyBinding, ...]
    schema_version: int = 1


@dataclass(frozen=True)
class PipelineOperationBinding:
    """Exact bridge from one compiled semantic step to the 2.3 kernel."""

    definition_sha256: str
    step_id: str
    primitive_id: str
    primitive_version: str
    session_mode: str
    input_sha256: str
    output_schema: str
    replay_key: str
    spec: OperationSpec
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineOperationBinding schema")
        _sha256(self.definition_sha256, "pipeline definition")
        _identifier(self.step_id, "pipeline step_id")
        _identifier(self.primitive_id, "pipeline primitive_id")
        _version(self.primitive_version, "pipeline primitive_version")
        if self.session_mode not in SESSION_MODES - {"controller"}:
            raise ContractError("operation binding requires an executable session mode")
        _sha256(self.input_sha256, "pipeline step input")
        _schema_id(self.output_schema, "pipeline step output_schema")
        _sha256(self.replay_key, "pipeline replay key")
        if self.spec.idempotency_key != self.replay_key:
            raise ContractError("pipeline operation must retain its replay identity")


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
    previous: PipelineStep | None = None
    for step in definition.steps:
        if step.primitive_id == "bounded_loop":
            raise ContractError(
                "bounded_loop requires an explicit bounded control contract"
            )
        primitive = registry.resolve(step.primitive_id, step.primitive_version)
        if previous and previous.output_schema != step.input_schema:
            raise ContractError(
                f"schema mismatch before step {step.step_id}: "
                f"{previous.output_schema} != {step.input_schema}"
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
        previous = step

    if definition.steps[0].input_schema != definition.input_schema:
        raise ContractError(
            "pipeline input schema does not match the first primitive"
        )
    if definition.steps[-1].output_schema != definition.output_schema:
        raise ContractError(
            "pipeline output schema does not match the terminal primitive"
        )
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
            "compiler_version": COMPILER_VERSION,
            "definition": to_dict(definition),
            "resolved_primitives": [to_dict(primitive) for primitive in resolved],
            "bindings": [to_dict(binding) for binding in bindings],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CompiledPipeline(
        definition=definition,
        compiler_version=COMPILER_VERSION,
        canonical_definition=canonical,
        definition_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        resolved_primitives=tuple(primitive.identity for primitive in resolved),
        budget=budget,
        permissions=ordered_permissions,
        side_effects=ordered_side_effects,
        bindings=bindings,
    )


def bind_step_operation(
    compiled: CompiledPipeline,
    *,
    step_id: str,
    operation_id: str,
    owner_id: str,
    route: RuntimeRoute,
    context_manifest: str,
    verification_profile: str,
    input_sha256: str,
) -> PipelineOperationBinding:
    """Bind one semantic/effectful step to one existing OperationSpec."""

    matches = tuple(
        step for step in compiled.definition.steps if step.step_id == step_id
    )
    if len(matches) != 1:
        raise ContractError(f"unknown pipeline step: {step_id}")
    step = matches[0]
    if step.primitive_id in {"human_gate", "bounded_loop"}:
        raise ContractError(
            f"{step.primitive_id} is a controller boundary, not an operation"
        )
    input_digest = _sha256(input_sha256, "pipeline step input")
    replay_payload = json.dumps(
        {
            "compiler_version": compiled.compiler_version,
            "definition_sha256": compiled.definition_sha256,
            "input_sha256": input_digest,
            "schema_version": 1,
            "step_id": step.step_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    replay_key = hashlib.sha256(replay_payload).hexdigest()
    spec = OperationSpec(
        operation_id=operation_id,
        idempotency_key=replay_key,
        kind=f"pipeline-{step.primitive_id.replace('_', '-')}",
        owner_id=owner_id,
        route=route,
        context_manifest=context_manifest,
        verification_profile=verification_profile,
    )
    return PipelineOperationBinding(
        definition_sha256=compiled.definition_sha256,
        step_id=step.step_id,
        primitive_id=step.primitive_id,
        primitive_version=step.primitive_version,
        session_mode=step.session_mode,
        input_sha256=input_digest,
        output_schema=step.output_schema,
        replay_key=replay_key,
        spec=spec,
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
