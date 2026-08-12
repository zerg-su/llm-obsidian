#!/usr/bin/env python3
"""Integrated E1-E7 trace for the 2.6.6 RC1 lifecycle simplification."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.audit_manifest import load_audit_manifest  # noqa: E402
import task_review_context  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def run_trace(path: str, terminal: str) -> str:
    result = subprocess.run(
        [sys.executable, path],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        f"named trace {Path(path).stem} is GREEN",
        result.returncode == 0 and terminal in result.stdout,
        result.stderr or result.stdout[-500:],
    )
    return result.stdout


exact_trace = run_trace(
    "tests/harness/test_exact_head_review_attempt.py",
    "All exact-HEAD ReviewAttempt gate tests passed.",
)
event_trace = run_trace(
    "tests/harness/test_event_only_review_effects.py",
    "All event-only reviewer effect tests passed.",
)
split_trace = run_trace(
    "tests/harness/test_split_base_identity.py",
    "All sealed Split base identity tests passed.",
)
check(
    "exact-attempt trace is terminal and inspect-only across old HEADs",
    "terminal attempts become digest-bound program decisions" in exact_trace
    and "pre-activation gate inspect-only" in exact_trace,
)
check(
    "provider-event trace is attention-only and has no reviewer restart",
    "turn-stopped is telemetry-only" in event_trace
    and "reviewer runtime contains no restart authority" in event_trace,
)
check(
    "Split trace binds branch drift and rejects unrelated history",
    "survives branch movement and deletion" in split_trace
    and "unrelated history cannot satisfy Split ancestry" in split_trace,
)

quality_spec = importlib.util.spec_from_file_location(
    "code_quality_audit", ROOT / "scripts" / "code-quality-audit.py"
)
assert quality_spec and quality_spec.loader
quality = importlib.util.module_from_spec(quality_spec)
sys.modules[quality_spec.name] = quality
quality_spec.loader.exec_module(quality)
authority = quality.audit_rc1_active_authority(ROOT)
authority_paths = set(quality.active_authority_files(ROOT))
check(
    "fresh artifact repair remains inside the measured authority contour",
    "scripts/harness/fresh_artifact_repair.py" in authority_paths,
    sorted(authority_paths),
)
amendment_owner = Path(
    sys.modules[task_review_context.load_amendments.__module__].__file__
).resolve().relative_to(ROOT).as_posix()
check(
    "the amendment reader's production owner is independently measured",
    amendment_owner in authority_paths,
    amendment_owner,
)
check(
    "the separately owned authority manifest is the exact measured contour",
    authority_paths
    == {item["path"] for item in authority["production_files"]},
    sorted(authority_paths ^ {item["path"] for item in authority["production_files"]}),
)
quality_ceiling = json.loads(
    (ROOT / "config/code-quality-baseline.json").read_text(encoding="utf-8")
)["rc1_active_authority"]
check(
    "active review contour has no writable or incident-pinned authority",
    len(authority["writable_authorities"])
    <= int(quality_ceiling["final_max_writable_authorities"])
    and len(authority["incident_literals"])
    <= int(quality_ceiling["final_max_incident_literals"])
    and int(authority["production_loc"])
    <= int(quality_ceiling["final_max_production_loc"])
    and len(authority["production_files"])
    == int(quality_ceiling["final_production_file_count"]),
    authority,
)

manifest = load_audit_manifest(ROOT)
manifest_sources = set(manifest.entrypoints)
deleted_authorities = {
    "scripts/task_review_authorization_boundary.py",
    "scripts/task_review_drift_contract.py",
    "scripts/task_review_legacy_rounds.py",
    "scripts/task_review_mechanism_recovery.py",
    "scripts/task_review_post_fresh_publication.py",
    "scripts/task_review_post_fresh_recovery.py",
    "scripts/task_review_resolution_flow.py",
    "scripts/task_review_resolution_evidence.py",
}
check(
    "audit manifest admits no deleted lifecycle authority",
    deleted_authorities.isdisjoint(manifest_sources),
    sorted(deleted_authorities & manifest_sources),
)

coverage_spec = importlib.util.spec_from_file_location(
    "harness_coverage_audit", ROOT / "scripts" / "harness-coverage-audit.py"
)
assert coverage_spec and coverage_spec.loader
coverage = importlib.util.module_from_spec(coverage_spec)
sys.modules[coverage_spec.name] = coverage
coverage_spec.loader.exec_module(coverage)
source_modules = coverage.source_modules()
check(
    "every critical coverage floor still names a live audited module",
    set(coverage.CRITICAL_FLOORS) <= source_modules,
    sorted(set(coverage.CRITICAL_FLOORS) - source_modules),
)

receipts = json.loads(
    (ROOT / "docs/acceptance/v2.6.6-rc1-slice-receipts.json").read_text(
        encoding="utf-8"
    )
)
forbidden = tuple(receipts["forbidden_before_integration_green"])
rows = receipts["slices"]
check(
    "incremental receipt ladder is complete through Slice G",
    [row["slice_id"] for row in rows] == list("ABCDEFG"),
)
for row in rows:
    commit_sha = row["commit_sha"]
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit_sha, "HEAD"],
        cwd=ROOT,
        check=False,
    ).returncode == 0
    commands = "\n".join(
        str(row[field])
        for field in ("red_command", "green_command", "focused_command")
    )
    check(f"Slice {row['slice_id']} receipt binds an ancestor commit", ancestor)
    check(
        f"Slice {row['slice_id']} stayed below the pre-integration broad gate",
        all(command not in commands for command in forbidden),
    )

dogfood = json.loads(
    (
        ROOT
        / "docs/acceptance/v2.6.6-rc1-real-dogfood.json"
    ).read_text(encoding="utf-8")
)
observations = dogfood["observations"]
check(
    "bounded RC1 dogfood preserves both callbacks and has no resource tail",
    dogfood["subject_head_sha"] == "b313632e285b13096e4c2692393cf16610aecd2a"
    and observations["provider_typed_artifact_count"] == 2
    and observations["accepted_receipt_count"] == 2
    and observations["callback_loss_count"] == 0
    and observations["terminal_resources_owned"] is False
    and observations["manual_callback_write_count"] == 0
    and observations["repeated_review_count"] == 0,
)
check(
    "dogfood terminal inventory is complete and resource-free",
    len(dogfood["terminal_resource_inventory"]) == 6
    and all(
        row["terminal"] and not row["resources_owned"]
        for row in dogfood["terminal_resource_inventory"]
    ),
    dogfood["terminal_resource_inventory"],
)
check(
    "dogfood callback-ingestion defect is explicitly deferred to RC2",
    dogfood["observed_adjacent_findings"]
    == ["RC2.REVIEW_CALLBACK_INGESTION_FINALIZING"],
)

print("\n2.6.6 RC1 integrated E1-E7 trace passed")
