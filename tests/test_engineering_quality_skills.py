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
            "prototype",
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
