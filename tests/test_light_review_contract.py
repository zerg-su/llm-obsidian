#!/usr/bin/env python3
"""Behavior tests for advisory Light Review routing and result validation."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
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
expected_snapshot = {
    "snapshot_sha256": snapshot_sha,
    "target_key": "b" * 32,
    "head": "c" * 40,
    "base": "d" * 40,
    "paths": [],
    "include_untracked": False,
}
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
    expected_snapshot=expected_snapshot,
    expected_route=terra,
    verification_requested=True,
)
check("valid Light Review contract is accepted", validated["status"] == "findings-observed")


def rejected(label: str, mutate) -> None:
    candidate = copy.deepcopy(payload)
    mutate(candidate)
    try:
        validate_light_review(
            candidate,
            expected_snapshot=expected_snapshot,
            expected_route=terra,
            verification_requested=True,
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
rejected("target scope mismatch is rejected", lambda value: value["scope"].__setitem__("target_key", "e" * 32))
rejected("HEAD scope mismatch is rejected", lambda value: value["scope"].__setitem__("head", "e" * 40))
rejected("base scope mismatch is rejected", lambda value: value["scope"].__setitem__("base", "e" * 40))
rejected("path scope mismatch is rejected", lambda value: value["scope"].__setitem__("paths", ["other.py"]))
rejected("untracked scope mismatch is rejected", lambda value: value["scope"].__setitem__("included_untracked", True))
rejected("requested verification cannot disappear behind a success status", lambda value: value.__setitem__("verification", []))

no_verify = copy.deepcopy(payload)
no_verify["verification"] = []
validated_no_verify = validate_light_review(
    no_verify,
    expected_snapshot=expected_snapshot,
    expected_route=terra,
    verification_requested=False,
)
check("omitted verification accepts exactly no verification claims", validated_no_verify["verification"] == [])

try:
    validate_light_review(
        payload,
        expected_snapshot=expected_snapshot,
        expected_route=terra,
        verification_requested=False,
    )
except LightReviewError:
    check("unrequested verification claims are rejected", True)
else:
    check("unrequested verification claims are rejected", False)

missing_verification = copy.deepcopy(payload)
missing_verification["verification"] = []
missing_verification["status"] = "incomplete"
validated_incomplete = validate_light_review(
    missing_verification,
    expected_snapshot=expected_snapshot,
    expected_route=terra,
    verification_requested=True,
)
check("missing requested verification forces incomplete", validated_incomplete["status"] == "incomplete")

with tempfile.TemporaryDirectory(prefix="light-review-contract.") as raw:
    snapshot_file = Path(raw) / "snapshot.json"
    snapshot_file.write_text(json.dumps(expected_snapshot), encoding="utf-8")
    cli = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/light_review_contract.py"),
            "--snapshot-file",
            str(snapshot_file),
            "--runtime",
            terra["runtime"],
            "--model",
            terra["model"],
            "--effort",
            terra["effort"],
            "--verify",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    check("Light Review CLI binds the canonical snapshot and verify request", cli.returncode == 0)

print("\nAll Light Review contract tests passed.")
