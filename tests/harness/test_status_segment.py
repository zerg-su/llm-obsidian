#!/usr/bin/env python3
"""Behavioral tests for the content-free cmux harness status segment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import AttentionReason, OperationSpec, RuntimeRoute
from harness.cli import main as cli_main
from harness import status_segment
from harness.status_segment import collect, publish, render
from harness.store import OperationStore


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def spec(owner: str, operation: str, runtime: str = "codex") -> OperationSpec:
    model = "fable" if runtime == "claude" else "gpt-5.6-sol"
    return OperationSpec(
        operation,
        f"idem-{operation}",
        "dispatch",
        owner,
        RuntimeRoute(runtime, model, "high", "executor", "a" * 64),
        "context/manifest.json",
        "focused",
    )


def advance(store: OperationStore, owner: str, operation: str, states: tuple[str, ...]) -> None:
    for state in states:
        store.transition(owner, operation, state)


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "state"
    store = OperationStore(state_root)

    store.create(spec("owner-a", "op-running"), lane_id="lane-a", run_id="run-a")
    advance(store, "owner-a", "op-running", ("preflight", "starting", "running"))

    store.create(
        spec("owner-b", "op-waiting", "claude"),
        lane_id="lane-b",
        run_id="run-b",
    )
    advance(
        store,
        "owner-b",
        "op-waiting",
        ("preflight", "starting", "running", "awaiting-callback"),
    )

    store.create(spec("owner-c", "op-attention"), lane_id="lane-c", run_id="run-c")
    store.transition(
        "owner-c",
        "op-attention",
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )

    store.create(spec("owner-a", "op-complete"), lane_id="lane-d", run_id="run-d")
    advance(
        store,
        "owner-a",
        "op-complete",
        (
            "preflight",
            "starting",
            "running",
            "finalizing",
            "exiting",
            "complete",
        ),
    )

    snapshot = collect(state_root)
    check(
        "aggregate excludes terminal history",
        (
            snapshot.completed,
            snapshot.total,
            snapshot.active,
            snapshot.waiting,
            snapshot.attention,
            snapshot.invalid,
        )
        == (1, 4, 3, 1, 1, 0),
    )
    check(
        "Claude and Codex share one content-free progress label",
        render(snapshot) == "1/4 · 3▶ 1⌛ 1!",
    )

    calls: list[list[str]] = []

    def fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    changed = publish(
        state_root,
        workspace_id="workspace:7",
        runner=fake,
        binary="/opt/cmux",
    )
    check("active snapshot publishes one workspace progress bar", changed)
    check(
        "progress update clears the legacy pill and targets the exact workspace",
        calls
        == [
            [
                "/opt/cmux",
                "clear-status",
                "llm-obsidian-harness",
                "--workspace",
                "workspace:7",
            ],
            [
                "/opt/cmux",
                "set-progress",
                "0.250000",
                "--label",
                "1/4 · 3▶ 1⌛ 1!",
                "--workspace",
                "workspace:7",
            ],
        ],
    )

    empty_calls: list[list[str]] = []

    def empty_fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        empty_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    check(
        "empty store clears stale pill",
        publish(
            Path(raw) / "empty",
            workspace_id="workspace:7",
            runner=empty_fake,
            binary="/opt/cmux",
        ),
    )
    check(
        "clear removes both legacy pill and workspace progress",
        empty_calls
        == [
            [
                "/opt/cmux",
                "clear-status",
                "llm-obsidian-harness",
                "--workspace",
                "workspace:7",
            ],
            [
                "/opt/cmux",
                "clear-progress",
                "--workspace",
                "workspace:7",
            ],
        ],
    )

    no_cmux_calls: list[list[str]] = []
    check(
        "missing workspace is a non-blocking no-op",
        not publish(
            state_root,
            workspace_id="",
            runner=lambda command, **kwargs: no_cmux_calls.append(command),
        )
        and not no_cmux_calls,
    )

    corrupt = state_root / "owners/owner-z/operations/op-corrupt.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{bad json\n", encoding="utf-8")
    broken = collect(state_root)
    check(
        "corrupt durable record is visible without leaking its content",
        broken.invalid == 1
        and render(broken) == "1/4 · 3▶ 1⌛ 2!",
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "terminal-state"
    store = OperationStore(state_root)
    store.create(
        spec("owner-final", "op-final"),
        lane_id="lane-final",
        run_id="run-final",
    )
    advance(
        store,
        "owner-final",
        "op-final",
        (
            "preflight",
            "starting",
            "running",
            "finalizing",
            "exiting",
            "complete",
        ),
    )
    terminal = collect(state_root, terminal_owner="owner-final")
    check(
        "explicit terminal owner preserves final progress without old owners",
        (
            terminal.completed,
            terminal.total,
            terminal.active,
            terminal.invalid,
        )
        == (1, 1, 0, 0)
        and render(terminal) == "1/1 · 0▶",
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "cli-state"
    store = OperationStore(state_root)
    cli_calls: list[list[str]] = []

    def cli_fake(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        cli_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    original_run_cmux = status_segment.run_cmux
    original_workspace = os.environ.get("CMUX_WORKSPACE_ID")
    status_segment.run_cmux = cli_fake
    os.environ["CMUX_WORKSPACE_ID"] = "workspace:terminal"
    try:
        for command in ("cancel", "close"):
            operation = f"op-{command}"
            owner = f"owner-{command}"
            store.create(
                spec(owner, operation),
                lane_id=f"lane-{command}",
                run_id=f"run-{command}",
            )
            check(
                f"CLI {command} succeeds",
                cli_main(
                    [
                        "--store",
                        str(state_root),
                        "--owner",
                        owner,
                        "--json",
                        command,
                        operation,
                    ]
                )
                == 0,
            )

        store.create(
            spec("owner-reconcile", "op-reconcile"),
            lane_id="lane-reconcile",
            run_id="run-reconcile",
        )
        store.transition("owner-reconcile", "op-reconcile", "cancelling")
        check(
            "CLI reconcile succeeds",
            cli_main(
                [
                    "--store",
                    str(state_root),
                    "--owner",
                    "owner-reconcile",
                    "--json",
                    "reconcile",
                ]
            )
            == 0,
        )
    finally:
        status_segment.run_cmux = original_run_cmux
        if original_workspace is None:
            os.environ.pop("CMUX_WORKSPACE_ID", None)
        else:
            os.environ["CMUX_WORKSPACE_ID"] = original_workspace

    check(
        "CLI cancel close and reconcile clear stale progress",
        len(cli_calls) == 6
        and cli_calls
        == [
            command
            for _ in range(3)
            for command in (
                [
                    "clear-status",
                    "llm-obsidian-harness",
                    "--workspace",
                    "workspace:terminal",
                ],
                [
                    "clear-progress",
                    "--workspace",
                    "workspace:terminal",
                ],
            )
        ],
    )
