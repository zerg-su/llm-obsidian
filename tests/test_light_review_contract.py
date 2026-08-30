#!/usr/bin/env python3
"""Behavior tests for advisory Light Review routing and result validation."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from light_review_contract import (  # noqa: E402
    LightReviewError,
    validate_light_review,
)
from model_routing import (  # noqa: E402
    RoutingError,
    load_config,
    resolve_light_review_route,
)


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


config = load_config(ROOT)
codex = {
    "runtime": "codex",
    "model": "gpt-5.6-sol",
    "effort": "high",
    "source": "host",
}
claude = {
    "runtime": "claude",
    "model": "claude-opus-5",
    "effort": "high",
    "source": "host",
}

inherited = resolve_light_review_route(config, codex)
check(
    "Light Review inherits the exact current route",
    (inherited["runtime"], inherited["model"], inherited["effort"])
    == ("codex", "gpt-5.6-sol", "high"),
)
check("Light Review declares native subagent isolation", inherited["isolation"] == "native-subagent")

terra = resolve_light_review_route(
    config, codex, explicit_model="terra", explicit_effort="xhigh"
)
check(
    "same-runtime model and effort overrides are accepted",
    (terra["runtime"], terra["model"], terra["effort"])
    == ("codex", "gpt-5.6-terra", "xhigh"),
)

fable = resolve_light_review_route(config, claude, explicit_model="fable")
check("Claude Light Review can select Fable", fable["runtime"] == "claude" and fable["model"] == "fable")

try:
    resolve_light_review_route(config, codex, explicit_model="opus")
except RoutingError as exc:
    check("cross-runtime Light override fails visibly", "same runtime" in str(exc))
else:
    check("cross-runtime Light override fails visibly", False)


section_names = (
    "quality",
    "implementation",
    "testing",
    "simplification",
    "documentation",
    "security",
)
snapshot_sha = "a" * 64
payload = {
    "schema_version": 1,
    "kind": "light-review",
    "snapshot_sha256": snapshot_sha,
    "status": "findings-observed",
    "route": {
        "runtime": terra["runtime"],
        "model": terra["model"],
        "effort": terra["effort"],
        "isolation": "native-subagent",
    },
    "scope": {
        "target_key": "b" * 32,
        "head": "c" * 40,
        "base": "d" * 40,
        "paths": [],
        "included_untracked": False,
    },
    "sections": [
        {
            "name": name,
            "status": "findings" if name == "quality" else "clean",
            "finding_ids": ["L-001"] if name == "quality" else [],
        }
        for name in section_names
    ],
    "findings": [
        {
            "id": "L-001",
            "section": "quality",
            "severity": "high",
            "path": "src/app.py",
            "line": 12,
            "summary": "The boundary accepts stale state.",
            "evidence": "The callback does not compare its starting snapshot.",
            "recommendation": "Reject the stale callback before consuming it.",
        }
    ],
    "coverage_gaps": [],
    "verification": [
        {
            "command": "git diff --check",
            "status": "passed",
            "detail": "No whitespace errors.",
        }
    ],
}

validated = validate_light_review(
    payload,
    expected_snapshot_sha256=snapshot_sha,
    expected_route=terra,
)
check("valid Light Review contract is accepted", validated["status"] == "findings-observed")


def rejected(label: str, mutate) -> None:
    candidate = copy.deepcopy(payload)
    mutate(candidate)
    try:
        validate_light_review(
            candidate,
            expected_snapshot_sha256=snapshot_sha,
            expected_route=terra,
        )
    except LightReviewError:
        check(label, True)
    else:
        check(label, False)


rejected("approval vocabulary cannot become a Light status", lambda value: value.__setitem__("status", "approved"))
rejected("all six review sections are mandatory", lambda value: value["sections"].pop())
rejected("snapshot identity mismatch is rejected", lambda value: value.__setitem__("snapshot_sha256", "0" * 64))
rejected("finding status must match finding presence", lambda value: value.__setitem__("status", "no-findings-observed"))
rejected("finding IDs must be section-owned", lambda value: value["sections"][0].__setitem__("finding_ids", []))
rejected("unexpected durable approval fields are rejected", lambda value: value.__setitem__("approved", True))

print("\nAll Light Review contract tests passed.")
