#!/usr/bin/env python3
"""Regression coverage for one coordinator-authorized profile continuation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RESUBMIT = ROOT / "scripts" / "pipeline-verification-resubmit.py"
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


def load_resubmit_module():
    """Validate published packets with the real standard packet consumer."""

    spec = importlib.util.spec_from_file_location(
        "pipeline_verification_resubmit", RESUBMIT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    resubmit = load_resubmit_module()
    (worktree / "handoff.txt").write_text("handoff\n", encoding="utf-8")
    subprocess.run(["git", "add", "handoff.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "handoff"], cwd=worktree, check=True)
    failed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    failed_escalation_id = str(uuid.uuid4())
    failed_resolution = decision_record(
        base / "failed-resolution.json",
        record_id="resolution-failed-authorized",
        record_type="resolution",
        payload={
            "id": failed_escalation_id,
            "status": "resolved",
            "category": "contract-drift",
            "decision": (
                "authorize-one-exact-head-full-profile-receipt-and-bind-amended-repair-contract; "
                "create at most one immutable full-profile verification receipt for exact clean HEAD "
                f"{head}, bind that receipt and amended Outcome Contract {outcome_sha256} "
                "into one fresh repair review continuation, and do not replay accepted phases, "
                "reviewers, providers, callbacks, or predecessor effects"
            ),
        },
    )
    failed_successor = decision_record(
        base / "failed-successor.json",
        record_id="resolution-failed-successor",
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
                f"{head} as the final candidate while preserving it as the clean "
                "proven predecessor. After the single mechanism commit, bind exactly one "
                "immutable full-profile receipt and exactly one fresh repair Deep review to "
                "the resulting exact clean HEAD and amended Outcome Contract "
                f"{outcome_sha256}. Do not replay accepted engineering phases, predecessor "
                "verification, prior reviewers/providers/callbacks, or external effects; any "
                "further product mutation requires a new exact-head receipt/review boundary."
            ),
        },
    )
    failed_calls: list[tuple[str, ...]] = []

    def failing_runner(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, failed_head + "\n", "")
        failed_calls.append(tuple(argv))
        return subprocess.CompletedProcess(
            argv, 1 if len(failed_calls) == 3 else 0, "profile output\n", ""
        )

    def failed_continuation() -> AuthorizedContinuationError:
        with (
            mock.patch(
                "task_review_authorized_continuation._validate_task",
                return_value=(meta, vault, task_id),
            ),
            mock.patch(
                "task_review_authorized_continuation.load_chain",
                return_value=(failed_resolution, failed_successor),
            ),
            mock.patch(
                "task_review_authorized_continuation.resolve_plan_authority",
                return_value=authority,
            ),
        ):
            try:
                run_authorized_continuation(
                    worktree,
                    authorization_escalation_id=failed_escalation_id,
                    expected_head=failed_head,
                    verification_profile="full",
                    verification_profile_sha256=profile.sha256,
                    outcome_contract_sha256=outcome_sha256,
                    verification_runner=failing_runner,
                    review_driver=review_driver,
                )
            except AuthorizedContinuationError as exc:
                return exc
        raise AssertionError("failed full-profile receipt continued the review")

    worktree.chmod(0o755)
    first_failure = failed_continuation()
    packet_path = worktree / ".task-verification.json"
    packet, _canonical = resubmit._read_packet(packet_path)
    failed_receipt = Path(str(packet["receipt_pointer"]))
    check(
        "durable failed receipt publishes the standard identity-bound attention packet",
        packet["operation_id"] == task_id
        and packet["head_sha"] == failed_head
        and packet["allowed_responses"] == ["escalate"]
        and [row["command_id"] for row in packet["evidence"]]
        == ["full-1", "full-2", "full-3"]
        and packet["evidence"][-1]["exit_code"] == 1
        and all(
            Path(str(row["output_pointer"])).is_file()
            for row in packet["evidence"]
        )
        and failed_receipt.is_file()
        and json.loads(failed_receipt.read_text(encoding="utf-8"))["status"]
        == "failed"
        and store.read(
            task_id, str(packet["verification_operation_id"])
        ).state
        == "failed"
        and len(review_calls) == 1,
    )
    published = packet_path.read_bytes()
    failed_calls.clear()
    failed_continuation()
    check(
        "repeated failed consumption republishes one packet and runs zero commands",
        packet_path.read_bytes() == published
        and failed_calls == []
        and len(review_calls) == 1
        and json.loads(binding_path.read_text(encoding="utf-8")) == binding,
    )
    check(
        "the published file is exactly the bytes the size guard measures",
        published
        == (
            json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        and len(published) <= 65_536,
    )
    check(
        "publishing the worktree packet leaves the checkout root mode alone",
        worktree.stat().st_mode & 0o777 == 0o755
        and packet_path.stat().st_mode & 0o777 == 0o600,
    )
    check(
        "the refusal points the coordinator at the published packet",
        str(packet_path) in str(first_failure)
        and store.read(
            task_id, str(packet["verification_operation_id"])
        ).state
        == "failed",
    )
    foreign = dict(packet)
    foreign["verification_operation_id"] = "foreign-verify-0123456789abcdef"
    foreign_bytes = (
        json.dumps(foreign, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    packet_path.write_bytes(foreign_bytes)
    foreign_failure = failed_continuation()
    check(
        "a foreign attention packet is refused instead of being replaced",
        packet_path.read_bytes() == foreign_bytes
        and "did not derive" in str(foreign_failure)
        and failed_calls == []
        and len(review_calls) == 1,
    )

    # --- publication invariants and every refusal branch -------------------
    import errno
    import os
    import stat

    import task_review_authorized_continuation as continuation

    def publication_window_states(target: Path) -> list[tuple[bool, int]]:
        """Record how the polled packet path looks mid-publication."""

        observed: list[tuple[bool, int]] = []
        real_link = os.link

        def watched_link(src, dst, **kwargs):
            observed.append(
                (
                    Path(dst).exists(),
                    Path(dst).stat().st_size if Path(dst).exists() else -1,
                )
            )
            return real_link(src, dst, **kwargs)

        packet_path.unlink(missing_ok=True)
        with mock.patch.object(os, "link", watched_link):
            failed_continuation()
        return observed

    packet_path.unlink(missing_ok=True)
    window = publication_window_states(packet_path)
    republished = packet_path.read_bytes()
    check(
        "the polled packet path never exists before its complete publication",
        window == [(False, -1)] and json.loads(republished) == packet,
    )

    interrupted: list[str] = []
    real_fdopen = os.fdopen

    def interrupting_fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        interrupted.append("staged")
        raise KeyboardInterrupt("publication interrupted")

    packet_path.unlink(missing_ok=True)
    with mock.patch.object(os, "fdopen", interrupting_fdopen):
        try:
            failed_continuation()
        except KeyboardInterrupt:
            pass
    leftovers = sorted(
        item.name
        for item in worktree.iterdir()
        if item.name.startswith(".task-verification")
    )
    check(
        "an interrupt mid-publication leaves no packet to wedge the handoff",
        interrupted == ["staged"]
        and not packet_path.exists()
        and leftovers == [],
    )

    failed_continuation()
    intact = packet_path.read_bytes()
    changed = dict(packet)
    changed["safe_boundary"] = "tampered"
    packet_path.write_bytes(
        (json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    tampered_bytes = packet_path.read_bytes()
    changed_failure = failed_continuation()
    check(
        "a changed same-identity packet is refused and left untouched",
        "attention packet changed" in str(changed_failure)
        and packet_path.read_bytes() == tampered_bytes,
    )

    packet_path.unlink()
    packet_path.write_bytes(b"")
    empty_failure = failed_continuation()
    check(
        "a zero-byte packet is refused as unreadable rather than replaced",
        "unreadable" in str(empty_failure)
        and packet_path.read_bytes() == b"",
    )

    packet_path.unlink()
    packet_path.symlink_to(worktree / "seed.txt")
    symlink_failure = failed_continuation()
    check(
        "a symlinked packet path is refused as invalid",
        "attention packet is invalid" in str(symlink_failure)
        and packet_path.is_symlink(),
    )
    packet_path.unlink()

    fifo_path = packet_path
    os.mkfifo(fifo_path, 0o600)
    fifo_failure = failed_continuation()
    check(
        "a non-regular packet path is refused without blocking on it",
        "attention packet is invalid" in str(fifo_failure)
        and stat.S_ISFIFO(fifo_path.lstat().st_mode),
    )
    fifo_path.unlink()

    with mock.patch.object(
        continuation, "MAX_ATTENTION_PACKET_BYTES", 16
    ):
        oversized_failure = failed_continuation()
    check(
        "an oversized packet is refused before anything is published",
        "too large" in str(oversized_failure)
        and not packet_path.exists()
        and not any(
            item.name.startswith(".task-verification")
            for item in worktree.iterdir()
        ),
    )

print("authorized task review continuation tests passed")
