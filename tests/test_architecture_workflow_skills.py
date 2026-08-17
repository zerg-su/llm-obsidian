#!/usr/bin/env python3
"""Semantic contract checks for Architecture Workflow policy artifacts."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "skill-references" / "architecture-artifacts.md"
ARCHITECTURE = ROOT / "skills" / "architecture" / "SKILL.md"
ARCHITECTURE_AGENT = ROOT / "skills" / "architecture" / "agents" / "openai.yaml"
DECOMPOSE = ROOT / "skills" / "decompose" / "SKILL.md"
DECOMPOSE_AGENT = ROOT / "skills" / "decompose" / "agents" / "openai.yaml"
IMPLEMENTATION_PLAN = ROOT / "skills" / "implementation-plan" / "SKILL.md"


class Suite:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, detail: object = "") -> None:
        if condition:
            print(f"OK   {label}")
        else:
            print(f"FAIL {label}: {detail}")
            self.failures.append(label)


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match else ""


def contains_all(text: str, terms: tuple[str, ...]) -> bool:
    folded = re.sub(r"\s+", " ", text.casefold())
    return all(re.sub(r"\s+", " ", term.casefold()) in folded for term in terms)


def missing_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if not contains_all(text, (term,))]


def main() -> int:
    suite = Suite()
    suite.check("authoritative artifact reference exists", REFERENCE.is_file())
    if not REFERENCE.is_file():
        return 1

    text = REFERENCE.read_text(encoding="utf-8")
    expected_sections = {
        "Authority and frontiers": (
            "Design Frontier",
            "Planning Frontier",
            "Execution Frontier",
            "may discover upstream problems",
            "may not silently resolve",
        ),
        "Physical mapping and roles": (
            "wiki/projects/<project>/",
            "artifact_role",
            "hub",
            "vision",
            "architecture",
            "design",
            "spec",
            "contract",
            "work-graph",
            "work-item",
            "wiki/decisions/",
            "wiki/plans/",
        ),
        "Identity and path safety": (
            "globally unique title",
            "DragonScale",
            "no artifact_id",
            "[a-z0-9-]",
            "NFC",
            "casefold",
            "aliases",
            "scripts/architecture_paths.py",
            "before ACCEPT",
            "before MATERIALIZE",
        ),
        "Revisions, pins, and freshness": (
            "artifact_revision",
            "upstream_pins",
            "total mapping",
            "missing",
            "orphan",
            "duplicate",
            "malformed",
            "superseded",
            "current",
            "needs-review",
            "stale",
            "report-only",
        ),
        "Lifecycle, Fog, and scope": (
            "draft | review | accepted | superseded",
            "active | accepted",
            "Open Questions / Fog",
            "Explicitly Out of Scope",
        ),
        "Persistence and recovery": (
            "conversational",
            "never ExitPlanMode",
            "separate explicit",
            "vault-write.py",
            "expected_sha256",
            "one bounded transaction",
            "roll-forward",
            "recovery journal",
            "address",
        ),
        "Work Graph and Work Item": (
            "MAP",
            "ACCEPT",
            "MATERIALIZE",
            "depends_on",
            "same-project",
            "self",
            "duplicate",
            "acyclic",
            "topological",
            "blocks",
            "parallel-safe-with",
            "file paths",
        ),
        "Durable consumption": (
            "conversation-only",
            "durable",
            "architecture",
            "decompose",
            "implementation-plan",
            "Upstream Gap",
            "partial",
            "inconsistent",
        ),
        "Conceptual lineage": (
            "arc42",
            "C4",
            "MADR",
            "Rust RFC",
            "Kubernetes KEP",
            "OpenSpec",
            "Spec Kit",
            "concepts only",
        ),
    }
    for heading, terms in expected_sections.items():
        body = section(text, heading)
        suite.check(f"reference section: {heading}", bool(body), "missing heading")
        suite.check(
            f"reference semantics: {heading}",
            contains_all(body, terms),
            missing_terms(body, terms),
        )

    suite.check("architecture carrier exists", ARCHITECTURE.is_file())
    suite.check("architecture OpenAI interface exists", ARCHITECTURE_AGENT.is_file())
    if ARCHITECTURE.is_file():
        architecture = ARCHITECTURE.read_text(encoding="utf-8")
        frontmatter = architecture.split("---", 2)[1]
        allowed_match = re.search(r"^allowed-tools:\s*(.+)$", frontmatter, re.M)
        allowed_tools = set(allowed_match.group(1).split()) if allowed_match else set()
        suite.check(
            "architecture can invoke its declared handoff carriers",
            "Skill" in allowed_tools,
            sorted(allowed_tools),
        )
        carriers = ("clarify", "design", "research", "prototype", "codebase-design", "review")
        suite.check(
            "architecture handoff carriers resolve to repository skills",
            all((ROOT / "skills" / carrier / "SKILL.md").is_file() for carrier in carriers),
            carriers,
        )
        suite.check(
            "architecture declares invocation and returned-artifact protocol",
            contains_all(
                architecture,
                (
                    "Make exactly one explicit handoff",
                    "Give the invoked carrier",
                    "expected return artifact",
                    "Collect its result",
                ),
            ),
        )
        description_match = re.search(r"^description:\s*(.+)$", architecture, re.M)
        description = description_match.group(1).strip(' "') if description_match else ""
        suite.check(
            "architecture description stays within frozen budget",
            0 < len(description.encode("utf-8")) <= 160,
            len(description.encode("utf-8")),
        )
        architecture_terms = (
            "read [the architecture artifact contract]",
            "completely",
            "explicit project",
            "exact contextual match",
            "clarify",
            "never guess by recency",
            "artifact graph",
            "upstream_pins",
            "Open Questions / Fog",
            "Explicitly Out of Scope",
            "freshness",
            "recovery journal",
            "highest-value bounded",
            "Design Frontier",
            "design",
            "research",
            "prototype",
            "codebase-design",
            "review",
            "conversational semantic acceptance",
            "separate explicit persistence authorization",
            "scripts/architecture_paths.py",
            "scripts/vault-write.py",
            "expected_sha256",
            "address",
            "must not perform work decomposition",
            "must not perform implementation planning",
            "must not decide alternatives",
        )
        suite.check(
            "architecture carrier preserves its complete authority boundary",
            contains_all(architecture, architecture_terms),
            missing_terms(architecture, architecture_terms),
        )
    if ARCHITECTURE_AGENT.is_file():
        agent = ARCHITECTURE_AGENT.read_text(encoding="utf-8")
        suite.check(
            "architecture OpenAI interface selects the architecture skill",
            contains_all(agent, ("display_name", "Architecture", "$architecture")),
        )

    suite.check("decompose carrier exists", DECOMPOSE.is_file())
    suite.check("decompose OpenAI interface exists", DECOMPOSE_AGENT.is_file())
    if DECOMPOSE.is_file():
        decompose = DECOMPOSE.read_text(encoding="utf-8")
        description_match = re.search(r"^description:\s*(.+)$", decompose, re.M)
        description = description_match.group(1).strip(' "') if description_match else ""
        suite.check(
            "decompose description stays within frozen budget",
            0 < len(description.encode("utf-8")) <= 150,
            len(description.encode("utf-8")),
        )
        decompose_terms = (
            "read [the architecture artifact contract]",
            "completely",
            "MAP",
            "ACCEPT",
            "MATERIALIZE",
            "durable accepted",
            "accepted in-context",
            "conversation-only",
            "Work Graph",
            "Title",
            "Outcome",
            "Why",
            "Source artifacts/upstream",
            "Inputs",
            "Produces",
            "depends_on",
            "Concurrency Constraints",
            "Acceptance/Evidence",
            "file paths",
            "function/class names",
            "edit order",
            "TDD steps",
            "covered",
            "deferred",
            "Explicitly Out of Scope",
            "orphan",
            "total",
            "upstream_pins",
            "sole authoritative relation",
            "exact same-project",
            "dangling",
            "self",
            "duplicate",
            "acyclic",
            "blocks",
            "waves",
            "Planning Frontier",
            "parallel-safe-with",
            "scripts/architecture_paths.py",
            "before ACCEPT",
            "before MATERIALIZE",
            "accepted upstream architecture does not advance decomposition",
            "existing concrete MAP draft",
            "explicitly accepts that decomposition",
            "conversational semantic acceptance",
            "zero writes",
            "zero address",
            "never ExitPlanMode",
            "separate explicit write authorization",
            "one bounded",
            "scripts/vault-write.py",
            "expected_sha256",
            "recovery journal",
            "projection",
            "Upstream Gap",
            "architecture",
            "decompose is not split",
            "must not perform implementation planning",
        )
        suite.check(
            "decompose carrier preserves MAP-ACCEPT-MATERIALIZE and DAG authority",
            contains_all(decompose, decompose_terms),
            missing_terms(decompose, decompose_terms),
        )
    if DECOMPOSE_AGENT.is_file():
        agent = DECOMPOSE_AGENT.read_text(encoding="utf-8")
        suite.check(
            "decompose OpenAI interface selects the decompose skill",
            contains_all(agent, ("display_name", "Decompose", "$decompose")),
        )

    implementation_plan = IMPLEMENTATION_PLAN.read_text(encoding="utf-8")
    legacy_terms = (
        "Consume the approved Outcome Contract and design",
        "unresolved architecture returns to `design` or `codebase-design`",
        "Write vertical, independently reviewable slices",
        "files/responsibility",
        "failing evidence",
        "minimal green",
        "focused verification",
        "Obtain approval before code",
    )
    suite.check(
        "implementation-plan legacy Outcome Contract path stays intact",
        contains_all(implementation_plan, legacy_terms),
        missing_terms(implementation_plan, legacy_terms),
    )
    guard_terms = (
        "ONE bounded delivery outcome",
        "one accepted Work Item",
        "approved Outcome Contract + design",
        "multiple independent outcomes",
        "return to `decompose`",
        "read [the architecture artifact contract]",
        "completely",
        "durable Work Item",
        "accepted",
        "project artifacts",
        "decisions",
        "active or accepted",
        "superseded",
        "total well-formed pin mapping",
        "current",
        "needs-review",
        "stale",
        "recovery journal",
        "partial",
        "inconsistent",
        "before any file/TDD planning",
        "Upstream Gap",
        "source artifact/decision",
        "why downstream work cannot proceed",
        "affected downstream artifacts/work",
        "required owner/action",
        "never resolves the gap",
        "remains the response carrier",
        "does not transfer the user's planning request",
        "address the gap to",
        "do not re-route the whole planning request",
    )
    suite.check(
        "implementation-plan Work Item guard fails closed at upstream authority",
        contains_all(implementation_plan, guard_terms),
        missing_terms(implementation_plan, guard_terms),
    )

    return int(bool(suite.failures))


if __name__ == "__main__":
    raise SystemExit(main())
