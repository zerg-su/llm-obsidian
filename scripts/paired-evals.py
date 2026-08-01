#!/usr/bin/env python3
"""Verify frozen paired fixtures and compare already completed eval evidence.

This is deliberately not an executor or scheduler. Provider tasks still run
through the repository harness; this script only validates immutable inputs
and compares bounded baseline/post result records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from outcome_contract import OutcomeContractError, extract_from_bytes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evals" / "paired-v2.6.0" / "manifest.json"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CASE_FIELDS = {
    "case_id",
    "workflow",
    "pipeline",
    "completion_policy",
    "route",
    "verification_profile",
    "review_mode",
    "review_max_verify_iterations",
    "baseline_plan",
    "baseline_plan_sha256",
    "post_plan",
    "post_plan_sha256",
    "contract_sha256",
    "expected_evidence_ids",
    "fixtures",
    "goal_misaligned_sentinel",
}
REPORT_CASE_FIELDS = {
    "case_id",
    "contract_sha256",
    "route",
    "verification_profile",
    "outcome_disposition",
    "outcome_evidence",
    "user_interventions",
    "model_rounds",
    "review_rounds",
    "callback_failures",
    "duplicate_effects",
    "duration_seconds",
}
COUNT_FIELDS = (
    "callback_failures",
    "duplicate_effects",
    "model_rounds",
    "review_rounds",
    "user_interventions",
)
OUTCOME_RANK = {"not-achieved": 0, "partially-achieved": 1, "achieved": 2}


class PairedEvalError(ValueError):
    """Frozen paired input or result evidence is invalid."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PairedEvalError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PairedEvalError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise PairedEvalError(f"{label} must be an object")
    return value


def _rooted(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise PairedEvalError(f"{label} must be repository-relative")
    resolved = (root / relative).resolve()
    if root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
        raise PairedEvalError(f"{label} escapes or is unavailable")
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_manifest(path: Path = DEFAULT_MANIFEST, *, root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    raw = _read_json(path.resolve(), "paired manifest")
    if set(raw) != {"schema_version", "release", "cases"}:
        raise PairedEvalError("paired manifest fields changed")
    if raw["schema_version"] != 1 or raw["release"] != "2.6.0":
        raise PairedEvalError("paired manifest identity changed")
    cases = raw["cases"]
    if not isinstance(cases, list) or len(cases) != 2:
        raise PairedEvalError("paired manifest requires exactly two cases")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(cases):
        label = f"paired case {index + 1}"
        if not isinstance(item, dict) or set(item) != CASE_FIELDS:
            raise PairedEvalError(f"{label} fields changed")
        case_id = item.get("case_id")
        if case_id not in {"fix", "design"} or case_id in seen:
            raise PairedEvalError(f"{label} identity changed")
        seen.add(case_id)
        expected_workflow = {
            "fix": ["clarify", "debug", "tdd", "review"],
            "design": ["clarify", "design", "prototype", "review"],
        }[case_id]
        expected_pipeline = {
            "fix": ("engineering/fix", "autonomous", True),
            "design": ("engineering/change", "attention", False),
        }[case_id]
        if item.get("workflow") != expected_workflow:
            raise PairedEvalError(f"{label} workflow changed")
        if (
            item.get("pipeline"),
            item.get("completion_policy"),
            item.get("goal_misaligned_sentinel"),
        ) != expected_pipeline:
            raise PairedEvalError(f"{label} execution envelope changed")
        if item.get("verification_profile") != "scoped" or item.get("review_mode") != "simple":
            raise PairedEvalError(f"{label} verification contract changed")
        if item.get("review_max_verify_iterations") != 1:
            raise PairedEvalError(f"{label} review budget changed")
        route = item.get("route")
        if route != {"runtime": "codex", "model": "sol", "effort": "high"}:
            raise PairedEvalError(f"{label} route changed")
        plan_contracts = []
        plan_hashes = []
        for phase in ("baseline", "post"):
            plan = _rooted(root, item.get(f"{phase}_plan"), f"{label} {phase} plan")
            actual_plan_sha = _sha256(plan)
            expected_plan_sha = item.get(f"{phase}_plan_sha256")
            if not isinstance(expected_plan_sha, str) or actual_plan_sha != expected_plan_sha:
                raise PairedEvalError(f"{label} {phase} plan bytes changed")
            try:
                plan_contracts.append(extract_from_bytes(plan.read_bytes()))
            except OutcomeContractError as exc:
                raise PairedEvalError(f"{label} {phase} contract is invalid") from exc
            plan_hashes.append(actual_plan_sha)
        contract_sha = item.get("contract_sha256")
        if (
            not isinstance(contract_sha, str)
            or not SHA256_RE.fullmatch(contract_sha)
            or {contract.sha256 for contract in plan_contracts} != {contract_sha}
        ):
            raise PairedEvalError(f"{label} contract identity changed")
        expected_evidence = item.get("expected_evidence_ids")
        if (
            not isinstance(expected_evidence, list)
            or expected_evidence != list(plan_contracts[0].evidence_ids)
        ):
            raise PairedEvalError(f"{label} evidence identity changed")
        fixtures = item.get("fixtures")
        if not isinstance(fixtures, list) or not fixtures:
            raise PairedEvalError(f"{label} fixtures are invalid")
        for fixture in fixtures:
            if not isinstance(fixture, dict) or set(fixture) != {"path", "sha256"}:
                raise PairedEvalError(f"{label} fixture entry is invalid")
            fixture_path = _rooted(root, fixture["path"], f"{label} fixture")
            if _sha256(fixture_path) != fixture["sha256"]:
                raise PairedEvalError(f"{label} fixture bytes changed")
        normalized.append({**item, "baseline_plan_sha256": plan_hashes[0], "post_plan_sha256": plan_hashes[1]})
    if tuple(item["case_id"] for item in normalized) != ("fix", "design"):
        raise PairedEvalError("paired case ordering changed")
    fixture_digest = _digest({"schema_version": 1, "release": "2.6.0", "cases": normalized})
    return {
        "schema_version": 1,
        "release": "2.6.0",
        "case_ids": [item["case_id"] for item in normalized],
        "fixture_set_sha256": fixture_digest,
        "cases": normalized,
    }


def _load_report(path: Path, phase: str, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = _read_json(path, f"paired {phase} report")
    if set(raw) != {"schema_version", "phase", "fixture_set_sha256", "cases"}:
        raise PairedEvalError(f"paired {phase} report fields changed")
    if raw["schema_version"] != 1 or raw["phase"] != phase:
        raise PairedEvalError(f"paired {phase} report identity changed")
    if raw["fixture_set_sha256"] != manifest["fixture_set_sha256"]:
        raise PairedEvalError(f"paired {phase} fixture identity changed")
    cases = raw["cases"]
    if not isinstance(cases, list) or len(cases) != len(manifest["cases"]):
        raise PairedEvalError(f"paired {phase} case set changed")
    indexed: dict[str, dict[str, Any]] = {}
    expected = {item["case_id"]: item for item in manifest["cases"]}
    for item in cases:
        if not isinstance(item, dict) or set(item) != REPORT_CASE_FIELDS:
            raise PairedEvalError(f"paired {phase} case fields changed")
        case_id = item.get("case_id")
        if case_id not in expected or case_id in indexed:
            raise PairedEvalError(f"paired {phase} case identity changed")
        contract = expected[case_id]
        if (
            item.get("contract_sha256") != contract["contract_sha256"]
            or item.get("route") != contract["route"]
            or item.get("verification_profile") != contract["verification_profile"]
        ):
            raise PairedEvalError(f"paired {phase} {case_id} contract changed")
        if item.get("outcome_disposition") not in OUTCOME_RANK:
            raise PairedEvalError(f"paired {phase} {case_id} disposition is invalid")
        evidence = item.get("outcome_evidence")
        if not isinstance(evidence, dict) or set(evidence) != {"established", "missing", "contradicted"}:
            raise PairedEvalError(f"paired {phase} {case_id} evidence is invalid")
        for field in (*COUNT_FIELDS, "duration_seconds"):
            if type(item.get(field)) is not int or item[field] < 0:
                raise PairedEvalError(f"paired {phase} {case_id} {field} is invalid")
        if any(type(value) is not int or value < 0 for value in evidence.values()):
            raise PairedEvalError(f"paired {phase} {case_id} evidence counts are invalid")
        if sum(evidence.values()) != len(contract["expected_evidence_ids"]):
            raise PairedEvalError(
                f"paired {phase} {case_id} evidence classification is incomplete"
            )
        indexed[case_id] = item
    return indexed


def compare_reports(
    manifest: dict[str, Any], *, baseline_path: Path, post_path: Path
) -> dict[str, Any]:
    baseline = _load_report(baseline_path, "baseline", manifest)
    post = _load_report(post_path, "post", manifest)
    regressions: list[str] = []
    deltas: dict[str, dict[str, int]] = {}
    for case_id in manifest["case_ids"]:
        before = baseline[case_id]
        after = post[case_id]
        case_deltas = {
            field: after[field] - before[field]
            for field in (*COUNT_FIELDS, "duration_seconds")
        }
        deltas[case_id] = case_deltas
        for field in COUNT_FIELDS:
            if case_deltas[field] > 0:
                regressions.append(f"{case_id}: {field} increased")
        if OUTCOME_RANK[after["outcome_disposition"]] < OUTCOME_RANK[before["outcome_disposition"]]:
            regressions.append(f"{case_id}: outcome disposition regressed")
        before_evidence = before["outcome_evidence"]
        after_evidence = after["outcome_evidence"]
        if after_evidence["established"] < before_evidence["established"]:
            regressions.append(f"{case_id}: established outcome evidence decreased")
        for field in ("missing", "contradicted"):
            if after_evidence[field] > before_evidence[field]:
                regressions.append(f"{case_id}: {field} outcome evidence increased")
    return {
        "schema_version": 1,
        "fixture_set_sha256": manifest["fixture_set_sha256"],
        "regressions": sorted(regressions),
        "deltas": deltas,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify")
    compare = sub.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--post", type=Path, required=True)
    compare.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "verify":
            payload = {
                "schema_version": 1,
                "release": manifest["release"],
                "case_ids": manifest["case_ids"],
                "fixture_set_sha256": manifest["fixture_set_sha256"],
            }
        else:
            payload = compare_reports(
                manifest, baseline_path=args.baseline, post_path=args.post
            )
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        if getattr(args, "report", None) is None:
            sys.stdout.write(rendered)
        else:
            args.report.write_text(rendered, encoding="utf-8")
        return 0 if not payload.get("regressions") else 1
    except (OSError, PairedEvalError) as exc:
        print(f"paired-evals: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
