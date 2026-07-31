#!/usr/bin/env python3
"""One-shot host-side provider exit fixture for 2.4.1 live dogfood."""

from __future__ import annotations

import json
import argparse
import re
import subprocess
import time
from pathlib import Path

from harness.adapters.process import ProcessAdapter, ProcessError


TASK_NAME = "df241-controlled-provider-restart-v2"
TASK_BRANCH = f"task/{TASK_NAME}"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class RestartFixtureError(ValueError):
    pass


def read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestartFixtureError(f"invalid restart fixture input: {path}") from exc
    if not isinstance(value, dict):
        raise RestartFixtureError(f"restart fixture input is not an object: {path}")
    return value


def request_restart(
    root: Path,
    *,
    branch: str,
    store_root: Path,
    owner_id: str,
    operation_id: str,
) -> Path:
    root = root.resolve()
    store_root = store_root.resolve()
    meta = read_object(root / ".task-meta.json")
    if (
        not IDENTIFIER.fullmatch(owner_id)
        or not IDENTIFIER.fullmatch(operation_id)
        or owner_id != operation_id
    ):
        raise RestartFixtureError("restart fixture identity is invalid")
    runtime = (
        store_root
        / "owners"
        / owner_id
        / "runtime"
        / operation_id
    )
    receipt = read_object(
        runtime / "pipeline-fix/pass-0/root-cause/receipt.json"
    )
    record = read_object(
        store_root
        / "owners"
        / owner_id
        / "operations"
        / f"{operation_id}.json"
    )
    if branch != TASK_BRANCH or meta.get("task_name") != TASK_NAME:
        raise RestartFixtureError("restart fixture is bound to one exact task")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("status") != "complete"
        or receipt.get("step_id") != "root-cause"
        or receipt.get("parent_operation_id") != operation_id
    ):
        raise RestartFixtureError("root-cause phase is not durably complete")
    spec = record.get("spec")
    resources = record.get("resources")
    if (
        record.get("schema_version") != 1
        or not isinstance(spec, dict)
        or spec.get("operation_id") != operation_id
        or spec.get("owner_id") != owner_id
        or record.get("state") in {"complete", "failed", "cancelled"}
        or record.get("model_restarts") != 0
        or type(record.get("model_restart_limit")) is not int
        or int(record["model_restart_limit"]) < 1
        or not isinstance(resources, dict)
    ):
        raise RestartFixtureError("provider operation is not restartable")
    process_group = resources.get("process_group")
    supervisor_pid = resources.get("supervisor_pid")
    process_identity = resources.get("process_identity")
    supervisor_identity = resources.get("supervisor_identity")
    if (
        type(process_group) is not int
        or process_group <= 1
        or type(supervisor_pid) is not int
        or supervisor_pid <= 1
        or not isinstance(process_identity, str)
        or not isinstance(supervisor_identity, str)
    ):
        raise RestartFixtureError("provider ownership is invalid")
    marker = root / ".task-pipeline/provider-restart-requested.json"
    if marker.exists():
        raise RestartFixtureError("provider restart was already requested")
    control = runtime / "process-control.json"
    try:
        ProcessAdapter.request_guardian_signal(
            control,
            action="request-exit",
            operation_id=operation_id,
            run_id=str(record.get("run_id") or ""),
            process_group=process_group,
            process_identity=process_identity,
            supervisor_pid=supervisor_pid,
            supervisor_identity=supervisor_identity,
        )
        command = read_object(control)
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "status": "requested",
                    "after_step": "root-cause",
                    "operation_id": operation_id,
                    "command_id": command.get("command_id"),
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
    except FileExistsError as exc:
        raise RestartFixtureError("provider restart was already requested") from exc
    except ProcessError as exc:
        raise RestartFixtureError("guardian restart request was rejected") from exc
    return marker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--wait-seconds", type=float, default=900.0)
    args = parser.parse_args()
    root = Path(args.worktree).resolve()
    store_root = (
        Path(args.vault_root).resolve() / ".vault-meta/harness"
    )
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    receipt = (
        store_root
        / "owners"
        / args.owner_id
        / "runtime"
        / args.operation_id
        / "pipeline-fix/pass-0/root-cause/receipt.json"
    )
    deadline = time.monotonic() + args.wait_seconds
    while not receipt.is_file():
        if time.monotonic() >= deadline:
            raise RestartFixtureError("root-cause receipt wait expired")
        time.sleep(0.1)
    marker = request_restart(
        root,
        branch=branch,
        store_root=store_root,
        owner_id=args.owner_id,
        operation_id=args.operation_id,
    )
    print(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
