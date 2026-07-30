#!/usr/bin/env python3
"""Model-free shadow parity against the existing 2.3 lifecycle."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import RuntimeRoute, to_dict
from harness.pipeline_builtins import builtin_definitions, builtin_registry
from harness.pipeline_shadow import shadow_lifecycle
from harness.pipelines import compile_pipeline
from harness.workflows.dispatch import DispatchRequest
from harness.workflows.dispatch import operation_spec as dispatch_spec
from harness.workflows.review import (
    ReviewContext,
    ReviewOperationRequest,
    ReviewRequest,
)
from harness.workflows.review import operation_spec as review_spec


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


executor = RuntimeRoute(
    "codex",
    "gpt-5.6-sol",
    "high",
    "executor",
    "a" * 64,
)
reviewer = RuntimeRoute(
    "claude",
    "claude-opus-5",
    "high",
    "reviewer-readonly",
    "b" * 64,
)
compiled = compile_pipeline(
    builtin_definitions()["lifecycle/default"],
    builtin_registry(),
    capabilities=("provider:authenticated",),
)
dispatch = dispatch_spec(
    DispatchRequest(
        "task-1",
        "owner-1",
        "c" * 64,
        "packets/task/manifest.json",
        executor,
    )
)
review = review_spec(
    ReviewOperationRequest(
        ReviewRequest("review-1"),
        "owner-1",
        reviewer,
        ReviewContext(
            "packets/review/manifest.json",
            "d" * 40,
            "full",
            "e" * 64,
        ),
    )
)

report = shadow_lifecycle(compiled, dispatch=dispatch, review=review)
check(
    "compiled lifecycle matches existing dispatch and simple review seams",
    report.parity
    and report.expected_steps == ("dispatch", "review")
    and report.observed_steps == ("dispatch", "review")
    and report.mismatches == (),
)
serialized = json.dumps(to_dict(report), sort_keys=True)
check(
    "shadow evidence remains content-free",
    "packets/task" not in serialized
    and "packets/review" not in serialized
    and "gpt-5.6-sol" not in serialized
    and "claude-opus-5" not in serialized,
)

wrong = shadow_lifecycle(
    compiled,
    dispatch=dispatch,
    review=dataclasses.replace(review, kind="dispatch"),
)
check(
    "shadow mismatch is visible without changing the production spec",
    not wrong.parity
    and wrong.mismatches == ("review: expected review session, observed dispatch",)
    and review.kind == "simple-review",
)
