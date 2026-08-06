#!/usr/bin/env python3
"""Split derives launch, terminal, and HEAD facts from owned durable state."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dispatch_setup import run_state_path  # noqa: E402
from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import (  # noqa: E402
    CallbackEnvelope,
    OperationSpec,
    RuntimeRoute,
)
from harness.split_activation import (  # noqa: E402
    split_child_policy,
    split_child_policy_payload,
)
from harness.split_evidence import SplitEvidenceStore  # noqa: E402
from harness.split_contracts import (  # noqa: E402
    ChildBudget,
    FrozenSplitBudget,
    JoinSpec,
    ParentContract,
    SplitCandidate,
    build_split_preview,
    manifest_to_dict,
)
from harness.store import OperationStore  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402


TASK_ID = "11111111-1111-4111-8111-111111111111"
BRANCH = "task/split-proof"
SUBPLAN = "whole-plan"
OWNED_PATH = "product/result.txt"
assert SplitEvidenceStore.__module__ == "harness.split_evidence"


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError((argv, result.stdout, result.stderr))
    return result.stdout.strip()


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/split-runner.py"), *argv],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


with tempfile.TemporaryDirectory(prefix="split-evidence.") as raw:
    root = Path(raw)
    vault = root / "vault"
    target_repo = root / "target"
    worktree = root / "child"
    (vault / "wiki" / "plans").mkdir(parents=True)
    (vault / "skills" / "dispatch").mkdir(parents=True)
    (vault / "config").mkdir()
    shutil.copy2(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    contract_value = {
        "schema_version": 1,
        "purpose": "Prove code-owned Split evidence.",
        "desired_outcome": "Join only exact durable child state.",
        "success_evidence": [
            {
                "evidence_id": "split-child-proof",
                "observable": "The child terminal receipt is projected.",
            }
        ],
        "non_goals": ["No provider replay."],
    }
    plan = vault / "wiki" / "plans" / "split-proof.md"
    plan.write_text(
        "---\nstatus: pending\n---\n\n# Split proof\n\n"
        "## Outcome Contract\n\n```json\n"
        + json.dumps(contract_value, sort_keys=True)
        + "\n```\n",
        encoding="utf-8",
    )
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    outcome_sha = extract_from_bytes(plan.read_bytes()).sha256
    target_repo.mkdir()
    git(target_repo, "init", "-b", "main")
    git(target_repo, "config", "user.name", "Split Test")
    git(target_repo, "config", "user.email", "split@example.invalid")
    (target_repo / "README.md").write_text("base\n", encoding="utf-8")
    git(target_repo, "add", "README.md")
    git(target_repo, "commit", "-m", "base")
    base = git(target_repo, "rev-parse", "HEAD")
    git(target_repo, "branch", "source-base", base)
    parent = ParentContract(
        plan_sha256=plan_sha,
        outcome_contract_sha256=outcome_sha,
        base_sha=base,
        evidence_ids=("split-child-proof",),
        non_goals=("No provider replay.",),
    )
    candidate = SplitCandidate(
        subplan_id=SUBPLAN,
        title="Child proof",
        pipeline="lifecycle/default",
        route_alias="task-default",
        owned_paths=(OWNED_PATH,),
        evidence_ids=parent.evidence_ids,
        dependencies=(),
        inherited_non_goals=parent.non_goals,
        budget=ChildBudget(200_000, 1_800),
        independence_proven=True,
    )
    manifest = build_split_preview(
        parent=parent,
        candidates=(candidate,),
        frozen_budget=FrozenSplitBudget(1, 1, 200_000, 1_800),
        requested_max_parallel=1,
        coordination_cost=0,
        parallel_benefit=1,
        fallback_pipeline="lifecycle/default",
        fallback_route_alias="task-default",
        join=JoinSpec(),
    ).manifest
    candidate = manifest.subplans[0]
    policy = split_child_policy_payload(split_child_policy(manifest, candidate))

    raw_dispatch = {
        "schema_version": 1,
        "request_id": TASK_ID,
        "task_name": "split-proof",
        "description": "Prove exact durable Split replay evidence.",
        "vault_root": str(vault.resolve()),
        "target_repo": str(target_repo.resolve()),
        "worktree": str(worktree.resolve()),
        "branch": BRANCH,
        "base_branch": "source-base",
        "plan_file": str(plan.resolve()),
        "origin_surface": "22222222-2222-4222-8222-222222222222",
        "origin_session": "split-runner-test",
        "session_route": {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "source": "unit-test",
        },
        "executor": {},
        "placement": "workspace",
        "pipeline": "lifecycle/default",
        "completion_policy": "attention",
        "wiki_context": [],
        "suggested_agents": [],
        "reap": {
            "type": "repo-touch",
            "title": "Split proof",
            "plan_mode": "shared",
        },
        "split": policy,
    }
    assert "base_sha" not in raw_dispatch
    spec = {
        "schema_version": 1,
        "manifest": manifest_to_dict(manifest),
        "current_parent": {
            "plan_sha256": plan_sha,
            "outcome_contract_sha256": outcome_sha,
        },
        "registered_pipelines": ["lifecycle/default"],
        "children": [{"subplan_id": SUBPLAN, "dispatch": raw_dispatch}],
    }
    spec_path = root / "activation.json"
    write_json(spec_path, spec)
    validated = run_cli("validate", "--spec", str(spec_path))
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["status"] == "valid"
    print("OK   replay fixture starts from one schema-valid public request")

    git(target_repo, "worktree", "add", "-b", BRANCH, str(worktree), "source-base")
    git(worktree, "config", "user.name", "Split Test")
    git(worktree, "config", "user.email", "split@example.invalid")

    product = worktree / OWNED_PATH
    product.parent.mkdir(parents=True)
    product.write_text("bounded result\n", encoding="utf-8")
    git(worktree, "add", OWNED_PATH)
    git(worktree, "commit", "-m", "child result")
    head = git(worktree, "rev-parse", "HEAD")

    profile = load_profiles(vault / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    summary = {
        "schema_version": 2,
        "type": "repo-touch",
        "title": "Split child proof",
        "session": "split-test-session",
        "body": "The bounded child result is complete.",
        "outcome_disposition": "achieved",
        "outcome_evidence_ids": ["split-child-proof"],
        "residual_gap_pointers": [],
    }
    summary_path = worktree / ".task-summary.json"
    write_json(summary_path, summary)
    summary_bytes = summary_path.read_bytes()
    summary_sha = hashlib.sha256(summary_bytes).hexdigest()
    payload_sha = canonical_sha256(summary)
    meta = {
        "version": 4,
        "task_id": TASK_ID,
        "worktree": str(worktree.resolve()),
        "branch": BRANCH,
        "vault_root": str(vault.resolve()),
        "base_branch": "deleted-parent-branch",
        "base_sha": base,
        "plan_file": str(plan.resolve()),
        "outcome_contract_sha256": outcome_sha,
        "split_policy": policy,
        "review_policy": {
            "mode": "skip",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 0,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile.sha256,
        },
    }
    write_json(worktree / ".task-meta.json", meta)

    harness_root = vault / ".vault-meta" / "harness"
    operation_store = OperationStore(harness_root)
    operation = OperationSpec(
        TASK_ID,
        "split-child-operation",
        "dispatch",
        TASK_ID,
        RuntimeRoute("codex", "bounded-model", "high", "executor", "a" * 64),
        "context/manifest.json",
        "scoped",
    )
    operation_store.create(operation, lane_id="split-lane", run_id="split-run")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        operation_store.transition(TASK_ID, TASK_ID, state)
    CallbackBroker(operation_store, TASK_ID).accept(
        CallbackEnvelope(
            callback_id=f"wiki-summary-{payload_sha[:24]}",
            operation_id=TASK_ID,
            run_id="split-run",
            kind="wiki-summary",
            payload=summary,
            payload_sha256=payload_sha,
        )
    )
    for state in ("exiting", "complete"):
        operation_store.transition(TASK_ID, TASK_ID, state)
    callback_id = f"wiki-summary-{payload_sha[:24]}"
    callback_receipt = {
        "schema_version": 1,
        "status": "accepted",
        "callback_id": callback_id,
        "operation_id": TASK_ID,
        "run_id": "split-run",
        "payload_sha256": payload_sha,
    }
    write_json(
        harness_root
        / "owners"
        / TASK_ID
        / "runtime"
        / TASK_ID
        / "callback-receipt.json",
        callback_receipt,
    )
    gate_root = harness_root / "review-data" / TASK_ID / TASK_ID
    write_json(
        gate_root / "review-gate.json",
        {
            "schema_version": 1,
            "dispatch_operation_id": TASK_ID,
            "owner_id": TASK_ID,
            "status": "skipped",
            "policy": {"enabled": False},
            "product_root": str(worktree.resolve()),
            "active_review_operation_id": "",
            "context": {
                "head_sha": head,
                "implementer_summary_sha256": summary_sha,
                "verification_profile": "scoped",
                "verification_profile_sha256": profile.sha256,
            },
            "fresh_reevaluation_used": False,
            "lanes": [],
            "round_results": {},
            "final_results": {},
            "resolution_evidence": {},
            "continuation_effects": {},
            "evidence": {},
        },
    )

    request_sha = canonical_sha256(raw_dispatch)
    state_path = run_state_path(vault, TASK_ID)
    write_json(
        state_path,
        {
            "schema_version": 1,
            "request_id": TASK_ID,
            "request_sha256": request_sha,
            "task_name": "split-proof",
            "status": "launched",
            "worktree": str(worktree.resolve()),
            "result": {
                "schema_version": 1,
                "status": "launched",
                "request_id": TASK_ID,
                "worktree": str(worktree.resolve()),
                "branch": BRANCH,
                "task_workspace": "workspace-child-proof",
                "task_surface": "surface-child-proof",
                "placement": "workspace",
                "harness": {
                    "owner_id": TASK_ID,
                    "operation_id": TASK_ID,
                    "lane_id": "split-lane",
                    "run_id": "split-run",
                },
            },
        },
    )

    split_root = (
        harness_root / "split-operations" / manifest.manifest_sha256
    )
    git(target_repo, "branch", "-D", "source-base")
    meta_path = worktree / ".task-meta.json"
    meta_bytes = meta_path.read_bytes()
    meta_path.unlink()
    missing_meta_recovery = run_cli("start", "--spec", str(spec_path))
    assert missing_meta_recovery.returncode == 3
    assert "cannot read exact JSON object" in missing_meta_recovery.stderr
    assert not split_root.exists()
    meta_path.write_bytes(meta_bytes)
    print("OK   recovery rejects missing child metadata before launch evidence")

    wrong_meta = json.loads(meta_bytes)
    wrong_meta["base_sha"] = "9" * 40
    write_json(meta_path, wrong_meta)
    rejected_recovery = run_cli("start", "--spec", str(spec_path))
    assert rejected_recovery.returncode == 3
    assert "recovered Split child base SHA drifted" in rejected_recovery.stderr
    assert not split_root.exists()
    meta_path.write_bytes(meta_bytes)
    print("OK   recovery rejects durable child base drift before launch evidence")

    assert not split_root.exists()
    recovered = run_cli("start", "--spec", str(spec_path))
    assert recovered.returncode == 0, recovered.stderr
    recovered_payload = json.loads(recovered.stdout)
    assert recovered_payload["disposition"] == "ready-to-join"
    assert recovered_payload["effects"] == {
        "dispatches": 0,
        "provider_calls": 0,
        "surfaces_created": 0,
        "worktrees_created": 0,
    }
    assert recovered_payload["launch_receipts"][0]["base_sha"] == base
    assert (split_root / "launches" / f"{SUBPLAN}.json").is_file()
    assert (split_root / "terminals" / f"{SUBPLAN}.json").is_file()
    print("OK   completed dispatch crash window recovers without a second launch")

    launch_stored = json.loads(
        (split_root / "launches" / f"{SUBPLAN}.json").read_text(encoding="utf-8")
    )["receipt"]
    terminal_stored = json.loads(
        (split_root / "terminals" / f"{SUBPLAN}.json").read_text(encoding="utf-8")
    )["receipt"]
    assert launch_stored["base_sha"] == base
    assert terminal_stored["child"]["base_sha"] == base
    assert terminal_stored["child"]["base_ancestor"] is True
    print("OK   launch and terminal evidence retain the sealed ancestor commit")

    launch_evidence_path = split_root / "launches" / f"{SUBPLAN}.json"
    launch_evidence_bytes = launch_evidence_path.read_bytes()
    drifted_launch = json.loads(launch_evidence_bytes)
    drifted_launch["receipt"]["base_sha"] = "9" * 40
    write_json(launch_evidence_path, drifted_launch)
    drifted_replay = run_cli("start", "--spec", str(spec_path))
    assert drifted_replay.returncode == 3
    assert "base SHA drifted" in drifted_replay.stderr
    launch_evidence_path.write_bytes(launch_evidence_bytes)
    print("OK   replay rejects launch evidence with a drifted sealed base")

    launches_path = root / "launches.json"
    terminals_path = root / "terminals.json"
    heads_path = root / "heads.json"
    write_json(
        launches_path,
        {"schema_version": 1, "receipts": recovered_payload["launch_receipts"]},
    )
    write_json(terminals_path, {"schema_version": 1, "receipts": [terminal_stored]})
    write_json(heads_path, {"schema_version": 1, "heads": {SUBPLAN: head}})
    joined = run_cli(
        "join",
        "--spec",
        str(spec_path),
        "--launch-receipts",
        str(launches_path),
        "--terminal-receipts",
        str(terminals_path),
        "--current-heads",
        str(heads_path),
    )
    assert joined.returncode == 0, joined.stderr
    assert json.loads(joined.stdout)["disposition"] == "ready"
    print("OK   public start receipt round-trips into exact Join")

    forged_launch = {**launch_stored, "workspace_id": "workspace-forged"}
    write_json(launches_path, {"schema_version": 1, "receipts": [forged_launch]})
    rejected = run_cli(
        "join",
        "--spec",
        str(spec_path),
        "--launch-receipts",
        str(launches_path),
        "--terminal-receipts",
        str(terminals_path),
        "--current-heads",
        str(heads_path),
    )
    assert rejected.returncode == 3
    assert "not authoritative" in rejected.stderr
    print("OK   caller launch receipt forgery is rejected")

    write_json(launches_path, {"schema_version": 1, "receipts": [launch_stored]})
    forged_terminal = json.loads(json.dumps(terminal_stored))
    forged_terminal["child"]["summary_sha256"] = "f" * 64
    write_json(
        terminals_path,
        {"schema_version": 1, "receipts": [forged_terminal]},
    )
    rejected = run_cli(
        "join",
        "--spec",
        str(spec_path),
        "--launch-receipts",
        str(launches_path),
        "--terminal-receipts",
        str(terminals_path),
        "--current-heads",
        str(heads_path),
    )
    assert rejected.returncode == 3
    assert "not authoritative" in rejected.stderr
    print("OK   caller terminal receipt forgery is rejected")

    write_json(terminals_path, {"schema_version": 1, "receipts": [terminal_stored]})
    write_json(
        heads_path,
        {"schema_version": 1, "heads": {SUBPLAN: "f" * 40}},
    )
    rejected = run_cli(
        "join",
        "--spec",
        str(spec_path),
        "--launch-receipts",
        str(launches_path),
        "--terminal-receipts",
        str(terminals_path),
        "--current-heads",
        str(heads_path),
    )
    assert rejected.returncode == 3
    assert "HEAD map is not authoritative" in rejected.stderr
    print("OK   caller HEAD claims are comparison-only")

    product.write_text("changed after receipt\n", encoding="utf-8")
    git(worktree, "add", OWNED_PATH)
    git(worktree, "commit", "-m", "stale terminal")
    write_json(heads_path, {"schema_version": 1, "heads": {SUBPLAN: head}})
    rejected = run_cli(
        "join",
        "--spec",
        str(spec_path),
        "--launch-receipts",
        str(launches_path),
        "--terminal-receipts",
        str(terminals_path),
        "--current-heads",
        str(heads_path),
    )
    assert rejected.returncode == 3
    assert "stale" in rejected.stderr or "changed" in rejected.stderr
    print("OK   sealed terminal evidence is revalidated against current child HEAD")

print("Split durable evidence matrix: ok")
