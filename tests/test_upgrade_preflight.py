#!/usr/bin/env python3
"""Hermetic upgrade gate checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/upgrade-preflight.py"
sys.path.insert(0, str(ROOT / "scripts"))
from harness.contracts import (
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from model_routing import load_config
from task_sessions import TaskSessionStore


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args], text=True, capture_output=True, check=False)


def diagnostic(root: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    result = run(root, "--diagnose-identities")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"diagnostic did not emit JSON: stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc
    if not isinstance(value, dict):
        raise AssertionError("diagnostic must emit one JSON object")
    return result, value


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


def harness_record(
    operation_id: str,
    kind: str,
    *,
    owner_id: str,
    state: str,
    resources: OwnedResources | None = None,
    pending_effect: str = "",
) -> dict[str, object]:
    return to_dict(OperationRecord(
        spec=OperationSpec(
            operation_id=operation_id,
            idempotency_key=f"{operation_id}-key",
            kind=kind,
            owner_id=owner_id,
            route=RuntimeRoute(
                "codex", "gpt-5.6-sol", "high", "executor", "a" * 64
            ),
            context_manifest="packet/manifest.json",
            verification_profile="scoped",
        ),
        state=state,
        revision=1,
        lane_id=f"{operation_id}-lane",
        run_id=f"{operation_id}-run",
        resources=resources or OwnedResources(),
        pending_effect=pending_effect,
        effect_id=pending_effect,
        effect_outcome=(
            EffectOutcome.PENDING if pending_effect else EffectOutcome.NONE
        ),
    ))


with tempfile.TemporaryDirectory(prefix="upgrade-preflight-test.") as raw:
    root = Path(raw)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "config").mkdir()
    (root / ".codex").mkdir()
    (root / "wiki").mkdir()
    shutil.copy2(ROOT / "config/model-routing.toml", root / "config/model-routing.toml")
    (root / ".codex/dispatch-env.toml").write_text('[codex_dispatch]\nclaude_review_model = "custom-claude"\nclaude_review_effort = "xhigh"\n', encoding="utf-8")
    (root / "seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)

    result = run(root)
    check("custom legacy route needs confirmation", result.returncode == 5)
    result = run(root, "--confirm-routing-migration", "--apply")
    check("confirmed migration succeeds", result.returncode == 0)
    check("migration writes ignored-style local config", (root / "config/model-routing.local.toml").is_file())
    migrated = load_config(root)
    check(
        "migration preserves ordinary Claude default",
        migrated.runtime_default("claude")["model"] == "claude-opus-5",
    )
    check("migration changes only Claude reviewer role", migrated.reviewer_default("claude") == {
        "runtime": "claude", "model": "custom-claude", "effort": "xhigh"
    })

    (root / "config/model-routing.local.toml").unlink()
    (root / ".codex/dispatch-env.toml").write_text(
        '[codex_dispatch]\n'
        'codex_review_model = "gpt-5.6-sol"\n'
        'codex_review_effort = "high"\n'
        'claude_review_model = "fable"\n'
        'claude_review_effort = "high"\n',
        encoding="utf-8",
    )
    result = run(root)
    check("stock v2.0.8 routes need no migration", result.returncode == 0)
    check("stock routes write no local override", not (root / "config/model-routing.local.toml").exists())

    (root / ".codex/dispatch-env.toml").write_text(
        '[codex_dispatch]\nclaude_review_model = "custom-claude"\nclaude_review_effort = "ultra"\n',
        encoding="utf-8",
    )
    result = run(root, "--confirm-routing-migration", "--apply")
    check("invalid migration fails before install", result.returncode == 3)
    check("invalid migration leaves no local override", not (root / "config/model-routing.local.toml").exists())
    (root / ".codex/dispatch-env.toml").write_text(
        '[codex_dispatch]\n'
        'codex_review_model = "gpt-5.6-sol"\n'
        'codex_review_effort = "high"\n'
        'claude_review_model = "fable"\n'
        'claude_review_effort = "high"\n',
        encoding="utf-8",
    )

    harness_ops = root / ".vault-meta/harness/owners/upgrade-test/operations"
    harness_ops.mkdir(parents=True)
    for kind in ("dispatch", "review", "research", "reap", "prototype", "resolve-conflict"):
        (harness_ops / f"{kind}.json").write_text(
            json.dumps(harness_record(
                kind,
                kind,
                owner_id="upgrade-test",
                state="running",
            )),
            encoding="utf-8",
        )
    result = run(root)
    check(
        "every active harness operation kind blocks upgrade",
        result.returncode == 4
        and all(f"harness:{kind}:{kind}" in result.stderr for kind in (
            "dispatch", "review", "research", "reap", "prototype", "resolve-conflict"
        )),
    )
    check(
        "active-operation rejection has one recovery instruction",
        result.stderr.count("Recovery:") == 1
        and "finish or cancel live operations" in result.stderr
        and "exact ownership reconciliation" in result.stderr,
    )
    shutil.rmtree(root / ".vault-meta")

    (root / ".vault-meta/research-runs/11111111-1111-1111-1111-111111111111").mkdir(parents=True)
    (root / ".vault-meta/research-runs/11111111-1111-1111-1111-111111111111/state.json").write_text(
        json.dumps({"schema_version": 1, "status": "fetch_ready"}), encoding="utf-8"
    )
    result = run(root)
    check("unfinished legacy research blocks upgrade", result.returncode == 4 and "research:" in result.stderr)
    shutil.rmtree(root / ".vault-meta")

    (root / ".task-meta.json").write_text(json.dumps({"version": 1, "task_name": "active"}), encoding="utf-8")
    result = run(root)
    check("active task blocks upgrade", result.returncode == 4)

    (root / ".task-meta.json").unlink()
    project_id = "11111111-1111-4111-8111-111111111111"
    terminal_id = "22222222-2222-4222-8222-222222222222"
    broker_store = TaskSessionStore(root)
    broker_store.create_task(project_id, terminal_id, worktree=root)
    broker = broker_store.task_dir(project_id, terminal_id)
    result = run(root)
    check("active broker task blocks upgrade", result.returncode == 4 and "broker-task:" in result.stderr)

    (root / ".task-meta.json").write_text(json.dumps({
        "version": 3,
        "task_id": terminal_id,
        "task_name": "cancelled",
        "worktree": str(root),
    }), encoding="utf-8")
    harness_ops = root / ".vault-meta/harness/owners" / terminal_id / "operations"
    harness_ops.mkdir(parents=True)
    terminal_path = harness_ops / f"{terminal_id}.json"
    owned = OwnedResources(
        surface_id="owned-surface",
        process_group=12345,
        process_identity="b" * 64,
        supervisor_pid=12344,
        supervisor_identity="c" * 64,
    )
    terminal_path.write_text(json.dumps(harness_record(
        terminal_id,
        "dispatch",
        owner_id=terminal_id,
        state="cancelled",
        resources=owned,
        pending_effect="request-exit",
    )), encoding="utf-8")
    result = run(root)
    check(
        "terminal dispatch with unsettled ownership keeps legacy mirrors active",
        result.returncode == 4
        and f"broker-task:{terminal_id}" in result.stderr
        and f"harness:dispatch:{terminal_id}" in result.stderr,
    )

    terminal_kinds = ("review", "research", "reap", "prototype", "resolve-conflict")
    terminal_other_paths: list[tuple[str, str, Path]] = []
    for kind in terminal_kinds:
        owner_id = f"terminal-{kind}"
        operation_id = f"{owner_id}-operation"
        operation_path = (
            root / ".vault-meta/harness/owners" / owner_id
            / "operations" / f"{operation_id}.json"
        )
        operation_path.parent.mkdir(parents=True)
        operation_path.write_text(json.dumps(harness_record(
            operation_id,
            kind,
            owner_id=owner_id,
            state="failed",
            resources=owned,
            pending_effect="request-exit",
        )), encoding="utf-8")
        terminal_other_paths.append((kind, operation_id, operation_path))
    result = run(root)
    check(
        "every resource-bearing terminal harness kind blocks upgrade",
        result.returncode == 4
        and all(
            f"harness:{kind}:{operation_id}" in result.stderr
            for kind, operation_id, _path in terminal_other_paths
        ),
    )
    for kind, operation_id, operation_path in terminal_other_paths:
        operation_path.write_text(json.dumps(harness_record(
            operation_id,
            kind,
            owner_id=operation_path.parents[1].name,
            state="failed",
        )), encoding="utf-8")

    terminal_path.write_text(json.dumps({
        "schema_version": 1,
        "state": "cancelled",
        "pending_effect": "",
        "resources": {
            "surface_id": "",
            "process_group": 0,
            "process_identity": "",
            "supervisor_pid": 0,
            "supervisor_identity": "",
        },
        "spec": {
            "operation_id": terminal_id,
            "owner_id": terminal_id,
            "kind": "dispatch",
        },
    }), encoding="utf-8")
    result = run(root)
    check(
        "malformed terminal dispatch cannot release legacy mirrors",
        result.returncode == 4
        and f"broker-task:{terminal_id}" in result.stderr
        and f"harness:dispatch:{terminal_id}" in result.stderr,
    )

    terminal_path.write_text(json.dumps(harness_record(
        terminal_id,
        "dispatch",
        owner_id=terminal_id,
        state="cancelled",
    )), encoding="utf-8")
    (root / ".task-meta.json").write_text(json.dumps({
        "version": 3,
        "task_id": terminal_id,
        "task_name": "cancelled",
        "worktree": str(root / "different-worktree"),
    }), encoding="utf-8")
    result = run(root)
    check(
        "released dispatch cannot release a mismatched worktree mirror",
        result.returncode == 4
        and f"task:{root.name}" in result.stderr
        and f"broker-task:{terminal_id}" not in result.stderr,
    )

    (root / ".task-meta.json").write_text(json.dumps({
        "version": 3,
        "task_id": terminal_id,
        "task_name": "cancelled",
        "worktree": str(root),
    }), encoding="utf-8")
    result = run(root)
    check(
        "resource-free terminal dispatch proves same-ID legacy mirrors stale",
        result.returncode == 0
        and f"broker-task:{terminal_id}" not in result.stderr
        and "task:upgrade-preflight-test" not in result.stderr,
    )

    broker_path = broker / "task.json"
    broker_task = json.loads(broker_path.read_text(encoding="utf-8"))
    broker_identity_cases = (
        ("missing project ID", "project_id", None),
        ("mismatched project ID", "project_id", "33333333-3333-4333-8333-333333333333"),
        ("missing task ID", "task_id", None),
        ("mismatched task ID", "task_id", "44444444-4444-4444-8444-444444444444"),
    )
    for label, field, value in broker_identity_cases:
        corrupt = dict(broker_task)
        if value is None:
            corrupt.pop(field)
        else:
            corrupt[field] = value
        broker_path.write_text(json.dumps(corrupt), encoding="utf-8")
        result = run(root)
        check(
            f"released dispatch cannot release broker with {label}",
            result.returncode == 4 and "broker-task:" in result.stderr,
        )
    broker_path.write_text(json.dumps(broker_task), encoding="utf-8")

print("upgrade preflight tests passed")


with tempfile.TemporaryDirectory(prefix="upgrade-identity-diagnostic.") as raw:
    root = Path(raw)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    resolved_root = root.resolve()
    task_id = "55555555-5555-4555-8555-555555555555"
    operation_path = (
        root / ".vault-meta/harness/owners" / task_id
        / "operations" / f"{task_id}.json"
    )
    operation_path.parent.mkdir(parents=True)
    task_path = root / ".task-meta.json"
    task_path.write_text(json.dumps({
        "version": 3,
        "task_id": task_id,
        "task_name": "identity-diagnostic",
        "worktree": str(root),
    }), encoding="utf-8")

    operation_path.write_text(json.dumps(harness_record(
        task_id,
        "dispatch",
        owner_id=task_id,
        state="running",
    )), encoding="utf-8")
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    result, packet = diagnostic(root)
    rows = packet.get("diagnostics", [])
    check(
        "identity diagnostic classifies the exact live operation and worktree as active",
        result.returncode == 4
        and packet.get("status") == "attention-required"
        and packet.get("read_only") is True
        and packet.get("counts", {}).get("active") == 2
        and {
            (row.get("resource"), row.get("classification"))
            for row in rows
        } == {("operation", "active"), ("worktree", "active")},
    )
    active_operation = next(
        row for row in rows if row.get("resource") == "operation"
    )
    check(
        "active recovery binds the exact operation identity",
        active_operation.get("identity") == {
            "operation_id": task_id,
            "owner_id": task_id,
        }
        and active_operation.get("recovery", {}).get("action")
        == "finish-or-cancel-exact-operation"
        and active_operation.get("recovery", {}).get("inspect_command", [])[-2:]
        == ["inspect", task_id],
    )

    operation_path.write_text(json.dumps(harness_record(
        task_id,
        "dispatch",
        owner_id=task_id,
        state="complete",
    )), encoding="utf-8")
    result, packet = diagnostic(root)
    rows = packet.get("diagnostics", [])
    stale_worktree = next(
        row for row in rows if row.get("resource") == "worktree"
    )
    check(
        "identity diagnostic proves only an exact terminal resource-free pair stale",
        result.returncode == 0
        and packet.get("status") == "stale"
        and packet.get("counts", {}).get("proven-stale") == 2
        and all(row.get("classification") == "proven-stale" for row in rows)
        and stale_worktree.get("identity", {}).get("task_id") == task_id,
    )
    check(
        "proven-stale guidance inspects before naming the exact Git removal",
        stale_worktree.get("recovery", {}).get("action")
        == "inspect-then-remove-exact-worktree"
        and stale_worktree.get("recovery", {}).get("inspect_command")
        == ["git", "-C", str(resolved_root), "status", "--short"]
        and stale_worktree.get("recovery", {}).get("recovery_command")
        == [
            "git", "-C", str(resolved_root), "worktree", "remove",
            str(resolved_root),
        ],
    )

    owned = OwnedResources(
        surface_id="owned-surface",
        process_group=12345,
        process_identity="b" * 64,
        supervisor_pid=12344,
        supervisor_identity="c" * 64,
    )
    operation_path.write_text(json.dumps(harness_record(
        task_id,
        "dispatch",
        owner_id=task_id,
        state="cancelled",
        resources=owned,
        pending_effect="request-exit",
    )), encoding="utf-8")
    result, packet = diagnostic(root)
    rows = packet.get("diagnostics", [])
    check(
        "terminal unsettled ownership remains ambiguous and fail-closed",
        result.returncode == 4
        and packet.get("counts", {}).get("ambiguous") == 2
        and all(row.get("classification") == "ambiguous" for row in rows)
        and all(
            row.get("recovery", {}).get("action")
            == "inspect-and-reconcile-exact-ownership"
            for row in rows
        )
        and "worktree remove"
        not in json.dumps(packet, sort_keys=True),
    )

    recorded_owner = "66666666-6666-4666-8666-666666666666"
    operation_path.write_text(json.dumps(harness_record(
        task_id,
        "dispatch",
        owner_id=recorded_owner,
        state="complete",
    )), encoding="utf-8")
    result, packet = diagnostic(root)
    rows = packet.get("diagnostics", [])
    mismatched_operation = next(
        row for row in rows if row.get("resource") == "operation"
    )
    check(
        "path and recorded owners are reported as mismatched without guessing",
        result.returncode == 4
        and packet.get("counts", {}).get("mismatched") == 2
        and all(row.get("classification") == "mismatched" for row in rows)
        and mismatched_operation.get("identity") == {
            "operation_id": task_id,
            "owner_id": recorded_owner,
            "path_operation_id": task_id,
            "path_owner_id": task_id,
        }
        and all(
            row.get("recovery", {}).get("action")
            == "resolve-identity-mismatch"
            for row in rows
        )
        and "worktree remove"
        not in json.dumps(packet, sort_keys=True),
    )
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    check(
        "identity diagnostic never mutates repository or harness state",
        before.keys() == after.keys()
        and all(
            before[path] == after[path]
            for path in before
            if path != operation_path.relative_to(root).as_posix()
        ),
    )

print("upgrade identity diagnostic tests passed")
