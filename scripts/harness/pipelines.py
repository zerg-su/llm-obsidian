"""Typed pipeline catalog, compiler, and state-free progress reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .contracts import (
    ContractError,
    DEFAULT_ATTEMPT_LIMIT,
    DEFAULT_MODEL_RESTART_LIMIT,
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    ID_RE,
    to_dict,
)


SCHEMA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
SESSION_MODES = frozenset({"parent-child", "review", "verification", "worktree"})
PRIMITIVE_KINDS = frozenset({"step", "control"})
COMPLETION_PASS_LIMITS = MappingProxyType(
    {"attention": 2, "autonomous": 3}
)
PERMISSION_BINDINGS = MappingProxyType(
    {
        "callback": "code-policy-enforced",
        "cmux-socket": "sandbox-enforced",
        "cmux-target": "policy-only",
        "git-common-dir": "sandbox-enforced",
        "git-write": "sandbox-enforced",
        "loopback": "sandbox-enforced",
        "product-worktree": "sandbox-enforced",
        "provider": "code-policy-enforced",
        "reap": "coordinator-owned",
    }
)
SIDE_EFFECT_BINDINGS = MappingProxyType(
    {
        "callback": "code-policy-enforced",
        "cmux-surface": "policy-only",
        "git-write": "code-policy-enforced",
        "provider": "adapter-enforced",
        "reap": "coordinator-owned",
        "worktree": "code-policy-enforced",
    }
)
DEFAULT_PERMISSION_CEILING = tuple(sorted(PERMISSION_BINDINGS))
DEFAULT_SIDE_EFFECTS = tuple(sorted(SIDE_EFFECT_BINDINGS))
COMPILER_VERSION = "1.1.0"


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
class PipelineBudget:
    """One immutable per-pass envelope used for static upper bounds."""

    attempt_limit: int = DEFAULT_ATTEMPT_LIMIT
    model_restart_limit: int = DEFAULT_MODEL_RESTART_LIMIT
    time_budget_seconds: int = int(DEFAULT_TIME_BUDGET_SECONDS)
    token_limit: int = DEFAULT_TOKEN_LIMIT
    schema_version: int = 1

    def __post_init__(self) -> None:
        values = (
            self.attempt_limit,
            self.model_restart_limit,
            self.time_budget_seconds,
            self.token_limit,
        )
        if (
            self.schema_version != 1
            or any(type(value) is not int for value in values)
            or self.attempt_limit < 1
            or self.model_restart_limit < 0
            or self.time_budget_seconds < 1
            or self.token_limit < 1
        ):
            raise ContractError("pipeline budget is invalid")

    def scaled(self, operation_count: int) -> PipelineBudget:
        if type(operation_count) is not int or operation_count < 1:
            raise ContractError("pipeline budget scale is invalid")
        return PipelineBudget(
            attempt_limit=self.attempt_limit * operation_count,
            model_restart_limit=self.model_restart_limit * operation_count,
            time_budget_seconds=self.time_budget_seconds * operation_count,
            token_limit=self.token_limit * operation_count,
        )


@dataclass(frozen=True)
class CompletionPolicy:
    """One request-time completion mode admitted by a built-in contract."""

    policy: str
    total_pass_limit: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        expected = COMPLETION_PASS_LIMITS.get(self.policy)
        if (
            self.schema_version != 1
            or expected is None
            or type(self.total_pass_limit) is not int
            or self.total_pass_limit != expected
        ):
            raise ContractError("completion policy and pass limit are invalid")


@dataclass(frozen=True)
class PrimitiveReference:
    """An exact primitive identity referenced outside the linear data path."""

    primitive_id: str
    version: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PrimitiveReference schema")
        _require_identifier(self.primitive_id, "control primitive_id")
        _require_version(self.version, "control primitive version")

    @property
    def identity(self) -> str:
        return f"{self.primitive_id}@{self.version}"


@dataclass(frozen=True)
class PrimitiveDefinition:
    """Versioned descriptor for one existing harness operation class."""

    primitive_id: str
    version: str
    session_modes: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()
    primitive_kind: str = "step"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PrimitiveDefinition schema")
        _require_identifier(self.primitive_id, "primitive_id")
        _require_version(self.version, "primitive version")
        modes = _normalized_identifiers(self.session_modes, "primitive session mode")
        if self.primitive_kind not in PRIMITIVE_KINDS:
            raise ContractError("primitive kind is invalid")
        if not set(modes).issubset(SESSION_MODES):
            raise ContractError("primitive session modes are invalid")
        if (self.primitive_kind == "step") != bool(modes):
            raise ContractError(
                "step primitives require session modes; control primitives forbid them"
            )
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
    control_primitives: tuple[PrimitiveReference, ...] = ()
    pass_budget: PipelineBudget = field(default_factory=PipelineBudget)
    permission_ceiling: tuple[str, ...] = DEFAULT_PERMISSION_CEILING
    side_effects: tuple[str, ...] = DEFAULT_SIDE_EFFECTS
    completion_policies: tuple[CompletionPolicy, ...] = field(
        default_factory=lambda: (CompletionPolicy("attention", 2),)
    )
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
        if not isinstance(self.control_primitives, tuple):
            raise ContractError("control primitives must be a tuple")
        controls = tuple(
            sorted(self.control_primitives, key=lambda item: item.identity)
        )
        if len({item.identity for item in controls}) != len(controls):
            raise ContractError("control primitive identities must be unique")
        object.__setattr__(self, "control_primitives", controls)
        if not isinstance(self.pass_budget, PipelineBudget):
            raise ContractError("pass budget must be a PipelineBudget")
        permissions = _normalized_identifiers(
            self.permission_ceiling,
            "permission ceiling",
        )
        unknown_permissions = set(permissions) - set(PERMISSION_BINDINGS)
        if unknown_permissions:
            raise ContractError(
                "permission ceiling exceeds code-owned bindings: "
                + ",".join(sorted(unknown_permissions))
            )
        object.__setattr__(self, "permission_ceiling", permissions)
        side_effects = _normalized_identifiers(
            self.side_effects,
            "side effects",
        )
        unknown_effects = set(side_effects) - set(SIDE_EFFECT_BINDINGS)
        if unknown_effects:
            raise ContractError(
                "side effects exceed code-owned bindings: "
                + ",".join(sorted(unknown_effects))
            )
        object.__setattr__(self, "side_effects", side_effects)
        if (
            not isinstance(self.completion_policies, tuple)
            or not self.completion_policies
            or any(
                not isinstance(item, CompletionPolicy)
                for item in self.completion_policies
            )
        ):
            raise ContractError("completion policies must be a non-empty tuple")
        completion_policies = tuple(
            sorted(self.completion_policies, key=lambda item: item.policy)
        )
        if len({item.policy for item in completion_policies}) != len(
            completion_policies
        ):
            raise ContractError("completion policies must be unique")
        object.__setattr__(
            self,
            "completion_policies",
            completion_policies,
        )


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
    resolved_control_primitives: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    worst_case_budget: PipelineBudget
    permission_ceiling: tuple[str, ...]
    side_effects: tuple[str, ...]
    permission_bindings: tuple[str, ...]
    side_effect_bindings: tuple[str, ...]
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
    resolved_controls: list[PrimitiveDefinition] = []
    required_capabilities: set[str] = set()
    for reference in definition.control_primitives:
        primitive = registry.resolve(reference.primitive_id, reference.version)
        if primitive.primitive_kind != "control":
            raise ContractError(
                f"{primitive.identity} is not a control primitive"
            )
        missing = set(primitive.required_capabilities) - available
        if missing:
            raise ContractError(
                f"control {primitive.identity} lacks capabilities: "
                f"{','.join(sorted(missing))}"
            )
        resolved_controls.append(primitive)
        required_capabilities.update(primitive.required_capabilities)

    control_ids = {item.primitive_id for item in resolved_controls}
    completion_pairs = tuple(
        (item.policy, item.total_pass_limit)
        for item in definition.completion_policies
    )
    if (
        definition.pipeline_id == "engineering"
        and definition.profile == "fix"
        and (
            control_ids != {"bounded_loop", "human_gate"}
            or completion_pairs != (("attention", 2), ("autonomous", 3))
        )
    ):
        raise ContractError(
            "engineering/fix requires exact bounded completion controls"
        )
    if (
        any(item.policy == "autonomous" for item in definition.completion_policies)
        and "bounded_loop" not in control_ids
    ):
        raise ContractError(
            "autonomous completion policy requires bounded_loop"
        )

    previous: PipelineStep | None = None
    for step in definition.steps:
        primitive = registry.resolve(step.primitive_id, step.primitive_version)
        if primitive.primitive_kind != "step":
            raise ContractError(
                f"{primitive.identity} is not an executable step primitive"
            )
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
    pass_limit = (
        max(
            item.total_pass_limit
            for item in definition.completion_policies
        )
        if "bounded_loop" in control_ids
        else 1
    )
    worst_case_budget = definition.pass_budget.scaled(pass_limit)
    permission_bindings = tuple(
        f"{item}:{PERMISSION_BINDINGS[item]}"
        for item in definition.permission_ceiling
    )
    side_effect_bindings = tuple(
        f"{item}:{SIDE_EFFECT_BINDINGS[item]}"
        for item in definition.side_effects
    )
    canonical = json.dumps(
        {
            "compiler_version": COMPILER_VERSION,
            "definition": to_dict(definition),
            "resolved_primitives": [to_dict(item) for item in resolved],
            "resolved_control_primitives": [
                to_dict(item) for item in resolved_controls
            ],
            "worst_case_budget": to_dict(worst_case_budget),
            "permission_bindings": permission_bindings,
            "side_effect_bindings": side_effect_bindings,
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
        resolved_control_primitives=tuple(
            item.identity for item in resolved_controls
        ),
        required_capabilities=tuple(sorted(required_capabilities)),
        worst_case_budget=worst_case_budget,
        permission_ceiling=definition.permission_ceiling,
        side_effects=definition.side_effects,
        permission_bindings=permission_bindings,
        side_effect_bindings=side_effect_bindings,
    )


def render_contract(
    compiled: CompiledPipeline,
    *,
    completion_policy: str = "attention",
    review_mode: str = "simple",
    max_verify_iterations: int = 1,
    verification_profile: str = "scoped",
) -> str:
    """Render the bounded semantic contract and its execution bindings."""

    if (
        review_mode not in {"simple", "deep", "skip"}
        or type(max_verify_iterations) is not int
        or max_verify_iterations
        != {"simple": 1, "deep": 2, "skip": 0}[review_mode]
    ):
        raise ContractError("rendered review budget is invalid")
    _require_identifier(
        verification_profile or "unbound",
        "verification profile",
    )
    definition = compiled.definition
    completion_modes = {
        item.policy: item for item in definition.completion_policies
    }
    try:
        selected_completion = completion_modes[completion_policy]
    except KeyError as exc:
        raise ContractError(
            "completion policy is outside the compiled contract"
        ) from exc
    budget = compiled.worst_case_budget
    permissions = ", ".join(compiled.permission_bindings) or "none"
    if "cmux-target" in definition.permission_ceiling:
        permissions += "; cmux target scope=policy-only"
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
        "Controls: "
        + (
            ", ".join(compiled.resolved_control_primitives)
            or "none"
        ),
        "Execution: existing harness supervisor with state-free reconciliation",
        "Limits: "
        f"attempts={budget.attempt_limit}, "
        f"model-restarts={budget.model_restart_limit}, "
        f"deadline={budget.time_budget_seconds}s, "
        f"tokens={budget.token_limit}",
        f"Completion: policy={selected_completion.policy}, "
        f"total-passes={selected_completion.total_pass_limit}",
        f"Review: mode={review_mode}, "
        f"verification-iterations={max_verify_iterations}, "
        f"profile={verification_profile or 'unbound'}",
        "Side effects: "
        + (", ".join(compiled.side_effect_bindings) or "none"),
        "Permissions: " + permissions,
        "Enforcement: task/review sequencing=code-policy-enforced",
        "Returns: completed, escalation, attention-required, cancelled, timeout",
    ]
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode()) > 8_192:
        raise ContractError("compiled contract exceeds size cap")
    return rendered
