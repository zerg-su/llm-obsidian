#!/usr/bin/env python3
"""Focused observable instruction contracts for 2.6 workstream A skills."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def skill(name: str) -> str:
    return (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def require(text: str, *clauses: str) -> None:
    normalized = " ".join(text.split())
    missing = [clause for clause in clauses if clause not in normalized]
    assert not missing, f"missing instruction clauses: {missing}"


clarify = skill("clarify")
require(
    clarify,
    "exactly one question",
    "Keep this inspection read-only",
    "terms, invariants, contradictions, edge cases, and ADR candidates",
    "Brainstorm or model the domain only when real ambiguity",
    "desired_outcome",
    "success_evidence",
    "non_goals",
    "optional `purpose`",
    "keep the interview open",
    "exactly one user-grounded Outcome Contract",
    "Do not infer or invent",
)
normalized_clarify = " ".join(clarify.split())
assert normalized_clarify.index("keep the interview open") < normalized_clarify.index(
    "exactly one user-grounded Outcome Contract"
), "Clarify must close material ambiguity before forming the Outcome Contract"
print("OK   clarify closes ambiguity before one user-grounded Outcome Contract")


design = skill("design")
require(
    design,
    "Stay read-only",
    "Start with ownership boundaries and owned test seams",
    "Preserve the incoming Outcome Contract without semantic drift",
    "`desired_outcome`, `success_evidence`, `non_goals`, and optional `purpose`",
    "the problem and non-goals",
    "invariants",
    "alternatives",
    "data/control flow, failure recovery, rollout, and rollback",
    "Unresolved fog",
    "explicitly closed as out of scope",
    "Do not leave placeholders or undefined interfaces",
    "Use vertical slices by default",
    "expand-contract only for a wide migration",
    "trace each declared evidence ID",
)
normalized_design = " ".join(design.split())
assert normalized_design.index(
    "Start with ownership boundaries and owned test seams"
) < normalized_design.index("Produce:"), "Owned seams must shape the design before output"
print("OK   design preserves the contract through owned, testable vertical slices")
