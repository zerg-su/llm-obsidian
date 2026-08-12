#!/usr/bin/env python3
"""One restart-safe XHigh fresh artifact-only repair session."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.artifact_repair import ContractArtifactOwner  # noqa: E402
from harness.adapters.claude import ClaudeDriver  # noqa: E402
from harness.adapters.codex import CodexDriver, REVIEWER_CONFIG  # noqa: E402
from harness.adapters.process import ProcessAdapter  # noqa: E402
from harness.contracts import (  # noqa: E402
    CanonicalContractTemplate,
    ContractFamily,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.fresh_artifact_repair import (  # noqa: E402
    FreshArtifactRepair,
    FreshRepairEffectUncertain,
    FreshRepairExhausted,
    FreshRepairInvalid,
    ProviderAvailability,
    select_fresh_repair_route,
)
from model_routing_config import load_config  # noqa: E402


failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}")
        failures.append(label)


class FakeManager:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def start(self, request: object) -> object:
        self.requests.append(request)
        spec = request.spec
        record = OperationRecord(
            spec,
            "running",
            1,
            request.lane_id,
            request.run_id,
            OwnedResources(surface_id="repair-surface"),
            attempt=1,
            attempt_limit=1,
            model_restart_limit=0,
        )
        return type("Started", (), {"record": record})()


config = load_config(ROOT)
prior = RuntimeRoute(
    "codex", "gpt-5.6-sol", "high", "executor", config.fingerprint
)
opposite = select_fresh_repair_route(config, prior)
check(
    "fresh repair prefers the opposite provider at XHigh",
    opposite.runtime == "claude"
    and opposite.effort == "xhigh"
    and opposite.profile == "artifact-repair",
)
try:
    select_fresh_repair_route(config, prior, same_provider=True)
except ValueError:
    no_unproven_fallback = True
else:
    no_unproven_fallback = False
fallback = select_fresh_repair_route(
    config,
    prior,
    same_provider=True,
    opposite_availability=ProviderAvailability(
        "claude", "unavailable", "9" * 64
    ),
)
check(
    "same-provider fallback requires durable opposite-provider unavailability",
    no_unproven_fallback
    and fallback.runtime == "codex"
    and fallback.effort == "xhigh",
)

with tempfile.TemporaryDirectory(prefix="fresh-artifact-repair.") as raw:
    base = Path(raw)
    worktree = base / "product"
    state = base / "store" / "runtime" / "root"
    worktree.mkdir()
    state.mkdir(parents=True)
    target = worktree / ".task-summary.json"
    template = CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="root",
        target_pointer=".task-summary.json",
        value={
            "schema_version": 2,
            "type": "repo-touch",
            "session": "session-1",
            "title": "",
            "body": "",
            "outcome_disposition": "",
            "outcome_evidence_ids": [],
            "residual_gap_pointers": [],
        },
        code_owned_fields={"schema_version", "type", "session"},
        model_owned_fields={
            "title",
            "body",
            "outcome_disposition",
            "outcome_evidence_ids",
            "residual_gap_pointers",
        },
    )
    owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=worktree,
        template=template,
        actual_target=target,
    )
    owner.restore_template()
    root_spec = OperationSpec(
        operation_id="root",
        idempotency_key="root-key",
        kind="dispatch",
        owner_id="root",
        route=prior,
        context_manifest="packets/root/manifest.json",
        verification_profile="scoped",
        root_operation_id="root",
    )
    parent = OperationRecord(
        root_spec, "running", 1, "root-lane", "root-run", OwnedResources()
    )
    repair = FreshArtifactRepair.reserve(
        owner=owner,
        parent=parent,
        invalid_sha256="1" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
    )
    product_before = {
        path.relative_to(worktree).as_posix(): path.read_bytes()
        for path in worktree.rglob("*")
        if path.is_file()
    }
    manager = FakeManager()
    started = repair.start(manager)
    replay = repair.start(manager)
    request = manager.requests[0]
    claude_route = RuntimeRoute(
        "claude", opposite.model, "xhigh", "artifact-repair", config.fingerprint
    )
    codex_route = RuntimeRoute(
        "codex", prior.model, "xhigh", "artifact-repair", config.fingerprint
    )
    claude_command = ClaudeDriver(Path("/usr/bin/claude")).command(
        claude_route,
        callback_pointer=request.cwd / request.callback_pointer,
        session_root=request.cwd,
    )
    codex_command = CodexDriver(Path("/usr/bin/codex")).command(
        codex_route,
        callback_pointer=request.cwd / request.callback_pointer,
        session_root=request.cwd,
    )
    launch = ProcessAdapter().prepare_surface_launch(
        argv=claude_command,
        cwd=request.cwd,
        state_root=state / "launch-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=request.cwd / request.callback_pointer,
        product_root=None,
        store_root=state,
        owner_id="root",
        operation_id=request.spec.operation_id,
        run_id=request.run_id,
        surface_id="22222222-2222-4222-8222-222222222222",
        runtime="claude",
        callback_mode="artifact-repair",
        origin_surface="11111111-1111-4111-8111-111111111111",
        initial_input_pointer=request.cwd / request.prompt_pointer,
    )
    scratch_files = sorted(
        path.relative_to(request.cwd).as_posix()
        for path in request.cwd.rglob("*")
        if path.is_file()
    )
    fresh_session_ok = (
        started.status == "started"
        and replay.status == "already-started"
        and len(manager.requests) == 1
        and request.cwd != worktree
        and not request.cwd.is_relative_to(worktree)
        and scratch_files == ["prompt.md", "template.json"]
        and request.attempt_limit == 1
        and request.model_restart_limit == 0
        and request.product_root is None
        and request.spec.kind == "artifact-repair"
        and request.spec.parent_operation_id == "root"
        and request.spec.root_operation_id == "root"
        and request.spec.route.profile == "artifact-repair"
        and "Bash" not in claude_command
        and "--add-dir" not in claude_command
        and "--strict-mcp-config" in claude_command
        and "--strict-config" in codex_command
        and "--add-dir" not in codex_command
        and all(value in codex_command for value in REVIEWER_CONFIG)
        and json.loads(launch.spec_path.read_text())["product_root"] == ""
    )
    check(
        "fresh session is artifact-only with zero replay budget",
        fresh_session_ok,
    )
    product_after = {
        path.relative_to(worktree).as_posix(): path.read_bytes()
        for path in worktree.rglob("*")
        if path.is_file()
    }
    check(
        "launch cannot mutate repository files or durable lifecycle authority",
        product_after == product_before
        and parent == OperationRecord(
            root_spec,
            "running",
            1,
            "root-lane",
            "root-run",
            OwnedResources(),
        ),
    )

    try:
        FreshArtifactRepair.reserve(
            owner=owner,
            parent=parent,
            invalid_sha256="2" * 64,
            route=opposite,
            origin_surface="11111111-1111-4111-8111-111111111111",
        )
    except FreshRepairExhausted:
        one_only = True
    else:
        one_only = False
    check("one family attempt cannot reserve a second fresh repair", one_only)

    callback = request.cwd / request.callback_pointer
    artifact = {
        **owner.template_value,
        "title": "Bounded repair",
        "body": "Artifact-only correction.",
        "outcome_disposition": "partially-achieved",
        "residual_gap_pointers": ["plan.md"],
    }
    payload = {
        "schema_version": 1,
        "family": "task-summary",
        "repair_id": repair.repair_id,
        "artifact": artifact,
    }
    import hashlib

    payload_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    callback.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "callback_id": f"result-{payload_sha[:24]}",
                "operation_id": request.spec.operation_id,
                "run_id": request.run_id,
                "kind": "result",
                "payload": payload,
                "payload_sha256": payload_sha,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = repair.accept(
        lambda value: value
        if value.get("title") == "Bounded repair"
        else (_ for _ in ()).throw(ValueError("invalid"))
    )
    check(
        "accepted repair emits only content-free identity and digest evidence",
        receipt.status == "self-healed"
        and receipt.family == "task-summary"
        and receipt.stage == "fresh-context"
        and json.loads(target.read_text(encoding="utf-8"))["title"]
        == "Bounded repair"
        and "Bounded repair" not in json.dumps(receipt.__dict__),
    )

with tempfile.TemporaryDirectory(prefix="fresh-artifact-invalid.") as raw:
    base = Path(raw)
    tree = base / "product"
    state = base / "state"
    tree.mkdir()
    state.mkdir()
    invalid_target = tree / ".task-summary.json"
    invalid_owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=tree,
        template=template,
        actual_target=invalid_target,
    )
    invalid_owner.restore_template()
    invalid = FreshArtifactRepair.reserve(
        owner=invalid_owner,
        parent=parent,
        invalid_sha256="5" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
    )
    invalid_manager = FakeManager()
    invalid.start(invalid_manager)
    bad_callback = invalid.scratch / ".artifact-repair-callback.json"
    bad_callback.write_text("{}\n", encoding="utf-8")
    try:
        invalid.accept(lambda value: value)
    except FreshRepairInvalid:
        invalid_rejected = True
    else:
        invalid_rejected = False
    try:
        invalid.start(FakeManager())
    except FreshRepairInvalid:
        no_second_effect = True
    else:
        no_second_effect = False
    failure = json.loads((invalid.root / "failed.json").read_text())
    restored = json.loads(invalid_target.read_text())
    check(
        "invalid fresh output converges terminally without another provider effect",
        invalid_rejected
        and no_second_effect
        and failure["status"] == "invalid"
        and set(failure) == {
            "status", "family", "stage", "repair_id", "input_sha256",
            "output_sha256", "route_sha256",
        }
        and restored == invalid_owner.template_value,
    )

with tempfile.TemporaryDirectory(prefix="fresh-artifact-crash.") as raw:
    base = Path(raw)
    tree = base / "product"
    state = base / "state"
    tree.mkdir()
    state.mkdir()
    target = tree / ".task-summary.json"
    owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=tree,
        template=template,
        actual_target=target,
    )
    owner.restore_template()
    crash = FreshArtifactRepair.reserve(
        owner=owner,
        parent=parent,
        invalid_sha256="3" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
    )
    crash.fault_observer = lambda boundary: (
        (_ for _ in ()).throw(RuntimeError("crash"))
        if boundary == "fresh-effect-reserved"
        else None
    )
    try:
        crash.start(FakeManager())
    except RuntimeError:
        pass
    reloaded = FreshArtifactRepair.load(owner=owner)
    try:
        reloaded.start(FakeManager())
    except FreshRepairEffectUncertain:
        fail_closed = True
    else:
        fail_closed = False
    check("restart never replays an uncertain provider effect", fail_closed)

try:
    FreshArtifactRepair.reserve(
        owner=owner,
        parent=parent,
        invalid_sha256="4" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
        family=ContractFamily.VERIFICATION_ESCALATION,
    )
except ValueError:
    forbidden = True
else:
    forbidden = False
check("code-owned and unregistered fresh targets fail closed", forbidden)

if failures:
    raise SystemExit(f"{len(failures)} fresh artifact repair test(s) failed")
print("All fresh artifact repair tests passed.")
