#!/usr/bin/env python3
"""Semantic contracts for maintainable-code engineering skills."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def normalized(relative: str) -> str:
    return " ".join(read(relative).split()).casefold()


def require(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


codebase = normalized("skills/codebase-design/SKILL.md")
planning = normalized("skills/implementation-plan/SKILL.md")
save_plan = normalized("skills/save-plan/SKILL.md")
tdd = normalized("skills/tdd/SKILL.md")
debug = normalized("skills/debug/SKILL.md")
review = normalized("skills/review/SKILL.md")
design = normalized("skills/design/SKILL.md")
improve = normalized("skills/improve-skills/SKILL.md")
quality = normalized("docs/skill-references/engineering-quality-contract.md")
test_quality = normalized("skills/tdd/references/test-quality.md")
capabilities = normalized("skills/improve-skills/references/capability-gap-model.md")

require(
    "codebase design binds one responsibility to a small durable interface and test seam",
    re.search(
        r"one coherent responsibility.+small.+interface.+test seam",
        codebase,
    )
    is not None,
)
require(
    "codebase design treats size as a review signal and rejects pass-through splitting",
    all(
        phrase in codebase
        for phrase in ("review signal", "not a universal limit", "pass-through")
    ),
)
require(
    "codebase design models domain concepts only when they own real invariants",
    all(
        phrase in codebase
        for phrase in (
            "shared domain language",
            "identity/lifecycle",
            "value objects",
            "aggregate invariants",
            "domain service",
        )
    ),
)
require(
    "implementation plan binds each slice to files, consumes, produces, red, green, and evidence",
    all(
        phrase in planning
        for phrase in (
            "files/responsibility",
            "consumes",
            "produces",
            "failing evidence",
            "minimal green",
            "outcome evidence",
        )
    ),
)
require(
    "implementation plan self-review rejects uncovered requirements and mixed ownership",
    all(
        phrase in planning
        for phrase in (
            "uncovered requirement",
            "contradictory interface",
            "unrelated responsibilities",
        )
    ),
)
require(
    "plan authoring and review keep post-review evidence outside the task Outcome Contract",
    all(
        all(phrase in skill for phrase in ("reviewer-observable", "post-review coordinator acceptance"))
        for skill in (planning, save_plan, review)
    )
    and all(
        phrase in planning
        for phrase in (
            "before the configured review verdict",
            "outside the canonical outcome contract",
            "review callback",
            "reap",
            "terminal cleanup",
        )
    )
    and "do not invent a second outcome contract" in save_plan
    and all(
        phrase in review
        for phrase in (
            "missing-evidence policy",
            "must not be weakened",
            "circular task contract",
        )
    ),
)
require(
    "design and planning enforce YAGNI against speculative scope",
    "yagni" in planning
    and "unrequired feature" in planning
    and "yagni" in design
    and "smallest design" in design,
)
require(
    "TDD loads the authoritative test-quality reference for tests and mocks",
    "references/test-quality.md" in tdd
    and "whenever writing or changing tests, fakes, or mocks" in tdd,
)
require(
    "TDD proves unknown integrations in isolation before production promotion",
    all(
        phrase in tdd
        for phrase in (
            "unknown adapter/runtime mechanism",
            "use the `prototype` skill",
            "production stays unchanged",
            "observed mechanism",
            "red regression at the real seam",
            "focused integration green before one broad gate",
        )
    ),
)
require(
    "test quality rejects tautological expectations and mock-only evidence",
    all(
        phrase in test_quality
        for phrase in (
            "independent expectation",
            "production algorithm",
            "mutation-sensitive",
            "mock-only",
            "refactor",
        )
    ),
)
require(
    "debug ranks falsifiable hypotheses and changes one variable per probe",
    "rank falsifiable hypotheses" in debug and "one variable per probe" in debug,
)
require(
    "review loads the common standards baseline while preserving repository overrides",
    "engineering-quality-contract.md" in review
    and "repository-specific standards override" in review,
)
require(
    "quality contract covers deep modules, locality, dependencies, errors, and test quality",
    all(
        phrase in quality
        for phrase in (
            "deep module",
            "locality",
            "dependency direction",
            "error handling",
            "shotgun surgery",
            "test quality",
        )
    ),
)
require(
    "review binds the whole six-part engineering denominator without new topology",
    all(
        phrase in review
        for phrase in (
            "engineering-quality-contract.md",
            "whole six-section review denominator",
            "even when a section is clean",
        )
    )
    and "no hidden lane, model call, severity cap" in review
    # The six section names stay single-sourced in the contract, not restated here.
    and "simplification, documentation" not in review,
)
require(
    "quality contract states the six-part review denominator and its explicit checks",
    all(
        phrase in quality
        for phrase in (
            "review denominator",
            "logic and edge cases",
            "error and resource behavior",
            "races and data integrity",
            "implementation completeness and wiring",
            "test branches",
            "integration/concurrency/time/cleanup independence",
            "branch-added overengineering",
            "injection and secret leakage",
        )
    )
    and all(
        f"**{section}**".casefold() in quality
        for section in (
            "Quality",
            "Implementation",
            "Testing",
            "Simplification",
            "Documentation",
            "Security",
        )
    ),
)
require(
    "quality contract keeps documentation staleness inside the denominator",
    "readme" in quality
    and "changelog" in quality
    and "plan documentation" in quality,
)
require(
    "improve-skills has an explicit exhaustive capability-gap audit mode",
    "capability-gap-model.md" in improve
    and "explicit capability-integration audit" in improve
    and "no relevant reference capability remains unclassified" in improve,
)
require(
    "capability matrix supports adopted, equivalent, missing, rejected, and deferred",
    all(
        f"`{verdict}`" in capabilities
        for verdict in ("adopted", "equivalent", "missing", "rejected", "deferred")
    ),
)

print("\nAll engineering-quality skill contracts passed.")
