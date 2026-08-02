#!/usr/bin/env python3
"""Deterministic purpose-bound review program policy and reconciliation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.review_program import (  # noqa: E402
    ReviewBoundaryInput,
    ReviewBoundaryReceipt,
    ReviewProgramError,
    compile_review_program,
    reconcile_review_program,
)
from harness.workflows.review import ReviewContext, ReviewRequest  # noqa: E402
from harness.workflows.review_gate import review_context_sha256  # noqa: E402

SHA = {
    name: char * 64
    for name, char in {
        "outcome": "a",
        "plan": "b",
        "design": "c",
        "capabilities": "d",
        "success": "8",
        "head": "e",
        "verification": "f",
        "evidence": "1",
        "deviations": "2",
        "result": "3",
    }.items()
}


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def rejected(label: str, callback) -> None:
    try:
        callback()
    except (ReviewProgramError, ValueError):
        print(f"OK   {label}")
        return
    raise AssertionError(label)


intent = ReviewBoundaryInput(
    purpose="intent",
    outcome_contract_sha256=SHA["outcome"],
    plan_sha256=SHA["plan"],
    design_sha256=SHA["design"],
    design_path="docs/design.md",
    capability_dispositions_sha256=SHA["capabilities"],
    capability_dispositions_path="docs/capabilities.json",
    success_evidence_map_sha256=SHA["success"],
    success_evidence_map_path="docs/success-evidence.md",
)
implementation = ReviewBoundaryInput(
    purpose="implementation",
    outcome_contract_sha256=SHA["outcome"],
    product_head_sha=SHA["head"],
    verification_evidence_sha256=SHA["verification"],
    verification_evidence_path="docs/verification.md",
)
release = ReviewBoundaryInput(
    purpose="release",
    outcome_contract_sha256=SHA["outcome"],
    integration_head_sha=SHA["head"],
    outcome_evidence_map_sha256=SHA["evidence"],
    outcome_evidence_map_path="docs/outcome-evidence.md",
    accepted_deviations_sha256=SHA["deviations"],
    accepted_deviations_path="docs/deviations.md",
)

small = compile_review_program("small-reversible", (implementation,))
check(
    "small reversible work collapses intent into implementation",
    small.purposes == ("implementation",) and small.boundaries[0].intent_collapsed,
)

standard = compile_review_program("standard", (intent, implementation))
check(
    "standard work preserves intent and implementation checkpoints",
    standard.purposes == ("intent", "implementation"),
)

for high_risk in ("architecture", "migration", "release", "skill-integration"):
    program = compile_review_program(high_risk, (intent, implementation, release))
    check(
        f"{high_risk} requires all three review purposes",
        program.purposes == ("intent", "implementation", "release")
        and program.boundaries[-1].max_verify_iterations == 0,
    )

rejected(
    "compiler rejects a missing required review purpose",
    lambda: compile_review_program("architecture", (intent, implementation)),
)
rejected(
    "compiler rejects reordered review purposes",
    lambda: compile_review_program("architecture", (implementation, intent, release)),
)
rejected(
    "compiler rejects duplicated review purposes",
    lambda: compile_review_program(
        "architecture", (intent, implementation, implementation)
    ),
)
rejected(
    "intent requires design and capability disposition digests",
    lambda: replace(intent, design_sha256=""),
)
rejected(
    "intent digest requires an exact artifact path",
    lambda: replace(intent, design_path=""),
)
rejected(
    "review evidence paths stay repository-relative",
    lambda: replace(intent, design_path="../outside.md"),
)
rejected(
    "implementation requires exact HEAD and verification evidence",
    lambda: replace(implementation, verification_evidence_sha256=""),
)
rejected(
    "release requires integration HEAD and complete evidence map",
    lambda: replace(release, outcome_evidence_map_sha256=""),
)
rejected(
    "review program preserves one Outcome Contract across boundaries",
    lambda: compile_review_program(
        "architecture",
        (intent, replace(implementation, outcome_contract_sha256="9" * 64), release),
    ),
)
check(
    "review boundary input round-trips through an exact typed mapping",
    ReviewBoundaryInput.from_mapping(release.payload()) == release,
)
rejected(
    "review boundary input rejects unknown fields",
    lambda: ReviewBoundaryInput.from_mapping(
        {**release.payload(), "unexpected": "value"}
    ),
)

program = compile_review_program("architecture", (intent, implementation, release))
intent_receipt = ReviewBoundaryReceipt.approved(
    operation_id="review-intent",
    boundary=intent,
    result_sha256=SHA["result"],
)
decision = reconcile_review_program(program, (intent_receipt,))
check(
    "accepted intent evidence advances only to implementation",
    decision.action == "start" and decision.purpose == "implementation",
)

implementation_receipt = ReviewBoundaryReceipt.approved(
    operation_id="review-implementation",
    boundary=implementation,
    result_sha256="4" * 64,
)
release_receipt = ReviewBoundaryReceipt.approved(
    operation_id="review-release",
    boundary=release,
    result_sha256="5" * 64,
)
decision = reconcile_review_program(
    program, (intent_receipt, implementation_receipt, release_receipt)
)
check(
    "three additive exact-digest approvals reach release-ready",
    decision.action == "complete" and decision.purpose == "",
)

stale = replace(
    implementation_receipt,
    boundary_input_sha256="6" * 64,
)
rejected(
    "a stale implementation receipt cannot authorize release",
    lambda: reconcile_review_program(program, (intent_receipt, stale)),
)

stopped_release = ReviewBoundaryReceipt.stopped(
    operation_id="review-release-stop",
    boundary=release,
    result_sha256="7" * 64,
)
decision = reconcile_review_program(
    program, (intent_receipt, implementation_receipt, stopped_release)
)
check(
    "release finding stops instead of opening a hidden fix loop",
    decision.action == "stop"
    and decision.purpose == "release"
    and decision.may_fix is False,
)

release_context = ReviewContext(
    "packets/release/manifest.json",
    SHA["head"],
    "scoped",
    SHA["verification"],
    purpose="release",
    boundary_input_sha256=release.input_sha256,
)
implementation_context = replace(
    release_context,
    purpose="implementation",
    boundary_input_sha256=implementation.input_sha256,
)
check(
    "review context identity binds purpose and exact boundary input",
    review_context_sha256(release_context)
    != review_context_sha256(implementation_context),
)
ReviewRequest(
    "release-review",
    depth="deep",
    purpose="release",
    max_verify_iterations=0,
)
rejected(
    "release review cannot open a verification fix loop",
    lambda: ReviewRequest(
        "release-review-invalid",
        depth="deep",
        purpose="release",
        max_verify_iterations=1,
    ),
)

with tempfile.TemporaryDirectory(prefix="review-program.") as raw:
    directory = Path(raw)
    input_paths = []
    for boundary in (intent, implementation, release):
        path = directory / f"{boundary.purpose}.json"
        path.write_text(
            json.dumps(boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        input_paths.append(path)
    base_command = [
        sys.executable,
        str(ROOT / "scripts/review-program.py"),
        "status",
        "--risk-profile",
        "architecture",
    ]
    for path in input_paths:
        base_command.extend(("--input", str(path)))
    initial_status = subprocess.run(
        base_command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    initial_payload = json.loads(initial_status.stdout)
    check(
        "code-owned CLI selects the intent checkpoint from approved risk",
        initial_payload["purpose"] == "intent"
        and initial_payload["action"] == "start"
        and initial_payload["definition_sha256"] == program.definition_sha256,
    )
    receipt_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/review-program.py"),
            "receipt",
            "--input",
            str(input_paths[0]),
            "--operation-id",
            "review-intent-cli",
            "--verdict",
            "approved",
            "--result-sha256",
            SHA["result"],
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    receipt_path = directory / "intent-receipt.json"
    receipt_path.write_text(receipt_result.stdout, encoding="utf-8")
    advanced_status = subprocess.run(
        [*base_command, "--receipt", str(receipt_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    advanced_payload = json.loads(advanced_status.stdout)
    check(
        "code-owned CLI advances only after an exact typed receipt",
        advanced_payload["purpose"] == "implementation"
        and advanced_payload["receipt_count"] == 1,
    )

print("\nReview program policy tests passed.")
