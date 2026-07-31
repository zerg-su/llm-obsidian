#!/usr/bin/env python3
"""Focused checks for the temporary live provider-restart dogfood helper."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dogfood_provider_restart import RestartFixtureError, request_restart


def write_fixture(
    root: Path,
    store: Path,
    *,
    task_name: str,
    operation_id: str,
) -> None:
    (root / ".task-pipeline/results/pass-0").mkdir(parents=True)
    (root / ".task-meta.json").write_text(
        json.dumps({"task_name": task_name}) + "\n",
        encoding="utf-8",
    )
    runtime = (
        store
        / "owners"
        / operation_id
        / "runtime"
        / operation_id
    )
    receipt = runtime / "pipeline-fix/pass-0/root-cause/receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "step_id": "root-cause",
                "parent_operation_id": operation_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime.chmod(stat.S_IRWXU)
    operations = store / "owners" / operation_id / "operations"
    operations.mkdir()
    (operations / f"{operation_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "awaiting-callback",
                "run_id": "run-1",
                "model_restarts": 0,
                "model_restart_limit": 1,
                "resources": {
                    "process_group": 4242,
                    "process_identity": "a" * 64,
                    "supervisor_pid": 4343,
                    "supervisor_identity": "b" * 64,
                },
                "spec": {
                    "operation_id": operation_id,
                    "owner_id": operation_id,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


with tempfile.TemporaryDirectory(prefix="dogfood-provider-restart.") as raw:
    root = Path(raw)
    store = root / "store"
    operation_id = "11111111-1111-4111-8111-111111111111"
    write_fixture(
        root,
        store,
        task_name="df241-controlled-provider-restart-v2",
        operation_id=operation_id,
    )
    marker = request_restart(
        root,
        branch="task/df241-controlled-provider-restart-v2",
        store_root=store,
        owner_id=operation_id,
        operation_id=operation_id,
    )
    assert marker.is_file()
    control = json.loads(
        (
            store
            / "owners"
            / operation_id
            / "runtime"
            / operation_id
            / "process-control.json"
        ).read_text(encoding="utf-8")
    )
    assert control["action"] == "request-exit"
    assert control["process_group"] == 4242
    assert control["process_identity"] == "a" * 64
    assert control["supervisor_pid"] == 4343
    assert control["supervisor_identity"] == "b" * 64
    assert len(control["command_id"]) == 64
    try:
        request_restart(
            root,
            branch="task/df241-controlled-provider-restart-v2",
            store_root=store,
            owner_id=operation_id,
            operation_id=operation_id,
        )
    except RestartFixtureError:
        pass
    else:
        raise AssertionError("restart fixture must be one-shot")

with tempfile.TemporaryDirectory(prefix="dogfood-provider-restart.") as raw:
    root = Path(raw)
    store = root / "store"
    operation_id = "22222222-2222-4222-8222-222222222222"
    write_fixture(
        root,
        store,
        task_name="another-task",
        operation_id=operation_id,
    )
    try:
        request_restart(
            root,
            branch="task/df241-controlled-provider-restart-v2",
            store_root=store,
            owner_id=operation_id,
            operation_id=operation_id,
        )
    except RestartFixtureError:
        pass
    else:
        raise AssertionError("restart fixture must reject every other task")

print("dogfood provider restart tests passed")
