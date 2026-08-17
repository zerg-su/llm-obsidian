#!/usr/bin/env python3
"""Adversarial self-check for the accepted cycle-five structural pivot."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED_PATHS = (
    "scripts/engineering-eval-runner.py",
    "skills/improve-skills/scripts/audit_skills.py",
    "tests/test_engineering_eval_runner.py",
    "tests/test_improve_skills.py",
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


audit = load_module(
    "architecture_workflow_structural_pivot_audit",
    ROOT / "scripts" / "architecture_workflow_audit.py",
)


def integration_base() -> str:
    """Resolve the commit this release integrates from.

    The guard must work in the code base, in a vault fork, and both before and
    after the release lands, so it takes the NEAREST common ancestor among the
    plausible integration refs instead of trusting one name. A bare ``main``
    means the fork's branch inside a fork checkout, and a tracked remote can lag
    a local integration branch; the nearest ancestor is correct in every case.
    """

    candidates = ["main"]
    remote_main = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"],
        cwd=ROOT,
        check=False,
    )
    if remote_main.returncode == 0:
        candidates.append("origin/main")
    tracked = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if tracked.returncode == 0 and tracked.stdout.strip():
        candidates.append(tracked.stdout.strip())
    bases = []
    for candidate in candidates:
        found = subprocess.run(
            ["git", "merge-base", "HEAD", candidate],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if found.returncode == 0 and found.stdout.strip():
            bases.append(found.stdout.strip())
    if not bases:
        raise AssertionError("no integration ref resolved for the shared-surface guard")
    # The nearest ancestor is the one that has every other candidate base as an
    # ancestor of itself.
    for base in bases:
        if all(
            subprocess.run(["git", "merge-base", "--is-ancestor", other, base],
                           cwd=ROOT, check=False).returncode == 0
            for other in bases
        ):
            return base
    raise AssertionError("integration bases are unordered for the shared-surface guard")


merge_base = integration_base()
shared_delta = subprocess.run(
    ["git", "diff", "--quiet", merge_base, "--", *SHARED_PATHS],
    cwd=ROOT,
    check=False,
)
assert shared_delta.returncode == 0, "release behavior leaked back into a shared surface"
shared_runner = (ROOT / SHARED_PATHS[0]).read_text(encoding="utf-8")
assert "--architecture-pressure" not in shared_runner
assert (ROOT / "scripts" / "architecture_workflow_pressure.py").is_file()
assert (ROOT / "scripts" / "architecture_workflow_audit.py").is_file()
print("OK   mixed-ownership pressure changes fail the release boundary")


with tempfile.TemporaryDirectory(prefix="architecture-pivot-handoff.") as raw:
    skills = Path(raw) / "skills"
    architecture = skills / "architecture"
    design = skills / "design"
    architecture.mkdir(parents=True)
    design.mkdir()
    (design / "SKILL.md").write_text(
        "---\nname: design\ndescription: Carrier.\n---\n\n# Design\n",
        encoding="utf-8",
    )
    skill = architecture / "SKILL.md"
    skill.write_text(
        "---\nname: architecture\ndescription: Architecture.\n"
        "allowed-tools: Skill Read\n---\n\n"
        "Make exactly one explicit handoff to `design`.\n"
        "Give the invoked carrier context and an expected return artifact.\n",
        encoding="utf-8",
    )
    error = audit.handoff_contract_error(skill, skills)
    assert "protocol is incomplete" in error, error
print("OK   handoff proof fails when returned-artifact collection is absent")


with tempfile.TemporaryDirectory(prefix="architecture-pivot-identity.") as raw:
    manifest_path = Path(raw) / "subject-evidence-manifest.json"
    evidence_ids = [
        "E1-contract-reference",
        "E2-architecture-carrier",
        "E3-decompose-carrier",
        "E4-implementation-plan-guard",
        "E5-router-rules",
        "E6-registry-budget",
        "E7-docs-inventory-sync",
        "E8-writer-fixture",
        "E9-deterministic-baseline",
        "E10-behavioral-pressure-set",
        "E11-five-pass-verdicts",
    ]
    evidence_ids[1] = evidence_ids[0]
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-workflow-v1-subject-evidence-manifest",
                "current_subject_head": "1" * 40,
                "invalidated_subject_heads": ["2" * 40],
                "evidence_ids": evidence_ids,
            }
        ),
        encoding="utf-8",
    )
    try:
        audit.load_subject_evidence_manifest(manifest_path)
    except ValueError as exc:
        assert "evidence inventory" in str(exc)
    else:
        raise AssertionError("duplicate evidence identity was accepted")
print("OK   duplicated evidence identity fails the sole manifest authority")

print("\nArchitecture Workflow structural-pivot adversarial self-check passed.")
