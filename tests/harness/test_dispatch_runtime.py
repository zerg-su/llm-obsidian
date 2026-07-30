#!/usr/bin/env python3
"""Dispatch clean-cut contract over the generic provider runtime."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationRecord, OwnedResources, RuntimeRoute
from harness.runtime_sessions import RuntimeSessionResult
from harness.workflows.dispatch import (
    DispatchRequest,
    ReviewPolicy,
    start_dispatch,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


class FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.prepared: list[object] = []

    def start(self, request: object, *, on_surface_opened=None) -> object:
        self.requests.append(request)
        record = OperationRecord(
            request.spec,
            "starting",
            2,
            request.lane_id,
            request.run_id,
            OwnedResources(
                "22222222-2222-4222-8222-222222222222"
            ),
        )
        opened = RuntimeSessionResult(
            record,
            "surface-opened",
            surface_ref="surface:2",
            workspace_id="33333333-3333-4333-8333-333333333333",
            workspace_ref="workspace:3",
            window_id="44444444-4444-4444-8444-444444444444",
            window_ref="window:4",
        )
        if on_surface_opened is not None:
            on_surface_opened(opened)
            self.prepared.append(opened)
        return replace(opened, action="started")


with tempfile.TemporaryDirectory() as raw:
    cwd = Path(raw).resolve()
    (cwd / ".task-prompt.md").write_text("execute approved plan\n", encoding="utf-8")
    request = DispatchRequest(
        task_id="11111111-1111-4111-8111-111111111111",
        owner_id="11111111-1111-4111-8111-111111111111",
        plan_sha256="a" * 64,
        context_manifest="wiki/plans/approved.md",
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "b" * 64,
        ),
        placement="workspace",
        review=ReviewPolicy(),
    )
    runtime = FakeRuntime()
    prepared: list[object] = []
    result = start_dispatch(
        request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=cwd,
        prompt_pointer=".task-prompt.md",
        summary_pointer=".task-summary.json",
        on_surface_opened=prepared.append,
    )
    session = runtime.requests[0]
    check(
        "dispatch creates one provider-backed task-summary session",
        len(runtime.requests) == 1
        and session.spec.kind == "dispatch"
        and session.callback_mode == "task-summary"
        and session.callback_pointer == ".task-summary.json"
        and session.placement == "workspace",
    )
    check(
        "dispatch lane and run identities are deterministic",
        session.lane_id == result.record.lane_id
        and session.run_id == result.record.run_id,
    )
    check(
        "task metadata hook receives exact container identity before prompt",
        prepared
        and prepared[0].record.resources.surface_id
        == "22222222-2222-4222-8222-222222222222"
        and prepared[0].workspace_id
        == "33333333-3333-4333-8333-333333333333",
    )
    check(
        "task summary transport stays canonical and code-owned",
        session.task_summary_pointer == ".task-summary.json",
    )
