#!/usr/bin/env python3
"""Behavior matrix for finalization-only adaptive route selection."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.finalization_policy import (  # noqa: E402
    AvailabilityEvidence,
    FinalizationPolicy,
    FinalizationPolicyError,
    compile_finalization_routes,
)
from model_routing_config import (  # noqa: E402
    RoutingError,
    _validate,
    load_tracked_config,
)
from review_contract import compile_review_axes  # noqa: E402
from review_contract import (  # noqa: E402
    ReviewContractError,
    compile_effective_review_topology,
    review_runtime_provider,
    validate_review_axes,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


config = load_tracked_config(ROOT)
policy = FinalizationPolicy()

primary = config.finalization_route("finalization-primary")
independent = config.finalization_route("finalization-independent")
check(
    "finalization routes are registered as independent provider choices",
    primary["runtime"] == "claude"
    and independent["runtime"] == "codex"
    and primary["runtime"] != independent["runtime"],
)

routing_doc = (ROOT / "docs/model-routing.md").read_text(encoding="utf-8")
check(
    "routing documentation gates the structural route on an accepted pivot receipt",
    "accepted structural-pivot receipt" in routing_doc.casefold()
    and "availability evidence does not authorize cycles 4–5"
    in routing_doc.casefold(),
)

for cycle in range(1, 4):
    decision = compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=cycle,
        now_epoch=1_000,
    )
    check(
        f"cycle {cycle} materializes only finalization-primary",
        tuple(route.logical_alias for route in decision.routes)
        == ("finalization-primary",)
        and decision.reason == "primary-only",
    )

available = AvailabilityEvidence(
    route_alias="finalization-independent",
    status="available",
    source="provider-adapter",
    checked_at_epoch=990,
    valid_until_epoch=1_010,
)
expanded = compile_finalization_routes(
    config=config,
    policy=policy,
    cycle_number=4,
    availability=available,
    now_epoch=1_000,
)
check(
    "cycle 4 expands only on fresh typed availability",
    tuple(route.logical_alias for route in expanded.routes)
    == ("finalization-primary", "finalization-independent")
    and expanded.reason == "independent-available",
)

unavailable = compile_finalization_routes(
    config=config,
    policy=policy,
    cycle_number=4,
    availability=AvailabilityEvidence(
        route_alias="finalization-independent",
        status="unavailable",
        source="capability-registry",
        checked_at_epoch=990,
        valid_until_epoch=1_010,
    ),
    now_epoch=1_000,
)
check(
    "typed unavailability keeps the single primary route",
    len(unavailable.routes) == 1 and unavailable.reason == "provider-unavailable",
)

for label, evidence in (
    ("missing", None),
    (
        "unknown",
        AvailabilityEvidence(
            route_alias="finalization-independent",
            status="unknown",
            source="provider-adapter",
            checked_at_epoch=990,
            valid_until_epoch=1_010,
        ),
    ),
    (
        "stale",
        AvailabilityEvidence(
            route_alias="finalization-independent",
            status="available",
            source="provider-adapter",
            checked_at_epoch=900,
            valid_until_epoch=950,
        ),
    ),
):
    decision = compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=5,
        availability=evidence,
        now_epoch=1_000,
    )
    check(
        f"{label} availability cannot trigger a probe or route expansion",
        len(decision.routes) == 1 and decision.reason == "availability-unknown",
    )

denied = compile_finalization_routes(
    config=config,
    policy=policy,
    cycle_number=4,
    independent_permitted=False,
    availability=available,
    now_epoch=1_000,
)
check(
    "frozen provider policy denial wins over availability",
    len(denied.routes) == 1 and denied.reason == "provider-policy",
)

explicit = compile_finalization_routes(
    config=config,
    policy=policy,
    cycle_number=5,
    explicit_runtime="claude",
    explicit_model="opus",
    explicit_effort="xhigh",
    availability=available,
    now_epoch=1_000,
)
check(
    "explicit single-model precedence suppresses late expansion",
    len(explicit.routes) == 1
    and explicit.routes[0].runtime == "claude"
    and explicit.routes[0].model == "claude-opus-5"
    and explicit.routes[0].source == "explicit-single-model"
    and explicit.reason == "explicit-single-model",
)

for label, kwargs, expected in (
    (
        "unknown explicit model alias fails before effects",
        {"explicit_runtime": "codex", "explicit_model": "not-registered"},
        "not registered",
    ),
    (
        "unknown finalization route alias fails before effects",
        {
            "policy": FinalizationPolicy(
                independent_route_alias="not-registered"
            )
        },
        "not registered",
    ),
    (
        "cycle ceiling fails before effects",
        {"cycle_number": 6},
        "cycle_number",
    ),
    (
        "availability route identity mismatch fails before effects",
        {
            "availability": AvailabilityEvidence(
                route_alias="finalization-primary",
                status="available",
                source="provider-adapter",
                checked_at_epoch=990,
                valid_until_epoch=1_010,
            )
        },
        "route identity",
    ),
):
    arguments = {
        "config": config,
        "policy": policy,
        "cycle_number": 4,
        "now_epoch": 1_000,
    }
    arguments.update(kwargs)
    try:
        compile_finalization_routes(**arguments)
    except FinalizationPolicyError as exc:
        check(label, expected in str(exc))
    else:
        check(label, False)

try:
    AvailabilityEvidence(
        route_alias="finalization-independent",
        status="available",
        source="statusline",
        checked_at_epoch=990,
        valid_until_epoch=1_010,
    )
except FinalizationPolicyError as exc:
    check(
        "statusline text is not a typed availability authority",
        "source" in str(exc),
    )
else:
    check("statusline text is not a typed availability authority", False)

for label, mutate, expected in (
    (
        "routing config requires both finalization aliases",
        lambda value: value["finalization_routes"].pop("finalization-independent"),
        "exactly finalization-primary and finalization-independent",
    ),
    (
        "routing config requires provider independence",
        lambda value: value["finalization_routes"][
            "finalization-independent"
        ].update({"runtime": "claude", "model": "fable"}),
        "different providers",
    ),
):
    candidate = copy.deepcopy(config.data)
    mutate(candidate)
    try:
        _validate(candidate)
    except RoutingError as exc:
        check(label, expected in str(exc))
    else:
        check(label, False)

check(
    "standalone Deep remains the dual-provider holistic topology",
    compile_review_axes("deep")
    == ("anthropic-holistic", "openai-holistic"),
)

matrix_cases = 0
for mode in ("simple", "deep", "full"):
    for cycle in range(1, 6):
        for availability_case in (None, available):
            decision = compile_finalization_routes(
                config=config,
                policy=policy,
                cycle_number=cycle,
                availability=availability_case,
                required_mode=mode,
                now_epoch=1_000,
            )
            routes = {
                review_runtime_provider(route.runtime): {
                    "runtime": route.runtime,
                    "model": route.model,
                    "effort": route.effort,
                    "profile": "reviewer-callback",
                    "routing_sha256": config.fingerprint,
                }
                for route in decision.routes
            }
            topology = compile_effective_review_topology(
                mode=mode,
                cross_model=False,
                max_verify_iterations={"simple": 1, "deep": 2, "full": 2}[mode],
                verification_profile="scoped",
                verification_profile_sha256="8" * 64,
                routes=routes,
            )
            expected_mode = (
                "deep"
                if mode == "simple"
                and cycle >= 4
                and availability_case is available
                else mode
            )
            expected_count = (
                4
                if mode == "full"
                else 2
                if expected_mode == "deep"
                else 1
            )
            check(
                f"{mode} cycle {cycle} availability "
                f"{availability_case is available} is classified exactly",
                topology.mode == expected_mode
                and len(topology.lanes) == expected_count,
            )
            matrix_cases += 1

check(
    "the bounded topology matrix has zero unclassified cases",
    matrix_cases == 30,
)
try:
    validate_review_axes(
        "deep", ("anthropic-intent", "openai-engineering")
    )
except ReviewContractError:
    check(
        "cross-provider responsibility pairs remain forbidden",
        True,
    )
else:
    check(
        "cross-provider responsibility pairs remain forbidden",
        False,
    )

print("\nAll finalization routing tests passed.")
