#!/usr/bin/env python3
"""Deterministic purpose-bound review program policy and reconciliation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

CLI_SPEC = importlib.util.spec_from_file_location(
    "review_program_cli", ROOT / "scripts/review-program.py"
)
assert CLI_SPEC and CLI_SPEC.loader
review_program_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(review_program_cli)

from harness.review_program import (  # noqa: E402
    ReviewBoundaryInput,
    ReviewBoundaryReceipt,
    ReviewProgramError,
    compile_review_program,
    reconcile_review_program,
)
from harness.review_program_authority import (  # noqa: E402
    trusted_review_receipt,
    validate_trusted_receipts,
)
from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import (  # noqa: E402
    CallbackEnvelope,
    OperationSpec,
    RuntimeRoute,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewRequest,
    review_round_payload,
)
from harness.workflows.review_gate import review_context_sha256  # noqa: E402
from harness.workflows.review_gate_contracts import (  # noqa: E402
    _result_from_payload,
)

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
AXIS_SHORT = {
    "holistic": "holistic",
    "spec": "spec",
    "standards-correctness-architecture-security": "standards",
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


def git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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
    plan_sha256=SHA["plan"],
    product_head_sha=SHA["head"],
    verification_evidence_sha256=SHA["verification"],
    verification_evidence_path="docs/verification.md",
)
release = ReviewBoundaryInput(
    purpose="release",
    outcome_contract_sha256=SHA["outcome"],
    plan_sha256=SHA["plan"],
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
    worktree = directory / "worktree"
    plan = worktree / "wiki/plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\ntype: plan\nreview_risk_profile: architecture\n---\n# Plan\n",
        encoding="utf-8",
    )
    cli_sources = {
        "docs/design.md": b"approved design\n",
        "docs/capabilities.json": b'{"approved":true}\n',
        "docs/success-evidence.md": b"success evidence\n",
        "docs/verification.md": b"verification evidence\n",
        "docs/outcome-evidence.md": b"outcome evidence\n",
        "docs/deviations.md": b"accepted deviations\n",
    }
    for relative, content in cli_sources.items():
        source = worktree / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    git(worktree, "init", "-q")
    git(worktree, "config", "user.name", "Review Program Test")
    git(worktree, "config", "user.email", "review-program@example.invalid")
    git(worktree, "add", "docs", "wiki")
    git(worktree, "commit", "-q", "-m", "review candidate")
    cli_head = git(worktree, "rev-parse", "HEAD")
    plan_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
    cli_boundaries = (
        replace(
            intent,
            plan_sha256=plan_sha256,
            design_sha256=hashlib.sha256(cli_sources["docs/design.md"]).hexdigest(),
            capability_dispositions_sha256=hashlib.sha256(
                cli_sources["docs/capabilities.json"]
            ).hexdigest(),
            success_evidence_map_sha256=hashlib.sha256(
                cli_sources["docs/success-evidence.md"]
            ).hexdigest(),
        ),
        replace(
            implementation,
            plan_sha256=plan_sha256,
            product_head_sha=cli_head,
            verification_evidence_sha256=hashlib.sha256(
                cli_sources["docs/verification.md"]
            ).hexdigest(),
        ),
        replace(
            release,
            plan_sha256=plan_sha256,
            integration_head_sha=cli_head,
            outcome_evidence_map_sha256=hashlib.sha256(
                cli_sources["docs/outcome-evidence.md"]
            ).hexdigest(),
            accepted_deviations_sha256=hashlib.sha256(
                cli_sources["docs/deviations.md"]
            ).hexdigest(),
        ),
    )
    input_paths = []
    for boundary in cli_boundaries:
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
        "--worktree",
        str(worktree),
        "--plan",
        str(plan),
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
        and initial_payload["risk_profile"] == "architecture",
    )
    cli_run_id = "review-intent-cli-run"
    cli_profile_sha256 = "5" * 64
    cli_payload = {
        "schema_version": 1,
        "operation_id": "review-intent-cli",
        "run_id": cli_run_id,
        "mode": "simple",
        "head_sha": SHA["head"][:40],
        "verification_profile": {
            "name": "scoped",
            "sha256": cli_profile_sha256,
        },
        "verdict": "approve",
        "axes": [
            {
                "axis": "holistic",
                "findings": [],
                "verdict": "approve",
                "verification_iteration": 0,
            }
        ],
        "verification_gaps": [],
        "notes_for_executor": [],
        "residual_risks": [],
    }
    cli_payload_bytes = json.dumps(
        cli_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    cli_payload_sha256 = hashlib.sha256(cli_payload_bytes).hexdigest()
    callback = {
        "schema_version": 1,
        "callback_id": f"review-{cli_payload_sha256[:24]}",
        "operation_id": "review-intent-cli",
        "run_id": cli_run_id,
        "kind": "review",
        "payload": cli_payload,
        "payload_sha256": cli_payload_sha256,
    }
    gate_root = (
        worktree
        / ".vault-meta/harness/review-data/review-intent-cli/review-intent-cli"
    )
    gate_root.mkdir(parents=True)
    callback_path = gate_root / ".review-callback.json"
    callback_bytes = (json.dumps(callback, sort_keys=True) + "\n").encode()
    callback_path.write_bytes(callback_bytes)
    (gate_root / "final-holistic.json").write_text(
        '{"axis":"holistic","findings":[],"verdict":"approve",'
        '"verification_iteration":0}\n',
        encoding="utf-8",
    )
    cli_lane_id = hashlib.sha256(b"review-intent-cli:lane").hexdigest()[:32]
    cli_parent_run_id = hashlib.sha256(b"review-intent-cli:run").hexdigest()[:32]
    cli_route = RuntimeRoute(
        "codex", "gpt-5.6-terra", "medium", "reviewer-callback", "6" * 64
    )
    cli_parent_spec = OperationSpec(
        "review-intent-cli",
        hashlib.sha256(b"review-intent-cli:parent").hexdigest(),
        "simple-review-holistic",
        "review-intent-cli",
        cli_route,
        "packets/review/manifest.json",
        "scoped",
    )
    cli_store = OperationStore(worktree / ".vault-meta/harness")
    cli_store.create(
        cli_parent_spec,
        lane_id=cli_lane_id,
        run_id=cli_parent_run_id,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        cli_store.transition("review-intent-cli", "review-intent-cli", state)
    cli_role = "round-0"
    cli_suffix = f"-round-{hashlib.sha256(cli_role.encode()).hexdigest()[:8]}"
    cli_child_id = f"{'review-intent-cli'[: 128 - len(cli_suffix)]}{cli_suffix}"
    cli_child_key = hashlib.sha256(
        (
            f"{cli_parent_spec.idempotency_key}:holistic:{cli_role}:"
            f"{cli_child_id}"
        ).encode()
    ).hexdigest()
    cli_child_run_id = hashlib.sha256(
        f"{cli_child_key}:run".encode()
    ).hexdigest()[:32]
    cli_child_spec = OperationSpec(
        cli_child_id,
        cli_child_key,
        "review-round",
        "review-intent-cli",
        cli_route,
        "packets/review/manifest.json",
        "scoped",
    )
    cli_store.create(
        cli_child_spec,
        lane_id=cli_lane_id,
        run_id=cli_child_run_id,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        cli_store.transition("review-intent-cli", cli_child_id, state)
    cli_round = _result_from_payload(
        json.loads((gate_root / "final-holistic.json").read_text(encoding="utf-8"))
    )
    cli_round_payload = review_round_payload("review-intent-cli", cli_round)
    cli_round_bytes = json.dumps(
        cli_round_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    cli_round_digest = hashlib.sha256(cli_round_bytes).hexdigest()
    CallbackBroker(cli_store, "review-intent-cli").accept(
        CallbackEnvelope(
            f"review-{cli_round_digest[:24]}",
            cli_child_id,
            cli_child_run_id,
            "review",
            cli_round_payload,
            cli_round_digest,
        )
    )
    for state in ("finalizing", "exiting", "complete"):
        cli_store.transition("review-intent-cli", cli_child_id, state)
    for state in ("finalizing", "exiting", "complete"):
        cli_store.transition("review-intent-cli", "review-intent-cli", state)
    callback_sha256 = hashlib.sha256(callback_bytes).hexdigest()
    (gate_root / "review-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "approved",
                "active_review_operation_id": "review-intent-cli",
                "dispatch_operation_id": "review-intent-cli",
                "product_root": str(worktree.resolve()),
                "context": {
                    "purpose": "intent",
                    "boundary_input_sha256": cli_boundaries[0].input_sha256,
                    "head_sha": SHA["head"][:40],
                    "verification_profile": "scoped",
                    "verification_profile_sha256": cli_profile_sha256,
                },
                "policy": {"depth": "simple", "purpose": "intent"},
                "evidence": {
                    "operation_id": "review-intent-cli",
                    "run_id": cli_run_id,
                    "pointer": ".review-callback.json",
                    "sha256": callback_sha256,
                },
                "final_results": {"holistic": "final-holistic.json"},
                "lanes": [
                    {
                        "axis": "holistic",
                        "checkpoint": "checkpoint-holistic",
                        "lane_id": cli_lane_id,
                        "operation_id": "review-intent-cli",
                        "run_id": cli_parent_run_id,
                        "state": "complete",
                        "surface_id": "",
                        "verification_iteration": 0,
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/review-program.py"),
            "receipt",
            "--worktree",
            str(worktree),
            "--input",
            str(input_paths[0]),
            "--operation-id",
            "review-intent-cli",
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
    fabricated_path = directory / "fabricated-receipt.json"
    fabricated_path.write_text(
        json.dumps(
            {
                **json.loads(receipt_result.stdout),
                "operation_id": "fabricated-review",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    fabricated = subprocess.run(
        [*base_command, "--receipt", str(fabricated_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check(
        "fabricated receipt cannot advance the trusted review program",
        fabricated.returncode == 3 and "trusted review gate" in fabricated.stderr,
    )
    downgraded = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/review-program.py"),
            "status",
            "--worktree",
            str(worktree),
            "--plan",
            str(plan),
            "--input",
            str(input_paths[1]),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check(
        "approved architecture risk cannot be caller-downgraded",
        downgraded.returncode == 3
        and "missing, duplicated, or out of order" in downgraded.stderr,
    )
    gate_path = gate_root / "review-gate.json"
    nonterminal_gate = json.loads(gate_path.read_text(encoding="utf-8"))
    nonterminal_gate["status"] = "reviewing"
    gate_path.write_text(
        json.dumps(nonterminal_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    receipt_command = [
        sys.executable,
        str(ROOT / "scripts/review-program.py"),
        "receipt",
        "--worktree",
        str(worktree),
        "--input",
        str(input_paths[0]),
        "--operation-id",
        "review-intent-cli",
    ]
    nonterminal = subprocess.run(
        receipt_command, cwd=ROOT, capture_output=True, text=True
    )
    check(
        "non-terminal gate cannot mint a review receipt",
        nonterminal.returncode == 3 and "not terminal" in nonterminal.stderr,
    )
    nonterminal_gate["status"] = "approved"
    gate_path.write_text(
        json.dumps(nonterminal_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    callback_path.write_bytes(callback_bytes + b" ")
    tampered = subprocess.run(
        receipt_command, cwd=ROOT, capture_output=True, text=True
    )
    check(
        "tampered terminal result bytes cannot mint a review receipt",
        tampered.returncode == 3 and "digest is stale" in tampered.stderr,
    )

def write_approved_gate(
    worktree: Path,
    boundary: ReviewBoundaryInput,
    operation_id: str,
    *,
    resolved_head: str = "",
    mode: str = "simple",
    valid_resolution_proof: bool = True,
    accepted_rounds: bool = True,
) -> None:
    gate_root = (
        worktree
        / ".vault-meta/harness/review-data"
        / operation_id
        / operation_id
    )
    gate_root.mkdir(parents=True)
    expected_head = boundary.product_head_sha or boundary.integration_head_sha
    terminal_head = resolved_head or expected_head or git(
        worktree, "rev-parse", "HEAD"
    )
    run_id = "trusted-review-run"
    profile_sha256 = "5" * 64
    axes = (
        ("holistic",)
        if mode == "simple"
        else ("spec", "standards-correctness-architecture-security")
    )
    terminal_iteration = 1 if resolved_head and valid_resolution_proof else 0
    parent_ids = {
        axis: (
            operation_id
            if axis == "holistic"
            else f"{operation_id[:96]}-{_short}"
        )
        for axis, _short in (
            ("holistic", "holistic"),
            ("spec", "spec"),
            ("standards-correctness-architecture-security", "standards"),
        )
        if axis in axes
    }
    lane_ids = {
        axis: hashlib.sha256(f"{parent_ids[axis]}:lane".encode()).hexdigest()[:32]
        for axis in axes
    }
    parent_run_ids = {
        axis: hashlib.sha256(f"{parent_ids[axis]}:run".encode()).hexdigest()[:32]
        for axis in axes
    }
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "run_id": run_id,
        "mode": mode,
        "head_sha": terminal_head,
        "verification_profile": {
            "name": "scoped",
            "sha256": profile_sha256,
        },
        "verdict": "approve",
        "axes": [
            {
                "axis": axis,
                "findings": [],
                "verdict": "approve",
                "verification_iteration": terminal_iteration,
            }
            for axis in axes
        ],
        "verification_gaps": [],
        "notes_for_executor": [],
        "residual_risks": [],
    }
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    callback = {
        "schema_version": 1,
        "callback_id": f"review-{payload_sha256[:24]}",
        "operation_id": operation_id,
        "run_id": run_id,
        "kind": "review",
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    callback_bytes = (json.dumps(callback, sort_keys=True) + "\n").encode()
    (gate_root / ".review-callback.json").write_bytes(callback_bytes)
    final_results = {}
    for axis in axes:
        short = "standards" if axis.startswith("standards-") else axis
        pointer = f"final-{short}.json"
        (gate_root / pointer).write_text(
            json.dumps(
                {
                    "axis": axis,
                    "findings": [],
                    "verdict": "approve",
                    "verification_iteration": terminal_iteration,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        final_results[axis] = pointer
    (gate_root / "review-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "approved",
                "active_review_operation_id": operation_id,
                "dispatch_operation_id": operation_id,
                "product_root": str(worktree.resolve()),
                "context": {
                    "purpose": boundary.purpose,
                    "boundary_input_sha256": boundary.input_sha256,
                    "head_sha": terminal_head,
                    "verification_profile": "scoped",
                    "verification_profile_sha256": profile_sha256,
                },
                "policy": {
                    "depth": mode,
                    "purpose": boundary.purpose,
                },
                "evidence": {
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "pointer": ".review-callback.json",
                    "sha256": hashlib.sha256(callback_bytes).hexdigest(),
                },
                "final_results": final_results,
                "lanes": [
                    {
                        "axis": axis,
                        "checkpoint": f"checkpoint-{_short}",
                        "lane_id": lane_ids[axis],
                        "operation_id": parent_ids[axis],
                        "run_id": parent_run_ids[axis],
                        "state": "complete",
                        "surface_id": "",
                        "verification_iteration": terminal_iteration,
                    }
                    for axis, _short in (
                        ("holistic", "holistic"),
                        ("spec", "spec"),
                        ("standards-correctness-architecture-security", "standards"),
                    )
                    if axis in axes
                ],
                "resolution_evidence": (
                    {"holistic:0": f"{operation_id}/resolution-holistic-0.json"}
                    if resolved_head
                    else {}
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if resolved_head:
        finding_id = "authority-terminal-head-rebind"
        fix_delta = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "diff",
                "--binary",
                "--no-ext-diff",
                expected_head,
                resolved_head,
                "--",
            ],
            check=True,
            capture_output=True,
        ).stdout
        material_ids = [finding_id] if valid_resolution_proof else []
        resolutions = (
            [
                {
                    "disposition": "applied",
                    "finding_id": finding_id,
                    "follow_up": "",
                    "rationale": "The exact terminal HEAD was verified after the material finding was resolved.",
                }
            ]
            if valid_resolution_proof
            else []
        )
        resolution = {
            "schema_version": 1,
            "operation_id": operation_id,
            "axis": "holistic",
            "reviewed_head_sha": expected_head,
            "resolved_head_sha": resolved_head,
            "fix_delta_sha256": (
                hashlib.sha256(fix_delta).hexdigest()
                if valid_resolution_proof
                else "4" * 64
            ),
            "previous_finding_ids": material_ids,
            "resolutions": resolutions,
        }
        resolution_bytes = (
            json.dumps(resolution, sort_keys=True) + "\n"
        ).encode()
        resolution_path = gate_root / operation_id / "resolution-holistic-0.json"
        resolution_path.parent.mkdir()
        resolution_path.write_bytes(resolution_bytes)
        if valid_resolution_proof:
            (gate_root / operation_id / "round-holistic-0.json").write_text(
                json.dumps(
                    {
                        "axis": "holistic",
                        "findings": [
                            {
                                "axis": "holistic",
                                "evidence": "The terminal HEAD differs from the reviewed boundary HEAD.",
                                "file": "product.py",
                                "finding_id": finding_id,
                                "line": 1,
                                "recommendation": "Resolve and verify the exact new HEAD.",
                                "severity": "important",
                                "summary": "The changed HEAD requires same-session verification.",
                            }
                        ],
                        "verdict": "changes-requested",
                        "verification_iteration": 0,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        (gate_root / ".review-meta.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "worktree": str(worktree.resolve()),
                    "head_sha": resolved_head,
                    "review_boundary_input_sha256": boundary.input_sha256,
                    "resolution_evidence": [
                        {
                            "pointer": f"{operation_id}/resolution-holistic-0.json",
                            "sha256": hashlib.sha256(resolution_bytes).hexdigest(),
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    if accepted_rounds and (not resolved_head or valid_resolution_proof):
        store = OperationStore(worktree / ".vault-meta/harness")
        route = RuntimeRoute(
            "codex", "gpt-5.6-terra", "medium", "reviewer-callback", "6" * 64
        )
        result_root = gate_root / operation_id
        for axis in axes:
            parent_id = parent_ids[axis]
            parent_spec = OperationSpec(
                parent_id,
                hashlib.sha256(f"{parent_id}:parent".encode()).hexdigest(),
                {
                    "holistic": "simple-review-holistic",
                    "spec": "deep-review-spec",
                    "standards-correctness-architecture-security": (
                        "deep-review-correctness"
                    ),
                }[axis],
                operation_id,
                route,
                "packets/review/manifest.json",
                "scoped",
            )
            store.create(
                parent_spec,
                lane_id=lane_ids[axis],
                run_id=parent_run_ids[axis],
            )
            for state in ("preflight", "starting", "running", "awaiting-callback"):
                store.transition(operation_id, parent_id, state)
            rounds = [
                (
                    terminal_iteration,
                    gate_root / f"final-{AXIS_SHORT[axis]}.json",
                )
            ]
            if resolved_head and valid_resolution_proof:
                rounds.insert(
                    0,
                    (
                        0,
                        result_root / f"round-{AXIS_SHORT[axis]}-0.json",
                    ),
                )
            for iteration, result_path in rounds:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                role = f"round-{iteration}"
                suffix = f"-round-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
                child_id = f"{parent_id[: 128 - len(suffix)]}{suffix}"
                child_key = hashlib.sha256(
                    (
                        f"{parent_spec.idempotency_key}:{axis}:{role}:"
                        f"{child_id}"
                    ).encode()
                ).hexdigest()
                child_spec = OperationSpec(
                    child_id,
                    child_key,
                    "review-round",
                    operation_id,
                    route,
                    "packets/review/manifest.json",
                    "scoped",
                )
                child_run_id = hashlib.sha256(
                    f"{child_key}:run".encode()
                ).hexdigest()[:32]
                store.create(child_spec, lane_id=lane_ids[axis], run_id=child_run_id)
                for state in ("preflight", "starting", "running", "awaiting-callback"):
                    store.transition(operation_id, child_id, state)
                payload = review_round_payload(
                    parent_id, _result_from_payload(result)
                )
                encoded = json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ).encode()
                digest = hashlib.sha256(encoded).hexdigest()
                CallbackBroker(store, operation_id).accept(
                    CallbackEnvelope(
                        f"review-{digest[:24]}",
                        child_id,
                        child_run_id,
                        "review",
                        payload,
                        digest,
                    )
                )
                child = store.read(operation_id, child_id)
                if child.state == "verifying":
                    store.transition(operation_id, child_id, "finalizing")
                store.transition(operation_id, child_id, "exiting")
                store.transition(operation_id, child_id, "complete")
            store.transition(operation_id, parent_id, "finalizing")
            store.transition(operation_id, parent_id, "exiting")
            store.transition(operation_id, parent_id, "complete")


with tempfile.TemporaryDirectory(prefix="review-program-authority.") as raw:
    directory = Path(raw)
    worktree = directory / "worktree"
    worktree.mkdir()
    sources = {
        "wiki/plan.md": b"approved plan\n",
        "docs/design.md": b"approved design\n",
        "docs/capabilities.json": b'{"approved":true}\n',
        "docs/success-evidence.md": b"success evidence\n",
        "docs/verification.md": b"verification evidence\n",
        "docs/outcome-evidence.md": b"outcome evidence\n",
        "docs/deviations.md": b"accepted deviations\n",
    }
    for relative, content in sources.items():
        source = worktree / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    git(worktree, "init", "-q")
    git(worktree, "config", "user.name", "Review Program Test")
    git(worktree, "config", "user.email", "review-program@example.invalid")
    git(worktree, "add", "docs", "wiki")
    git(worktree, "commit", "-q", "-m", "candidate A")
    head_a = git(worktree, "rev-parse", "HEAD")

    def source_digest(relative: str) -> str:
        return hashlib.sha256((worktree / relative).read_bytes()).hexdigest()

    plan_digest = source_digest("wiki/plan.md")
    authority_boundaries = {
        "intent": ReviewBoundaryInput(
            purpose="intent",
            outcome_contract_sha256=SHA["outcome"],
            plan_sha256=plan_digest,
            design_sha256=source_digest("docs/design.md"),
            design_path="docs/design.md",
            capability_dispositions_sha256=source_digest("docs/capabilities.json"),
            capability_dispositions_path="docs/capabilities.json",
            success_evidence_map_sha256=source_digest("docs/success-evidence.md"),
            success_evidence_map_path="docs/success-evidence.md",
        ),
        "implementation": ReviewBoundaryInput(
            purpose="implementation",
            outcome_contract_sha256=SHA["outcome"],
            plan_sha256=plan_digest,
            product_head_sha=head_a,
            verification_evidence_sha256=source_digest("docs/verification.md"),
            verification_evidence_path="docs/verification.md",
        ),
        "release": ReviewBoundaryInput(
            purpose="release",
            outcome_contract_sha256=SHA["outcome"],
            plan_sha256=plan_digest,
            integration_head_sha=head_a,
            outcome_evidence_map_sha256=source_digest("docs/outcome-evidence.md"),
            outcome_evidence_map_path="docs/outcome-evidence.md",
            accepted_deviations_sha256=source_digest("docs/deviations.md"),
            accepted_deviations_path="docs/deviations.md",
        ),
    }
    operations = {
        purpose: f"review-authority-{purpose}"
        for purpose in authority_boundaries
    }
    authority_receipts = {}
    for purpose, boundary in authority_boundaries.items():
        write_approved_gate(worktree, boundary, operations[purpose])
        authority_receipts[purpose] = trusted_review_receipt(
            worktree, boundary, operations[purpose]
        )

    for purpose, boundary in authority_boundaries.items():
        for mode in ("simple", "deep"):
            operation = f"review-authority-{purpose}-{mode}-synthetic-same-head"
            write_approved_gate(
                worktree,
                boundary,
                operation,
                mode=mode,
                accepted_rounds=False,
            )
            rejected(
                f"synthetic same-HEAD {mode} {purpose} approval without accepted callbacks fails closed",
                lambda boundary=boundary, operation=operation: trusted_review_receipt(
                    worktree,
                    boundary,
                    operation,
                ),
            )

    deep_operation = "review-authority-deep-implementation"
    deep_boundary = authority_boundaries["implementation"]
    write_approved_gate(
        worktree,
        deep_boundary,
        deep_operation,
        mode="deep",
    )
    deep_root = (
        worktree
        / ".vault-meta/harness/review-data"
        / deep_operation
        / deep_operation
    )
    deep_gate_path = deep_root / "review-gate.json"
    deep_callback_path = deep_root / ".review-callback.json"
    original_gate = deep_gate_path.read_bytes()
    original_callback = deep_callback_path.read_bytes()

    missing_axis = json.loads(original_gate)
    missing_axis["final_results"].pop(
        "standards-correctness-architecture-security"
    )
    deep_gate_path.write_text(
        json.dumps(missing_axis, sort_keys=True) + "\n", encoding="utf-8"
    )
    rejected(
        "trusted deep approval rejects a missing final axis",
        lambda: trusted_review_receipt(
            worktree, deep_boundary, deep_operation
        ),
    )
    deep_gate_path.write_bytes(original_gate)

    for label, mutate in (
        ("truncated callback envelope", lambda raw: raw.pop("callback_id")),
        ("wrong callback run", lambda raw: raw.__setitem__("run_id", "wrong-run")),
        ("wrong callback kind", lambda raw: raw.__setitem__("kind", "result")),
        (
            "mismatched payload digest",
            lambda raw: raw.__setitem__("payload_sha256", "f" * 64),
        ),
    ):
        callback_value = json.loads(original_callback)
        mutate(callback_value)
        callback_bytes = (
            json.dumps(callback_value, sort_keys=True) + "\n"
        ).encode()
        deep_callback_path.write_bytes(callback_bytes)
        gate_value = json.loads(original_gate)
        gate_value["evidence"]["sha256"] = hashlib.sha256(
            callback_bytes
        ).hexdigest()
        if label == "wrong callback run":
            gate_value["evidence"]["run_id"] = "wrong-run"
        deep_gate_path.write_text(
            json.dumps(gate_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rejected(
            f"trusted approval rejects a {label}",
            lambda: trusted_review_receipt(
                worktree, deep_boundary, deep_operation
            ),
        )
        deep_callback_path.write_bytes(original_callback)
        deep_gate_path.write_bytes(original_gate)

    bound_sources = (
        ("intent design", "intent", "design_path"),
        ("intent capability dispositions", "intent", "capability_dispositions_path"),
        ("intent success evidence", "intent", "success_evidence_map_path"),
        ("implementation verification evidence", "implementation", "verification_evidence_path"),
        ("release outcome evidence", "release", "outcome_evidence_map_path"),
        ("release accepted deviations", "release", "accepted_deviations_path"),
    )
    for label, purpose, path_field in bound_sources:
        boundary = authority_boundaries[purpose]
        source = worktree / str(getattr(boundary, path_field))
        original = source.read_bytes()
        source.write_bytes(original + b"drift")
        rejected(
            f"{label} drift makes trusted authority stale",
            lambda boundary=boundary, purpose=purpose: trusted_review_receipt(
                worktree, boundary, operations[purpose]
            ),
        )
        source.write_bytes(original)

        outside = directory / f"outside-{path_field}"
        outside.write_bytes(original)
        source.unlink()
        source.symlink_to(outside)
        rejected(
            f"{label} symlink escape fails closed",
            lambda boundary=boundary, purpose=purpose: trusted_review_receipt(
                worktree, boundary, operations[purpose]
            ),
        )
        source.unlink()
        source.write_bytes(original)

    validate_trusted_receipts(
        worktree,
        (authority_boundaries["intent"], authority_boundaries["implementation"]),
        (authority_receipts["intent"],),
    )
    check(
        "intent receipt accepts the exact immediate implementation HEAD",
        git(worktree, "rev-parse", "HEAD") == head_a,
    )
    verification_source = worktree / "docs/verification.md"
    verification_bytes = verification_source.read_bytes()
    verification_source.write_bytes(verification_bytes + b"drift")
    rejected(
        "intent receipt rejects drifted next implementation evidence",
        lambda: validate_trusted_receipts(
            worktree,
            (
                authority_boundaries["intent"],
                authority_boundaries["implementation"],
            ),
            (authority_receipts["intent"],),
        ),
    )
    verification_source.write_bytes(verification_bytes)

    (worktree / "later-stage.txt").write_text("release stage\n", encoding="utf-8")
    git(worktree, "add", "later-stage.txt")
    git(worktree, "commit", "-q", "-m", "candidate B")
    head_b = git(worktree, "rev-parse", "HEAD")
    rejected(
        "intent receipt rejects a mismatched next implementation HEAD",
        lambda: validate_trusted_receipts(
            worktree,
            (
                authority_boundaries["intent"],
                authority_boundaries["implementation"],
            ),
            (authority_receipts["intent"],),
        ),
    )
    rejected(
        "actual candidate HEAD mismatch blocks implementation receipt minting",
        lambda: trusted_review_receipt(
            worktree,
            authority_boundaries["implementation"],
            operations["implementation"],
        ),
    )

    forged_operation = "review-authority-implementation-forged-resolution"
    write_approved_gate(
        worktree,
        authority_boundaries["implementation"],
        forged_operation,
        resolved_head=head_b,
        valid_resolution_proof=False,
    )
    rejected(
        "empty post-approval resolution injection cannot rebind implementation authority",
        lambda: trusted_review_receipt(
            worktree,
            authority_boundaries["implementation"],
            forged_operation,
        ),
    )

    synthetic_operation = "review-authority-implementation-synthetic-history"
    write_approved_gate(
        worktree,
        authority_boundaries["implementation"],
        synthetic_operation,
        resolved_head=head_b,
        accepted_rounds=False,
    )
    rejected(
        "synthetic round history without accepted callbacks cannot rebind authority",
        lambda: trusted_review_receipt(
            worktree,
            authority_boundaries["implementation"],
            synthetic_operation,
        ),
    )

    resolved_operation = "review-authority-implementation-resolved"
    write_approved_gate(
        worktree,
        authority_boundaries["implementation"],
        resolved_operation,
        resolved_head=head_b,
    )
    resolved_receipt = trusted_review_receipt(
        worktree,
        authority_boundaries["implementation"],
        resolved_operation,
    )
    check(
        "trusted resolution evidence authorizes the exact terminal implementation HEAD",
        resolved_receipt.verdict == "approved",
    )
    resolved_gate_root = (
        worktree
        / ".vault-meta/harness/review-data"
        / resolved_operation
        / resolved_operation
    )
    resolved_meta_path = resolved_gate_root / ".review-meta.json"
    resolved_meta = json.loads(resolved_meta_path.read_text(encoding="utf-8"))
    resolved_pointer = resolved_meta["resolution_evidence"][0]["pointer"]
    resolved_evidence_path = resolved_gate_root / resolved_pointer
    resolved_evidence_bytes = resolved_evidence_path.read_bytes()
    wrong_delta = json.loads(resolved_evidence_bytes)
    wrong_delta["fix_delta_sha256"] = "4" * 64
    wrong_delta_bytes = (json.dumps(wrong_delta, sort_keys=True) + "\n").encode()
    resolved_evidence_path.write_bytes(wrong_delta_bytes)
    resolved_meta["resolution_evidence"][0]["sha256"] = hashlib.sha256(
        wrong_delta_bytes
    ).hexdigest()
    resolved_meta_path.write_text(
        json.dumps(resolved_meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    rejected(
        "arbitrary fix-delta digest cannot rebind implementation authority",
        lambda: trusted_review_receipt(
            worktree,
            authority_boundaries["implementation"],
            resolved_operation,
        ),
    )
    resolved_evidence_path.write_bytes(resolved_evidence_bytes)
    resolved_meta["resolution_evidence"][0]["sha256"] = hashlib.sha256(
        resolved_evidence_bytes
    ).hexdigest()
    resolved_meta_path.write_text(
        json.dumps(resolved_meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    original_digest = resolved_meta["resolution_evidence"][0]["sha256"]
    resolved_meta["resolution_evidence"][0]["sha256"] = "5" * 64
    resolved_meta_path.write_text(
        json.dumps(resolved_meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    rejected(
        "tampered resolution evidence cannot rebind an implementation receipt",
        lambda: trusted_review_receipt(
            worktree,
            authority_boundaries["implementation"],
            resolved_operation,
        ),
    )
    resolved_meta["resolution_evidence"][0]["sha256"] = original_digest
    resolved_meta_path.write_text(
        json.dumps(resolved_meta, sort_keys=True) + "\n", encoding="utf-8"
    )

    later_release = replace(
        authority_boundaries["release"], integration_head_sha=head_b
    )
    later_release_operation = "review-authority-release-later"
    write_approved_gate(worktree, later_release, later_release_operation)
    later_release_receipt = trusted_review_receipt(
        worktree, later_release, later_release_operation
    )
    validate_trusted_receipts(
        worktree,
        (authority_boundaries["implementation"], later_release),
        (authority_receipts["implementation"], later_release_receipt),
    )
    check(
        "an explicit later release boundary authorizes its exact newer HEAD",
        git(worktree, "rev-parse", "HEAD") == head_b,
    )

    (worktree / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")
    git(worktree, "add", "unreviewed.txt")
    git(worktree, "commit", "-q", "-m", "candidate C")
    rejected(
        "actual candidate HEAD mismatch blocks release completion",
        lambda: validate_trusted_receipts(
            worktree,
            (authority_boundaries["implementation"], later_release),
            (authority_receipts["implementation"], later_release_receipt),
        ),
    )

print("\nReview program policy tests passed.")
