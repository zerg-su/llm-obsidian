#!/usr/bin/env python3
"""Regression coverage for one coordinator-authorized profile continuation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.verification import VerificationAuthority, load_profiles  # noqa: E402
from task_escalation_records import DecisionRecord  # noqa: E402
from task_review_authorized_continuation import (  # noqa: E402
    AuthorizedContinuationError,
    run_authorized_continuation,
)
from task_review_context import _authorized_continuation_inputs  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def decision_record(
    path: Path,
    *,
    record_id: str,
    record_type: str,
    payload: dict[str, object],
) -> DecisionRecord:
    path.write_text("{}\n", encoding="utf-8")
    return DecisionRecord(record_id, record_type, payload, "a" * 64, path)


with tempfile.TemporaryDirectory(prefix="authorized-review-continuation.") as raw:
    base = Path(raw)
    worktree = base / "worktree"
    vault = base / "vault"
    worktree.mkdir()
    (vault / "wiki").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    (vault / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=worktree, check=True
    )
    (worktree / ".gitignore").write_text(".task-*\n", encoding="utf-8")
    (worktree / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "seed.txt"], cwd=worktree, check=True
    )
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=worktree, check=True)
    predecessor_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (worktree / "mechanism.txt").write_text("mechanism\n", encoding="utf-8")
    subprocess.run(["git", "add", "mechanism.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "mechanism"], cwd=worktree, check=True
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    task_id = str(uuid.uuid4())
    profile = load_profiles(vault / "config/verification-profiles.toml")["full"]
    outcome_sha256 = "6" * 64
    escalation_id = str(uuid.uuid4())
    decision = (
        "authorize-one-exact-head-full-profile-receipt-and-bind-amended-repair-contract; "
        f"create at most one immutable full-profile verification receipt for exact clean HEAD {predecessor_head}, "
        f"bind that receipt and amended Outcome Contract {outcome_sha256} into one fresh repair review continuation, "
        "and do not replay accepted phases, reviewers, providers, callbacks, or predecessor effects"
    )
    resolution = decision_record(
        base / "resolution.json",
        record_id="resolution-authorized",
        record_type="resolution",
        payload={
            "id": escalation_id,
            "status": "resolved",
            "category": "contract-drift",
            "decision": decision,
        },
    )
    successor_resolution = decision_record(
        base / "successor-resolution.json",
        record_id="resolution-successor",
        record_type="resolution",
        payload={
            "id": str(uuid.uuid4()),
            "status": "resolved",
            "category": "contract-drift",
            "decision": (
                "A: authorize one new committed mechanism HEAD in the existing "
                "task/llm-obsidian-2-8-1-concurrency-fix-sol worktree. Implement and "
                "regression-test the narrow registered full-profile continuation primitive "
                "as part of the 2.8.1 product repair; supersede "
                f"{predecessor_head} as the final candidate while preserving it as the clean "
                "proven predecessor. After the single mechanism commit, bind exactly one "
                "immutable full-profile receipt and exactly one fresh repair Deep review to "
                "the resulting exact clean HEAD and amended Outcome Contract "
                f"{outcome_sha256}. Do not replay accepted engineering phases, predecessor "
                "verification, prior reviewers/providers/callbacks, or external effects; any "
                "further product mutation requires a new exact-head receipt/review boundary."
            ),
        },
    )
    amendment = decision_record(
        base / "amendment.json",
        record_id="amendment-authorized",
        record_type="amendment",
        payload={"new_outcome_sha256": outcome_sha256},
    )
    meta = json.loads((ROOT / ".task-meta.json").read_text(encoding="utf-8"))
    meta.update(
        {
            "task_id": task_id,
            "worktree": str(worktree),
            "vault_root": str(vault),
        }
    )
    (worktree / ".task-meta.json").write_text(
        json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    store = OperationStore(vault / ".vault-meta/harness")
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "xhigh", "executor", "7" * 64
    )
    parent = store.create(
        OperationSpec(
            task_id,
            "parent-key",
            "dispatch",
            task_id,
            route,
            "packets/task.json",
            "scoped",
            contract_sha256=str(meta["pipeline_policy"]["definition_sha256"]),
            root_operation_id=task_id,
        ),
        lane_id="parent-lane",
        run_id="parent-run",
    )
    authority = SimpleNamespace(
        outcome_sha256=outcome_sha256,
        amendments=(amendment,),
    )
    command_calls: list[tuple[str, ...]] = []

    def verification_runner(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, head + "\n", "")
        command_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    review_calls: list[tuple[dict[str, object], Path]] = []

    def review_driver(
        rebound: dict[str, object],
        _vault: Path,
        _worktree: Path,
        rebound_task_id: str,
        runtime_root: Path,
        **_kwargs: object,
    ) -> dict[str, object]:
        review_calls.append((rebound, runtime_root))
        admission = json.loads(
            (runtime_root / "review-launch-admission.json").read_text(
                encoding="utf-8"
            )
        )
        assert rebound_task_id == task_id
        assert rebound["review_policy"]["verification_profile"] == "full"
        assert admission["operation_id"] == task_id
        assert admission["head_sha"] == head
        return {"status": "reviewing", "operation_id": task_id}

    with (
        mock.patch(
            "task_review_authorized_continuation._validate_task",
            return_value=(meta, vault, task_id),
        ),
        mock.patch(
            "task_review_authorized_continuation.load_chain",
            return_value=(resolution, successor_resolution),
        ),
        mock.patch(
            "task_review_authorized_continuation.resolve_plan_authority",
            return_value=authority,
        ),
    ):
        result = run_authorized_continuation(
            worktree,
            authorization_escalation_id=escalation_id,
            expected_head=head,
            verification_profile="full",
            verification_profile_sha256=profile.sha256,
            outcome_contract_sha256=outcome_sha256,
            verification_runner=verification_runner,
            review_driver=review_driver,
        )

    binding_path = worktree / ".task-review-authorized-continuation.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    receipt_path = Path(binding["receipt_pointer"])
    receipt = VerificationAuthority.load(
        receipt_path,
        store=store,
        parent=parent,
        runtime_root=Path(binding["verification_runtime_root"]),
        expected_definition_sha256=parent.spec.contract_sha256,
        expected_profile="full",
        expected_profile_sha256=profile.sha256,
        expected_head_sha=head,
        expected_command_ids=tuple(
            f"full-{index + 1}" for index in range(len(profile.commands))
        ),
        child_states=("complete",),
        require_released=True,
        require_effect_succeeded=True,
    )
    check(
        "authorized continuation issues one exact full-profile receipt",
        result["status"] == "reviewing"
        and receipt.status == "complete"
        and len(command_calls) == len(profile.commands),
    )
    check(
        "predecessor dispatch remains immutable while review uses the rebound profile",
        store.read(task_id, task_id).spec.verification_profile == "scoped"
        and len(review_calls) == 1
        and review_calls[0][0]["review_topology"]["payload"][
            "verification_profile"
        ]
        == {"name": "full", "sha256": profile.sha256},
    )
    check(
        "continuation binding is amendment and authorization exact",
        binding["authorization_escalation_id"] == escalation_id
        and binding["authorization_resolution_id"] == resolution.record_id
        and binding["successor_resolution_id"]
        == successor_resolution.record_id
        and binding["predecessor_head_sha"] == predecessor_head
        and binding["amendment_record_id"] == amendment.record_id
        and binding["outcome_contract_sha256"] == outcome_sha256,
    )
    packet_inputs = _authorized_continuation_inputs(
        review_calls[0][0], worktree, review_calls[0][1], head
    )
    check(
        "review packet exposes the immutable receipt and every command output",
        [item.name for item in packet_inputs]
        == [
            "authorized-continuation.json",
            "full-verification-receipt.json",
            *[
                f"full-verification-{index + 1}.log"
                for index in range(len(profile.commands))
            ],
        ],
    )

    wrong_head = "f" * 40
    with (
        mock.patch(
            "task_review_authorized_continuation._validate_task",
            return_value=(meta, vault, task_id),
        ),
        mock.patch(
            "task_review_authorized_continuation.load_chain",
            return_value=(resolution, successor_resolution),
        ),
        mock.patch(
            "task_review_authorized_continuation.resolve_plan_authority",
            return_value=authority,
        ),
    ):
        try:
            run_authorized_continuation(
                worktree,
                authorization_escalation_id=escalation_id,
                expected_head=wrong_head,
                verification_profile="full",
                verification_profile_sha256=profile.sha256,
                outcome_contract_sha256=outcome_sha256,
                verification_runner=verification_runner,
                review_driver=review_driver,
            )
        except AuthorizedContinuationError:
            pass
        else:
            raise AssertionError("changed HEAD reused continuation authority")
    check("changed HEAD fails before another receipt or review invocation", len(review_calls) == 1)

print("authorized task review continuation tests passed")
