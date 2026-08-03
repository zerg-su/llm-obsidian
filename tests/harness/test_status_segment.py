#!/usr/bin/env python3
"""Behavioral tests for the content-free cmux harness status segment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (
    AttentionReason,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.cli import main as cli_main
from harness import status_segment
from harness.status_segment import HarnessStatus, publish, render
from harness.store import OperationStore


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def spec(
    owner: str,
    operation: str,
    runtime: str = "codex",
    *,
    kind: str = "dispatch",
) -> OperationSpec:
    model = "fable" if runtime == "claude" else "gpt-5.6-sol"
    return OperationSpec(
        operation,
        f"idem-{operation}",
        kind,
        owner,
        RuntimeRoute(runtime, model, "high", "executor", "a" * 64),
        "context/manifest.json",
        "focused",
    )


def advance(store: OperationStore, owner: str, operation: str, states: tuple[str, ...]) -> None:
    for state in states:
        store.transition(owner, operation, state)


def bind_runtime(
    store: OperationStore,
    owner: str,
    operation: str,
    *,
    origin_surface: str,
    surface_id: str = "",
) -> None:
    record = store.read(owner, operation)
    if surface_id:
        store.save(
            replace(record, resources=OwnedResources(surface_id=surface_id)),
            expected_revision=record.revision,
        )
        record = store.read(owner, operation)
    runtime = (
        store.root
        / "owners"
        / owner
        / "runtime"
        / operation
    )
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": operation,
                "run_id": record.run_id,
                "origin_surface": origin_surface,
                "placement": "split",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def cmux_tree(
    *placements: tuple[str, str, str],
) -> str:
    windows: dict[str, dict[str, list[str]]] = {}
    for surface_id, workspace_id, window_id in placements:
        windows.setdefault(window_id, {}).setdefault(workspace_id, []).append(
            surface_id
        )
    return json.dumps(
        {
            "windows": [
                {
                    "id": window_id,
                    "workspaces": [
                        {
                            "id": workspace_id,
                            "panes": [
                                {
                                    "id": f"pane:{workspace_index + 1}",
                                    "surfaces": [
                                        {"id": surface_id}
                                        for surface_id in surfaces
                                    ],
                                }
                            ],
                        }
                        for workspace_index, (workspace_id, surfaces) in enumerate(
                            workspaces.items()
                        )
                    ],
                }
                for window_id, workspaces in windows.items()
            ]
        },
        sort_keys=True,
    )


def topology_runner(
    calls: list[list[str]],
    tree: str,
    *,
    tree_returncode: int = 0,
):
    def fake(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:] == [
            "--id-format",
            "both",
            "tree",
            "--all",
            "--json",
        ]:
            return subprocess.CompletedProcess(
                command,
                tree_returncode,
                tree if tree_returncode == 0 else "",
                "" if tree_returncode == 0 else "inventory unavailable",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    return fake


def ui_calls(calls: list[list[str]]) -> list[list[str]]:
    return [
        command
        for command in calls
        if len(command) > 1
        and command[1] in {"clear-status", "set-progress", "clear-progress"}
    ]


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "state"
    store = OperationStore(state_root)
    workspace = "12121212-1212-1212-1212-121212121212"
    window = "13131313-1313-1313-1313-131313131313"
    origin_running = "14141414-1414-1414-1414-141414141414"
    origin_waiting = "15151515-1515-1515-1515-151515151515"
    origin_attention = "16161616-1616-1616-1616-161616161616"
    surface_running = "17171717-1717-1717-1717-171717171717"
    surface_waiting = "18181818-1818-1818-1818-181818181818"
    surface_attention = "19191919-1919-1919-1919-191919191919"

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

    store.create(
        spec(
            "owner-a",
            "op-complete",
            kind="pipeline-model-step",
        ),
        lane_id="lane-a",
        run_id="run-d",
    )
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

    bind_runtime(
        store,
        "owner-a",
        "op-running",
        origin_surface=origin_running,
        surface_id=surface_running,
    )
    bind_runtime(
        store,
        "owner-b",
        "op-waiting",
        origin_surface=origin_waiting,
        surface_id=surface_waiting,
    )
    bind_runtime(
        store,
        "owner-c",
        "op-attention",
        origin_surface=origin_attention,
        surface_id=surface_attention,
    )

    expected = HarnessStatus(
        completed=1,
        total=4,
        active=3,
        waiting=1,
        attention=1,
    )
    check(
        "Claude and Codex share one content-free progress label",
        render(expected) == "1/4 · 3▶ 1⌛ 1!",
    )

    calls: list[list[str]] = []

    def fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:] == [
            "--id-format",
            "both",
            "tree",
            "--all",
            "--json",
        ]:
            return subprocess.CompletedProcess(
                command,
                0,
                cmux_tree(
                    (origin_running, workspace, window),
                    (surface_running, workspace, window),
                    (origin_waiting, workspace, window),
                    (surface_waiting, workspace, window),
                    (origin_attention, workspace, window),
                    (surface_attention, workspace, window),
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    changed = publish(
        state_root,
        workspace_id=workspace,
        runner=fake,
        binary="/opt/cmux",
    )
    check("active snapshot publishes one workspace progress bar", changed)
    check(
        "progress update clears the legacy pill and targets the exact workspace",
        sum("tree" in command for command in calls) == 1
        and ui_calls(calls)
        == [
            [
                "/opt/cmux",
                "clear-status",
                "llm-obsidian-harness",
                "--workspace",
                workspace,
            ],
            [
                "/opt/cmux",
                "set-progress",
                "0.250000",
                "--label",
                "1/4 · 3▶ 1⌛ 1!",
                "--workspace",
                workspace,
            ],
        ],
    )

    empty_calls: list[list[str]] = []

    def empty_fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        empty_calls.append(command)
        if command[1:] == [
            "--id-format",
            "both",
            "tree",
            "--all",
            "--json",
        ]:
            return subprocess.CompletedProcess(command, 0, cmux_tree(), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    check(
        "empty store clears stale pill",
        publish(
            Path(raw) / "empty",
            workspace_id=workspace,
            runner=empty_fake,
            binary="/opt/cmux",
        ),
    )
    check(
        "clear removes both legacy pill and workspace progress",
        sum("tree" in command for command in empty_calls) == 1
        and ui_calls(empty_calls)
        == [
            [
                "/opt/cmux",
                "clear-status",
                "llm-obsidian-harness",
                "--workspace",
                workspace,
            ],
            [
                "/opt/cmux",
                "clear-progress",
                "--workspace",
                workspace,
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
    terminal_calls: list[list[str]] = []
    check(
        "terminal transition clears progress instead of preserving 100 percent",
        publish(
            state_root,
            terminal_owner="owner-final",
            workspace_id="workspace:terminal",
            runner=topology_runner(terminal_calls, cmux_tree()),
            binary="/opt/cmux",
        )
        and ui_calls(terminal_calls)
        == [
            [
                "/opt/cmux",
                "clear-status",
                "llm-obsidian-harness",
                "--workspace",
                "workspace:terminal",
            ],
            [
                "/opt/cmux",
                "clear-progress",
                "--workspace",
                "workspace:terminal",
            ],
        ],
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "truth-matrix"
    store = OperationStore(state_root)
    workspace_a = "33333333-3333-3333-3333-333333333333"
    workspace_b = "44444444-4444-4444-4444-444444444444"
    window_a = "55555555-5555-5555-5555-555555555555"
    window_b = "66666666-6666-6666-6666-666666666666"
    origin_a = "77777777-7777-7777-7777-777777777777"
    origin_b = "88888888-8888-8888-8888-888888888888"
    surface_a = "99999999-9999-9999-9999-999999999999"
    surface_b = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    store.create(
        spec("owner-live-a", "controller-live-a"),
        lane_id="lane-live-a",
        run_id="run-live-a",
    )
    advance(
        store,
        "owner-live-a",
        "controller-live-a",
        ("preflight", "starting", "running", "awaiting-callback"),
    )
    bind_runtime(
        store,
        "owner-live-a",
        "controller-live-a",
        origin_surface=origin_a,
        surface_id=surface_a,
    )
    store.create(
        spec(
            "owner-live-a",
            "child-complete-a",
            kind="pipeline-model-step",
        ),
        lane_id="lane-live-a",
        run_id="run-child-a",
    )
    advance(
        store,
        "owner-live-a",
        "child-complete-a",
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )

    store.create(
        spec("owner-live-b", "controller-live-b", "claude"),
        lane_id="lane-live-b",
        run_id="run-live-b",
    )
    advance(
        store,
        "owner-live-b",
        "controller-live-b",
        ("preflight", "starting", "running"),
    )
    bind_runtime(
        store,
        "owner-live-b",
        "controller-live-b",
        origin_surface=origin_b,
        surface_id=surface_b,
    )

    scoped_calls: list[list[str]] = []
    tree = cmux_tree(
        (origin_a, workspace_a, window_a),
        (surface_a, workspace_a, window_a),
        (origin_b, workspace_b, window_b),
        (surface_b, workspace_b, window_b),
    )
    check(
        "workspace aggregate includes only exact live programs from its origin",
        publish(
            state_root,
            workspace_id=workspace_a,
            runner=topology_runner(scoped_calls, tree),
            binary="/opt/cmux",
        )
        and ui_calls(scoped_calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.500000",
            "--label",
            "1/2 · 1▶ 1⌛",
            "--workspace",
            workspace_a,
        ]
        and sum("tree" in command for command in scoped_calls) == 1,
        scoped_calls,
    )

    missing_calls: list[list[str]] = []
    missing_tree = cmux_tree((origin_a, workspace_a, window_a))
    check(
        "missing exact controller surface makes the workspace idle",
        publish(
            state_root,
            workspace_id=workspace_a,
            runner=topology_runner(missing_calls, missing_tree),
            binary="/opt/cmux",
        )
        and ui_calls(missing_calls)[-1]
        == [
            "/opt/cmux",
            "clear-progress",
            "--workspace",
            workspace_a,
        ],
        missing_calls,
    )

    unknown_calls: list[list[str]] = []
    check(
        "unknown live inventory preserves the existing projection",
        not publish(
            state_root,
            workspace_id=workspace_a,
            runner=topology_runner(
                unknown_calls,
                "",
                tree_returncode=1,
            ),
            binary="/opt/cmux",
        )
        and ui_calls(unknown_calls) == [],
        unknown_calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "starting-boundary"
    store = OperationStore(state_root)
    origin = "abababab-abab-abab-abab-abababababab"
    workspace = "bcbcbcbc-bcbc-bcbc-bcbc-bcbcbcbcbcbc"
    window = "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd"
    store.create(
        spec("owner-start", "controller-start"),
        lane_id="lane-start",
        run_id="run-start",
    )
    store.transition("owner-start", "controller-start", "preflight")
    bind_runtime(
        store,
        "owner-start",
        "controller-start",
        origin_surface=origin,
    )
    boundary_calls: list[list[str]] = []
    for state in ("preflight", "starting"):
        if state == "starting":
            store.transition("owner-start", "controller-start", state)
        check(
            f"{state} controller is visible before surface binding",
            publish(
                state_root,
                terminal_owner="owner-start",
                workspace_id=workspace,
                runner=topology_runner(
                    boundary_calls,
                    cmux_tree((origin, workspace, window)),
                ),
                binary="/opt/cmux",
            )
            and ui_calls(boundary_calls)[-1]
            == [
                "/opt/cmux",
                "set-progress",
                "0.000000",
                "--label",
                "0/1 · 1▶",
                "--workspace",
                workspace,
            ],
            boundary_calls,
        )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "stale-controller"
    store = OperationStore(state_root)
    store.create(
        spec("owner-stale", "controller-stale"),
        lane_id="lane-stale",
        run_id="run-stale",
    )
    advance(
        store,
        "owner-stale",
        "controller-stale",
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )
    store.create(
        spec(
            "owner-stale",
            "child-stale",
            kind="pipeline-model-step",
        ),
        lane_id="lane-stale",
        run_id="run-child-stale",
    )
    advance(
        store,
        "owner-stale",
        "child-stale",
        ("preflight", "starting", "running"),
    )
    stale_calls: list[list[str]] = []
    check(
        "terminal top-level controller suppresses a stale nonterminal child",
        publish(
            state_root,
            terminal_owner="owner-stale",
            workspace_id="workspace:stale",
            runner=topology_runner(stale_calls, cmux_tree()),
            binary="/opt/cmux",
        )
        and ui_calls(stale_calls)[-1]
        == [
            "/opt/cmux",
            "clear-progress",
            "--workspace",
            "workspace:stale",
        ],
        stale_calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "corrupt-history"
    corrupt = state_root / "owners/old-owner/operations/old-corrupt.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{bad json\n", encoding="utf-8")
    corrupt_calls: list[list[str]] = []
    check(
        "corrupt inactive history cannot establish live workspace work",
        publish(
            state_root,
            workspace_id="workspace:corrupt",
            runner=topology_runner(corrupt_calls, cmux_tree()),
            binary="/opt/cmux",
        )
        and ui_calls(corrupt_calls)[-1]
        == [
            "/opt/cmux",
            "clear-progress",
            "--workspace",
            "workspace:corrupt",
        ],
        corrupt_calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "cli-state"
    store = OperationStore(state_root)
    cli_calls: list[list[str]] = []

    def cli_fake(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        cli_calls.append(command)
        if command == [
            "--id-format",
            "both",
            "tree",
            "--all",
            "--json",
        ]:
            return subprocess.CompletedProcess(
                command, 0, cmux_tree(), ""
            )
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
        sum("tree" in command for command in cli_calls) == 3
        and [
            command
            for command in cli_calls
            if command
            and command[0]
            in {"clear-status", "set-progress", "clear-progress"}
        ]
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
