#!/usr/bin/env python3
"""Deterministic Outcome Contract schema, block, and digest regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from outcome_contract import (  # noqa: E402
    MAX_CONTRACT_BYTES,
    OutcomeContractError,
    canonical_bytes,
    extract_from_plan,
)


CONTRACT = {
    "schema_version": 1,
    "purpose": "Keep the user goal stable.",
    "desired_outcome": "Ship the bounded transport.",
    "success_evidence": [
        {
            "evidence_id": "digest-stable",
            "observable": "Equivalent JSON yields one digest.",
        },
        {
            "evidence_id": "authority-closed",
            "observable": "Contract fields cannot expand authority.",
        },
    ],
    "non_goals": ["No scheduler.", "No permission expansion."],
}
CANONICAL = (
    b'{"desired_outcome":"Ship the bounded transport.",'
    b'"non_goals":["No scheduler.","No permission expansion."],'
    b'"purpose":"Keep the user goal stable.","schema_version":1,'
    b'"success_evidence":[{"evidence_id":"digest-stable",'
    b'"observable":"Equivalent JSON yields one digest."},'
    b'{"evidence_id":"authority-closed",'
    b'"observable":"Contract fields cannot expand authority."}]}'
)
DIGEST = "69f7064524657040a973edd4309fac3ba7a5b754bbb1b09a323667d41a3c84a0"


def plan(block: str) -> str:
    return f"# Approved plan\n\n## Outcome Contract\n\n```json\n{block}\n```\n"


def expect_error(label: str, text: str, needle: str) -> None:
    try:
        extract_from_plan(text)
    except OutcomeContractError as exc:
        assert needle in str(exc), f"{label}: {exc}"
    else:
        raise AssertionError(f"{label}: expected OutcomeContractError")


compact = json.dumps(CONTRACT, ensure_ascii=False, separators=(",", ":"))
pretty_reordered = json.dumps(
    {
        "non_goals": CONTRACT["non_goals"],
        "success_evidence": CONTRACT["success_evidence"],
        "desired_outcome": CONTRACT["desired_outcome"],
        "schema_version": 1,
        "purpose": CONTRACT["purpose"],
    },
    ensure_ascii=False,
    indent=4,
)

first = extract_from_plan(plan(compact))
second = extract_from_plan(plan(pretty_reordered))
assert first.value == CONTRACT
assert first.canonical == CANONICAL == canonical_bytes(CONTRACT)
assert first.sha256 == DIGEST == second.sha256
assert first.evidence_ids == ("digest-stable", "authority-closed")
print("OK   canonical serialization and digest are stable")

expect_error("missing block", "# Plan\n\nNo contract.\n", "exactly one")
expect_error(
    "duplicate block",
    plan(compact) + "\n" + plan(compact),
    "exactly one",
)
expect_error(
    "duplicate JSON member",
    plan(compact.replace('"desired_outcome":', '"desired_outcome":"shadow",\n"desired_outcome":', 1)),
    "duplicate JSON member",
)
print("OK   missing, duplicated, and ambiguous blocks fail closed")

oversized = dict(CONTRACT)
oversized["desired_outcome"] = "x" * (MAX_CONTRACT_BYTES + 1)
expect_error(
    "oversized block",
    plan(json.dumps(oversized, separators=(",", ":"))),
    "too large",
)
for forbidden in ("invariants", "stop_conditions", "permissions", "forbidden_actions"):
    expanded = dict(CONTRACT)
    expanded[forbidden] = ["model-owned authority"]
    expect_error(
        f"forbidden authority field {forbidden}",
        plan(json.dumps(expanded, separators=(",", ":"))),
        "unknown fields",
    )
print("OK   bounds and closed fields prevent authority expansion")

duplicate_evidence = dict(CONTRACT)
duplicate_evidence["success_evidence"] = [
    CONTRACT["success_evidence"][0],
    CONTRACT["success_evidence"][0],
]
expect_error(
    "duplicate evidence id",
    plan(json.dumps(duplicate_evidence, separators=(",", ":"))),
    "evidence_id values must be unique",
)
print("OK   evidence identifiers are bounded and unique")
