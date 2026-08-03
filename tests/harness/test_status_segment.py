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

TERMINAL_WORKSPACE = "45454545-4545-4545-4545-454545454545"
STALE_WORKSPACE = "46464646-4646-4646-4646-464646464646"
CORRUPT_WORKSPACE = "47474747-4747-4747-4747-474747474747"

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
    parent_operation_id: str = "",
) -> OperationSpec:
    model = "fable" if runtime == "claude" else "gpt-5.6-sol"
    return OperationSpec(
        operation,
        f"idem-{operation}"[:128],
        kind,
        owner,
        RuntimeRoute(runtime, model, "high", "executor", "a" * 64),
        "context/manifest.json",
        "focused",
        parent_operation_id=parent_operation_id,
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


def observed_cmux_tree() -> dict[str, object]:
    return json.loads(
        (ROOT / "tests/fixtures/cmux-tree-content-free.json").read_text(
            encoding="utf-8"
        )
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "observed-tree"
    store = OperationStore(state_root)
    owner = "owner-observed"
    operation = "op-observed"
    workspace = "6F3DE542-E296-454B-80A8-EED18B5BEC01"
    surface = "F4E45A1C-F461-480A-B285-F598F0303DCC"
    store.create(spec(owner, operation), lane_id="lane-observed", run_id="run-observed")
    advance(store, owner, operation, ("preflight", "starting", "running"))
    bind_runtime(
        store,
        owner,
        operation,
        origin_surface=surface,
        surface_id=surface,
    )

    drifted = observed_cmux_tree()
    drifted["windows"][0]["workspaces"].append(
        {"ref": "workspace:999", "panes": [{"surfaces": [{}]}]}
    )
    drift_calls: list[list[str]] = []
    check(
        "one unrelated malformed cmux entry does not hide a live program",
        publish(
            state_root,
            trigger_owner=owner,
            workspace_id=workspace,
            runner=topology_runner(drift_calls, json.dumps(drifted)),
            binary="/opt/cmux",
        )
        and any("0/1 · 1▶" in command for command in ui_calls(drift_calls)),
        ui_calls(drift_calls),
    )

    ref_only = observed_cmux_tree()
    target = ref_only["windows"][0]["workspaces"][0]
    target.pop("id")
    ref_calls: list[list[str]] = []
    check(
        "ref-only controller placement fails closed instead of reading idle",
        publish(
            state_root,
            trigger_owner=owner,
            workspace_id=workspace,
            runner=topology_runner(ref_calls, json.dumps(ref_only)),
            binary="/opt/cmux",
        )
        and any("1!" in command for command in ui_calls(ref_calls))
        and not any("clear-progress" in command for command in ui_calls(ref_calls)),
        ui_calls(ref_calls),
    )

    sessionstart_calls: list[list[str]] = []
    check(
        "SessionStart preserves progress when active placement is ambiguous",
        not publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(sessionstart_calls, json.dumps(ref_only)),
            binary="/opt/cmux",
        )
        and ui_calls(sessionstart_calls) == [],
        ui_calls(sessionstart_calls),
    )

    terminal_owner = "owner-terminal-update"
    terminal_operation = "op-terminal-update"
    store.create(
        spec(terminal_owner, terminal_operation),
        lane_id="lane-terminal-update",
        run_id="run-terminal-update",
    )
    advance(
        store,
        terminal_owner,
        terminal_operation,
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )
    terminal_update_calls: list[list[str]] = []
    check(
        "terminal update preserves progress when another owner is ambiguous",
        not publish(
            state_root,
            trigger_owner=terminal_owner,
            workspace_id=workspace,
            runner=topology_runner(terminal_update_calls, json.dumps(ref_only)),
            binary="/opt/cmux",
        )
        and ui_calls(terminal_update_calls) == [],
        ui_calls(terminal_update_calls),
    )

    duplicate = observed_cmux_tree()
    duplicate["windows"][0]["workspaces"][1]["panes"][0]["surfaces"].append(
        {"id": surface, "ref": "surface:999"}
    )
    duplicate_calls: list[list[str]] = []
    check(
        "duplicate cross-workspace placement is attention, never guessed",
        publish(
            state_root,
            trigger_owner=owner,
            workspace_id=workspace,
            runner=topology_runner(duplicate_calls, json.dumps(duplicate)),
            binary="/opt/cmux",
        )
        and any("1!" in command for command in ui_calls(duplicate_calls))
        and not any(
            "clear-progress" in command for command in ui_calls(duplicate_calls)
        ),
        ui_calls(duplicate_calls),
    )

    timeout_values: list[object] = []

    def timeout_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeout_values.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, float(kwargs.get("timeout") or 0))

    check(
        "status inventory is bounded and timeout leaves the current bar untouched",
        not publish(
            state_root,
            trigger_owner=owner,
            workspace_id=workspace,
            runner=timeout_runner,
            binary="/opt/cmux",
        )
        and timeout_values == [2.0],
        timeout_values,
    )


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

    invalid_workspace_calls: list[list[str]] = []
    check(
        "positional workspace refs fail before inventory or UI mutation",
        not publish(
            state_root,
            workspace_id="workspace:1",
            runner=lambda command, **kwargs: invalid_workspace_calls.append(
                command
            ),
        )
        and not invalid_workspace_calls,
    )
    prior_workspace = os.environ.get("CMUX_WORKSPACE_ID")
    os.environ["CMUX_WORKSPACE_ID"] = "workspace:2"
    try:
        check(
            "malformed environment workspace fails before cmux mutation",
            not publish(
                state_root,
                runner=lambda command, **kwargs: invalid_workspace_calls.append(
                    command
                ),
            )
            and not invalid_workspace_calls,
        )
    finally:
        if prior_workspace is None:
            os.environ.pop("CMUX_WORKSPACE_ID", None)
        else:
            os.environ["CMUX_WORKSPACE_ID"] = prior_workspace

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
            trigger_owner="owner-final",
            workspace_id=TERMINAL_WORKSPACE,
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
                TERMINAL_WORKSPACE,
            ],
            [
                "/opt/cmux",
                "clear-progress",
                "--workspace",
                TERMINAL_WORKSPACE,
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
    check(
        "collection cannot represent missing inventory as idle",
        status_segment.collect(
            state_root,
            workspace_id=workspace_a,
            inventory=None,
        )
        is None,
    )
    check(
        "collection requires an exact workspace scope",
        status_segment.collect(
            state_root,
            workspace_id="",
            inventory=status_segment.LiveInventory({}),
        )
        is None,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "missing-owned-surface"
    store = OperationStore(state_root)
    origin = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
    workspace = "a2a2a2a2-a2a2-a2a2-a2a2-a2a2a2a2a2a2"
    window = "a3a3a3a3-a3a3-a3a3-a3a3-a3a3a3a3a3a3"
    store.create(
        spec("owner-empty-surface", "controller-empty-surface"),
        lane_id="lane-empty-surface",
        run_id="run-empty-surface",
    )
    advance(
        store,
        "owner-empty-surface",
        "controller-empty-surface",
        ("preflight", "starting", "running", "awaiting-callback"),
    )
    bind_runtime(
        store,
        "owner-empty-surface",
        "controller-empty-surface",
        origin_surface=origin,
    )
    calls: list[list[str]] = []
    check(
        "surface-bound controller without exact ownership is idle",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree((origin, workspace, window)),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == ["/opt/cmux", "clear-progress", "--workspace", workspace],
        calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "stale-attention-surface"
    store = OperationStore(state_root)
    owner = "owner-stale-attention"
    operation = "controller-stale-attention"
    origin = "a4a4a4a4-a4a4-a4a4-a4a4-a4a4a4a4a4a4"
    missing_surface = "a5a5a5a5-a5a5-a5a5-a5a5-a5a5a5a5a5a5"
    workspace = "a6a6a6a6-a6a6-a6a6-a6a6-a6a6a6a6a6a6"
    window = "a7a7a7a7-a7a7-a7a7-a7a7-a7a7a7a7a7a7"
    store.create(
        spec(owner, operation),
        lane_id="lane-stale-attention",
        run_id="run-stale-attention",
    )
    bind_runtime(
        store,
        owner,
        operation,
        origin_surface=origin,
        surface_id=missing_surface,
    )
    store.transition(
        owner,
        operation,
        "attention-required",
        reason=AttentionReason.PROCESS_START_FAILED,
    )
    calls: list[list[str]] = []
    check(
        "attention controller with a known missing exact surface is idle",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree((origin, workspace, window)),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == ["/opt/cmux", "clear-progress", "--workspace", workspace],
        calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "program-identity"
    store = OperationStore(state_root)
    owner = "owner-programs"
    origin = "b1b1b1b1-b1b1-b1b1-b1b1-b1b1b1b1b1b1"
    surface = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"
    workspace = "b3b3b3b3-b3b3-b3b3-b3b3-b3b3b3b3b3b3"
    window = "b4b4b4b4-b4b4-b4b4-b4b4-b4b4b4b4b4b4"
    contract = "c" * 64
    old = replace(
        spec(owner, "dispatch-old"),
        contract_sha256=contract,
    )
    new = replace(
        spec(owner, "dispatch-new"),
        contract_sha256=contract,
    )
    store.create(old, lane_id="lane-old", run_id="run-old")
    advance(
        store,
        owner,
        "dispatch-old",
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )
    stale = replace(
        spec(
            owner,
            "dispatch-old-verify-0123456789abcdef",
            kind="pipeline-verify",
            parent_operation_id="dispatch-old",
        ),
        contract_sha256=contract,
    )
    store.create(stale, lane_id="lane-stale-verify", run_id="run-stale-verify")
    advance(
        store,
        owner,
        stale.operation_id,
        ("preflight", "starting", "running"),
    )
    store.create(new, lane_id="lane-new", run_id="run-new")
    advance(store, owner, "dispatch-new", ("preflight", "starting", "running"))
    bind_runtime(
        store,
        owner,
        "dispatch-new",
        origin_surface=origin,
        surface_id=surface,
    )
    calls: list[list[str]] = []
    check(
        "terminal program descendants cannot attach to a live same-contract program",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree(
                    (origin, workspace, window),
                    (surface, workspace, window),
                ),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.000000",
            "--label",
            "0/1 · 1▶",
            "--workspace",
            workspace,
        ],
        calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "exact-parent-identity"
    store = OperationStore(state_root)
    owner = "owner-prefix-collision"
    workspace = "d4d4d4d4-d4d4-d4d4-d4d4-d4d4d4d4d4d4"
    window = "d5d5d5d5-d5d5-d5d5-d5d5-d5d5d5d5d5d5"
    origin = "d6d6d6d6-d6d6-d6d6-d6d6-d6d6d6d6d6d6"
    first_surface = "d7d7d7d7-d7d7-d7d7-d7d7-d7d7d7d7d7d7"
    second_surface = "d8d8d8d8-d8d8-d8d8-d8d8-d8d8d8d8d8d8"
    for controller, surface_id in (
        ("job", first_surface),
        ("job-more", second_surface),
    ):
        store.create(
            spec(owner, controller),
            lane_id=f"lane-{controller}",
            run_id=f"run-{controller}",
        )
        advance(store, owner, controller, ("preflight", "starting", "running"))
        bind_runtime(
            store,
            owner,
            controller,
            origin_surface=origin,
            surface_id=surface_id,
        )
    completed = "job-verify-0123456789abcdef"
    store.create(
        spec(
            owner,
            completed,
            kind="pipeline-verify",
            parent_operation_id="job",
        ),
        lane_id="lane-independent-child",
        run_id="run-independent-child",
    )
    advance(
        store,
        owner,
        completed,
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )
    calls: list[list[str]] = []
    check(
        "explicit parent identity survives controller prefix collisions",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree(
                    (origin, workspace, window),
                    (first_surface, workspace, window),
                    (second_surface, workspace, window),
                ),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.333333",
            "--label",
            "1/3 · 2▶",
            "--workspace",
            workspace,
        ],
        calls,
    )

    long_parent = "dispatch-" + "p" * 119
    long_surface = "d9d9d9d9-d9d9-d9d9-d9d9-d9d9d9d9d9d9"
    store.create(
        spec(owner, long_parent),
        lane_id="lane-long-parent",
        run_id="run-long-parent",
    )
    advance(store, owner, long_parent, ("preflight", "starting", "running"))
    bind_runtime(
        store,
        owner,
        long_parent,
        origin_surface=origin,
        surface_id=long_surface,
    )
    suffix = "-verify-fedcba9876543210"
    truncated_child = f"{long_parent[: 128 - len(suffix)]}{suffix}"
    store.create(
        spec(
            owner,
            truncated_child,
            kind="pipeline-verify",
            parent_operation_id=long_parent,
        ),
        lane_id="lane-truncated-child",
        run_id="run-truncated-child",
    )
    advance(
        store,
        owner,
        truncated_child,
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )
    truncated_calls: list[list[str]] = []
    check(
        "explicit parent identity survives bounded child id truncation",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                truncated_calls,
                cmux_tree(
                    (origin, workspace, window),
                    (first_surface, workspace, window),
                    (second_surface, workspace, window),
                    (long_surface, workspace, window),
                ),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(truncated_calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.400000",
            "--label",
            "2/5 · 3▶",
            "--workspace",
            workspace,
        ],
        truncated_calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "orphan-contract-binding"
    store = OperationStore(state_root)
    owner = "owner-orphan-contract"
    controller = "dispatch-current"
    orphan = "pipeline-verify-orphan"
    contract = "e" * 64
    workspace = "e1e1e1e1-e1e1-e1e1-e1e1-e1e1e1e1e1e1"
    window = "e2e2e2e2-e2e2-e2e2-e2e2-e2e2e2e2e2e2"
    origin = "e3e3e3e3-e3e3-e3e3-e3e3-e3e3e3e3e3e3"
    surface = "e4e4e4e4-e4e4-e4e4-e4e4-e4e4e4e4e4e4"
    store.create(
        replace(spec(owner, controller), contract_sha256=contract),
        lane_id="lane-current",
        run_id="run-current",
    )
    advance(store, owner, controller, ("preflight", "starting", "running"))
    bind_runtime(
        store,
        owner,
        controller,
        origin_surface=origin,
        surface_id=surface,
    )
    store.create(
        replace(
            spec(owner, orphan, kind="pipeline-verify"),
            contract_sha256=contract,
        ),
        lane_id="lane-orphan",
        run_id="run-orphan",
    )
    advance(store, owner, orphan, ("preflight", "starting", "running"))
    calls: list[list[str]] = []
    check(
        "unparented shared-contract child is excluded instead of guessed",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree(
                    (origin, workspace, window),
                    (surface, workspace, window),
                ),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.000000",
            "--label",
            "0/1 · 1▶",
            "--workspace",
            workspace,
        ],
        calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "research-program-identity"
    store = OperationStore(state_root)
    owner = "owner-research-programs"
    old_origin = "c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1"
    old_surface = "c2c2c2c2-c2c2-c2c2-c2c2-c2c2c2c2c2c2"
    new_origin = "c3c3c3c3-c3c3-c3c3-c3c3-c3c3c3c3c3c3"
    new_surface = "c4c4c4c4-c4c4-c4c4-c4c4-c4c4c4c4c4c4"
    workspace = "c5c5c5c5-c5c5-c5c5-c5c5-c5c5c5c5c5c5"
    window = "c6c6c6c6-c6c6-c6c6-c6c6-c6c6c6c6c6c6"
    store.create(
        spec(owner, "research-old", kind="research"),
        lane_id="lane-research-old",
        run_id="run-research-old",
    )
    advance(
        store,
        owner,
        "research-old",
        ("preflight", "starting", "running", "finalizing", "exiting", "complete"),
    )
    store.create(
        spec(
            owner,
            "research-old-fetch-e7d3799e",
            kind="research-fetch",
            parent_operation_id="research-old",
        ),
        lane_id="lane-research-old-fetch",
        run_id="run-research-old-fetch",
    )
    advance(
        store,
        owner,
        "research-old-fetch-e7d3799e",
        ("preflight", "starting", "running", "awaiting-callback"),
    )
    bind_runtime(
        store,
        owner,
        "research-old-fetch-e7d3799e",
        origin_surface=old_origin,
        surface_id=old_surface,
    )
    store.create(
        spec(owner, "research-new", kind="research"),
        lane_id="lane-research-new",
        run_id="run-research-new",
    )
    advance(
        store,
        owner,
        "research-new",
        ("preflight", "starting", "running", "awaiting-callback"),
    )
    store.create(
        spec(
            owner,
            "research-new-fetch-e7d3799e",
            kind="research-fetch",
            parent_operation_id="research-new",
        ),
        lane_id="lane-research-new-fetch",
        run_id="run-research-new-fetch",
    )
    advance(
        store,
        owner,
        "research-new-fetch-e7d3799e",
        ("preflight", "starting", "running", "awaiting-callback"),
    )
    bind_runtime(
        store,
        owner,
        "research-new-fetch-e7d3799e",
        origin_surface=new_origin,
        surface_id=new_surface,
    )
    calls: list[list[str]] = []
    check(
        "research placement and liveness come only from its exact current stage",
        publish(
            state_root,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree(
                    (new_origin, workspace, window),
                    (new_surface, workspace, window),
                ),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.000000",
            "--label",
            "0/2 · 2▶ 2⌛",
            "--workspace",
            workspace,
        ],
        calls,
    )


for stage, suffix in (("fetch", "e7d3799e"), ("synth", "e8c3e4fa")):
    with tempfile.TemporaryDirectory() as raw:
        state_root = Path(raw) / f"research-missing-{stage}-surface"
        store = OperationStore(state_root)
        owner = f"owner-research-{stage}"
        controller = f"research-{stage}"
        child = f"{controller}-{stage}-{suffix}"
        origin = (
            "e1e1e1e1-e1e1-e1e1-e1e1-e1e1e1e1e1e1"
            if stage == "fetch"
            else "e2e2e2e2-e2e2-e2e2-e2e2-e2e2e2e2e2e2"
        )
        surface = (
            "e3e3e3e3-e3e3-e3e3-e3e3-e3e3e3e3e3e3"
            if stage == "fetch"
            else "e4e4e4e4-e4e4-e4e4-e4e4-e4e4e4e4e4e4"
        )
        workspace = "e5e5e5e5-e5e5-e5e5-e5e5-e5e5e5e5e5e5"
        window = "e6e6e6e6-e6e6-e6e6-e6e6-e6e6e6e6e6e6"
        store.create(
            spec(owner, controller, kind="research"),
            lane_id=f"lane-{controller}",
            run_id=f"run-{controller}",
        )
        advance(
            store,
            owner,
            controller,
            ("preflight", "starting", "running", "awaiting-callback"),
        )
        store.create(
            spec(
                owner,
                child,
                kind=f"research-{stage}",
                parent_operation_id=controller,
            ),
            lane_id=f"lane-{child}",
            run_id=f"run-{child}",
        )
        advance(
            store,
            owner,
            child,
            ("preflight", "starting", "running", "awaiting-callback"),
        )
        bind_runtime(
            store,
            owner,
            child,
            origin_surface=origin,
            surface_id=surface,
        )
        calls: list[list[str]] = []
        check(
            f"missing exact research {stage} surface makes the program idle",
            publish(
                state_root,
                workspace_id=workspace,
                runner=topology_runner(
                    calls,
                    cmux_tree((origin, workspace, window)),
                ),
                binary="/opt/cmux",
            )
            and ui_calls(calls)[-1]
            == ["/opt/cmux", "clear-progress", "--workspace", workspace],
            calls,
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
            trigger_owner="owner-start",
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
            trigger_owner="owner-stale",
            workspace_id=STALE_WORKSPACE,
            runner=topology_runner(stale_calls, cmux_tree()),
            binary="/opt/cmux",
        )
        and ui_calls(stale_calls)[-1]
        == [
            "/opt/cmux",
            "clear-progress",
            "--workspace",
            STALE_WORKSPACE,
        ],
        stale_calls,
    )


with tempfile.TemporaryDirectory() as raw:
    state_root = Path(raw) / "corrupt-active-history"
    store = OperationStore(state_root)
    owner = "owner-corrupt-active"
    operation = "controller-corrupt-active"
    origin = "d1d1d1d1-d1d1-d1d1-d1d1-d1d1d1d1d1d1"
    workspace = "d2d2d2d2-d2d2-d2d2-d2d2-d2d2d2d2d2d2"
    window = "d3d3d3d3-d3d3-d3d3-d3d3-d3d3d3d3d3d3"
    store.create(
        spec(owner, operation),
        lane_id="lane-corrupt-active",
        run_id="run-corrupt-active",
    )
    store.transition(owner, operation, "preflight")
    bind_runtime(store, owner, operation, origin_surface=origin)
    runtime = state_root / "owners" / owner / "runtime" / operation / "session.json"
    runtime.write_text("{bad json\n", encoding="utf-8")
    calls: list[list[str]] = []
    check(
        "corrupt active identity renders attention without a false denominator",
        publish(
            state_root,
            trigger_owner=owner,
            workspace_id=workspace,
            runner=topology_runner(
                calls,
                cmux_tree((origin, workspace, window)),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(calls)[-1]
        == [
            "/opt/cmux",
            "set-progress",
            "0.000000",
            "--label",
            "1!",
            "--workspace",
            workspace,
        ],
        calls,
    )

    terminal_owner = "owner-corrupt-terminal"
    terminal_operation = "controller-corrupt-terminal"
    store.create(
        spec(terminal_owner, terminal_operation),
        lane_id="lane-corrupt-terminal",
        run_id="run-corrupt-terminal",
    )
    advance(
        store,
        terminal_owner,
        terminal_operation,
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
        "terminal update preserves progress for another corrupt active owner",
        not publish(
            state_root,
            trigger_owner=terminal_owner,
            workspace_id=workspace,
            runner=topology_runner(
                terminal_calls,
                cmux_tree((origin, workspace, window)),
            ),
            binary="/opt/cmux",
        )
        and ui_calls(terminal_calls) == [],
        ui_calls(terminal_calls),
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
            workspace_id=CORRUPT_WORKSPACE,
            runner=topology_runner(corrupt_calls, cmux_tree()),
            binary="/opt/cmux",
        )
        and ui_calls(corrupt_calls)[-1]
        == [
            "/opt/cmux",
            "clear-progress",
            "--workspace",
            CORRUPT_WORKSPACE,
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
    os.environ["CMUX_WORKSPACE_ID"] = TERMINAL_WORKSPACE
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
                    TERMINAL_WORKSPACE,
                ],
                [
                    "clear-progress",
                    "--workspace",
                    TERMINAL_WORKSPACE,
                ],
            )
        ],
    )
