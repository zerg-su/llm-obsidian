#!/usr/bin/env python3
"""Deterministic contracts for the public review topology compiler."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from review_contract import (  # noqa: E402
    MODES,
    REVIEW_PARENT_KINDS,
    VERIFY_BUDGETS,
    compile_effective_review_topology,
    compile_review_axes,
    review_axis_provider,
    review_axis_responsibility,
    review_provider_runtime,
    review_runtime_provider,
)
from harness.status_segment import CONTROLLER_KINDS  # noqa: E402
from harness.review_workspace import ReviewWorkspaceBinding  # noqa: E402
from harness.finalization_policy import (  # noqa: E402
    FinalizationPolicy,
    compile_finalization_routes,
)
from harness.workflows.review_gate import ReviewPreset  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import ReviewContext, review_session_specs  # noqa: E402
from task_review_request import _prompt, _request  # noqa: E402
from task_review_context import (  # noqa: E402
    _assert_frozen_topology,
    _request as frozen_task_request,
)
from task_review_finalization_attempt import _bind_routes  # noqa: E402
from model_routing import load_config  # noqa: E402

def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


fixture_path = ROOT / "tests/fixtures/rc4/review-control-plane.json"
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
fixture_cases = fixture.get("cases")
fixture_cases_sha256 = hashlib.sha256(
    json.dumps(
        fixture_cases,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
check(
    "RC4 control-plane baseline fixture is exact and content-bound",
    fixture.get("schema_version") == 1
    and fixture.get("product_base_sha")
    == "7033413afc9dc7666f4a2a76de33c088dbfcbfef"
    and fixture.get("cases_sha256") == fixture_cases_sha256
    and isinstance(fixture_cases, list)
    and {item.get("case_id") for item in fixture_cases}
    == {
        "preview-runtime-topology-drift",
        "callback-prefix-not-durable",
    },
)
check(
    "RC4 baseline premises perform zero provider, model, cmux, or replay effects",
    all(
        item.get(field, 0) == 0
        for item in fixture_cases
        for field in (
            "provider_effect_count",
            "model_effect_count",
            "cmux_effect_count",
            "provider_replay_count",
            "reviewer_replay_count",
            "coordinator_poll_count",
        )
    ),
)


check("full is an explicit review mode", MODES == {"simple", "deep", "full"})
check(
    "status projection covers every canonical review parent kind",
    REVIEW_PARENT_KINDS <= CONTROLLER_KINDS,
)
check(
    "simple keeps one selected-model holistic lane",
    compile_review_axes("simple", selected_provider="openai")
    == ("openai-holistic",),
)
check(
    "default deep uses two independent holistic models",
    compile_review_axes("deep")
    == ("anthropic-holistic", "openai-holistic"),
)
check(
    "single-model deep splits intent and engineering",
    compile_review_axes("deep", selected_provider="anthropic")
    == ("anthropic-intent", "anthropic-engineering"),
)
check(
    "full is the ordered dual-model specialist grid",
    compile_review_axes("full")
    == (
        "anthropic-intent",
        "anthropic-engineering",
        "openai-intent",
        "openai-engineering",
    ),
)
check(
    "full reuses the deep verification budget",
    VERIFY_BUDGETS["full"] == VERIFY_BUDGETS["deep"] == 2,
)

simple_effective = compile_effective_review_topology(
    mode="simple",
    cross_model=False,
    max_verify_iterations=1,
    verification_profile="scoped",
    verification_profile_sha256="8" * 64,
    routes={
        "openai": {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
            "profile": "reviewer-callback",
            "routing_sha256": "9" * 64,
        }
    },
    selected_provider="openai",
)
expected_simple_payload = {
    "schema_version": 1,
    "requested_mode": "simple",
    "mode": "simple",
    "cross_model": False,
    "max_verify_iterations": 1,
    "verification_profile": {
        "name": "scoped",
        "sha256": "8" * 64,
    },
    "session_count": 1,
    "lanes": [
        {
            "axis": "openai-holistic",
            "provider": "openai",
            "responsibility": "holistic",
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
            "profile": "reviewer-callback",
            "routing_sha256": "9" * 64,
        }
    ],
}
expected_simple_sha256 = hashlib.sha256(
    json.dumps(
        expected_simple_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
check(
    "effective topology serializes one exact canonical value",
    simple_effective.payload() == expected_simple_payload
    and simple_effective.sha256 == expected_simple_sha256,
)
changed_effort = compile_effective_review_topology(
    mode="simple",
    cross_model=False,
    max_verify_iterations=1,
    verification_profile="scoped",
    verification_profile_sha256="8" * 64,
    routes={
        "openai": {
            **expected_simple_payload["lanes"][0],
            "effort": "high",
        }
    },
    selected_provider="openai",
)
check(
    "any concrete route mutation changes the effective topology digest",
    changed_effort.sha256 != simple_effective.sha256,
)
caller_routed = compile_effective_review_topology(
    mode="simple",
    cross_model=False,
    max_verify_iterations=1,
    verification_profile="scoped",
    verification_profile_sha256="8" * 64,
    routes={
        "openai": {
            **expected_simple_payload["lanes"][0],
            "runtime": "claude",
            "model": "fable",
        }
    },
    selected_provider="openai",
)
check(
    "caller-supplied logical route is digest-bound without runtime inference",
    caller_routed.lanes[0].runtime == "claude"
    and caller_routed.sha256 != simple_effective.sha256,
)
adaptive_routes = {
    "anthropic": {
        "runtime": "claude",
        "model": "claude-opus-5",
        "effort": "xhigh",
        "profile": "reviewer-callback",
        "routing_sha256": "9" * 64,
    },
    "openai": {
        "runtime": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "profile": "reviewer-callback",
        "routing_sha256": "9" * 64,
    },
}
late_simple = compile_effective_review_topology(
    mode="simple",
    cross_model=False,
    max_verify_iterations=1,
    verification_profile="scoped",
    verification_profile_sha256="8" * 64,
    routes=adaptive_routes,
)
check(
    "two adaptive routes promote Simple to the existing holistic Deep topology",
    late_simple.payload()["requested_mode"] == "simple"
    and late_simple.mode == "deep"
    and tuple(lane.axis for lane in late_simple.lanes)
    == ("anthropic-holistic", "openai-holistic"),
)
check(
    "responsibility is derived from the exact lane identity",
    review_axis_responsibility("anthropic-holistic") == "holistic"
    and review_axis_responsibility("openai-intent") == "intent"
    and review_axis_responsibility("anthropic-engineering") == "engineering",
)
check(
    "provider identity is independent from mutable model aliases",
    review_provider_runtime("anthropic") == "claude"
    and review_provider_runtime("openai") == "codex"
    and review_runtime_provider("claude") == "anthropic"
    and review_runtime_provider("codex") == "openai"
    and all(
        'explicit_model="fable"' not in (ROOT / path).read_text(encoding="utf-8")
        and 'explicit_model="sol"' not in (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "scripts/task_review_request.py",
            "scripts/review-runner.py",
            "scripts/dispatch_setup.py",
        )
    ),
)
try:
    review_axis_provider("fable-holistic")
except ValueError:
    check("unreleased model-family lane identities have no compatibility path", True)
else:
    check("unreleased model-family lane identities have no compatibility path", False)

try:
    compile_review_axes("full", selected_provider="openai")
except ValueError as exc:
    check(
        "single-model full fails fast with a deep recommendation",
        "Deep" in str(exc),
    )
else:
    check("single-model full fails fast with a deep recommendation", False)

simple = ReviewPreset.from_flags().request(
    "simple-topology", selected_provider="openai"
)
deep_default = ReviewPreset.from_flags(deep=True).request("deep-topology")
deep_single = ReviewPreset.from_flags(deep=True, model="opus").request(
    "deep-single-topology", selected_provider="anthropic"
)
full = ReviewPreset.from_flags(full=True).request("full-topology")
check(
    "simple request binds its exact lane",
    simple.axes == ("openai-holistic",),
)
check(
    "default deep request binds both holistic lanes",
    deep_default.axes == ("anthropic-holistic", "openai-holistic"),
)
check(
    "single-model deep request binds both specialist lanes",
    deep_single.axes == ("anthropic-intent", "anthropic-engineering"),
)
check(
    "full request binds all four specialist lanes",
    full.axes
    == (
        "anthropic-intent",
        "anthropic-engineering",
        "openai-intent",
        "openai-engineering",
    ),
)
# The lane-barrier and deferred-resolution helpers were retired with the
# production-dead half of _complete_ready_results (rc4-complete-ready-results-
# dead-half): the exact-HEAD attempt is the only completion path and it never
# defers.  Lane cardinality itself is still pinned by the compile_review_axes
# checks above.

for label, flags in (
    ("deep and full are mutually exclusive", {"deep": True, "full": True}),
    (
        "full runtime override is rejected before routing",
        {"full": True, "runtime": "codex"},
    ),
    (
        "full model override is rejected before routing",
        {"full": True, "model": "sol"},
    ),
):
    try:
        ReviewPreset.from_flags(**flags)
    except ValueError as exc:
        check(label, "Deep" in str(exc) if "override" in label else True)
    else:
        check(label, False)

profile = load_profiles(ROOT / "config/verification-profiles.toml")["scoped"]
context = ReviewContext(
    "packets/review/manifest.json",
    "a" * 40,
    profile.name,
    profile.sha256,
)


def routed(mode: str, **overrides: object):
    policy = {
        "mode": mode,
        "cross_model": False,
        "runtime": "",
        "model": "",
        "effort": "",
        "max_verify_iterations": VERIFY_BUDGETS[mode],
        "verification_profile": profile.name,
        "verification_profile_sha256": profile.sha256,
    }
    policy.update(overrides)
    return _request(
        {
            "review_policy": policy,
            "routing": {
                "session": {
                    "runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "source": "host-confirmed-test",
                }
            },
        },
        ROOT,
        f"route-{mode}",
        context,
    )[1]


simple_request = routed("simple")
check(
    "simple route and identity stay on the selected model",
    simple_request is not None
    and simple_request.policy.axes == ("openai-holistic",)
    and [simple_request.route_for(axis).runtime for axis in simple_request.policy.axes]
    == ["codex"],
)
deep_request = routed("deep")
check(
    "default deep routes one holistic lane to each provider",
    deep_request is not None
    and deep_request.policy.axes
    == ("anthropic-holistic", "openai-holistic")
    and [deep_request.route_for(axis).runtime for axis in deep_request.policy.axes]
    == ["claude", "codex"],
)
single_request = routed("deep", model="opus")
check(
    "single-model deep has zero route to the other provider",
    single_request is not None
    and single_request.policy.axes
    == ("anthropic-intent", "anthropic-engineering")
    and {single_request.route_for(axis).runtime for axis in single_request.policy.axes}
    == {"claude"},
)
full_request = routed("full")
check(
    "full routes the exact four-lane provider grid",
    full_request is not None
    and full_request.policy.axes
    == (
        "anthropic-intent",
        "anthropic-engineering",
        "openai-intent",
        "openai-engineering",
    )
    and [full_request.route_for(axis).runtime for axis in full_request.policy.axes]
    == ["claude", "claude", "codex", "codex"],
)
check(
    "every runtime request carries the canonical effective topology digest",
    all(
        len(request.topology_sha256) == 64
        and request.topology.sha256 == request.topology_sha256
        for request in (
            simple_request,
            deep_request,
            single_request,
            full_request,
        )
    ),
)
simple_policy = {
    "mode": "simple",
    "cross_model": False,
    "runtime": "",
    "model": "",
    "effort": "",
    "max_verify_iterations": 1,
    "verification_profile": profile.name,
    "verification_profile_sha256": profile.sha256,
}
frozen_meta = {
    "review_policy": simple_policy,
    "review_topology": {
        "payload": simple_request.topology.payload(),
        "sha256": simple_request.topology_sha256,
    },
    "routing": {
        "session": {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "source": "host-confirmed-test",
        }
    },
}
frozen_runtime = frozen_task_request(
    frozen_meta, ROOT, "frozen-simple", context
)[1]
check(
    "runtime consumes the topology digest frozen at dispatch",
    frozen_runtime is not None
    and frozen_runtime.topology_sha256 == simple_request.topology_sha256
    and frozen_runtime.topology.payload()
    == frozen_meta["review_topology"]["payload"],
)


def finalization_bound_frozen_request(
    label: str,
    policy: dict[str, object],
) -> object:
    config = load_config(ROOT)
    decision = compile_finalization_routes(
        config=config,
        policy=FinalizationPolicy(),
        cycle_number=1,
        independent_permitted=True,
        availability=None,
        explicit_runtime=str(policy["runtime"]),
        explicit_model=str(policy["model"]),
        explicit_effort=str(policy["effort"]),
        required_mode=str(policy["mode"]),
        now_epoch=0,
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
    frozen = compile_effective_review_topology(
        mode=str(policy["mode"]),
        cross_model=bool(policy["cross_model"]),
        max_verify_iterations=int(policy["max_verify_iterations"]),
        verification_profile=str(policy["verification_profile"]),
        verification_profile_sha256=str(
            policy["verification_profile_sha256"]
        ),
        routes=routes,
    )
    meta = {
        "review_policy": policy,
        "review_topology": {
            "payload": frozen.payload(),
            "sha256": frozen.sha256,
        },
        "routing": frozen_meta["routing"],
    }
    request = frozen_task_request(meta, ROOT, label, context)[1]
    assert request is not None
    bound = _bind_routes(
        request,
        attempt_id=f"{label}-attempt",
        routes=decision,
        routing_sha256=config.fingerprint,
    )
    _assert_frozen_topology(meta, bound)
    return bound


deep_finalization = finalization_bound_frozen_request(
    "frozen-default-deep",
    {**simple_policy, "mode": "deep"},
)
cross_simple_finalization = finalization_bound_frozen_request(
    "frozen-cross-simple",
    {**simple_policy, "cross_model": True},
)
check(
    "generated default Deep metadata equals finalization-bound runtime topology",
    deep_finalization.policy.axes
    == ("anthropic-intent", "anthropic-engineering"),
)
check(
    "generated cross-model Simple metadata equals finalization-bound runtime topology",
    cross_simple_finalization.policy.axes == ("anthropic-holistic",),
)
drifted_meta = json.loads(json.dumps(frozen_meta))
drifted_meta["review_topology"]["sha256"] = "0" * 64
try:
    frozen_task_request(drifted_meta, ROOT, "frozen-drift", context)
except ValueError as exc:
    check(
        "runtime rejects temporal topology drift before provider effects",
        "topology" in str(exc),
    )
else:
    check("runtime rejects temporal topology drift before provider effects", False)
drifted_routes = dict(full_request.axis_routes or {})
drifted_routes["openai-engineering"] = replace(
    drifted_routes["openai-engineering"], effort="high"
)
try:
    replace(
        full_request,
        axis_routes=drifted_routes,
        topology_sha256=full_request.topology_sha256,
    )
except ValueError as exc:
    check(
        "non-primary route drift rejects the request before provider effect",
        "topology" in str(exc),
    )
else:
    check(
        "non-primary route drift rejects the request before provider effect",
        False,
    )
for label, request, expected_kinds in (
    (
        "default deep compiles two holistic parent sessions",
        deep_request,
        ("simple-review-holistic", "simple-review-holistic"),
    ),
    (
        "single-model deep compiles intent and engineering parents",
        single_request,
        ("deep-review-spec", "deep-review-correctness"),
    ),
    (
        "full compiles four specialist parent sessions",
        full_request,
        (
            "deep-review-spec",
            "deep-review-correctness",
            "deep-review-spec",
            "deep-review-correctness",
        ),
    ),
):
    identities = review_session_specs(request)
    check(
        label,
        tuple(item.spec.kind for item in identities) == expected_kinds
        and len({item.spec.operation_id for item in identities})
        == len(expected_kinds)
        and len({item.lane_id for item in identities}) == len(expected_kinds),
    )

prompt_context = ReviewContext(
    "packets/review/manifest.json",
    "b" * 40,
    profile.name,
    profile.sha256,
    implementer_summary_sha256="c" * 64,
)
prompt_context_without_summary = ReviewContext(
    "packets/review/manifest.json",
    "d" * 40,
    profile.name,
    profile.sha256,
)
with tempfile.TemporaryDirectory(prefix="review-topology-prompts.") as raw:
    runtime_root = Path(raw)
    prompts = {}
    for axis in (
        "anthropic-holistic",
        "openai-holistic",
        "anthropic-intent",
        "anthropic-engineering",
        "openai-intent",
        "openai-engineering",
    ):
        pointer = _prompt(
            vault=ROOT,
            worktree=ROOT,
            runtime_root=runtime_root,
            context=prompt_context,
            axis=axis,
            verification=False,
        )
        prompts[axis] = (runtime_root / pointer).read_text(encoding="utf-8")

    engineering_pointer = "docs/skill-references/engineering-quality-contract.md"
    for axis in ("anthropic-holistic", "openai-holistic"):
        check(
            f"{axis} independently owns outcome and engineering review",
            "Classify every declared success-evidence item" in prompts[axis]
            and engineering_pointer in prompts[axis]
            and "Repository-specific standards override" in prompts[axis],
        )
    for axis in ("anthropic-intent", "openai-intent"):
        check(
            f"{axis} is intent-only",
            "Classify every declared success-evidence item" in prompts[axis]
            and engineering_pointer not in prompts[axis]
            and "correctness, failure behavior" not in prompts[axis],
        )
    for axis in ("anthropic-engineering", "openai-engineering"):
        check(
            f"{axis} is engineering-only",
            "Classify every declared success-evidence item" not in prompts[axis]
            and engineering_pointer in prompts[axis]
            and "correctness, failure behavior" in prompts[axis]
            and "Repository-specific standards override" in prompts[axis],
        )
    no_summary_pointer = _prompt(
        vault=ROOT,
        worktree=ROOT,
        runtime_root=runtime_root,
        context=prompt_context_without_summary,
        axis="openai-holistic",
        verification=False,
    )
    no_summary_prompt = (runtime_root / no_summary_pointer).read_text(
        encoding="utf-8"
    )
    check(
        "outcome review remains mandatory without an implementer summary",
        "Classify every declared success-evidence item" in no_summary_prompt
        and "Check every declared non-goal for scope creep" in no_summary_prompt,
    )

binding = ReviewWorkspaceBinding(
    review_operation_id="review-program",
    workspace_id="11111111-1111-4111-8111-111111111111",
    workspace_ref="workspace:1",
    window_id="22222222-2222-4222-8222-222222222222",
    window_ref="window:2",
    anchor_surface_id="33333333-3333-4333-8333-333333333333",
    anchor_surface_ref="surface:3",
)
check(
    "review topology persists one exact program workspace binding",
    ReviewWorkspaceBinding.from_payload(binding.payload()) == binding,
)
