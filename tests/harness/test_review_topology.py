#!/usr/bin/env python3
"""Deterministic contracts for the public review topology compiler."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from review_contract import (  # noqa: E402
    MODES,
    VERIFY_BUDGETS,
    compile_review_axes,
    review_axis_provider,
    review_axis_responsibility,
    review_provider_runtime,
    review_runtime_provider,
)
from harness.workflows.review_gate import ReviewPreset  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import ReviewContext, review_session_specs  # noqa: E402
from task_review_request import _prompt, _request  # noqa: E402
from task_review_flow import (  # noqa: E402
    _requires_lane_barrier,
    _should_defer_ready_results,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


check("full is an explicit review mode", MODES == {"simple", "deep", "full"})
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
check(
    "every multi-lane mode aggregates all callbacks before resolution",
    not _requires_lane_barrier(ReviewPreset.from_flags())
    and _requires_lane_barrier(ReviewPreset.from_flags(deep=True))
    and _requires_lane_barrier(ReviewPreset.from_flags(full=True)),
)
check(
    "an interrupted Full barrier absorbs later callbacks into one resolution",
    _should_defer_ready_results(
        ReviewPreset.from_flags(full=True),
        purpose="implementation",
        has_material=False,
        already_awaiting=True,
    ),
)

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
