#!/usr/bin/env python3
"""Append-only coordinator decision record and latest-pointer regressions."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_escalation_records import (  # noqa: E402
    EscalationRecordError,
    append_amendment,
    append_raise,
    append_resolution,
    load_attention,
    load_chain,
    load_latest,
    record_path,
)
import task_escalation_records as escalation_records  # noqa: E402
import task_escalation as task_escalation_cli  # noqa: E402
from harness.contracts import (  # noqa: E402
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.custom_pipelines import (  # noqa: E402
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenPipelineStore,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry  # noqa: E402
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402
from harness.runtime_worker_custom import RuntimeWorkerCustomMixin  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.workflows.custom_sequence import (  # noqa: E402
    accept_custom_step,
    custom_step_envelope,
    custom_step_request,
    prepare_custom_step,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def expect_error(label: str, action: object, message: str) -> None:
    try:
        action()
    except EscalationRecordError as exc:
        check(label, message in str(exc))
    else:
        raise AssertionError(label)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def meta(worktree: Path, *, task_id: str = "task-a") -> None:
    write_json(
        worktree / ".task-meta.json",
        {
            "version": 4,
            "project_id": "project-a",
            "task_id": task_id,
            "origin_session": "coordinator-a",
            "task_surface": "11111111-1111-4111-8111-111111111111",
            "worktree": str(worktree.resolve()),
        },
    )


def raised_payload(worktree: Path, escalation_id: str, reason: str) -> dict[str, object]:
    return {
        "version": 1,
        "id": escalation_id,
        "status": "pending",
        "task_name": "durable decisions",
        "category": "scope",
        "reason": reason,
        "question": "Keep the approved boundary?",
        "worktree": str(worktree.resolve()),
        "task_surface": "11111111-1111-4111-8111-111111111111",
        "raised_at": "2026-08-04T12:00:00Z",
    }


class FakeCmux:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, surface: str, message: str) -> None:
        self.sent.append((surface, message))

    def send_key(self, surface: str, key: str) -> None:
        self.sent.append((surface, key))


class CustomWriterFixture(RuntimeWorkerCustomMixin):
    def __init__(self, worktree: Path) -> None:
        self.spec = {
            "cwd": worktree,
            "operation_id": "custom-operation",
            "surface_id": "11111111-1111-4111-8111-111111111111",
            "origin_surface": "22222222-2222-4222-8222-222222222222",
        }
        self.pipeline = SimpleNamespace(definition_sha256="d" * 64)
        self.spec_path = worktree / "state" / "spec.json"
        self.spec_path.parent.mkdir()
        self.trusted_vault = worktree
        self.cmux_adapter = FakeCmux()

    def write_immutable_json(self, path: Path, value: dict[str, object]) -> None:
        if not path.exists():
            write_json(path, value)


class ControlWriterFixture(RuntimeWorkerControlMixin):
    def __init__(self, worktree: Path) -> None:
        self.spec = {
            "cwd": worktree,
            "operation_id": "fix-operation",
            "surface_id": "11111111-1111-4111-8111-111111111111",
            "origin_surface": "22222222-2222-4222-8222-222222222222",
            "store_root": worktree / ".vault-meta" / "harness",
        }
        self.spec_path = worktree / "state" / "spec.json"
        self.spec_path.parent.mkdir()
        self.cmux_adapter = FakeCmux()

    def write_immutable_json(self, path: Path, value: dict[str, object]) -> None:
        if not path.exists():
            write_json(path, value)


with tempfile.TemporaryDirectory(prefix="task-escalation-ignore.") as raw:
    worktree = Path(raw) / "task"
    worktree.mkdir()
    shutil.copy2(ROOT / ".gitignore", worktree / ".gitignore")
    meta(worktree, task_id="gitignore-task")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    exclude = worktree / ".git" / "info" / "exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8")
        + ".task-needs-attention.json\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", ".gitignore", ".task-meta.json"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Escalation Test",
            "-c",
            "user.email=escalation@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    raised = append_raise(
        worktree,
        raised_payload(worktree, "gitignore-escalation", "durable raise"),
    )
    raised_bytes = record_path(worktree, raised.record_id).read_bytes()
    resolved = append_resolution(
        worktree,
        "preserve the durable escalation evidence",
        resolved_at="2026-08-04T12:00:30Z",
    )
    chain = load_chain(worktree)
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    check(
        "raised and resolved records stay durable while Git remains clean",
        [item.record_type for item in chain] == ["raise", "resolution"]
        and chain[0].sha256 == raised.sha256
        and chain[1].sha256 == resolved.sha256
        and record_path(worktree, raised.record_id).read_bytes()
        == raised_bytes
        and record_path(worktree, resolved.record_id).is_file()
        and status.stdout == "",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-records.") as raw:
    worktree = Path(raw) / "task"
    worktree.mkdir()
    meta(worktree)

    durability_worktree = Path(raw) / "durability-task"
    durability_worktree.mkdir()
    meta(durability_worktree, task_id="durability-task")
    durability_order: list[str] = []
    real_sync_directory = escalation_records._fsync_directory
    real_write_pointer = escalation_records._write_pointer

    def record_directory_sync(path: Path) -> None:
        durability_order.append(f"sync:{path.name}")
        real_sync_directory(path)

    def record_pointer_write(task_root: Path, record: object) -> None:
        durability_order.append("pointer")
        real_write_pointer(task_root, record)

    with patch.object(
        escalation_records,
        "_fsync_directory",
        side_effect=record_directory_sync,
    ), patch.object(
        escalation_records,
        "_write_pointer",
        side_effect=record_pointer_write,
    ):
        durable = append_raise(
            durability_worktree,
            raised_payload(
                durability_worktree,
                "durable-escalation",
                "durable decision",
            ),
        )
    check(
        "record directory entries are durable before pointer publication",
        durability_order.index(f"sync:{escalation_records.RECORDS_NAME}")
        < durability_order.index("pointer")
        and durability_order.index(f"sync:{durability_worktree.name}")
        < durability_order.index("pointer")
        and load_latest(durability_worktree).sha256 == durable.sha256,
    )

    first = append_raise(
        worktree,
        raised_payload(worktree, "escalation-1", "first decision"),
    )
    marker_path = worktree / ".task-needs-attention.json"
    marker_bytes = marker_path.read_bytes()
    marker = json.loads(marker_bytes)
    check(
        "new writer emits a pointer-only latest marker",
        set(marker) == {"schema_version", "record_id", "record_sha256"}
        and marker["schema_version"] == 2
        and marker["record_id"] == first.record_id
        and marker["record_sha256"] == first.sha256,
    )
    first_bytes = record_path(worktree, first.record_id).read_bytes()
    marker_path.unlink()
    recovered = append_raise(
        worktree,
        raised_payload(worktree, "escalation-1", "first decision"),
    )
    check(
        "record-first crash replay restores only its matching pointer",
        recovered.sha256 == first.sha256
        and marker_path.read_bytes() == marker_bytes,
    )
    replay = append_raise(
        worktree,
        raised_payload(worktree, "escalation-1", "first decision"),
    )
    check(
        "repeated immutable raise preserves record and pointer bytes",
        replay.sha256 == first.sha256
        and record_path(worktree, first.record_id).read_bytes() == first_bytes
        and marker_path.read_bytes() == marker_bytes,
    )

    first_resolution = append_resolution(
        worktree,
        "keep the first boundary",
        resolved_at="2026-08-04T12:01:00Z",
    )
    second = append_raise(
        worktree,
        raised_payload(worktree, "escalation-2", "second decision"),
    )
    second_resolution = append_resolution(
        worktree,
        "keep the second boundary",
        resolved_at="2026-08-04T12:02:00Z",
    )
    chain = load_chain(worktree)
    check(
        "two decisions retain the complete ordered history",
        [item.record_type for item in chain]
        == ["raise", "resolution", "raise", "resolution"]
        and chain[0].payload["reason"] == "first decision"
        and chain[1].payload["decision"] == "keep the first boundary"
        and chain[2].payload["reason"] == "second decision"
        and chain[3].payload["decision"] == "keep the second boundary"
        and load_latest(worktree).sha256 == second_resolution.sha256,
    )

    amendment = append_amendment(
        worktree,
        task_id="task-a",
        root_operation_id="task-a",
        prior_plan_sha256="a" * 64,
        prior_outcome_sha256="b" * 64,
        prior_amendment_id="",
        prior_amendment_sha256="",
        new_plan_sha256="c" * 64,
        new_plan_snapshot_file=str((Path(raw) / "snapshots" / ("c" * 64 + ".md")).resolve()),
        new_outcome_sha256="d" * 64,
        decision="approve the digest-bound amendment",
        recorded_at="2026-08-04T12:03:00Z",
    )
    amendment_bytes = record_path(worktree, amendment.record_id).read_bytes()
    amendment_replay = append_amendment(
        worktree,
        task_id="task-a",
        root_operation_id="task-a",
        prior_plan_sha256="a" * 64,
        prior_outcome_sha256="b" * 64,
        prior_amendment_id="",
        prior_amendment_sha256="",
        new_plan_sha256="c" * 64,
        new_plan_snapshot_file=str((Path(raw) / "snapshots" / ("c" * 64 + ".md")).resolve()),
        new_outcome_sha256="d" * 64,
        decision="approve the digest-bound amendment",
        recorded_at="2026-08-04T12:03:00Z",
    )
    check(
        "amendment binds frozen plan and Outcome digests idempotently",
        amendment.payload["prior_plan_sha256"] == "a" * 64
        and amendment.payload["prior_outcome_sha256"] == "b" * 64
        and amendment.payload["new_plan_sha256"] == "c" * 64
        and amendment.payload["new_outcome_sha256"] == "d" * 64
        and amendment.payload["task_id"] == "task-a"
        and amendment.payload["root_operation_id"] == "task-a"
        and amendment.payload["decision"] == "approve the digest-bound amendment"
        and amendment_replay.sha256 == amendment.sha256
        and record_path(worktree, amendment.record_id).read_bytes() == amendment_bytes,
    )

    expect_error(
        "stale expected predecessor cannot overwrite a newer decision",
        lambda: append_raise(
            worktree,
            raised_payload(worktree, "escalation-stale", "stale writer"),
            expected_record_sha256=first_resolution.sha256,
        ),
        "latest record changed",
    )
    check(
        "stale writer leaves the authoritative pointer unchanged",
        load_latest(worktree).sha256 == amendment.sha256,
    )

    record = record_path(worktree, second.record_id)
    original = record.read_bytes()
    record.write_bytes(original.replace(b"second decision", b"tamper decision"))
    expect_error(
        "record tamper fails closed",
        lambda: load_chain(worktree),
        "digest",
    )
    record.write_bytes(original)

    copied = Path(raw) / "copied-task"
    copied.mkdir()
    meta(copied, task_id="task-b")
    shutil.copytree(
        worktree / ".task-escalation-records",
        copied / ".task-escalation-records",
    )
    shutil.copy2(marker_path, copied / marker_path.name)
    expect_error(
        "record chain is bound to its originating task identity",
        lambda: load_chain(copied),
        "origin",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-duplicate.") as raw:
    worktree = Path(raw)
    meta(worktree)
    append_raise(
        worktree,
        raised_payload(worktree, "duplicate-id", "original payload"),
    )
    expect_error(
        "duplicate record identity with changed payload fails closed",
        lambda: append_raise(
            worktree,
            raised_payload(worktree, "duplicate-id", "changed payload"),
        ),
        "record identity",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-chain-tamper.") as raw:
    worktree = Path(raw)
    meta(worktree)
    append_raise(
        worktree,
        raised_payload(worktree, "chain-tamper", "semantic chain"),
    )
    resolution = append_resolution(
        worktree,
        "original resolution",
        resolved_at="2026-08-04T12:04:30Z",
    )
    path = record_path(worktree, resolution.record_id)
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["payload"]["decision"] = "forged resolution"
    forged_bytes = (
        json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    path.write_bytes(forged_bytes)
    write_json(
        worktree / ".task-needs-attention.json",
        {
            "schema_version": 2,
            "record_id": resolution.record_id,
            "record_sha256": hashlib.sha256(forged_bytes).hexdigest(),
        },
    )
    expect_error(
        "chain semantics reject a re-digested forged resolution",
        lambda: load_chain(worktree),
        "identity",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-symlink.") as raw:
    worktree = Path(raw) / "task"
    worktree.mkdir()
    meta(worktree)
    target = Path(raw) / "outside-records"
    target.mkdir()
    (worktree / ".task-escalation-records").symlink_to(
        target, target_is_directory=True
    )
    expect_error(
        "symlinked records root cannot redirect immutable writes",
        lambda: append_raise(
            worktree,
            raised_payload(worktree, "symlink-root", "redirect attempt"),
        ),
        "records directory",
    )
    (worktree / ".task-escalation-records").unlink()
    marker_target = Path(raw) / "outside-marker.json"
    write_json(marker_target, raised_payload(worktree, "symlink-marker", "redirect"))
    (worktree / ".task-needs-attention.json").symlink_to(marker_target)
    expect_error(
        "symlinked latest marker cannot masquerade as absent",
        lambda: load_latest(worktree),
        "symlink",
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-legacy.") as raw:
    worktree = Path(raw)
    meta(worktree)
    legacy = raised_payload(worktree, "legacy-escalation", "legacy full marker")
    write_json(worktree / ".task-needs-attention.json", legacy)
    before = (worktree / ".task-needs-attention.json").read_bytes()
    expect_error(
        "legacy full marker fails closed at the current-schema reader",
        lambda: load_latest(worktree),
        "current pointer schema",
    )
    expect_error(
        "legacy full marker cannot trigger implicit migration",
        lambda: append_resolution(worktree, "resolve the legacy escalation"),
        "current pointer schema",
    )
    check(
        "legacy rejection has zero immutable-store or pointer effect",
        (worktree / ".task-needs-attention.json").read_bytes() == before
        and not (worktree / ".task-escalation-records").exists(),
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-custom-writer.") as raw:
    worktree = Path(raw)
    meta(worktree)
    writer = CustomWriterFixture(worktree)
    writer.notify_custom_attention("declared-stop", None)
    first_pointer = (worktree / ".task-needs-attention.json").read_bytes()
    writer.notify_custom_attention("declared-stop", None)
    custom = load_latest(worktree)
    check(
        "custom runtime writer is record-first and replay-idempotent",
        custom is not None
        and custom.payload["category"] == "pipeline-decision"
        and custom.payload["allowed_decisions"]
        == ["stop", "reapprove-pipeline"]
        and len(load_chain(worktree)) == 1
        and (worktree / ".task-needs-attention.json").read_bytes()
        == first_pointer
        and len(writer.cmux_adapter.sent) == 2,
    )
    append_resolution(
        worktree,
        "stop the custom pipeline",
        resolved_at="2026-08-04T12:05:00Z",
    )
    (
        writer.spec_path.parent
        / "pipeline-custom"
        / "attention-notify.json"
    ).unlink()
    writer.notify_custom_attention("declared-stop", None)
    check(
        "resolved custom decision cannot replay a stale wakeup",
        len(load_chain(worktree)) == 2 and len(writer.cmux_adapter.sent) == 2,
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-fix-writer.") as raw:
    worktree = Path(raw)
    meta(worktree)
    writer = ControlWriterFixture(worktree)
    receipt = SimpleNamespace(
        receipt_sha256="e" * 64,
        operation_id="reproduce-operation",
    )
    writer.notify_cannot_reproduce(receipt)
    first_pointer = (worktree / ".task-needs-attention.json").read_bytes()
    writer.notify_cannot_reproduce(receipt)
    cannot_reproduce = load_latest(worktree)
    join_reader_payload = load_attention(worktree)
    raw_latest_marker = json.loads(first_pointer)
    check(
        "engineering/fix writer is record-first and replay-idempotent",
        cannot_reproduce is not None
        and cannot_reproduce.payload["category"] == "pipeline-decision"
        and cannot_reproduce.payload["allowed_decisions"]
        == ["stop", "retry-with-fixture"]
        and cannot_reproduce.payload["receipt_operation_id"]
        == "reproduce-operation"
        and len(load_chain(worktree)) == 1
        and (worktree / ".task-needs-attention.json").read_bytes()
        == first_pointer
        and len(writer.cmux_adapter.sent) == 2,
    )
    check(
        "integration assertion must resolve the pointer through the authoritative reader",
        set(raw_latest_marker)
        == {"schema_version", "record_id", "record_sha256"}
        and join_reader_payload is not None
        and join_reader_payload["category"] == "pipeline-decision"
        and join_reader_payload["status"] == "pending"
        and join_reader_payload["allowed_decisions"]
        == ["stop", "retry-with-fixture"],
    )
    append_resolution(
        worktree,
        "stop the fix pipeline",
        resolved_at="2026-08-04T12:06:00Z",
    )
    (
        writer.spec_path.parent
        / "pipeline-fix"
        / "cannot-reproduce-notify.json"
    ).unlink()
    writer.notify_cannot_reproduce(receipt)
    check(
        "resolved engineering/fix decision cannot replay a stale wakeup",
        len(load_chain(worktree)) == 2 and len(writer.cmux_adapter.sent) == 2,
    )


def stale_custom_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "spec_id": "stale-custom-pipeline",
        "version": "1.0.0",
        "intent": "stale-escalation-regression",
        "task_profile": "change",
        "baseline_pipeline": "engineering/change",
        "route_alias": "executor-default",
        "required_capabilities": ["route:resolved"],
        "input_schema": "approved-plan/v1",
        "output_schema": "reap-ready/v1",
        "steps": [
            {
                "step_id": "design",
                "primitive_id": "model_step",
                "primitive_version": "1.0.0",
                "input_schema": "approved-plan/v1",
                "output_schema": "approved-plan/v1",
                "session_mode": "worktree",
                "semantic_skills": ["dispatch"],
            },
            {
                "step_id": "implement",
                "primitive_id": "model_step",
                "primitive_version": "1.0.0",
                "input_schema": "approved-plan/v1",
                "output_schema": "implementation-result/v1",
                "session_mode": "parent-child",
                "semantic_skills": ["tdd"],
            },
            {
                "step_id": "verify",
                "primitive_id": "verify",
                "primitive_version": "1.0.0",
                "input_schema": "implementation-result/v1",
                "output_schema": "verified-result/v1",
                "session_mode": "verification",
                "semantic_skills": [],
            },
            {
                "step_id": "review",
                "primitive_id": "review",
                "primitive_version": "1.0.0",
                "input_schema": "verified-result/v1",
                "output_schema": "reap-ready/v1",
                "session_mode": "review",
                "semantic_skills": ["review"],
            },
        ],
        "transitions": [
            {
                "from_step": "design",
                "outcome": "complete",
                "target": "implement",
                "max_traversals": 1,
            },
            {
                "from_step": "implement",
                "outcome": "complete",
                "target": "verify",
                "max_traversals": 1,
            },
            {
                "from_step": "verify",
                "outcome": "complete",
                "target": "review",
                "max_traversals": 1,
            },
            {
                "from_step": "review",
                "outcome": "complete",
                "target": "terminal:completed",
                "max_traversals": 1,
            },
        ],
        "controls": [],
        "budget": {
            "attempt_limit": 2,
            "model_restart_limit": 1,
            "time_budget_seconds": 900,
            "token_limit": 50000,
        },
        "completion_policy": "attention",
        "requested_permissions": ["git-write", "product-worktree"],
        "requested_side_effects": ["git-write", "worktree"],
        "context_pointers": [],
        "verification_checks": ["diff-check"],
        "review_mode": "skip",
        "human_gates": ["initial-approval"],
        "terminal_outcomes": ["completed", "attention-required"],
    }


def stale_custom_fixture(root: Path, name: str) -> SimpleNamespace:
    task_id = f"task-{name}"
    project_id = f"project-{name}"
    vault = root / f"vault-{name}"
    worktree = root / f"worktree-{name}"
    worktree.mkdir()
    store = OperationStore(vault / ".vault-meta" / "harness")
    parsed = parse_pipeline_spec(stale_custom_spec())
    policy = CustomPipelinePolicy.default()
    compiled = compile_custom_spec(
        parsed,
        builtin_registry(),
        policy=policy,
        capabilities=("route:resolved",),
    )
    card = render_custom_approval(parsed, compiled, policy=policy)
    approval = ExplicitPipelineApproval.for_card(
        definition_sha256=compiled.definition_sha256,
        approval_card=card,
        actor="user",
        decision="approve",
    )
    frozen = freeze_custom_pipeline(parsed, compiled, approval, card)
    plan_sha256 = "a" * 64
    head_sha = "b" * 40
    parent_spec = OperationSpec(
        operation_id=task_id,
        idempotency_key=hashlib.sha256(task_id.encode()).hexdigest(),
        kind="dispatch",
        owner_id=task_id,
        route=RuntimeRoute(
            "codex", "gpt-5.6-sol", "high", "executor", "c" * 64
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
        contract_sha256=compiled.definition_sha256,
    )
    parent = store.create(parent_spec, lane_id="custom-lane", run_id="custom-run")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(task_id, task_id, state)
    parent = store.read(task_id, task_id)
    runtime_root = store.root / "owners" / task_id / "runtime" / task_id
    FrozenPipelineStore(runtime_root.parent).save(
        operation_id=task_id,
        spec=parsed,
        frozen=frozen,
        approval=approval,
    )
    write_json(
        runtime_root / "pipeline-custom" / "controller.json",
        {
            "schema_version": 1,
            "operation_id": task_id,
            "definition_sha256": compiled.definition_sha256,
            "approved_plan_sha256": plan_sha256,
            "initial_head_sha": head_sha,
        },
    )
    first = prepare_custom_step(
        store,
        parent,
        parsed,
        definition_sha256=compiled.definition_sha256,
        approved_plan_sha256=plan_sha256,
        initial_head_sha=head_sha,
        receipts=(),
    )
    first_envelope = custom_step_envelope(
        first,
        outcome="complete",
        output_pointer=".task-pipeline/custom/00-design-output.md",
        output_sha256="d" * 64,
        head_sha=head_sha,
    )
    receipt = accept_custom_step(
        store,
        first,
        first_envelope,
        current_head_sha=head_sha,
        receipt_path=runtime_root / "pipeline-custom" / "receipts" / "000.json",
    )
    second = prepare_custom_step(
        store,
        parent,
        parsed,
        definition_sha256=compiled.definition_sha256,
        approved_plan_sha256=plan_sha256,
        initial_head_sha=head_sha,
        receipts=(receipt,),
    )
    request = custom_step_request(second)
    write_json(worktree / ".task-pipeline-step-request.json", request)
    meta_value = {
        "version": 4,
        "project_id": project_id,
        "task_id": task_id,
        "vault_root": str(vault.resolve()),
        "worktree": str(worktree.resolve()),
        "approved_plan_sha256": plan_sha256,
        "pipeline_policy": {
            "name": "custom",
            "source": "custom",
            "definition_sha256": compiled.definition_sha256,
        },
    }
    write_json(worktree / ".task-meta.json", meta_value)
    return SimpleNamespace(
        worktree=worktree,
        vault=vault,
        store=store,
        meta=meta_value,
        runtime_root=runtime_root,
        previous_operation_id=first.spec.operation_id,
        successor_operation_id=second.spec.operation_id,
        request=request,
    )


with tempfile.TemporaryDirectory(prefix="task-escalation-stale-self-heal.") as raw:
    root = Path(raw)
    positive = stale_custom_fixture(root, "positive")
    with patch.object(
        task_escalation_cli,
        "load_unattended",
        return_value=(positive.meta, {}),
    ), patch.object(
        task_escalation_cli, "read_surface", return_value="coordinator-surface"
    ), patch.object(
        task_escalation_cli, "notify"
    ) as notify_mock, patch.object(task_escalation_cli, "send") as send_mock:
        raised_rc = task_escalation_cli.raise_escalation(
            positive.worktree,
            "mechanism-failure",
            "delayed stale executor raise",
            "classify the repository-owned mechanism",
        )
    raised = load_latest(positive.worktree)
    check(
        "every untyped raise remains an ordinary fail-closed escalation",
        raised_rc == 0
        and raised is not None
        and raised.payload["status"] == "pending"
        and raised.payload["category"] == "mechanism-failure"
        and notify_mock.call_count == 1
        and send_mock.call_count == 1
        and (positive.worktree / ".task-needs-attention.json").is_file()
        and (positive.worktree / ".task-escalation-records").is_dir(),
    )


print("All task escalation record tests passed.")
