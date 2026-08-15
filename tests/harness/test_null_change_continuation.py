#!/usr/bin/env python3
"""Every null-change retry path parks, on any decision-chain state."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import AttentionReason  # noqa: E402
from harness.runtime_worker import RuntimeWorkerError  # noqa: E402
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402
from harness.runtime_worker_fix import (  # noqa: E402
    FixTransportState,
    RuntimeWorkerFixMixin,
)
from task_escalation_records import (  # noqa: E402
    append_resolution,
    load_attention,
)


HEAD = "a" * 40
ORIGIN = "11111111-1111-4111-8111-111111111111"
SURFACE = "22222222-2222-4222-8222-222222222222"


def check(label: str, value: bool, detail: object = None) -> None:
    if not value:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"OK   {label}")


class FakeCmux:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def send(self, surface: str, message: str) -> None:
        self.sent.append((surface, message))

    def send_key(self, surface: str, key: str) -> None:
        self.keys.append((surface, key))


class FakeDefinition:
    pipeline_id = "engineering/fix"
    version = 1
    profile = "scoped"
    steps = ()


class FakePipeline:
    definition = FakeDefinition()
    definition_sha256 = "b" * 64


class Worker(RuntimeWorkerFixMixin):
    """The real seam over a stubbed surface, store, and HEAD."""

    write_immutable_json = RuntimeWorkerControlMixin.write_immutable_json
    publish_pipeline_decision = RuntimeWorkerControlMixin.publish_pipeline_decision

    def __init__(self, worktree: Path, state_root: Path) -> None:
        self.spec = {
            "cwd": worktree,
            "operation_id": "operation-null-change",
            "surface_id": SURFACE,
            "origin_surface": ORIGIN,
            "store_root": worktree / "vault" / ".vault-meta" / "harness",
            "task_summary_pointer": worktree / ".task-summary.json",
        }
        self.spec_path = state_root / "spec.json"
        self.cmux_adapter = FakeCmux()
        self.pipeline = FakePipeline()
        self.parked: list[tuple[str, AttentionReason]] = []

    def git_head(self) -> str:
        return HEAD

    def summary_attention(
        self,
        status: str,
        reason: AttentionReason = AttentionReason.CALLBACK_INVALID,
        *,
        write_error: bool = True,
    ) -> None:
        self.parked.append((status, reason))


def fixture(root: Path, name: str) -> tuple[Worker, Path, Path]:
    worktree = root / name
    (worktree / "vault" / ".vault-meta" / "harness").mkdir(parents=True)
    (worktree / ".task-meta.json").write_text(
        json.dumps(
            {
                "version": 4,
                "worktree": str(worktree),
                "project_id": "llm-obsidian",
                "task_id": name,
                "origin_session": "session-null-change",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    state_root = root / f"state-{name}"
    state_root.mkdir()
    worker = Worker(worktree, state_root)
    notify_path = (
        state_root / "pipeline-fix" / "pass-1" / "null-change-notify.json"
    )
    return worker, worktree, notify_path


def state(iteration: int = 1) -> FixTransportState:
    return FixTransportState(
        "attention",
        2,
        "c" * 64,
        HEAD,
        object(),
        [],
        iteration,
        Path(f"pass-{iteration}"),
        [],
        object(),
        {"current_head_sha": HEAD},
    )


with tempfile.TemporaryDirectory(prefix="null-change-continuation.") as raw:
    root = Path(raw)

    worker, worktree, notify_path = fixture(root, "published")
    worker.continue_null_change_retry(state())
    attention = load_attention(worktree)
    check(
        "a null-change retry publishes one typed decision and parks",
        worker.parked
        == [("pipeline-fix-retry-null-change", AttentionReason.ATTENTION_REQUIRED)]
        and notify_path.is_file()
        and json.loads(notify_path.read_text(encoding="utf-8"))
        == {
            "schema_version": 1,
            "operation_id": "operation-null-change",
            "iteration": 1,
            "head_sha": HEAD,
            "status": "sent",
        }
        and attention is not None
        and attention["category"] == "pipeline-decision"
        and attention["status"] == "pending"
        and attention["allowed_decisions"] == ["stop", "retry-with-scope"]
        and len(worker.cmux_adapter.sent) == 1
        and worker.cmux_adapter.sent[0][0] == ORIGIN
        and "retry-with-scope" in worker.cmux_adapter.sent[0][1],
        (worker.parked, attention, worker.cmux_adapter.sent),
    )

    worker.continue_null_change_retry(state())
    check(
        "delivery replay parks again without a second notification",
        worker.parked
        == [
            ("pipeline-fix-retry-null-change", AttentionReason.ATTENTION_REQUIRED)
        ]
        * 2
        and len(worker.cmux_adapter.sent) == 1
        and len(worker.cmux_adapter.keys) == 1,
        (worker.parked, worker.cmux_adapter.sent),
    )

    # A crash between the raise and its delivery marker, with the coordinator
    # already resolving the decision, must still park instead of returning.
    resolved_worker, resolved_worktree, resolved_notify = fixture(root, "resolved")
    resolved_worker.continue_null_change_retry(state())
    resolved_notify.unlink()
    append_resolution(resolved_worktree, "stop")
    resolved_worker.parked.clear()
    resolved_worker.cmux_adapter.sent.clear()
    resolved_worker.continue_null_change_retry(state())
    check(
        "a resolved decision chain parks without re-delivering",
        resolved_worker.parked
        == [("pipeline-fix-retry-null-change", AttentionReason.ATTENTION_REQUIRED)]
        and not resolved_notify.exists()
        and resolved_worker.cmux_adapter.sent == [],
        (resolved_worker.parked, resolved_worker.cmux_adapter.sent),
    )

    # A later retry at the same HEAD is a distinct decision: resolving the
    # first one must not suppress the second, and its own replay stays quiet.
    sequential, sequential_worktree, sequential_notify = fixture(root, "sequential")
    sequential.continue_null_change_retry(state(1))
    append_resolution(sequential_worktree, "retry-with-scope")
    first_decision = load_attention(sequential_worktree)
    sequential.parked.clear()
    sequential.cmux_adapter.sent.clear()
    sequential.continue_null_change_retry(state(2))
    second_decision = load_attention(sequential_worktree)
    second_notify = sequential_notify.with_name("null-change-notify.json").parent.parent / "pass-2" / "null-change-notify.json"
    check(
        "a later retry at the same HEAD publishes its own actionable decision",
        sequential.parked
        == [("pipeline-fix-retry-null-change", AttentionReason.ATTENTION_REQUIRED)]
        and second_notify.is_file()
        and json.loads(second_notify.read_text(encoding="utf-8"))["iteration"] == 2
        and second_decision is not None
        and second_decision["status"] == "pending"
        and second_decision["iteration"] == 2
        and second_decision["id"] != first_decision["id"]
        and len(sequential.cmux_adapter.sent) == 1,
        (sequential.parked, first_decision, second_decision, sequential.cmux_adapter.sent),
    )

    sequential.parked.clear()
    sequential.continue_null_change_retry(state(2))
    check(
        "the later retry replays idempotently after a crash",
        sequential.parked
        == [("pipeline-fix-retry-null-change", AttentionReason.ATTENTION_REQUIRED)]
        and len(sequential.cmux_adapter.sent) == 1
        and load_attention(sequential_worktree)["id"] == second_decision["id"],
        (sequential.parked, sequential.cmux_adapter.sent),
    )

    changed_worker, _changed_worktree, changed_notify = fixture(root, "changed")
    changed_notify.parent.mkdir(parents=True)
    changed_notify.write_text('{"schema_version":1,"status":"other"}\n', encoding="utf-8")
    try:
        changed_worker.continue_null_change_retry(state())
    except RuntimeWorkerError as exc:
        changed_error = str(exc)
    else:
        changed_error = ""
    check(
        "changed delivery bytes stay fail-closed",
        changed_error == "pipeline decision delivery changed"
        and changed_worker.parked == []
        and changed_worker.cmux_adapter.sent == [],
        (changed_error, changed_worker.parked),
    )

    blocked_worker, blocked_worktree, blocked_notify = fixture(root, "blocked")
    blocked_worker.publish_pipeline_decision(
        marker={
            "version": 1,
            "id": "pipeline-decision-unrelated0000000000000",
            "status": "pending",
            "task_name": "unrelated escalation",
            "category": "pipeline-decision",
            "reason": "An unrelated coordinator decision is already pending",
            "question": "Choose stop or continue",
            "worktree": str(blocked_worktree),
            "task_surface": SURFACE,
            "allowed_decisions": ["stop", "continue"],
        },
        notify_path=blocked_notify.with_name("unrelated-notify.json"),
        delivery={"schema_version": 1, "status": "sent"},
        body="An unrelated decision is pending.",
        allowed_decisions=("stop", "continue"),
    )
    blocked_worker.cmux_adapter.sent.clear()
    blocked_worker.continue_null_change_retry(state())
    check(
        "an unresolved decision chain parks under its own cause",
        blocked_worker.parked
        == [
            (
                "pipeline-fix-retry-null-change-blocked",
                AttentionReason.ATTENTION_REQUIRED,
            )
        ]
        and not blocked_notify.exists()
        and blocked_worker.cmux_adapter.sent == [],
        (blocked_worker.parked, blocked_worker.cmux_adapter.sent),
    )

print("null-change continuation matrix: ok")
