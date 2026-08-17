#!/usr/bin/env python3
"""Tests for the release-owned Architecture Workflow audit composition."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "architecture_workflow_audit.py"
spec = importlib.util.spec_from_file_location("architecture_workflow_audit_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def verdict_payload(subject: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "records": [
            {
                "skill": "fixture",
                "verdict": "fix",
                "passes": {
                    "invocation": "pass",
                    "hierarchy": "pass",
                    "steering": "F-001",
                    "pruning": "pass",
                    "goal_preservation": "F-001",
                },
                "overall_input": "The approved task and Outcome Contract.",
                "overall_outcome": "Deliver the declared observable result.",
                "local_subgoal": "Keep the bounded step predictable.",
                "completion_proxies": ["The focused test is green."],
                "required_outcome_evidence": ["E10-behavioral-pressure-set"],
                "evidence": f"pressure subject {subject}",
                "change": "Sharpen the completion boundary.",
                "behavior_proof": "Invocation and authority remain unchanged.",
            }
        ],
    }


with tempfile.TemporaryDirectory(prefix="architecture-workflow-audit.") as raw:
    tmp = Path(raw)
    current = "1" * 40
    stale = "2" * 40
    ids = [
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
    manifest_path = tmp / "subject-evidence-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-workflow-v1-subject-evidence-manifest",
                "current_subject_head": current,
                "invalidated_subject_heads": [stale],
                "evidence_ids": ids,
            }
        ),
        encoding="utf-8",
    )
    manifest = module.load_subject_evidence_manifest(manifest_path)
    assert manifest.current_subject_head == current
    assert manifest.invalidated_subject_heads == frozenset({stale})
    assert manifest.evidence_ids == tuple(ids)

    release_path = tmp / "release-evidence.json"
    release_path.write_text(
        json.dumps(
            {
                "implementation_subject_head": current,
                "gates": [{"evidence_ids": ids}],
            }
        ),
        encoding="utf-8",
    )
    module.validate_release_evidence(release_path, manifest)
    module.validate_verdict_records(verdict_payload(current), ("fixture",), manifest)
    print("OK   one manifest binds the current subject and canonical evidence IDs")

    renamed = json.loads(release_path.read_text(encoding="utf-8"))
    renamed["gates"][0]["evidence_ids"][0] = "E1-renamed"
    release_path.write_text(json.dumps(renamed), encoding="utf-8")
    try:
        module.validate_release_evidence(release_path, manifest)
    except ValueError as exc:
        assert "unknown evidence" in str(exc)
    else:
        raise AssertionError("renamed release evidence identifier was accepted")

    stale_verdict = verdict_payload(current)
    stale_verdict["records"][0]["evidence"] += f" and {stale}"
    try:
        module.validate_verdict_records(stale_verdict, ("fixture",), manifest)
    except ValueError as exc:
        assert "invalidated subject" in str(exc)
    else:
        raise AssertionError("invalidated subject reference was accepted")

    missing_current = verdict_payload(current)
    missing_current["records"][0]["evidence"] = "pressure subject omitted"
    try:
        module.validate_verdict_records(missing_current, ("fixture",), manifest)
    except ValueError as exc:
        assert "current pressure subject" in str(exc)
    else:
        raise AssertionError("missing current subject reference was accepted")
    print("OK   stale, renamed, and missing evidence bindings fail closed")

    skills = tmp / "skills"
    architecture = skills / "architecture"
    architecture.mkdir(parents=True)
    for carrier in (
        "clarify",
        "design",
        "research",
        "prototype",
        "codebase-design",
        "review",
    ):
        target = skills / carrier
        target.mkdir()
        (target / "SKILL.md").write_text(
            f"---\nname: {carrier}\ndescription: Carrier.\n---\n\n# Carrier\n",
            encoding="utf-8",
        )
    body = (
        "# Architecture\n\nMake exactly one explicit handoff to `design`.\n"
        "Give the invoked carrier context and an expected return artifact.\n"
        "Collect its result into project context.\n"
    )
    skill_path = architecture / "SKILL.md"
    skill_path.write_text(
        "---\nname: architecture\ndescription: Architecture.\n"
        "allowed-tools: Read\n---\n\n" + body,
        encoding="utf-8",
    )
    assert "allowed-tools omits Skill" in module.handoff_contract_error(skill_path, skills)
    skill_path.write_text(
        "---\nname: architecture\ndescription: Architecture.\n"
        "allowed-tools: Skill Read\n---\n\n" + body,
        encoding="utf-8",
    )
    assert module.handoff_contract_error(skill_path, skills) == ""
    print("OK   release audit enforces the provider-independent handoff contract")

print("\nAll Architecture Workflow audit tests passed.")
