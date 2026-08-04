#!/usr/bin/env python3
"""Behavior matrix for the code-owned plan review facade."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def outcome(observable: str = "the exact behavior is established") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "purpose": "review the plan safely",
            "desired_outcome": "the plan is reviewed as intent",
            "success_evidence": [
                {"evidence_id": "E-plan", "observable": observable}
            ],
            "non_goals": ["launch an implementation review"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def plan_text(
    *,
    outcome_headings: int = 1,
    disposition_headings: int = 1,
    evidence_headings: int = 1,
    design: str = "Implement the bounded facade.",
) -> str:
    parts = ["---", "type: plan", "status: pending", "---", "", "# Plan", ""]
    for _ in range(outcome_headings):
        parts.extend(("## Outcome Contract", "", "```json", outcome(), "```", ""))
    parts.extend(("## Design", "", design, ""))
    for _ in range(disposition_headings):
        parts.extend(
            (
                "## Capability Dispositions and Defect Ledger",
                "",
                "| Capability | Disposition |",
                "|---|---|",
                "| plan facade | included |",
                "",
            )
        )
    for _ in range(evidence_headings):
        parts.extend(
            (
                "## Success Evidence Map",
                "",
                "| Evidence | Check |",
                "|---|---|",
                "| E-plan | facade matrix |",
                "",
            )
        )
    return "\n".join(parts)


with tempfile.TemporaryDirectory(prefix="plan-review-facade.") as raw:
    tmp = Path(raw)
    worktree = tmp / "repo"
    plan_dir = worktree / "wiki/plans"
    plan_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "plan-review@example.invalid"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Plan Review Test"],
        cwd=worktree,
        check=True,
    )
    (worktree / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan = plan_dir / "approved.md"
    plan.write_text(plan_text(), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add approved plan"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        plan_review = importlib.import_module("task_review_plan")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "RED: task_review_plan must provide the code-owned compiler/facade"
        ) from exc

    compiled = plan_review.compile_plan_review(worktree, plan)
    check(
        "1 valid plan compiles four independent artifacts",
        set(compiled.artifacts)
        == {"outcome", "design", "capability_dispositions", "success_evidence"}
        and len(set(compiled.artifact_sha256.values())) == 4,
        compiled,
    )
    check(
        "2 design artifact replaces every protected region with its digest",
        b"the plan is reviewed as intent" not in compiled.artifacts["design"]
        and b"plan facade | included" not in compiled.artifacts["design"]
        and b"E-plan | facade matrix" not in compiled.artifacts["design"]
        and all(
            digest.encode() in compiled.artifacts["design"]
            for name, digest in compiled.artifact_sha256.items()
            if name != "design"
        ),
    )

    def rejected(label: str, text: str, **kwargs: object) -> None:
        candidate = plan_dir / f"{label}.md"
        candidate.write_text(text, encoding="utf-8")
        try:
            plan_review.compile_plan_review(worktree, candidate, **kwargs)
        except plan_review.PlanReviewError as exc:
            guarded = exc.code == "plan-review-artifact-boundary-invalid"
        else:
            guarded = False
        check(label, guarded)

    rejected("3 missing Outcome heading fails closed", plan_text(outcome_headings=0))
    rejected("4 duplicated Outcome heading fails closed", plan_text(outcome_headings=2))
    rejected(
        "5 missing dispositions without explicit pointer fails closed",
        plan_text(disposition_headings=0),
    )
    rejected(
        "6 missing evidence map without explicit pointer fails closed",
        plan_text(evidence_headings=0),
    )

    disposition = worktree / "wiki/dispositions.md"
    evidence = worktree / "wiki/evidence.md"
    disposition.write_text("# dispositions\n\nplan facade: included\n", encoding="utf-8")
    evidence.write_text("# evidence\n\nE-plan: facade matrix\n", encoding="utf-8")
    explicit_plan = plan_dir / "explicit.md"
    explicit_plan.write_text(
        plan_text(disposition_headings=0, evidence_headings=0),
        encoding="utf-8",
    )
    explicit = plan_review.compile_plan_review(
        worktree,
        explicit_plan,
        capability_dispositions="wiki/dispositions.md",
        success_evidence_map="wiki/evidence.md",
    )
    check(
        "7 absent inline sections accept two distinct exact pointers",
        explicit.artifacts["capability_dispositions"] == disposition.read_bytes()
        and explicit.artifacts["success_evidence"] == evidence.read_bytes(),
    )
    rejected(
        "8 overlapping explicit artifacts fail closed",
        plan_text(disposition_headings=0, evidence_headings=0),
        capability_dispositions="wiki/dispositions.md",
        success_evidence_map="wiki/dispositions.md",
    )

    base_sha, head_sha = plan_review.resolve_plan_oids(worktree, compiled)
    check(
        "9 single-parent exact plan commit derives HEAD parent",
        base_sha == baseline
        and head_sha
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        (base_sha, head_sha),
    )
    try:
        plan_review.resolve_plan_oids(worktree, compiled, explicit_base="HEAD")
    except plan_review.PlanReviewError as exc:
        symbolic_rejected = exc.code == "plan-review-base-invalid"
    else:
        symbolic_rejected = False
    check("10 symbolic base is rejected before launch", symbolic_rejected)

    (worktree / "README.md").write_text("baseline\nunrelated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "unrelated head"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        plan_review.resolve_plan_oids(worktree, compiled)
    except plan_review.PlanReviewError as exc:
        missing_base_rejected = exc.code == "plan-review-base-invalid"
    else:
        missing_base_rejected = False
    check("11 current plan review without a safe base fails closed", missing_base_rejected)

    legacy = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/task-review-runner.py"),
            "current",
            "--worktree",
            str(worktree),
            "--plan",
            str(plan),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "12 legacy current --plan rejects the ambiguous implementation default",
        legacy.returncode == 3
        and "use the plan facade" in legacy.stderr
        and not (worktree / ".vault-meta").exists(),
        (legacy.stdout, legacy.stderr),
    )

print("\nPlan review facade RED/green matrix passed.")
