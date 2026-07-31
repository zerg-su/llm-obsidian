#!/usr/bin/env python3
"""Focused checks for the temporary live provider-restart dogfood helper."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dogfood_provider_restart import RestartFixtureError, request_restart


def write_fixture(root: Path, *, task_name: str, step_id: str) -> None:
    (root / ".task-pipeline/results/pass-0").mkdir(parents=True)
    (root / ".task-meta.json").write_text(
        json.dumps({"task_name": task_name}) + "\n",
        encoding="utf-8",
    )
    (root / ".task-pipeline-step-request.json").write_text(
        json.dumps({"step_id": step_id, "pass_index": 0}) + "\n",
        encoding="utf-8",
    )
    (root / ".task-pipeline/results/pass-0/root-cause.json").write_text(
        json.dumps({"schema_version": 1, "status": "complete"}) + "\n",
        encoding="utf-8",
    )


with tempfile.TemporaryDirectory(prefix="dogfood-provider-restart.") as raw:
    root = Path(raw)
    write_fixture(
        root,
        task_name="df241-controlled-provider-restart",
        step_id="root-cause",
    )
    signals: list[tuple[int, int]] = []
    marker = request_restart(
        root,
        branch="task/df241-controlled-provider-restart",
        process_group=4242,
        signal_group=lambda pgid, signum: signals.append((pgid, signum)),
    )
    assert marker.is_file()
    assert signals == [(4242, 15)]
    try:
        request_restart(
            root,
            branch="task/df241-controlled-provider-restart",
            process_group=4242,
            signal_group=lambda _pgid, _signum: None,
        )
    except RestartFixtureError:
        pass
    else:
        raise AssertionError("restart fixture must be one-shot")

with tempfile.TemporaryDirectory(prefix="dogfood-provider-restart.") as raw:
    root = Path(raw)
    write_fixture(
        root,
        task_name="another-task",
        step_id="root-cause",
    )
    try:
        request_restart(
            root,
            branch="task/df241-controlled-provider-restart",
            process_group=4242,
            signal_group=lambda _pgid, _signum: None,
        )
    except RestartFixtureError:
        pass
    else:
        raise AssertionError("restart fixture must reject every other task")

print("dogfood provider restart tests passed")
