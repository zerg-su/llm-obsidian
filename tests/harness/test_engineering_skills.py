#!/usr/bin/env python3
"""Engineering-skill governance and operation-contract tests."""

from __future__ import annotations

import sys
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.workflows.conflict import ConflictRequest
from harness.workflows.conflict import resolve_conflict
from harness.workflows.prototype import (
    PrototypeRequest,
    capture_decision,
    cleanup_prototype,
    run_prototype,
)
from harness.git_ops import ConflictEvidence, GitAdapter, GitError, GitSnapshot
from harness.workflows.review import ReviewContext
from task_review_request import _prompt


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


names = (
    "debug",
    "tdd",
    "design",
    "codebase-design",
    "implementation-plan",
    "prototype",
    "resolve-conflict",
    "review",
    "research",
)
descriptions: set[str] = set()
skill_text: dict[str, str] = {}
for name in names:
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    skill_text[name] = text
    check(f"{name} has no scaffold residue", "TODO" not in text)
    check(f"{name} stays within skill budget", len(text.encode()) <= 5_000)
    description = next(line for line in text.splitlines() if line.startswith("description:"))
    check(f"{name} has a distinct trigger", description not in descriptions and "Use " in description)
    descriptions.add(description)
    check(f"{name} names an authorization boundary", any(token in text.lower() for token in ("authoriz", "approval", "read-only", "coordinator")))

check(
    "debug diagnosis-only mode stops before edits",
    "diagnosis-only" in skill_text["debug"]
    and "stop without product edits" in skill_text["debug"],
)
check(
    "debug removes temporary instrumentation and reruns the original loop",
    "Remove temporary instrumentation" in skill_text["debug"]
    and "original feedback loops" in skill_text["debug"],
)
check(
    "TDD defines a vertical red-green-original-loop workflow",
    "vertical slice" in skill_text["tdd"]
    and "test that fails" in skill_text["tdd"]
    and "makes it pass" in skill_text["tdd"]
    and "affected integration checks" in skill_text["tdd"],
)
tdd_normalized = " ".join(skill_text["tdd"].split())
check(
    "TDD names bounded non-executable exemptions",
    all(
        token in tdd_normalized
        for token in (
            "pure documentation",
            "deterministic generated output",
            "disposable prototypes",
            "mechanical moves",
        )
    ),
)
completion_markers = {
    "debug": "report residual uncertainty",
    "tdd": "Commit a runnable slice",
    "design": "testable acceptance criteria",
    "codebase-design": "approved module map",
    "implementation-plan": "every requirement/evidence item maps",
    "prototype": "decision is durably captured",
    "resolve-conflict": "Report the proposed exact stage list",
    "review": "archive",
    "research": "cited",
}
for name, marker in completion_markers.items():
    check(
        f"{name} has a verifiable completion marker",
        marker.casefold() in skill_text[name].casefold(),
    )
    check(
        f"{name} keeps one authoritative SKILL source",
        len(list((ROOT / "skills" / name).glob("SKILL.md"))) == 1,
    )
audit_test = (ROOT / "tests" / "test_improve_skills.py").read_text(
    encoding="utf-8"
)
router_test = (ROOT / "tests" / "test_skill_router.sh").read_text(
    encoding="utf-8"
)
check(
    "skill governance is covered by the repository-wide structural audit",
    'audit_directory(ROOT / "skills")' in audit_test,
)
check(
    "changed engineering skills keep positive and false-positive router coverage",
    all(
        marker in router_test
        for marker in (
            "debug-EN",
            "tdd-EN",
            "design-EN",
            "codebase-design-EN",
            "implementation-plan-EN",
            "prototype-EN",
            "conflict-EN",
            "fp-debug-symbols",
            "fp-design-color",
            "fp-tdd-definition",
            "fp-prototype-car",
        )
    ),
)

prototype = PrototypeRequest("p-1", "Does it parse?", "One fixture succeeds", ("python3", "probe.py"), "/tmp/owned-prototype")
check("prototype has one bounded run command", prototype.run_command == ("python3", "probe.py"))
with tempfile.TemporaryDirectory(prefix="prototype-evidence.") as raw:
    prototype_root = Path(raw)
    request = PrototypeRequest(
        "p-2",
        "Does it print?",
        "stdout is yes",
        ("python3", "-c", "print('yes')"),
        str(prototype_root),
    )
    evidence = run_prototype(request, max_output_bytes=64)
    check(
        "prototype runs exactly one bounded command in its disposable worktree",
        evidence.exit_code == 0 and evidence.stdout == "yes\n",
    )
    decision_path = prototype_root / "decision.json"
    capture_decision(request, evidence, "adopt", "criterion met", decision_path)
    check(
        "prototype decision is durable before cleanup",
        decision_path.is_file()
        and '"decision":"adopt"' in decision_path.read_text(),
    )
    try:
        cleanup_prototype(request, None, decision_path=decision_path)  # type: ignore[arg-type]
    except ValueError:
        check("prototype cleanup cannot erase its own decision", True)
    else:
        check("prototype cleanup cannot erase its own decision", False)
conflict = ConflictRequest("/tmp/owned-conflict", "rebase", ("src/main.py",))
check("conflict defaults to no stage/continue/abort authority", not any((conflict.stage_authorized, conflict.continue_authorized, conflict.abort_authorized)))
try:
    ConflictRequest("/tmp/owned-conflict", "merge", ("src/main.py",), continue_authorized=True, abort_authorized=True)
except ValueError:
    check("conflict cannot authorize continue and abort", True)
else:
    check("conflict cannot authorize continue and abort", False)


class ConflictGit:
    root = Path("/tmp/owned-conflict")

    def __init__(self) -> None:
        self.staged: tuple[str, ...] = ()
        self.continued = ""

    def inspect(self, _base: str = "HEAD") -> GitSnapshot:
        return GitSnapshot(
            self.root,
            "a" * 40,
            "b" * 40,
            ("src/main.py",),
            ("src/main.py",),
            operation="rebase",
        )

    def stage_exact(self, paths: tuple[str, ...], *, authorized: bool = False) -> None:
        if not authorized:
            raise GitError("authorization required")
        self.staged = paths

    def continue_operation(self, operation: str, *, authorized: bool = False) -> None:
        if not authorized:
            raise GitError("authorization required")
        self.continued = operation

    def conflict_evidence(self) -> ConflictEvidence:
        return ConflictEvidence(
            "rebase",
            ("src/main.py",),
            "b" * 40,
            "a" * 40,
            "c" * 40,
        )


conflict_git = ConflictGit()
proposal = resolve_conflict(conflict, conflict_git, ("src/main.py",), verification_passed=True)
check(
    "conflict workflow gathers evidence but does not mutate before authorization",
    proposal.proposed_stage == ("src/main.py",)
    and not proposal.staged
    and not conflict_git.staged,
)
check(
    "conflict workflow binds BASE ours and theirs evidence",
    proposal.base == "b" * 40
    and proposal.ours == "a" * 40
    and proposal.theirs == "c" * 40,
)
authorized = ConflictRequest(
    "/tmp/owned-conflict",
    "rebase",
    ("src/main.py",),
    stage_authorized=True,
    continue_authorized=True,
)
applied = resolve_conflict(
    authorized,
    conflict_git,
    ("src/main.py",),
    verification_passed=True,
    marker_probe=lambda _path: False,
)
check(
    "conflict workflow stages exact authorized paths and continues exact operation",
    applied.staged
    and applied.continued
    and conflict_git.staged == ("src/main.py",)
    and conflict_git.continued == "rebase",
)

DENOMINATOR_SECTIONS = (
    "Quality",
    "Implementation",
    "Testing",
    "Simplification",
    "Documentation",
    "Security",
)
DENOMINATOR_CHECKS = (
    "logic and edge cases",
    "error and resource behavior",
    "races and data integrity",
    "implementation completeness and wiring",
    "test branches",
    "integration/concurrency/time/cleanup independence",
    "branch-added overengineering",
    "injection and secret leakage",
)
contract_path = ROOT / "docs/skill-references/engineering-quality-contract.md"
contract_text = contract_path.read_text(encoding="utf-8")
contract_flat = " ".join(contract_text.split())
check(
    "engineering contract owns exactly one authoritative review denominator",
    contract_text.count("## Review denominator") == 1,
)
check(
    "review denominator names each of the six sections exactly once",
    all(contract_text.count(f"**{section}**") == 1 for section in DENOMINATOR_SECTIONS),
)
for phrase in DENOMINATOR_CHECKS:
    check(
        f"review denominator explicitly checks {phrase}",
        phrase in contract_flat,
    )
check(
    "review denominator stays a coverage floor and adds no topology",
    "not a severity cap" in contract_flat
    and "adds no lane, model call, or loop" in contract_flat,
)

DENOMINATOR_INSTRUCTION = (
    "Cover its Review denominator in full and report each section explicitly, "
    "including when it is clean: Quality, Implementation, Testing, "
    "Simplification, Documentation, and Security."
)
prompt_context = ReviewContext(
    "packets/review/manifest.json",
    "b" * 40,
    "scoped",
    "c" * 64,
    implementer_summary_sha256="d" * 64,
)
with tempfile.TemporaryDirectory(prefix="review-denominator-prompts.") as raw:
    prompt_root = Path(raw)
    prompts: dict[tuple[str, bool], str] = {}
    for axis in ("anthropic-holistic", "openai-engineering", "anthropic-intent"):
        for verification in (False, True):
            pointer = _prompt(
                vault=ROOT,
                worktree=ROOT,
                runtime_root=prompt_root,
                context=prompt_context,
                axis=axis,
                verification=verification,
            )
            prompts[(axis, verification)] = (prompt_root / pointer).read_text(
                encoding="utf-8"
            )

for axis in ("anthropic-holistic", "openai-engineering"):
    for verification in (False, True):
        prompt = prompts[(axis, verification)]
        flat = " ".join(prompt.split())
        check(
            f"{axis} verification={verification} binds the authoritative rubric file",
            str(contract_path) in prompt,
        )
        check(
            f"{axis} verification={verification} binds the whole six-part denominator",
            DENOMINATOR_INSTRUCTION in flat,
        )
        check(
            f"{axis} verification={verification} keeps repository overrides authoritative",
            "Repository-specific standards override" in prompt,
        )

for verification in (False, True):
    intent_prompt = prompts[("anthropic-intent", verification)]
    check(
        f"intent verification={verification} stays outcome-only without the denominator",
        "Review denominator" not in intent_prompt
        and str(contract_path) not in intent_prompt
        and "Classify every declared success-evidence item" in intent_prompt,
    )

def prompt_sections(text: str) -> tuple[str, ...]:
    """Sections a review prompt actually demands, in prompt order."""
    listed = " ".join(text.split()).split("including when it is clean: ", 1)
    if len(listed) != 2:
        return ()
    return tuple(
        section.strip()
        for section in listed[1]
        .split(".", 1)[0]
        .replace(" and ", " ")
        .split(", ")
    )


def bound_to_rubric(text: str) -> bool:
    """The prompt demands exactly the sections the rubric defines."""
    sections = prompt_sections(text)
    return bool(sections) and all(
        f"**{section}**" in contract_text for section in sections
    )


check(
    "prompt denominator sections match the rubric sections exactly",
    prompt_sections(prompts[("anthropic-holistic", False)]) == DENOMINATOR_SECTIONS,
)
check(
    "no prompt section drifts away from a rubric heading",
    bound_to_rubric(prompts[("anthropic-holistic", False)]),
)
check(
    "a prompt section with no rubric heading is rejected as drift",
    not bound_to_rubric(
        prompts[("anthropic-holistic", False)].replace(
            "Simplification", "Observability"
        )
    ),
)
check(
    "a prompt that drops the denominator entirely is rejected as drift",
    not bound_to_rubric(
        prompts[("anthropic-holistic", False)].replace(DENOMINATOR_INSTRUCTION, "")
    ),
)
