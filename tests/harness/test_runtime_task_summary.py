#!/usr/bin/env python3
"""Hermetic task-summary callback and coordinator wake regressions."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.process import ProcessAdapter
from harness.contracts import AttentionReason, OperationSpec, RuntimeRoute
from harness.pipeline_builtins import builtin_definitions, builtin_registry
from harness.pipelines import compile_pipeline
from harness.runtime_sessions import RuntimeSessionRequest
from harness.runtime_worker import run as run_worker
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.workflows.review import ReviewContext
from harness.workflows.review_gate import ReviewGateController, ReviewPreset


ORIGIN = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
TASK = "44444444-4444-4444-8444-444444444444"
INVALID_TASK = "55555555-5555-4555-8555-555555555555"
BLOCKED_TASK = "66666666-6666-4666-8666-666666666666"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


class FakeCmux:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def dispatch_record(
    store: OperationStore,
    operation_id: str,
    *,
    bind_contract: bool = True,
    pipeline_name: str = "lifecycle/default",
) -> None:
    route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
    lifecycle = compile_pipeline(
        builtin_definitions()[pipeline_name],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    store.create(
        OperationSpec(
            operation_id,
            f"key-{operation_id}",
            "dispatch",
            "owner-1",
            route,
            "packets/task.json",
            "scoped",
            contract_sha256=(
                lifecycle.definition_sha256 if bind_contract else ""
            ),
        ),
        lane_id="lane-1",
        run_id=f"run-{operation_id}",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", operation_id, state)
def task_meta(
    vault: Path,
    worktree: Path,
    plan: Path,
    task_id: str,
    profile_sha: str,
) -> dict[str, object]:
    return {
        "version": 3,
        "project_id": PROJECT,
        "task_id": task_id,
        "task_name": "Runtime summary",
        "origin_session": "coordinator-session",
        "executor_runtime": "codex",
        "interaction_policy": "unattended",
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "vault_root": str(vault),
        "review_policy": {
            "mode": "skip",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 0,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "Runtime Result",
        },
        "surface_policy": {"auto_close": True, "placement": "split"},
        "watchdog_policy": {
            "enabled": True,
            "poll_seconds": 30,
            "warn_after_seconds": 900,
            "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
        "task_surface": CHILD,
        "worktree": str(worktree),
    }


def run_case(
    root: Path,
    operation_id: str,
    summary: object,
    *,
    review_state: str = "skipped",
    review_launcher: Callable[[Path, Path], None] | None = None,
    before_start: (
        Callable[[Path, Path, Path, str], None] | None
    ) = None,
    bind_contract: bool = True,
    pipeline_name: str = "lifecycle/default",
    verification_runner: Callable[..., subprocess.CompletedProcess[str]]
    | None = None,
) -> tuple[
    OperationStore, FakeCmux, Path, int
]:
    vault = root / f"vault-{operation_id}"
    worktree = root / f"worktree-{operation_id}"
    (vault / "wiki" / "plans").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    shutil.copy2(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    (vault / "scripts" / "reap-runner.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "runtime@example.invalid"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime Summary Test"],
        cwd=worktree,
        check=True,
    )
    (worktree / "product.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "ready"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text("# Approved\n", encoding="utf-8")
    write_json(
        vault
        / ".vault-meta"
        / "task-sessions"
        / "session-bindings"
        / "coordinator-session"
        / "binding.json",
        {
            "session_id": "coordinator-session",
            "project_id": PROJECT,
            "task_id": operation_id,
        },
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    dispatch_record(
        store,
        operation_id,
        bind_contract=bind_contract,
        pipeline_name=pipeline_name,
    )
    profile_sha = load_profiles(
        vault / "config" / "verification-profiles.toml"
    )["scoped"].sha256
    meta = task_meta(vault, worktree, plan, operation_id, profile_sha)
    write_json(worktree / ".task-meta.json", meta)
    if review_state == "skipped":
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / operation_id / operation_id,
            dispatch_operation_id=operation_id,
            owner_id=operation_id,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )
    (worktree / ".task-prompt.md").write_text(
        "Complete the approved task and write the canonical summary.",
        encoding="utf-8",
    )
    parent = store.read("owner-1", operation_id)
    request = RuntimeSessionRequest(
        parent.spec,
        parent.lane_id,
        parent.run_id,
        ORIGIN,
        worktree,
        ".task-prompt.md",
        ".task-summary.json",
        callback_mode="task-summary",
        task_summary_pointer=".task-summary.json",
    )
    check(
        "request carries canonical task-summary mode without a wake command",
        request.callback_mode == "task-summary"
        and request.task_summary_pointer == ".task-summary.json"
        and not hasattr(request, "wake_message"),
    )
    provider = root / f"provider-{operation_id}.py"
    provider.write_text(
        "import json,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n"
        "time.sleep(0.3)\n",
        encoding="utf-8",
    )
    summary_path = worktree / ".task-summary.json"
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(summary_path),
            json.dumps(summary, sort_keys=True),
        ),
        cwd=worktree,
        state_root=root / f"state-{operation_id}",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=summary_path,
        store_root=store.root,
        owner_id="owner-1",
        operation_id=operation_id,
        run_id=f"run-{operation_id}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=summary_path,
        origin_surface=ORIGIN,
    )
    cmux = FakeCmux()
    if before_start is not None:
        before_start(
            vault,
            worktree,
            launch.spec_path.parent,
            profile_sha,
        )
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_worker(
                launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
                cmux_adapter=cmux,
                review_launcher=review_launcher,
                verification_runner=verification_runner,
            )
        )
    )
    thread.start()
    if review_state == "delayed-skip":
        import time

        # The provider exits after 0.3s; approval arrives later. The runtime
        # worker must remain alive as the code-owned finalization watcher.
        time.sleep(0.45)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / operation_id / operation_id,
            dispatch_operation_id=operation_id,
            owner_id=operation_id,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )
    thread.join(timeout=3)
    return store, cmux, launch.spec_path.parent, result[0]


with tempfile.TemporaryDirectory(prefix="runtime-task-summary.") as raw:
    root = Path(raw)
    valid_summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "Bounded completed task.",
    }
    store, cmux, state, rc = run_case(root, TASK, valid_summary)
    record = store.read("owner-1", TASK)
    check(
        "valid canonical v3 summary becomes one durable parent callback",
        rc == 0
        and record.state == "finalizing"
        and record.accepted_callback_kind == "wiki-summary"
        and record.accepted_callback_id.startswith("wiki-summary-"),
        record,
    )
    check(
        "accepted receipt wakes only the exact origin with code-owned reap command",
        len(cmux.sent) == 1
        and cmux.sent[0][0] == ORIGIN
        and "Typed final task summary callback was accepted" in cmux.sent[0][1]
        and "scripts/reap-runner.py" in cmux.sent[0][1]
        and str(root / f"vault-{TASK}") in cmux.sent[0][1]
        and str(root / f"worktree-{TASK}") in cmux.sent[0][1]
        and "Bounded completed task" not in cmux.sent[0][1]
        and cmux.keys == [(ORIGIN, "Enter")],
        (cmux.sent, cmux.keys),
    )
    sent_marker = json.loads(
        (state / "task-summary-notify.json").read_text(encoding="utf-8")
    )
    check(
        "notification marker records exact durable callback before send completion",
        sent_marker["status"] == "sent"
        and sent_marker["callback_id"] == record.accepted_callback_id,
        sent_marker,
    )

    engineering_task = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    verification_calls: list[tuple[str, ...]] = []

    def pass_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        verification_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    (
        engineering_store,
        engineering_cmux,
        engineering_state,
        engineering_rc,
    ) = run_case(
        root,
        engineering_task,
        valid_summary,
        pipeline_name="engineering/change",
        verification_runner=pass_verification,
    )
    engineering_record = engineering_store.read(
        "owner-1", engineering_task
    )
    verification_receipt = json.loads(
        (
            engineering_state / "pipeline-step-verify.json"
        ).read_text(encoding="utf-8")
    )
    engineering_operations = engineering_store.list("owner-1")
    engineering_verify = [
        record
        for record in engineering_operations
        if record.spec.kind == "pipeline-verify"
    ]
    check(
        "engineering change runs verification in one derived operation",
        engineering_rc == 0
        and engineering_record.spec.operation_id == engineering_task
        and engineering_record.state == "finalizing"
        and engineering_record.accepted_callback_kind == "wiki-summary"
        and len(engineering_verify) == 1
        and engineering_verify[0].spec.operation_id
        == verification_receipt["operation_id"]
        and engineering_verify[0].spec.operation_id != engineering_task
        and engineering_verify[0].lane_id
        == verification_receipt["lane_id"]
        and engineering_verify[0].run_id
        == verification_receipt["run_id"]
        and engineering_verify[0].state == "complete"
        and verification_receipt["parent_operation_id"]
        == engineering_task
        and verification_receipt["status"] == "complete"
        and verification_receipt["step_id"] == "verify"
        and len(verification_receipt["evidence"]) == 3
        and verification_calls
        == [
            ("make", "test-harness"),
            ("make", "test-model-routing"),
            ("git", "diff", "--check"),
        ]
        and len(engineering_cmux.sent) == 1,
        (
            engineering_record,
            engineering_verify,
            verification_receipt,
            verification_calls,
        ),
    )

    failing_task = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    failing_commands = [0]
    commands_before_resubmission = []

    def fail_then_pass_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        failing_commands[0] += 1
        return subprocess.CompletedProcess(
            argv,
            1 if failing_commands[0] == 1 else 0,
            "ok\n" if failing_commands[0] > 1 else "",
            "failed\n" if failing_commands[0] == 1 else "",
        )

    def resubmit_failed_verification(
        _vault: Path,
        worktree: Path,
        _state: Path,
        _profile_sha: str,
    ) -> None:
        def respond() -> None:
            import time

            packet_path = worktree / ".task-verification.json"
            for _ in range(100):
                if packet_path.is_file():
                    break
                time.sleep(0.02)
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            (worktree / "product.txt").write_text(
                "ready\nfixed\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "product.txt"], cwd=worktree, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "fix verification"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            )
            resubmitted_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (
                _vault
                / ".vault-meta"
                / "harness"
                / "review-data"
                / failing_task
                / failing_task
                / "review-gate.json"
            ).unlink(missing_ok=True)
            time.sleep(0.12)
            commands_before_resubmission.append(failing_commands[0])
            packet_sha256 = hashlib.sha256(
                json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            write_json(
                worktree / ".task-verification-response.json",
                {
                    "schema_version": 1,
                    "operation_id": failing_task,
                    "verification_operation_id": packet[
                        "verification_operation_id"
                    ],
                    "failed_head_sha": packet["head_sha"],
                    "packet_sha256": packet_sha256,
                    "response": "fix-and-resubmit",
                    "resubmitted_head_sha": resubmitted_head,
                },
            )

        threading.Thread(target=respond).start()

    def approve_resubmitted_verification(
        vault: Path, worktree: Path
    ) -> None:
        gate = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / failing_task
            / failing_task
        )
        (gate / "review-gate.json").unlink(missing_ok=True)
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=failing_task,
            owner_id=failing_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    failed_store, failed_cmux, failed_state, failed_rc = run_case(
        root,
        failing_task,
        valid_summary,
        pipeline_name="engineering/change",
        verification_runner=fail_then_pass_verification,
        before_start=resubmit_failed_verification,
        review_launcher=approve_resubmitted_verification,
    )
    failed_record = failed_store.read("owner-1", failing_task)
    resubmitted_receipt = json.loads(
        (failed_state / "pipeline-step-verify.json").read_text(
            encoding="utf-8"
        )
    )
    failed_packet = json.loads(
        (
            root
            / f"worktree-{failing_task}"
            / ".task-verification.json"
        ).read_text(encoding="utf-8")
    )
    response_receipts = list(
        (failed_state / "pipeline-verification").glob(
            "*/response-receipt.json"
        )
    )
    failed_verifications = [
        record
        for record in failed_store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    ]
    failed_by_id = {
        record.spec.operation_id: record
        for record in failed_verifications
    }
    failed_attempt = failed_by_id.get(
        str(failed_packet["verification_operation_id"])
    )
    resubmitted_attempt = failed_by_id.get(
        str(resubmitted_receipt["operation_id"])
    )
    check(
        "fix-and-resubmit consumes an identity-bound response and reaches review",
        failed_rc == 0
        and failed_record.state == "finalizing"
        and failed_record.accepted_callback_kind == "wiki-summary"
        and len(failed_verifications) == 2
        and failed_attempt is not None
        and failed_attempt.state == "failed"
        and resubmitted_attempt is not None
        and resubmitted_attempt.state == "complete"
        and failed_attempt.spec.operation_id
        != resubmitted_attempt.spec.operation_id
        and resubmitted_receipt["parent_operation_id"] == failing_task
        and resubmitted_receipt["status"] == "complete"
        and failed_packet["status"] == "attention-required"
        and failed_packet["step_id"] == "verify"
        and failed_packet["safe_boundary"] == "tdd-slices-complete"
        and failed_packet["allowed_responses"]
        == ["fix-and-resubmit", "escalate"]
        and failed_packet["evidence"][0]["command_id"]
        == "scoped-1"
        and failed_packet["response_pointer"]
        == ".task-verification-response.json"
        and len(response_receipts) == 1
        and json.loads(
            response_receipts[0].read_text(encoding="utf-8")
        )["status"]
        == "accepted"
        and commands_before_resubmission == [1]
        and failing_commands == [4]
        and failed_cmux.sent
        and failed_cmux.sent[0][0] == CHILD
        and ".task-verification.json" in failed_cmux.sent[0][1],
        (
            failed_record,
            failed_verifications,
            resubmitted_receipt,
            failed_packet,
            response_receipts,
            commands_before_resubmission,
            failed_cmux.sent,
        ),
    )

    # A restarted worker sees a duplicate durable callback and the sent marker;
    # it must not wake the coordinator twice.
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(0.15)",
        ),
        cwd=root / f"worktree-{TASK}",
        state_root=state,
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=root
        / f"worktree-{TASK}"
        / ".task-summary.json",
        store_root=store.root,
        owner_id="owner-1",
        operation_id=TASK,
        run_id=f"run-{TASK}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=root
        / f"worktree-{TASK}"
        / ".task-summary.json",
        origin_surface=ORIGIN,
    )
    second_rc = run_worker(
        launch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
        cmux_adapter=cmux,
    )
    check(
        "duplicate summary callback never duplicates coordinator notification",
        second_rc == 0 and len(cmux.sent) == 1 and len(cmux.keys) == 1,
        (cmux.sent, cmux.keys),
    )

    invalid = {**valid_summary, "title": "Unapproved title"}
    invalid_store, invalid_cmux, invalid_state, invalid_rc = run_case(
        root, INVALID_TASK, invalid
    )
    invalid_record = invalid_store.read("owner-1", INVALID_TASK)
    check(
        "invalid handoff becomes attention and never notifies coordinator",
        invalid_rc == 0
        and invalid_record.state == "attention-required"
        and not invalid_record.accepted_callback_id
        and invalid_cmux.sent == []
        and invalid_cmux.keys == []
        and not (invalid_state / "task-summary-notify.json").exists(),
        invalid_record,
    )

    drift_task = "88888888-8888-4888-8888-888888888888"
    drift_store, drift_cmux, drift_state, drift_rc = run_case(
        root,
        drift_task,
        valid_summary,
        review_state="skipped",
        bind_contract=False,
    )
    drift_record = drift_store.read("owner-1", drift_task)
    check(
        "unbound lifecycle operation stops as typed contract drift",
        drift_rc == 0
        and drift_record.state == "attention-required"
        and drift_record.attention_reason
        == AttentionReason.CONTRACT_DRIFT
        and not drift_record.accepted_callback_id
        and drift_cmux.sent == []
        and json.loads(
            (drift_state / "callback-error.json").read_text(encoding="utf-8")
        )["status"]
        == "pipeline-contract-drift",
        drift_record,
    )

    delayed_task = BLOCKED_TASK
    delayed_store, delayed_cmux, _delayed_state, delayed_rc = run_case(
        root,
        delayed_task,
        valid_summary,
        review_state="delayed-skip",
        review_launcher=lambda _vault, _worktree: None,
    )
    delayed_record = delayed_store.read("owner-1", delayed_task)
    check(
        "worker outlives provider and accepts once typed review state arrives",
        delayed_rc == 0
        and delayed_record.state == "finalizing"
        and delayed_record.accepted_callback_kind == "wiki-summary"
        and len(delayed_cmux.sent) == 1,
        delayed_record,
    )

    automatic_task = "77777777-7777-4777-8777-777777777777"
    automatic_calls: list[str] = []

    def approve_automatically(vault: Path, worktree: Path) -> None:
        automatic_calls.append(str(worktree))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        profile_sha = meta["review_policy"]["verification_profile_sha256"]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / automatic_task
            / automatic_task,
            dispatch_operation_id=automatic_task,
            owner_id=automatic_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )

    automatic_store, automatic_cmux, automatic_state, automatic_rc = run_case(
        root,
        automatic_task,
        valid_summary,
        review_state="missing",
        review_launcher=approve_automatically,
    )
    automatic_record = automatic_store.read("owner-1", automatic_task)
    check(
        "compiled lifecycle starts the missing review gate without model orchestration",
        automatic_rc == 0
        and len(automatic_calls) == 1
        and automatic_record.state == "finalizing"
        and automatic_record.accepted_callback_kind == "wiki-summary"
        and len(automatic_cmux.sent) == 1,
        (automatic_calls, automatic_record),
    )
    automatic_marker = json.loads(
        (automatic_state / "pipeline-review-start.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "automatic review launch has one durable local receipt",
        automatic_marker["status"] == "started"
        and automatic_marker["operation_id"] == automatic_task,
        automatic_marker,
    )

    asynchronous_task = "99999999-9999-4999-8999-999999999999"
    asynchronous_calls: list[str] = []
    asynchronous_verification_heads: list[str] = []
    asynchronous_verification_calls: list[tuple[str, ...]] = []

    def record_asynchronous_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            result = subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
            asynchronous_verification_heads.append(
                result.stdout.strip()
            )
            return result
        asynchronous_verification_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def complete_when_callback_arrives(vault: Path, worktree: Path) -> None:
        asynchronous_calls.append(str(worktree))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        profile_sha = meta["review_policy"]["verification_profile_sha256"]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        gate_root = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / asynchronous_task
            / asynchronous_task
        )
        if len(asynchronous_calls) == 1:
            write_json(
                gate_root / "review-gate.json",
                {
                    "schema_version": 1,
                    "dispatch_operation_id": asynchronous_task,
                    "owner_id": asynchronous_task,
                    "status": "reviewing",
                    "product_root": str(worktree),
                    "context": {
                        "head_sha": head,
                        "verification_profile": "scoped",
                        "verification_profile_sha256": profile_sha,
                    },
                },
            )
            # The callback lands before the launch facade returns. The receipt
            # must not acknowledge it as processed until a later drive.
            write_json(
                vault
                / ".vault-meta"
                / "harness"
                / "review-runtime"
                / asynchronous_task
                / "callbacks"
                / "spec"
                / ".review-callback.json",
                {"schema_version": 1, "status": "ready"},
            )
            return
        if len(asynchronous_calls) == 2:
            # A second deep-review axis lands while the facade is already
            # returning from its first incomplete readiness scan.
            write_json(
                vault
                / ".vault-meta"
                / "harness"
                / "review-runtime"
                / asynchronous_task
                / "callbacks"
                / "standards"
                / ".review-callback.json",
                {"schema_version": 1, "status": "ready"},
            )
            return
        if len(asynchronous_calls) == 3:
            result_pointer = (
                gate_root / asynchronous_task / "round-spec-0.json"
            )
            write_json(
                result_pointer,
                {
                    "axis": "spec",
                    "verdict": "changes-requested",
                    "verification_iteration": 0,
                    "findings": [
                        {
                            "axis": "spec",
                            "finding_id": "F-material",
                            "severity": "important",
                            "file": "product.txt",
                            "line": 1,
                            "summary": "Material review finding",
                            "evidence": "The original content is incomplete.",
                            "recommendation": "Commit the exact correction.",
                        }
                    ],
                },
            )
            gate_state = json.loads(
                (gate_root / "review-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            gate_state["status"] = "awaiting-resolution"
            gate_state["awaiting_resolution"] = {
                "spec": {
                    "pointer": result_pointer.relative_to(
                        gate_root
                    ).as_posix(),
                    "reviewed_head_sha": head,
                }
            }
            write_json(gate_root / "review-gate.json", gate_state)

            def resolve_after_packet() -> None:
                import time

                packets: list[Path] = []
                for _ in range(100):
                    packet = worktree / ".task-review.json"
                    packets = [packet] if packet.is_file() else []
                    if packets:
                        break
                    time.sleep(0.02)
                if not packets:
                    return
                (worktree / "resolution.txt").write_text(
                    "resolved\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "resolution.txt"],
                    cwd=worktree,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "resolve review"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                )

            threading.Thread(target=resolve_after_packet).start()
            return
        (gate_root / "review-gate.json").unlink()
        ReviewGateController.skip(
            gate_root,
            dispatch_operation_id=asynchronous_task,
            owner_id=asynchronous_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )

    (
        asynchronous_store,
        asynchronous_cmux,
        _asynchronous_state,
        asynchronous_rc,
    ) = run_case(
        root,
        asynchronous_task,
        valid_summary,
        review_state="missing",
        review_launcher=complete_when_callback_arrives,
        pipeline_name="engineering/change",
        verification_runner=record_asynchronous_verification,
    )
    asynchronous_record = asynchronous_store.read(
        "owner-1", asynchronous_task
    )
    check(
        "engineering change re-verifies the new resolution HEAD before same-session review",
        asynchronous_rc == 0
        and len(asynchronous_calls) == 4
        and len(set(asynchronous_verification_heads)) == 2
        and len(asynchronous_verification_calls) == 6
        and asynchronous_record.state == "finalizing"
        and asynchronous_record.accepted_callback_kind == "wiki-summary"
        and len(asynchronous_cmux.sent) == 2
        and asynchronous_cmux.sent[0][0] == CHILD
        and "Typed review findings" in asynchronous_cmux.sent[0][1]
        and asynchronous_cmux.sent[1][0] == ORIGIN,
        (
            asynchronous_calls,
            asynchronous_verification_heads,
            asynchronous_verification_calls,
            asynchronous_record,
        ),
    )
    asynchronous_packet = (
        root / f"worktree-{asynchronous_task}" / ".task-review.json"
    )
    check(
        "executor receives a bounded typed decision packet",
        asynchronous_packet.is_file()
        and json.loads(
            asynchronous_packet.read_text(encoding="utf-8")
        )["findings"][0]["finding_id"]
        == "F-material",
        asynchronous_packet,
    )

    pending_task = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    pending_calls: list[str] = []

    def prepare_pending_review(
        vault: Path,
        worktree: Path,
        state: Path,
        profile_sha: str,
    ) -> None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        write_json(
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / pending_task
            / pending_task
            / "review-gate.json",
            {
                "schema_version": 1,
                "dispatch_operation_id": pending_task,
                "owner_id": pending_task,
                "status": "reviewing",
                "product_root": str(worktree),
                "context": {
                    "head_sha": head,
                    "verification_profile": "scoped",
                    "verification_profile_sha256": profile_sha,
                },
            },
        )
        write_json(
            state / "pipeline-review-start.json",
            {
                "schema_version": 1,
                "operation_id": pending_task,
                "definition_sha256": compile_pipeline(
                    builtin_definitions()["lifecycle/default"],
                    builtin_registry(),
                    capabilities=("route:resolved",),
                ).definition_sha256,
                "status": "pending",
            },
        )

    def approve_pending(vault: Path, worktree: Path) -> None:
        pending_calls.append(str(worktree))
        gate = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / pending_task
            / pending_task
        )
        (gate / "review-gate.json").unlink()
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=pending_task,
            owner_id=pending_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    pending_store, _pending_cmux, _pending_state, pending_rc = run_case(
        root,
        pending_task,
        valid_summary,
        review_state="missing",
        review_launcher=approve_pending,
        before_start=prepare_pending_review,
    )
    pending_record = pending_store.read("owner-1", pending_task)
    check(
        "pending receipt plus live gate resumes the idempotent drive",
        pending_rc == 0
        and len(pending_calls) == 1
        and pending_record.state == "finalizing",
        (pending_calls, pending_record),
    )
