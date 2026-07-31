#!/usr/bin/env python3
"""RT02 regressions: the live acceptance run must release exactly the cmux
surfaces it opened at every termination boundary.

Backlog: wiki/backlog.md `cmux-acceptance-surface-cleanup` (2026-07-19) —
automatic close of the exact coordinator-owned cmux surface after a normal
exit, a timeout, or an interrupted acceptance run.

These checks drive production `live_acceptance_driver.run_cell` and
`live-acceptance-runner.execute_release` against an external runtime double
that models exact cmux surface ownership: `start` binds one surface per
operation into a shared `cmux tree --all` equivalent and only `cleanup`
removes it, mirroring `harness.runtime_sessions.cleanup`, which closes exactly
`record.resources.surface_id`.  A surface still in the tree after the run
terminated is therefore precisely an auto-close miss.

Live cmux is never invoked.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/live-acceptance-runner.py"
sys.path.insert(0, str(ROOT / "scripts"))
import live_acceptance_driver as driver  # noqa: E402
from harness.contracts import (  # noqa: E402
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    to_dict,
)
from harness.store import OperationStore  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "live_acceptance_surface_cleanup_test", SCRIPT
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


FAILED: list[str] = []


def check(name: str, condition: bool) -> None:
    """Record every boundary so one repaired boundary cannot mask another."""

    if condition:
        print(f"OK   {name}")
        return
    FAILED.append(name)
    print(f"FAIL {name}")


COMMIT = "a" * 40
FINGERPRINT = "b" * 64
ORIGIN = "11111111-1111-4111-8111-111111111111"
# A surface owned by an unrelated concurrent task; never a valid close target.
UNRELATED = "99999999-9999-4999-8999-999999999999"
CELL = {
    "cell_id": "claude-lifecycle",
    "kind": "runtime-lifecycle",
    "runtimes": ["claude"],
    "required_trace": [
        "open",
        "callback",
        "same-run-continue",
        "exit",
        "close",
    ],
    "dependency_fingerprint": FINGERPRINT,
    "dependencies": ["scripts/live_acceptance_driver.py"],
}
RELEASE = {"schema_version": 2, "commit_sha": COMMIT, "cells": [CELL]}
# cmux never reissues a live surface id; keep ids unique across every manager
# that shares one tree, so a resumed run cannot accidentally reuse a stale id.
_SURFACE_SEQUENCE = iter(range(1, 10000))


class SurfaceTrackingSessions:
    """Runtime double that models one exact owned cmux surface per operation."""

    def __init__(
        self,
        root: Path,
        tree: dict[str, str],
        *,
        deliver_callback: bool = True,
        interrupt_once: bool = False,
    ) -> None:
        self.root = root
        self.store = OperationStore(
            root / ".vault-meta/acceptance/rt02-store"
        )
        self.tree = tree
        self.records: dict[tuple[str, str], OperationRecord] = {}
        self.checkpoints: dict[tuple[str, str], str] = {}
        self.cleanup_attempts: dict[tuple[str, str], int] = {}
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.deliver_callback = deliver_callback
        self.interrupt_once = interrupt_once
        self._waits = 0

    def start(self, request: object, *, on_surface_opened: object = None) -> object:
        spec = request.spec
        key = (spec.owner_id, spec.operation_id)
        surface = f"2222{next(_SURFACE_SEQUENCE):04d}-2222-4222-8222-222222222222"
        self.tree[surface] = spec.operation_id
        self.opened.append(surface)
        record = OperationRecord(
            spec,
            "awaiting-callback",
            4,
            request.lane_id,
            request.run_id,
            OwnedResources(
                surface_id=surface, process_group=2222, supervisor_pid=3333
            ),
            effect_id="runtime-start",
            effect_outcome=EffectOutcome.SUCCEEDED,
        )
        self.records[key] = record
        self.checkpoints[key] = f"checkpoint-{spec.operation_id}"
        # Production binds the surface and notifies this seam before it
        # returns; the double must do the same or it cannot exercise the
        # ownership window that exists before `start` returns.
        if on_surface_opened is not None:
            on_surface_opened(
                SimpleNamespace(record=record, surface_id=surface)
            )
        if self.deliver_callback:
            pointer = request.cwd / request.callback_pointer
            pointer.parent.mkdir(parents=True, exist_ok=True)
            envelope = driver._callback_template(
                spec.operation_id, request.run_id, spec.kind
            )
            pointer.write_text(
                json.dumps(to_dict(envelope)), encoding="utf-8"
            )
        return SimpleNamespace(
            record=record,
            checkpoint=self.checkpoints[key],
            callback_pointer=request.callback_pointer,
        )

    def accept_callback(self, envelope: object) -> object:
        key = next(
            k
            for k, record in self.records.items()
            if record.spec.operation_id == envelope.operation_id
        )
        current = self.records[key]
        self.records[key] = replace(
            current,
            state="verifying",
            revision=current.revision + 1,
            accepted_callback_id=envelope.callback_id,
            accepted_callback_kind=envelope.kind,
            accepted_callback_sha256=envelope.payload_sha256,
        )
        return SimpleNamespace(record=self.records[key])

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> object:
        key = (owner_id, operation_id)
        current = self.records[key]
        self.records[key] = replace(
            current, state="running", revision=current.revision + 1
        )
        return SimpleNamespace(record=self.records[key], checkpoint=checkpoint)

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        key = (owner_id, operation_id)
        current = self.records[key]
        self.records[key] = replace(
            current, state="exiting", revision=current.revision + 1
        )
        return SimpleNamespace(record=self.records[key])

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        key = (owner_id, operation_id)
        current = self.records[key]
        attempt = self.cleanup_attempts.get(key, 0) + 1
        self.cleanup_attempts[key] = attempt
        if attempt == 1:
            return SimpleNamespace(record=current, action="wait-for-ownership")
        surface = current.resources.surface_id
        # Exact ownership: only ever this operation's own bound surface.
        if surface in self.tree and self.tree[surface] == operation_id:
            del self.tree[surface]
            self.closed.append(surface)
        self.records[key] = replace(
            current,
            state="complete",
            revision=current.revision + 1,
            resources=OwnedResources(),
        )
        return SimpleNamespace(record=self.records[key])

    def status(self, owner_id: str, operation_id: str) -> object:
        self._waits += 1
        if self.interrupt_once and self._waits >= 3:
            # Model one operator SIGINT during the callback wait.  A real
            # interrupt fires once; the handler then runs normally.
            self.interrupt_once = False
            raise KeyboardInterrupt("operator interrupted the acceptance run")
        return SimpleNamespace(record=self.records[(owner_id, operation_id)])

    def register_callback_target(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("claude-lifecycle uses no review callback child")

    # Surfaces this run opened that are still in the tree.
    def leaked(self) -> list[str]:
        return sorted(surface for surface in self.opened if surface in self.tree)


class InterruptDuringStartSessions(SurfaceTrackingSessions):
    """Interrupt after the surface is bound but before `start` returns.

    `runtime_sessions.start` binds the surface and notifies
    `on_surface_opened` well before it returns its record, so an interrupt in
    that window leaves an exactly-owned surface that a post-return ledger
    never learned about.
    """

    def start(self, request: object, *, on_surface_opened: object = None) -> object:
        result = super().start(request, on_surface_opened=on_surface_opened)
        raise KeyboardInterrupt("operator interrupted during runtime start")


def prepared_root(stack: list[tempfile.TemporaryDirectory]) -> Path:
    handle = tempfile.TemporaryDirectory(prefix="rt02-surface-cleanup.")
    stack.append(handle)
    root = Path(handle.name)
    (root / "config").mkdir()
    shutil.copy2(
        ROOT / "config/model-routing.toml", root / "config/model-routing.toml"
    )
    return root


def preflight(_root: object, _release: object, *, timeout: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "commit_sha": COMMIT,
        "origin_surface": ORIGIN,
        "routes": [
            {
                "runtime": "claude",
                "model": "opus-5",
                "effort": "high",
                "profile": "executor",
                "capabilities": [
                    "binary:claude",
                    "provider:authenticated",
                    "cmux:origin-alive",
                ],
            }
        ],
        "status": "compatible",
    }


def drive_cell(root: Path, manager: SurfaceTrackingSessions, *, timeout: int) -> str:
    try:
        driver.run_cell(
            root,
            {**CELL, "commit_sha": COMMIT},
            timeout=timeout,
            session_manager=manager,
            origin_surface=ORIGIN,
            sleep=lambda _seconds: None,
        )
    except BaseException as exc:  # noqa: BLE001 - the boundary under test
        return f"{type(exc).__name__}: {exc}"
    return ""


def drive_release(
    root: Path,
    manager: SurfaceTrackingSessions,
    *,
    timeout: int,
    restart: bool,
    state_path: Path,
    report_path: Path,
) -> tuple[str, dict[str, object]]:
    def cell_driver(cell_root: Path, request: dict[str, object], *, timeout: int):
        return driver.run_cell(
            cell_root,
            request,
            timeout=timeout,
            session_manager=manager,
            origin_surface=ORIGIN,
            sleep=lambda _seconds: None,
        )

    try:
        report = runner.execute_release(
            root,
            RELEASE,
            state_path=state_path,
            report_path=report_path,
            selected={"claude-lifecycle"},
            restart=restart,
            timeout=timeout,
            cell_driver=cell_driver,
            release_preflight=preflight,
        )
    except BaseException as exc:  # noqa: BLE001 - the boundary under test
        return f"{type(exc).__name__}: {exc}", {}
    return "", report


handles: list[tempfile.TemporaryDirectory] = []
try:
    # ---- baseline -------------------------------------------------------
    root = prepared_root(handles)
    tree: dict[str, str] = {ORIGIN: "coordinator", UNRELATED: "other-task"}
    manager = SurfaceTrackingSessions(root, tree)
    raised = drive_cell(root, manager, timeout=5)
    check(
        "normal exit releases every surface the run opened",
        raised == "" and manager.leaked() == [] and len(manager.opened) == 1,
    )

    # ---- boundary 1: timeout --------------------------------------------
    root = prepared_root(handles)
    tree = {ORIGIN: "coordinator", UNRELATED: "other-task"}
    manager = SurfaceTrackingSessions(root, tree, deliver_callback=False)
    raised = drive_cell(root, manager, timeout=1)
    check(
        "typed callback timeout still raises its exact classification",
        "typed callback timed out" in raised,
    )
    check(
        "timeout releases every surface the run opened",
        manager.leaked() == [],
    )

    # ---- boundary 2: interrupted ----------------------------------------
    root = prepared_root(handles)
    tree = {ORIGIN: "coordinator", UNRELATED: "other-task"}
    manager = SurfaceTrackingSessions(
        root, tree, deliver_callback=False, interrupt_once=True
    )
    state_path = root / "live-state.json"
    report_path = root / "latest-live.json"
    raised, _ = drive_release(
        root,
        manager,
        timeout=5,
        restart=True,
        state_path=state_path,
        report_path=report_path,
    )
    check(
        "operator interrupt still propagates to the coordinator",
        raised.startswith("KeyboardInterrupt"),
    )
    check(
        "interrupt releases every surface the run opened",
        manager.leaked() == [],
    )
    check(
        "interrupt persists a durable failure classification",
        state_path.is_file()
        and json.loads(state_path.read_text(encoding="utf-8")).get("failures"),
    )

    # ---- boundary 2b: interrupt inside runtime start ---------------------
    root = prepared_root(handles)
    tree = {ORIGIN: "coordinator", UNRELATED: "other-task"}
    start_manager = InterruptDuringStartSessions(root, tree)
    raised = drive_cell(root, start_manager, timeout=5)
    check(
        "interrupt inside runtime start still propagates",
        raised.startswith("KeyboardInterrupt"),
    )
    check(
        "interrupt inside runtime start releases the bound surface",
        start_manager.leaked() == [] and len(start_manager.opened) == 1,
    )
    check(
        "interrupt inside runtime start leaves other surfaces untouched",
        ORIGIN in tree and UNRELATED in tree,
    )

    # ---- boundary 3: normal exit must not pass over a stale surface ------
    root = prepared_root(handles)
    tree = {ORIGIN: "coordinator", UNRELATED: "other-task"}
    state_path = root / "live-state.json"
    report_path = root / "latest-live.json"
    leaking = SurfaceTrackingSessions(root, tree, deliver_callback=False)
    first, _ = drive_release(
        root,
        leaking,
        timeout=1,
        restart=True,
        state_path=state_path,
        report_path=report_path,
    )
    stale = sorted(surface for surface in leaking.opened if surface in tree)
    resuming = SurfaceTrackingSessions(root, tree, deliver_callback=True)
    second, report = drive_release(
        root,
        resuming,
        timeout=5,
        restart=False,
        state_path=state_path,
        report_path=report_path,
    )
    passed = bool(report) and not second
    check(
        "a normally exiting run never reports success over a stale surface",
        not passed or all(surface not in tree for surface in stale),
    )

    # ---- negative guard: exact ownership only ---------------------------
    check(
        "no boundary ever closes the coordinator origin surface",
        ORIGIN in tree,
    )
    check(
        "no boundary ever closes an unrelated task's surface",
        UNRELATED in tree,
    )
    check(
        "cleanup only ever closed surfaces this run opened",
        set(resuming.closed) <= set(resuming.opened)
        and set(leaking.closed) <= set(leaking.opened),
    )
finally:
    for handle in handles:
        handle.cleanup()

if FAILED:
    raise AssertionError(
        f"{len(FAILED)} surface-cleanup boundary check(s) failed: "
        + "; ".join(FAILED)
    )
print("live acceptance surface cleanup regressions passed")
