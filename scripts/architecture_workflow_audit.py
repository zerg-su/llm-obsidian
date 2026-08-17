#!/usr/bin/env python3
"""Compose generic skill audit with Architecture Workflow release invariants."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
SHARED_AUDIT = ROOT / "skills" / "improve-skills" / "scripts" / "audit_skills.py"
DEFAULT_EVIDENCE_ROOT = (
    ROOT / "docs" / "acceptance" / "evidence" / "architecture-workflow-v1"
)
SHA256 = re.compile(r"[0-9a-f]{40}\Z")
EVIDENCE_ID = re.compile(r"E(?:[1-9]|10|11)-[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _load_shared_audit():
    spec = importlib.util.spec_from_file_location(
        "architecture_workflow_shared_skill_audit", SHARED_AUDIT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("shared skill audit cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shared_audit = _load_shared_audit()


class SubjectEvidenceManifest(NamedTuple):
    current_subject_head: str
    invalidated_subject_heads: frozenset[str]
    evidence_ids: tuple[str, ...]


def load_subject_evidence_manifest(path: Path) -> SubjectEvidenceManifest:
    """Load the release's sole current-subject and evidence-ID authority."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("subject/evidence manifest is unavailable") from exc
    required = {
        "schema_version",
        "type",
        "current_subject_head",
        "invalidated_subject_heads",
        "evidence_ids",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("type")
        != "architecture-workflow-v1-subject-evidence-manifest"
    ):
        raise ValueError("subject/evidence manifest schema changed")
    current = value["current_subject_head"]
    invalidated = value["invalidated_subject_heads"]
    evidence_ids = value["evidence_ids"]
    if not isinstance(current, str) or SHA256.fullmatch(current) is None:
        raise ValueError("current subject head is invalid")
    if (
        not isinstance(invalidated, list)
        or any(not isinstance(item, str) or SHA256.fullmatch(item) is None for item in invalidated)
        or len(invalidated) != len(set(invalidated))
        or current in invalidated
    ):
        raise ValueError("invalidated subject inventory is invalid")
    if (
        not isinstance(evidence_ids, list)
        or any(
            not isinstance(item, str) or EVIDENCE_ID.fullmatch(item) is None
            for item in evidence_ids
        )
        or len(evidence_ids) != 11
        or len(evidence_ids) != len(set(evidence_ids))
        or {
            int(item[1:].split("-", 1)[0])
            for item in evidence_ids
        }
        != set(range(1, 12))
    ):
        raise ValueError("canonical E1-E11 evidence inventory is invalid")
    return SubjectEvidenceManifest(
        current,
        frozenset(invalidated),
        tuple(evidence_ids),
    )


def _release_evidence_ids(release: object) -> set[str]:
    if not isinstance(release, dict) or not isinstance(release.get("gates"), list):
        raise ValueError("release evidence gates are unavailable")
    observed: set[str] = set()
    for gate in release["gates"]:
        if not isinstance(gate, dict) or not isinstance(gate.get("evidence_ids"), list):
            raise ValueError("release gate evidence_ids are invalid")
        identifiers = gate["evidence_ids"]
        if any(not isinstance(item, str) for item in identifiers):
            raise ValueError("release gate evidence_ids are invalid")
        observed.update(identifiers)
    return observed


def validate_release_evidence(
    path: Path, manifest: SubjectEvidenceManifest
) -> dict[str, object]:
    """Bind release gates to the sole manifest authority."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("release evidence is unavailable") from exc
    if not isinstance(value, dict):
        raise ValueError("release evidence must be an object")
    if value.get("implementation_subject_head") != manifest.current_subject_head:
        raise ValueError("release evidence cites a non-current subject")
    observed = _release_evidence_ids(value)
    allowed = set(manifest.evidence_ids)
    unknown = sorted(observed - allowed)
    if unknown:
        raise ValueError("release names unknown evidence: " + ", ".join(unknown))
    missing = sorted(allowed - observed)
    if missing:
        raise ValueError("release omits canonical evidence: " + ", ".join(missing))
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    stale = sorted(item for item in manifest.invalidated_subject_heads if item in encoded)
    if stale:
        raise ValueError("release cites invalidated subject: " + ", ".join(stale))
    return value


def validate_verdict_records(
    payload: object,
    inventory: tuple[str, ...],
    manifest: SubjectEvidenceManifest,
) -> dict[str, object]:
    """Apply generic five-pass validation, then release evidence bindings."""

    validated = shared_audit.validate_verdict_records(payload, inventory)
    allowed = set(manifest.evidence_ids)
    for index, record in enumerate(validated["records"]):
        label = f"verdict record {index + 1}"
        evidence_ids = record["required_outcome_evidence"]
        unknown = sorted(set(evidence_ids) - allowed)
        if unknown:
            raise ValueError(f"{label} names unknown evidence: {', '.join(unknown)}")
        evidence = record["evidence"]
        stale = sorted(
            subject
            for subject in manifest.invalidated_subject_heads
            if subject in evidence
        )
        if stale:
            raise ValueError(f"{label} cites invalidated subject: {', '.join(stale)}")
        if (
            "E10-behavioral-pressure-set" in evidence_ids
            and manifest.current_subject_head not in evidence
        ):
            raise ValueError(f"{label} does not cite the current pressure subject")
    return validated


def handoff_contract_error(skill_path: Path, skills_root: Path) -> str:
    """Return one bounded error for an incomplete provider-independent handoff."""

    try:
        frontmatter, body = shared_audit.split_frontmatter(skill_path)
        metadata = shared_audit.parse_frontmatter(frontmatter)
    except (OSError, ValueError) as exc:
        return f"architecture handoff contract is unreadable: {exc}"
    marker = "Make exactly one explicit handoff"
    terms = (
        marker,
        "Give the invoked carrier",
        "expected return artifact",
        "Collect its result",
    )
    tools = str(metadata.get("allowed-tools") or "").split()
    carriers = set(re.findall(r"(?:→|handoff to)\s*`([a-z0-9-]+)`", body))
    missing = sorted(
        name for name in carriers if not (skills_root / name / "SKILL.md").is_file()
    )
    details: list[str] = []
    if "Skill" not in tools:
        details.append("allowed-tools omits Skill")
    if not all(term in body for term in terms):
        details.append("invocation/returned-artifact protocol is incomplete")
    if not carriers:
        details.append("no named carrier is declared")
    if missing:
        details.append("missing carrier skills: " + ", ".join(missing))
    return "; ".join(details)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=ROOT / "skills")
    parser.add_argument("--verdicts", type=Path, required=True)
    parser.add_argument("--scope", action="append", default=[], metavar="SKILL")
    parser.add_argument(
        "--subject-evidence-manifest",
        type=Path,
        default=DEFAULT_EVIDENCE_ROOT / "subject-evidence-manifest.json",
    )
    parser.add_argument(
        "--release-evidence",
        type=Path,
        default=DEFAULT_EVIDENCE_ROOT / "release-evidence.json",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    rows = shared_audit.audit_directory(args.skills_dir)
    findings = [finding for row in rows for finding in row.get("findings", [])]
    errors = sum(item["severity"] == "error" for item in findings)
    warnings = sum(item["severity"] == "warning" for item in findings)
    release_error = ""
    try:
        installed = tuple(str(row["skill"]) for row in rows)
        inventory = tuple(args.scope) if args.scope else installed
        if len(inventory) != len(set(inventory)) or not set(inventory) <= set(installed):
            raise ValueError("verdict scope must contain unique installed skill names")
        manifest = load_subject_evidence_manifest(args.subject_evidence_manifest)
        validate_release_evidence(args.release_evidence, manifest)
        verdict_payload = json.loads(args.verdicts.read_text(encoding="utf-8"))
        validate_verdict_records(verdict_payload, inventory, manifest)
        handoff_error = handoff_contract_error(
            args.skills_dir / "architecture" / "SKILL.md", args.skills_dir
        )
        if handoff_error:
            raise ValueError(handoff_error)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        release_error = str(exc)
        errors += 1

    payload = {
        "schema_version": 1,
        "skills_dir": str(args.skills_dir),
        "audited": len(rows),
        "errors": errors,
        "warnings": warnings,
        "verdicts_validated": not release_error,
    }
    if release_error:
        payload["verdicts_error"] = release_error
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            for finding in row.get("findings", []):
                print(
                    f"{finding['severity'].upper()} {row['skill']} "
                    f"{finding['code']}: {finding['message']}"
                )
        if release_error:
            print(f"ERROR architecture-workflow: {release_error}")
        else:
            print("Architecture Workflow verdict and evidence bindings passed")
        print(f"skill audit: {len(rows)} audited, {errors} errors, {warnings} warnings")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
