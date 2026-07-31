#!/usr/bin/env python3
"""One-shot provider exit fixture for the 2.4.1 live dogfood window."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Callable


TASK_NAME = "df241-controlled-provider-restart"
TASK_BRANCH = f"task/{TASK_NAME}"


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
    process_group: int,
    signal_group: Callable[[int, int], None] = os.killpg,
) -> Path:
    root = root.resolve()
    meta = read_object(root / ".task-meta.json")
    receipt = read_object(
        root / ".task-pipeline/results/pass-0/root-cause.json"
    )
    if branch != TASK_BRANCH or meta.get("task_name") != TASK_NAME:
        raise RestartFixtureError("restart fixture is bound to one exact task")
    if receipt.get("schema_version") != 1 or receipt.get("status") != "complete":
        raise RestartFixtureError("root-cause phase is not durably complete")
    if type(process_group) is not int or process_group <= 1:
        raise RestartFixtureError("provider process group is invalid")
    marker = root / ".task-pipeline/provider-restart-requested.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "status": "requested",
                    "after_step": "root-cause",
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
    except FileExistsError as exc:
        raise RestartFixtureError("provider restart was already requested") from exc
    signal_group(process_group, signal.SIGTERM)
    return marker


def main() -> int:
    root = Path.cwd()
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    request_restart(
        root,
        branch=branch,
        process_group=os.getpgrp(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
