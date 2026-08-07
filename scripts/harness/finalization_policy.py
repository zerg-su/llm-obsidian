"""Pure compiler for bounded, adaptive finalization-only model routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from model_routing_config import RoutingConfig, RoutingError, validate_effort

from .finalization_ledger import MAX_FINALIZATION_CYCLES


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
AVAILABILITY_SOURCES = frozenset({"provider-adapter", "capability-registry"})
AVAILABILITY_STATUSES = frozenset({"available", "unavailable", "unknown"})
FINALIZATION_POLICY_FIELDS = frozenset(
    {
        "max_cycles",
        "add_independent_model_after",
        "execution",
        "primary_route_alias",
        "independent_route_alias",
    }
)
REGISTERED_FINALIZATION_ROUTES = (
    "finalization-primary",
    "finalization-independent",
)


class FinalizationPolicyError(ValueError):
    """The requested finalization policy cannot be compiled safely."""


def _bounded_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise FinalizationPolicyError(f"{label} must be a bounded identifier")
    return value


def _bounded_epoch(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinalizationPolicyError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class FinalizationPolicy:
    """Additive DSL policy constrained by the code-owned release ceiling."""

    max_cycles: int = MAX_FINALIZATION_CYCLES
    add_independent_model_after: int = 3
    execution: str = "ephemeral"
    primary_route_alias: str = "finalization-primary"
    independent_route_alias: str = "finalization-independent"

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_cycles, bool)
            or not isinstance(self.max_cycles, int)
            or not 1 <= self.max_cycles <= MAX_FINALIZATION_CYCLES
        ):
            raise FinalizationPolicyError(
                "max_cycles must be an integer from 1 to 5"
            )
        if (
            isinstance(self.add_independent_model_after, bool)
            or not isinstance(self.add_independent_model_after, int)
            or not 3 <= self.add_independent_model_after < MAX_FINALIZATION_CYCLES
        ):
            raise FinalizationPolicyError(
                "add_independent_model_after must be 3 or 4"
            )
        if self.execution != "ephemeral":
            raise FinalizationPolicyError(
                "finalization execution must be ephemeral"
            )
        _bounded_identifier(self.primary_route_alias, "primary_route_alias")
        _bounded_identifier(
            self.independent_route_alias, "independent_route_alias"
        )
        if self.primary_route_alias == self.independent_route_alias:
            raise FinalizationPolicyError(
                "finalization route aliases must be independent"
            )


def finalization_policy_payload(policy: FinalizationPolicy) -> dict[str, Any]:
    if not isinstance(policy, FinalizationPolicy):
        raise FinalizationPolicyError("finalization policy is invalid")
    return {
        "max_cycles": policy.max_cycles,
        "add_independent_model_after": policy.add_independent_model_after,
        "execution": policy.execution,
        "primary_route_alias": policy.primary_route_alias,
        "independent_route_alias": policy.independent_route_alias,
    }


def parse_finalization_policy(value: Any) -> FinalizationPolicy:
    """Parse the exact additive nested policy without convenience coercions."""

    if not isinstance(value, Mapping):
        raise FinalizationPolicyError("finalization_policy must be an object")
    unknown = set(value) - FINALIZATION_POLICY_FIELDS
    missing = FINALIZATION_POLICY_FIELDS - set(value)
    if unknown:
        raise FinalizationPolicyError(
            "finalization_policy has unknown fields: "
            + ",".join(sorted(unknown))
        )
    if missing:
        raise FinalizationPolicyError(
            "finalization_policy is missing fields: "
            + ",".join(sorted(missing))
        )
    return FinalizationPolicy(**dict(value))


def require_registered_finalization_routes(policy: FinalizationPolicy) -> None:
    if (
        policy.primary_route_alias,
        policy.independent_route_alias,
    ) != REGISTERED_FINALIZATION_ROUTES:
        raise FinalizationPolicyError(
            "finalization_policy route aliases are not registered"
        )


@dataclass(frozen=True)
class AvailabilityEvidence:
    """Provider-owned, freshness-bounded availability without a model probe."""

    route_alias: str
    status: Literal["available", "unavailable", "unknown"]
    source: Literal["provider-adapter", "capability-registry"]
    checked_at_epoch: int
    valid_until_epoch: int

    def __post_init__(self) -> None:
        _bounded_identifier(self.route_alias, "availability route_alias")
        if self.status not in AVAILABILITY_STATUSES:
            raise FinalizationPolicyError("availability status is invalid")
        if self.source not in AVAILABILITY_SOURCES:
            raise FinalizationPolicyError(
                "availability source must be a provider adapter or capability registry"
            )
        checked = _bounded_epoch(self.checked_at_epoch, "checked_at_epoch")
        valid_until = _bounded_epoch(
            self.valid_until_epoch, "valid_until_epoch"
        )
        if valid_until < checked:
            raise FinalizationPolicyError(
                "availability validity cannot precede its check"
            )

    def is_fresh(self, now_epoch: int) -> bool:
        now = _bounded_epoch(now_epoch, "now_epoch")
        return self.checked_at_epoch <= now <= self.valid_until_epoch

    def payload(self) -> dict[str, Any]:
        return {
            "route_alias": self.route_alias,
            "status": self.status,
            "source": self.source,
            "checked_at_epoch": self.checked_at_epoch,
            "valid_until_epoch": self.valid_until_epoch,
        }


@dataclass(frozen=True)
class FinalizationRoute:
    logical_alias: str
    runtime: str
    model: str
    effort: str
    source: Literal["registered-policy", "explicit-single-model"]


@dataclass(frozen=True)
class FinalizationRouteDecision:
    cycle_number: int
    routes: tuple[FinalizationRoute, ...]
    reason: str
    availability: AvailabilityEvidence | None = None

    def ledger_policy(self) -> dict[str, Any]:
        """Return bounded provider policy bytes for a ledger reservation."""

        value: dict[str, Any] = {
            "routes": [route.logical_alias for route in self.routes],
            "reason": self.reason,
            "bindings": [
                {
                    "logical_alias": route.logical_alias,
                    "runtime": route.runtime,
                    "model": route.model,
                    "effort": route.effort,
                    "source": route.source,
                }
                for route in self.routes
            ],
        }
        if self.availability is not None:
            value["availability"] = self.availability.payload()
        return value


def _registered_route(
    config: RoutingConfig,
    alias: str,
    *,
    effort_override: str = "",
) -> FinalizationRoute:
    try:
        value = config.finalization_route(alias)
        effort = effort_override or value["effort"]
        validate_effort(value["runtime"], effort)
    except (KeyError, RoutingError) as exc:
        raise FinalizationPolicyError(str(exc)) from exc
    return FinalizationRoute(
        logical_alias=alias,
        runtime=value["runtime"],
        model=value["model"],
        effort=effort,
        source="registered-policy",
    )


def _explicit_route(
    config: RoutingConfig,
    primary: FinalizationRoute,
    *,
    runtime: str,
    model: str,
    effort: str,
) -> FinalizationRoute:
    aliases = config.data["model_aliases"]
    registry = config.data["model_registry"]
    if model and model not in aliases and model not in registry:
        raise FinalizationPolicyError(
            f"explicit model alias {model!r} is not registered"
        )
    try:
        if model:
            target = config.resolve_alias(model, runtime)
        else:
            if runtime not in {"claude", "codex"}:
                raise RoutingError("explicit runtime must be claude or codex")
            target = config.reviewer_default(runtime, "deep")
        selected_effort = effort or primary.effort
        validate_effort(target["runtime"], selected_effort)
    except (KeyError, RoutingError) as exc:
        raise FinalizationPolicyError(str(exc)) from exc
    return FinalizationRoute(
        logical_alias=primary.logical_alias,
        runtime=target["runtime"],
        model=target["model"],
        effort=selected_effort,
        source="explicit-single-model",
    )


def compile_finalization_routes(
    *,
    config: RoutingConfig,
    policy: FinalizationPolicy,
    cycle_number: int,
    independent_permitted: bool = True,
    availability: AvailabilityEvidence | None = None,
    explicit_runtime: str = "",
    explicit_model: str = "",
    explicit_effort: str = "",
    required_mode: str = "",
    now_epoch: int,
) -> FinalizationRouteDecision:
    """Compile one finalization cycle without performing availability effects."""

    if not isinstance(policy, FinalizationPolicy):
        raise FinalizationPolicyError("finalization policy is invalid")
    if (
        isinstance(cycle_number, bool)
        or not isinstance(cycle_number, int)
        or not 1 <= cycle_number <= policy.max_cycles
    ):
        raise FinalizationPolicyError(
            "cycle_number exceeds the finalization policy"
        )
    if type(independent_permitted) is not bool:
        raise FinalizationPolicyError("independent_permitted must be boolean")
    if required_mode not in {"", "simple", "deep", "full"}:
        raise FinalizationPolicyError("required review mode is invalid")
    _bounded_epoch(now_epoch, "now_epoch")
    primary = _registered_route(
        config, policy.primary_route_alias, effort_override=explicit_effort
    )
    # Validate the frozen candidate even when this early cycle cannot use it.
    independent = _registered_route(
        config, policy.independent_route_alias, effort_override=explicit_effort
    )
    if primary.runtime == independent.runtime:
        raise FinalizationPolicyError(
            "finalization routes must use different providers"
        )
    if required_mode == "full":
        if explicit_runtime or explicit_model:
            raise FinalizationPolicyError(
                "Full finalization cannot carry a single-model override"
            )
        if not independent_permitted:
            raise FinalizationPolicyError(
                "Full finalization requires both permitted providers"
            )
        return FinalizationRouteDecision(
            cycle_number,
            (primary, independent),
            "explicit-full",
            availability,
        )
    if explicit_runtime or explicit_model:
        selected = _explicit_route(
            config,
            primary,
            runtime=explicit_runtime,
            model=explicit_model,
            effort=explicit_effort,
        )
        return FinalizationRouteDecision(
            cycle_number,
            (selected,),
            "explicit-single-model",
        )
    if cycle_number <= policy.add_independent_model_after:
        return FinalizationRouteDecision(
            cycle_number, (primary,), "primary-only"
        )
    if not independent_permitted:
        return FinalizationRouteDecision(
            cycle_number, (primary,), "provider-policy"
        )
    if (
        availability is not None
        and availability.route_alias != policy.independent_route_alias
    ):
        raise FinalizationPolicyError(
            "availability route identity mismatches finalization policy"
        )
    if (
        availability is None
        or availability.status == "unknown"
        or not availability.is_fresh(now_epoch)
    ):
        return FinalizationRouteDecision(
            cycle_number,
            (primary,),
            "availability-unknown",
            availability,
        )
    if availability.status == "unavailable":
        return FinalizationRouteDecision(
            cycle_number,
            (primary,),
            "provider-unavailable",
            availability,
        )
    return FinalizationRouteDecision(
        cycle_number,
        (primary, independent),
        "independent-available",
        availability,
    )
