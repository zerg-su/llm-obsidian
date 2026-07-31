"""Strict model-authored pipeline specs compiled by the 2.4 harness compiler.

This module accepts data, never source code.  Parsing, policy comparison, and
canonical hashing are code-owned and happen before any runtime effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, ID_RE, SHA256_RE, to_dict
from .pipelines import (
    DEFAULT_PERMISSION_CEILING,
    DEFAULT_SIDE_EFFECTS,
    CompletionPolicy,
    CompiledPipeline,
    PipelineBudget,
    PipelineDefinition,
    PipelineStep,
    PrimitiveReference,
    PrimitiveRegistry,
    SCHEMA_RE,
    SEMVER_RE,
    compile_pipeline,
)


CUSTOM_SPEC_VERSION = 1
CUSTOM_COMPILER_VERSION = "1.0.0"
REVIEW_MODES = frozenset({"simple", "deep", "skip"})
BASELINES = {
    "change": "engineering/change",
    "fix": "engineering/fix",
}


def _exact_fields(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise ContractError(f"{label} has unknown fields: {','.join(sorted(unknown))}")
    if missing:
        raise ContractError(f"{label} is missing fields: {','.join(sorted(missing))}")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded identifier")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be an array")
    items = tuple(
        _identifier(item, label[:-1] if label.endswith("s") else label)
        for item in value
    )
    if len(items) != len(set(items)):
        raise ContractError(f"{label} must be unique")
    return items


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class ContextPointer:
    pointer_id: str
    content_sha256: str
    byte_limit: int

    def __post_init__(self) -> None:
        _identifier(self.pointer_id, "context pointer_id")
        if not isinstance(self.content_sha256, str) or not SHA256_RE.fullmatch(
            self.content_sha256
        ):
            raise ContractError("context content_sha256 must be a lowercase sha256")
        _integer(self.byte_limit, "context byte_limit", minimum=1)


@dataclass(frozen=True)
class PipelineSpec:
    spec_id: str
    version: str
    intent: str
    task_profile: str
    baseline_pipeline: str
    input_schema: str
    output_schema: str
    steps: tuple[PipelineStep, ...]
    controls: tuple[PrimitiveReference, ...]
    budget: PipelineBudget
    completion_policy: str
    requested_permissions: tuple[str, ...]
    requested_side_effects: tuple[str, ...]
    context_pointers: tuple[ContextPointer, ...]
    verification_checks: tuple[str, ...]
    review_mode: str
    human_gates: tuple[str, ...]
    terminal_outcomes: tuple[str, ...]
    schema_version: int = CUSTOM_SPEC_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CUSTOM_SPEC_VERSION:
            raise ContractError("unsupported PipelineSpec schema")
        for value, label in (
            (self.spec_id, "spec_id"),
            (self.intent, "intent"),
            (self.task_profile, "task_profile"),
        ):
            _identifier(value, label)
        if not SEMVER_RE.fullmatch(self.version):
            raise ContractError("version must be a semantic version")
        if (
            not SCHEMA_RE.fullmatch(self.baseline_pipeline)
            or "/" not in self.baseline_pipeline
        ):
            raise ContractError("baseline_pipeline must name a built-in pipeline")
        if self.review_mode not in REVIEW_MODES:
            raise ContractError("review_mode is invalid")
        if self.completion_policy not in {"attention", "autonomous"}:
            raise ContractError("completion_policy is invalid")
        if not self.steps:
            raise ContractError("PipelineSpec requires at least one step")
        if not self.human_gates or "initial-approval" not in self.human_gates:
            raise ContractError("PipelineSpec requires an initial-approval gate")
        if not self.terminal_outcomes:
            raise ContractError("PipelineSpec requires terminal outcomes")


@dataclass(frozen=True)
class CustomPipelinePolicy:
    """Code-owned limits that model output cannot widen."""

    max_steps: int
    max_controls: int
    max_context_pointers: int
    max_context_bytes: int
    max_verification_checks: int
    permission_ceiling: tuple[str, ...]
    side_effect_ceiling: tuple[str, ...]
    worst_case_budget: PipelineBudget
    schema_version: int = 1

    @classmethod
    def default(cls) -> "CustomPipelinePolicy":
        return cls(
            max_steps=8,
            max_controls=2,
            max_context_pointers=8,
            max_context_bytes=1_048_576,
            max_verification_checks=8,
            permission_ceiling=DEFAULT_PERMISSION_CEILING,
            side_effect_ceiling=DEFAULT_SIDE_EFFECTS,
            worst_case_budget=PipelineBudget().scaled(3),
        )


@dataclass(frozen=True)
class ExplicitPipelineApproval:
    definition_sha256: str
    approval_card_sha256: str
    actor: str
    decision: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported pipeline approval schema")
        for value, label in (
            (self.definition_sha256, "approval definition_sha256"),
            (self.approval_card_sha256, "approval card sha256"),
        ):
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise ContractError(f"{label} must be a lowercase sha256")
        _identifier(self.actor, "approval actor")
        if self.decision not in {"approve", "revise", "reject"}:
            raise ContractError("approval decision is invalid")

    @classmethod
    def for_card(
        cls,
        *,
        definition_sha256: str,
        approval_card: str,
        actor: str,
        decision: str,
    ) -> "ExplicitPipelineApproval":
        return cls(
            definition_sha256=definition_sha256,
            approval_card_sha256=hashlib.sha256(approval_card.encode()).hexdigest(),
            actor=actor,
            decision=decision,
        )


@dataclass(frozen=True)
class FrozenCustomPipeline:
    spec: PipelineSpec
    compiled: CompiledPipeline
    approval: ExplicitPipelineApproval
    spec_sha256: str
    approval_sha256: str
    schema_version: int = 1

    @property
    def definition_sha256(self) -> str:
        return self.compiled.definition_sha256


def pipeline_spec_payload(spec: PipelineSpec) -> dict[str, Any]:
    """Return the exact published JSON shape, excluding dataclass internals."""

    value = to_dict(spec)
    for item in value["steps"]:
        item.pop("schema_version", None)
    for item in value["controls"]:
        item.pop("schema_version", None)
    value["budget"].pop("schema_version", None)
    for item in value["context_pointers"]:
        item.pop("schema_version", None)
    return value


class FrozenPipelineStore:
    """Owner-only scratch for an approved custom contract."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, operation_id: str) -> Path:
        _identifier(operation_id, "custom operation_id")
        return self.root / operation_id / "custom-pipeline.json"

    def save(
        self,
        *,
        operation_id: str,
        spec: PipelineSpec,
        frozen: FrozenCustomPipeline,
        approval: ExplicitPipelineApproval,
    ) -> Path:
        if spec != frozen.spec or approval != frozen.approval:
            raise ContractError("frozen custom pipeline inputs do not match")
        path = self._path(operation_id)
        payload = {
            "schema_version": 1,
            "operation_id": operation_id,
            "definition_sha256": frozen.definition_sha256,
            "spec_sha256": frozen.spec_sha256,
            "approval": to_dict(approval),
            "spec": pipeline_spec_payload(spec),
        }
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise ContractError("frozen custom pipeline must be a regular file")
            if path.read_bytes() != encoded:
                raise ContractError("frozen custom pipeline changed during replay")
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ContractError("frozen custom pipeline is not owner-only")
            return path
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        os.chmod(path, 0o600)
        return path

    def load(
        self,
        *,
        operation_id: str,
        registry: PrimitiveRegistry,
        policy: CustomPipelinePolicy,
        capabilities: tuple[str, ...] = (),
    ) -> FrozenCustomPipeline:
        path = self._path(operation_id)
        if path.is_symlink() or not path.is_file():
            raise ContractError("frozen custom pipeline is unavailable")
        if (
            stat.S_IMODE(path.stat().st_mode) & 0o077
            or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
        ):
            raise ContractError("frozen custom pipeline is not owner-only")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("frozen custom pipeline is invalid") from exc
        value = _object(value, "frozen custom pipeline")
        _exact_fields(
            value,
            frozenset(
                {
                    "schema_version",
                    "operation_id",
                    "definition_sha256",
                    "spec_sha256",
                    "approval",
                    "spec",
                }
            ),
            "frozen custom pipeline",
        )
        if value["schema_version"] != 1 or value["operation_id"] != operation_id:
            raise ContractError("frozen custom pipeline identity is invalid")
        approval_value = _object(value["approval"], "pipeline approval")
        _exact_fields(
            approval_value,
            frozenset(
                {
                    "schema_version",
                    "definition_sha256",
                    "approval_card_sha256",
                    "actor",
                    "decision",
                }
            ),
            "pipeline approval",
        )
        approval = ExplicitPipelineApproval(**approval_value)
        spec = parse_pipeline_spec(_object(value["spec"], "PipelineSpec"))
        compiled = compile_custom_spec(
            spec,
            registry,
            policy=policy,
            capabilities=capabilities,
        )
        card = render_custom_approval(spec, compiled, policy=policy)
        frozen = freeze_custom_pipeline(spec, compiled, approval, card)
        if (
            value["definition_sha256"] != frozen.definition_sha256
            or value["spec_sha256"] != frozen.spec_sha256
        ):
            raise ContractError("frozen custom pipeline definition changed")
        return frozen


def resolve_custom_executable(
    *,
    store_root: Path | str,
    operation_id: str,
    definition_sha256: str,
    registry: PrimitiveRegistry,
    policy: CustomPipelinePolicy,
    capabilities: tuple[str, ...] = (),
) -> tuple[str, CompiledPipeline]:
    """Resolve an approved custom contract onto an existing runtime executor."""

    from .pipeline_builtins import EXECUTABLE_BUILTINS, compiled_builtin

    frozen = FrozenPipelineStore(store_root).load(
        operation_id=operation_id,
        registry=registry,
        policy=policy,
        capabilities=capabilities,
    )
    if frozen.definition_sha256 != definition_sha256:
        raise ContractError("custom executable definition does not match operation")
    baseline = frozen.spec.baseline_pipeline
    if baseline not in EXECUTABLE_BUILTINS:
        raise ContractError("custom executable baseline is unavailable")
    baseline_shape = tuple(
        step.primitive_id for step in compiled_builtin(baseline).definition.steps
    )
    custom_shape = tuple(
        step.primitive_id for step in frozen.compiled.definition.steps
    )
    if custom_shape != baseline_shape:
        raise ContractError("custom executable shape differs from its baseline")
    return baseline, frozen.compiled


TOP_FIELDS = frozenset(
    {
        "schema_version",
        "spec_id",
        "version",
        "intent",
        "task_profile",
        "baseline_pipeline",
        "input_schema",
        "output_schema",
        "steps",
        "controls",
        "budget",
        "completion_policy",
        "requested_permissions",
        "requested_side_effects",
        "context_pointers",
        "verification_checks",
        "review_mode",
        "human_gates",
        "terminal_outcomes",
    }
)
STEP_FIELDS = frozenset(
    {
        "step_id",
        "primitive_id",
        "primitive_version",
        "input_schema",
        "output_schema",
        "session_mode",
        "semantic_skills",
    }
)
CONTROL_FIELDS = frozenset({"primitive_id", "version"})
BUDGET_FIELDS = frozenset(
    {"attempt_limit", "model_restart_limit", "time_budget_seconds", "token_limit"}
)
CONTEXT_FIELDS = frozenset({"pointer_id", "content_sha256", "byte_limit"})


def parse_pipeline_spec(raw: str | bytes | Mapping[str, Any]) -> PipelineSpec:
    """Parse exact schema-v1 JSON without accepting convenience coercions."""

    if isinstance(raw, (str, bytes)):
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("PipelineSpec must be valid JSON") from exc
    elif isinstance(raw, Mapping):
        value = dict(raw)
    else:
        raise ContractError("PipelineSpec must be JSON or an object")
    value = _object(value, "PipelineSpec")
    _exact_fields(value, TOP_FIELDS, "PipelineSpec")
    if value["schema_version"] != CUSTOM_SPEC_VERSION:
        raise ContractError("unsupported PipelineSpec schema")

    raw_steps = value["steps"]
    if not isinstance(raw_steps, list):
        raise ContractError("steps must be an array")
    steps: list[PipelineStep] = []
    for raw_step in raw_steps:
        item = _object(raw_step, "step")
        _exact_fields(item, STEP_FIELDS, "step")
        skills = _string_tuple(item["semantic_skills"], "semantic skills")
        steps.append(
            PipelineStep(
                step_id=item["step_id"],
                primitive_id=item["primitive_id"],
                primitive_version=item["primitive_version"],
                input_schema=item["input_schema"],
                output_schema=item["output_schema"],
                session_mode=item["session_mode"],
                semantic_skills=skills,
            )
        )

    raw_controls = value["controls"]
    if not isinstance(raw_controls, list):
        raise ContractError("controls must be an array")
    controls: list[PrimitiveReference] = []
    for raw_control in raw_controls:
        item = _object(raw_control, "control")
        _exact_fields(item, CONTROL_FIELDS, "control")
        controls.append(PrimitiveReference(item["primitive_id"], item["version"]))

    budget_value = _object(value["budget"], "budget")
    _exact_fields(budget_value, BUDGET_FIELDS, "budget")
    budget = PipelineBudget(
        attempt_limit=budget_value["attempt_limit"],
        model_restart_limit=budget_value["model_restart_limit"],
        time_budget_seconds=budget_value["time_budget_seconds"],
        token_limit=budget_value["token_limit"],
    )

    raw_pointers = value["context_pointers"]
    if not isinstance(raw_pointers, list):
        raise ContractError("context_pointers must be an array")
    pointers: list[ContextPointer] = []
    for raw_pointer in raw_pointers:
        item = _object(raw_pointer, "context pointer")
        _exact_fields(item, CONTEXT_FIELDS, "context pointer")
        pointers.append(
            ContextPointer(
                item["pointer_id"], item["content_sha256"], item["byte_limit"]
            )
        )

    return PipelineSpec(
        spec_id=value["spec_id"],
        version=value["version"],
        intent=value["intent"],
        task_profile=value["task_profile"],
        baseline_pipeline=value["baseline_pipeline"],
        input_schema=value["input_schema"],
        output_schema=value["output_schema"],
        steps=tuple(steps),
        controls=tuple(controls),
        budget=budget,
        completion_policy=value["completion_policy"],
        requested_permissions=_string_tuple(
            value["requested_permissions"], "requested permissions"
        ),
        requested_side_effects=_string_tuple(
            value["requested_side_effects"], "requested side effects"
        ),
        context_pointers=tuple(pointers),
        verification_checks=_string_tuple(
            value["verification_checks"], "verification checks"
        ),
        review_mode=value["review_mode"],
        human_gates=_string_tuple(value["human_gates"], "human gates"),
        terminal_outcomes=_string_tuple(
            value["terminal_outcomes"], "terminal outcomes"
        ),
    )


def select_builtin_baseline(intent: str, task_profile: str) -> str:
    """Choose the comparison baseline without trusting model output."""

    _identifier(intent, "intent")
    _identifier(task_profile, "task_profile")
    return BASELINES.get(task_profile, "lifecycle/default")


def render_authoring_contract(*, intent: str, task_profile: str) -> str:
    """Bounded prompt fragment for one model proposal step."""

    baseline = select_builtin_baseline(intent, task_profile)
    return "\n".join(
        (
            "Return exactly one JSON object matching schemas/pipeline-spec-v1.schema.json.",
            f"Intent: {intent}; task profile: {task_profile}.",
            f"Baseline: {baseline}.",
            "Recommend the built-in when it satisfies the task; propose custom only for a semantic gap.",
            "Use only registered primitive ids, bounded identifiers, and declared context hashes.",
            "Do not emit shell, Python, filesystem paths, credentials, dependencies, or plugins.",
            "The harness validates, renders, and requires explicit user approval before effects.",
        )
    ) + "\n"


def _built_in_semantically_fits(spec: PipelineSpec) -> bool:
    from .pipeline_builtins import compiled_builtin

    baseline = compiled_builtin(spec.baseline_pipeline).definition
    return (
        spec.input_schema == baseline.input_schema
        and spec.output_schema == baseline.output_schema
        and spec.steps == baseline.steps
        and spec.controls == baseline.control_primitives
        and spec.completion_policy
        in {item.policy for item in baseline.completion_policies}
        and not spec.verification_checks
        and spec.review_mode == "simple"
    )


def _budget_within(value: PipelineBudget, ceiling: PipelineBudget) -> bool:
    return all(
        left <= right
        for left, right in (
            (value.attempt_limit, ceiling.attempt_limit),
            (value.model_restart_limit, ceiling.model_restart_limit),
            (value.time_budget_seconds, ceiling.time_budget_seconds),
            (value.token_limit, ceiling.token_limit),
        )
    )


def compile_custom_spec(
    spec: PipelineSpec,
    registry: PrimitiveRegistry,
    *,
    policy: CustomPipelinePolicy,
    capabilities: tuple[str, ...] = (),
) -> CompiledPipeline:
    """Compile a model proposal without creating any operation or effect."""

    selected_baseline = select_builtin_baseline(spec.intent, spec.task_profile)
    if spec.baseline_pipeline != selected_baseline:
        raise ContractError(
            "PipelineSpec baseline differs from the deterministic baseline"
        )
    if _built_in_semantically_fits(spec):
        raise ContractError("built-in already fits; custom pipeline is unnecessary")

    if len(spec.steps) > policy.max_steps:
        raise ContractError("PipelineSpec exceeds the code-owned step limit")
    if len(spec.controls) > policy.max_controls:
        raise ContractError("PipelineSpec exceeds the code-owned control limit")
    if len(spec.context_pointers) > policy.max_context_pointers:
        raise ContractError("PipelineSpec exceeds the context pointer limit")
    if sum(item.byte_limit for item in spec.context_pointers) > policy.max_context_bytes:
        raise ContractError("PipelineSpec exceeds the context byte limit")
    if len(spec.verification_checks) > policy.max_verification_checks:
        raise ContractError("PipelineSpec exceeds the verification check limit")
    if not set(spec.requested_permissions).issubset(policy.permission_ceiling):
        raise ContractError("PipelineSpec exceeds the code-owned permission ceiling")
    if not set(spec.requested_side_effects).issubset(policy.side_effect_ceiling):
        raise ContractError("PipelineSpec exceeds the code-owned side-effect ceiling")

    policies = [CompletionPolicy("attention", 2)]
    if spec.completion_policy == "autonomous":
        policies.append(CompletionPolicy("autonomous", 3))
    definition = PipelineDefinition(
        pipeline_id="custom",
        version=spec.version,
        profile=spec.task_profile,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        steps=spec.steps,
        control_primitives=spec.controls,
        pass_budget=spec.budget,
        permission_ceiling=spec.requested_permissions,
        side_effects=spec.requested_side_effects,
        completion_policies=tuple(policies),
    )
    compiled = compile_pipeline(definition, registry, capabilities=capabilities)
    if not _budget_within(compiled.worst_case_budget, policy.worst_case_budget):
        raise ContractError("PipelineSpec exceeds the code-owned budget ceiling")

    canonical = json.dumps(
        {
            "custom_compiler_version": CUSTOM_COMPILER_VERSION,
            "pipeline_spec": pipeline_spec_payload(spec),
            "compiled_pipeline": json.loads(compiled.canonical_definition),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return replace(
        compiled,
        canonical_definition=canonical,
        definition_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def freeze_custom_pipeline(
    spec: PipelineSpec,
    compiled: CompiledPipeline,
    approval: ExplicitPipelineApproval,
    approval_card: str,
) -> FrozenCustomPipeline:
    """Bind exact user consent to an immutable compiled definition."""

    if approval.actor != "user" or approval.decision != "approve":
        raise ContractError("only explicit user approval can freeze a pipeline")
    if approval.definition_sha256 != compiled.definition_sha256:
        raise ContractError("approval definition does not match compiled definition")
    card_sha256 = hashlib.sha256(approval_card.encode()).hexdigest()
    if approval.approval_card_sha256 != card_sha256:
        raise ContractError("approval card does not match the reviewed contract")
    spec_sha256 = hashlib.sha256(
        json.dumps(
            pipeline_spec_payload(spec),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return FrozenCustomPipeline(
        spec=spec,
        compiled=compiled,
        approval=approval,
        spec_sha256=spec_sha256,
        approval_sha256=approval.approval_card_sha256,
    )


def render_custom_approval(
    spec: PipelineSpec,
    compiled: CompiledPipeline,
    *,
    policy: CustomPipelinePolicy,
) -> str:
    """Render a bounded human-readable pre-effect approval card."""

    budget = compiled.worst_case_budget
    ceiling = policy.worst_case_budget
    rendered = "\n".join(
        (
            f"Custom pipeline: {spec.spec_id}@{spec.version}",
            f"Definition: {compiled.definition_sha256}",
            f"Baseline: {spec.baseline_pipeline}",
            "Delta: " + " -> ".join(step.step_id for step in spec.steps),
            "Worst case: "
            f"attempts={budget.attempt_limit}, restarts={budget.model_restart_limit}, "
            f"deadline={budget.time_budget_seconds}s, tokens={budget.token_limit}",
            "Absolute ceiling: "
            f"attempts={ceiling.attempt_limit}, restarts={ceiling.model_restart_limit}, "
            f"deadline={ceiling.time_budget_seconds}s, tokens={ceiling.token_limit}",
            "Requested permissions: "
            + (", ".join(compiled.permission_bindings) or "none"),
            "Requested side effects: "
            + (", ".join(compiled.side_effect_bindings) or "none"),
            f"Review: {spec.review_mode}; completion: {spec.completion_policy}",
            "Stops: " + ", ".join(spec.terminal_outcomes),
            "Explicit user approval required before freeze or execution.",
        )
    ) + "\n"
    if len(rendered.encode()) > 8_192:
        raise ContractError("custom approval summary exceeds size cap")
    return rendered
