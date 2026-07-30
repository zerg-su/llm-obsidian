#!/usr/bin/env python3
"""Hermetic checks for the fixed four-cell live acceptance port."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/live-acceptance-runner.py"
sys.path.insert(0, str(ROOT / "scripts"))
import live_acceptance_driver as driver
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    CapabilityReport,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    to_dict,
)
from harness.callbacks import CallbackBroker
from harness.store import OperationStore
import harness.workflows.dispatch as dispatch_workflow
import harness.workflows.reap as reap_workflow
import harness.workflows.review_gate as review_gate_workflow

SPEC = importlib.util.spec_from_file_location("live_acceptance_runner_test", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


COMMIT = "a" * 40
FINGERPRINT = "b" * 64
NOW = datetime.now(timezone.utc).isoformat()
SURFACE = "11111111-1111-4111-8111-111111111111"
CONTRACT_CELLS = [
    {
        "cell_id": cell_id,
        "kind": kind,
        "runtimes": runtimes,
        "required_trace": list(required_trace),
        "dependency_fingerprint": FINGERPRINT,
        "dependencies": ["scripts/live_acceptance_driver.py"],
    }
    for cell_id, kind, runtimes, required_trace in (
        (
            "claude-lifecycle",
            "runtime-lifecycle",
            ["claude"],
            ("open", "callback", "same-run-continue", "exit", "close"),
        ),
        (
            "codex-lifecycle",
            "runtime-lifecycle",
            ["codex"],
            ("open", "callback", "same-run-continue", "exit", "close"),
        ),
        (
            "cross-runtime-composition",
            "workflow-composition",
            ["codex", "claude"],
            ("dispatch", "simple-review", "reap"),
        ),
        (
            "deep-review",
            "deep-review",
            ["claude", "codex"],
            ("spec-axis", "correctness-axis", "bounded-callback", "terminal-cleanup"),
        ),
    )
]
RELEASE = {"schema_version": 2, "commit_sha": COMMIT, "cells": CONTRACT_CELLS}


def preflight_evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "commit_sha": COMMIT,
        "origin_surface": SURFACE,
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


def operation(
    operation_id: str,
    kind: str,
    runtime: str,
    lane_id: str,
    run_id: str,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "kind": kind,
        "runtime": runtime,
        "lane_id": lane_id,
        "run_id": run_id,
        "terminal_state": "complete",
        "effect_outcome": "succeeded",
        "callback_count": 1,
        "owned_resources_remaining": 0,
    }


def evidence(row: dict[str, object]) -> dict[str, object]:
    cell_id = str(row["cell_id"])
    if cell_id == "claude-lifecycle":
        operations = [operation("claude-op", "runtime-lifecycle", "claude", "claude-lane", "claude-run")]
    elif cell_id == "codex-lifecycle":
        operations = [operation("codex-op", "runtime-lifecycle", "codex", "codex-lane", "codex-run")]
    elif cell_id == "cross-runtime-composition":
        operations = [
            operation("dispatch-op", "dispatch", "codex", "composition-lane", "dispatch-run"),
            operation("review-op", "simple-review", "claude", "composition-lane", "review-run"),
        ]
    else:
        operations = [
            operation("spec-op", "deep-review-spec", "claude", "spec-lane", "spec-run"),
            operation(
                "correctness-op",
                "deep-review-correctness",
                "codex",
                "correctness-lane",
                "correctness-run",
            ),
        ]
    return {
        "schema_version": 2,
        "cell_id": cell_id,
        "commit_sha": COMMIT,
        "dependency_fingerprint": FINGERPRINT,
        "started_at": NOW,
        "finished_at": NOW,
        "operations": operations,
        "trace": list(row["required_trace"]),
        "status": "passed",
    }


for contract_cell in CONTRACT_CELLS:
    driver.validate_cell_evidence(contract_cell, evidence(contract_cell), commit_sha=COMMIT)
check("all four typed cell contracts validate", True)

complete_report = {
    "schema_version": 3,
    "commit_sha": COMMIT,
    "preflight": preflight_evidence(),
    "cells": [evidence(row) for row in CONTRACT_CELLS],
    "failures": [],
}
complete_report["cells"][1]["operations"][0]["operation_id"] = "claude-op"
try:
    driver.validate_release_evidence(RELEASE, complete_report)
except driver.LiveDriverError:
    check("operation identities are unique across live cells", True)
else:
    raise AssertionError("operation identities are unique across live cells")

bad_sha = evidence(CONTRACT_CELLS[0])
bad_sha["commit_sha"] = "c" * 40
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[0], bad_sha, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("cell evidence is bound to the exact commit", True)
else:
    raise AssertionError("cell evidence is bound to the exact commit")

missing_trace = evidence(CONTRACT_CELLS[0])
missing_trace["trace"] = ["open", "callback", "exit", "close"]
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[0], missing_trace, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("incomplete lifecycle trace is rejected", True)
else:
    raise AssertionError("incomplete lifecycle trace is rejected")

leaked = evidence(CONTRACT_CELLS[1])
leaked["operations"][0]["owned_resources_remaining"] = 1
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[1], leaked, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("owned resource leak is rejected", True)
else:
    raise AssertionError("owned resource leak is rejected")

wrong_callback = evidence(CONTRACT_CELLS[2])
wrong_callback["operations"][1]["callback_count"] = 0
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[2], wrong_callback, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("composition requires exact callbacks", True)
else:
    raise AssertionError("composition requires exact callbacks")

swapped_composition = evidence(CONTRACT_CELLS[2])
swapped_composition["operations"][0]["runtime"] = "claude"
swapped_composition["operations"][1]["runtime"] = "codex"
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[2], swapped_composition, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("composition runtime assignments are exact", True)
else:
    raise AssertionError("composition runtime assignments are exact")

boolean_callback = evidence(CONTRACT_CELLS[0])
boolean_callback["operations"][0]["callback_count"] = True
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[0], boolean_callback, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("typed callback count rejects booleans", True)
else:
    raise AssertionError("typed callback count rejects booleans")

unfinished = evidence(CONTRACT_CELLS[0])
unfinished["operations"][0]["terminal_state"] = "failed"
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[0], unfinished, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("non-complete terminal state is rejected", True)
else:
    raise AssertionError("non-complete terminal state is rejected")

unresolved_effect = evidence(CONTRACT_CELLS[0])
unresolved_effect["operations"][0]["effect_outcome"] = "pending"
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[0], unresolved_effect, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("unresolved operation effect is rejected", True)
else:
    raise AssertionError("unresolved operation effect is rejected")

shared_deep_lane = evidence(CONTRACT_CELLS[3])
shared_deep_lane["operations"][1]["lane_id"] = "spec-lane"
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[3], shared_deep_lane, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("deep review requires independent lanes and runs", True)
else:
    raise AssertionError("deep review requires independent lanes and runs")

swapped_deep_runtimes = evidence(CONTRACT_CELLS[3])
swapped_deep_runtimes["operations"][0]["runtime"] = "codex"
swapped_deep_runtimes["operations"][1]["runtime"] = "claude"
try:
    driver.validate_cell_evidence(CONTRACT_CELLS[3], swapped_deep_runtimes, commit_sha=COMMIT)
except driver.LiveDriverError:
    check("deep review runtime assignments are exact", True)
else:
    raise AssertionError("deep review runtime assignments are exact")

with tempfile.TemporaryDirectory(prefix="live-release-preflight.") as raw:
    preflight_root = Path(raw)
    (preflight_root / "config").mkdir()
    shutil.copy2(
        ROOT / "config/model-routing.toml",
        preflight_root / "config/model-routing.toml",
    )
    checked_routes: list[tuple[object, Path]] = []
    checked_surfaces: list[str] = []

    def fake_route_preflight(
        requests: tuple[tuple[object, Path, str], ...],
    ) -> tuple[CapabilityReport, ...]:
        reports: list[CapabilityReport] = []
        for route, callback_dir, surface_id in requests:
            checked_routes.append((route, callback_dir))
            checked_surfaces.append(surface_id)
            reports.append(
                CapabilityReport(
                    route,
                    True,
                    ("provider:profile-valid", "cmux:origin-alive"),
                )
            )
        return tuple(reports)

    preflight_report = driver.preflight_release(
        preflight_root,
        RELEASE,
        timeout=17,
        origin_surface=SURFACE,
        route_preflight=fake_route_preflight,
    )
    check(
        "release preflight validates exact origin before provider routes",
        checked_surfaces == [SURFACE] * 5
        and preflight_report["origin_surface"] == SURFACE,
    )
    check(
        "release preflight covers every unique provider profile",
        len(checked_routes) == 5
        and {
            (
                route.runtime,
                route.effort,
                route.profile,
            )
            for route, _callback_dir in checked_routes
        }
        == {
            ("claude", "high", "executor"),
            ("codex", "high", "executor"),
            ("claude", "high", "reviewer-callback"),
            ("claude", "xhigh", "reviewer-callback"),
            ("codex", "xhigh", "reviewer-callback"),
        },
    )
    check(
        "release preflight creates only owner-private callback directories",
        all(
            callback_dir.is_dir()
            and callback_dir.stat().st_mode & 0o077 == 0
            for _route, callback_dir in checked_routes
        ),
    )
    check(
        "review preflight remains outside the product checkout",
        all(
            callback_dir != preflight_root
            and preflight_root not in callback_dir.parents
            for route, callback_dir in checked_routes
            if route.profile == "reviewer-callback"
        ),
    )


class FakeRuntimeSessions:
    """External runtime double; live-cell orchestration remains production code."""

    def __init__(self, root: Path):
        self.root = root
        self.store = OperationStore(root / ".vault-meta/acceptance/fake-store")
        self.calls: list[tuple[str, str, str]] = []
        self.records: dict[tuple[str, str], OperationRecord] = {}
        self.checkpoints: dict[tuple[str, str], str] = {}
        self.cleanup_attempts: dict[tuple[str, str], int] = {}
        self.cwds: dict[tuple[str, str], Path] = {}
        self.callback_targets: dict[
            tuple[str, str], tuple[str, str, str]
        ] = {}

    def start(
        self,
        request: object,
        *,
        on_surface_opened: object | None = None,
    ) -> object:
        spec = request.spec
        lane_id = request.lane_id
        run_id = request.run_id
        key = (spec.owner_id, spec.operation_id)
        if key in self.records:
            current = self.records[key]
            self.calls.append(("start-replay", spec.operation_id, lane_id))
            return SimpleNamespace(
                record=current,
                checkpoint=self.checkpoints[key],
                callback_pointer=request.callback_pointer,
            )
        record = OperationRecord(
            spec,
            "awaiting-callback",
            4,
            lane_id,
            run_id,
            OwnedResources(
                surface_id="22222222-2222-4222-8222-222222222222",
                process_group=2222,
                supervisor_pid=3333,
            ),
            effect_id="runtime-start",
            effect_outcome=EffectOutcome.SUCCEEDED,
        )
        if spec.kind == "dispatch":
            stored = self.store.create(
                spec, lane_id=lane_id, run_id=run_id
            )
            if stored.state == "created":
                for state in (
                    "preflight",
                    "starting",
                    "running",
                    "awaiting-callback",
                ):
                    self.store.transition(
                        spec.owner_id, spec.operation_id, state
                    )
                stored = self.store.read(
                    spec.owner_id, spec.operation_id
                )
                stored = replace(
                    stored,
                    resources=record.resources,
                    revision=stored.revision + 1,
                    effect_id="runtime-start",
                    effect_outcome=EffectOutcome.SUCCEEDED,
                )
                self.store.save(
                    stored, expected_revision=stored.revision - 1
                )
            record = stored
        self.records[key] = record
        self.cwds[key] = request.cwd
        checkpoint = f"checkpoint-{spec.operation_id}"
        self.checkpoints[key] = checkpoint
        self.calls.append(("start", spec.operation_id, lane_id))
        result = SimpleNamespace(
            record=record,
            checkpoint=checkpoint,
            callback_pointer=request.callback_pointer,
        )
        if on_surface_opened is not None:
            on_surface_opened(result)
        if spec.route.profile == "reviewer-callback":
            prompt = (
                request.cwd / request.prompt_pointer
            ).read_text(encoding="utf-8")
            callback_root = (
                request.cwd / request.callback_pointer
            ).resolve().parents[1]
            declared_root = next(
                (
                    line.partition(": ")[2]
                    for line in prompt.splitlines()
                    if line.startswith("Callback scratch root: ")
                ),
                "",
            )
            check(
                "review probe names its absolute callback scratch root",
                Path(declared_root).is_absolute()
                and Path(declared_root) == callback_root,
            )
        target = self.callback_targets.get(key)
        if target is None:
            if spec.kind == "dispatch":
                (request.cwd / ".live-dispatch-ack.json").write_text(
                    json.dumps(driver._dispatch_ack(spec.operation_id)),
                    encoding="utf-8",
                )
            else:
                callback_pointer = request.cwd / request.callback_pointer
                envelope = driver._callback_template(spec.operation_id, run_id, spec.kind)
                callback_pointer.parent.mkdir(parents=True, exist_ok=True)
                callback_pointer.write_text(
                    json.dumps(to_dict(envelope)), encoding="utf-8"
                )
        else:
            _child_id, _child_run, pointer = target
            callback_pointer = request.cwd / pointer
            expected = callback_pointer.with_name("expected.json")
            callback_pointer.write_bytes(expected.read_bytes())
        return result

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> object:
        key = (owner_id, parent_operation_id)
        parent = self.records[key]
        child = self.store.read(owner_id, child_operation_id)
        check(
            "review callback child reuses its exact parent lane",
            child.run_id == child_run_id and child.lane_id == parent.lane_id,
        )
        self.callback_targets[key] = (
            child_operation_id,
            child_run_id,
            callback_pointer,
        )
        self.calls.append(
            ("register-review-callback", parent_operation_id, parent.lane_id)
        )
        return SimpleNamespace(record=parent, callback_pointer=callback_pointer)

    def accept_callback(self, envelope: CallbackEnvelope) -> object:
        child_matches = [
            record
            for record in self.store.list("live-" + COMMIT[:16])
            if record.spec.operation_id == envelope.operation_id
        ]
        if child_matches:
            child = child_matches[0]
            acceptance = CallbackBroker(
                self.store, child.spec.owner_id
            ).accept(envelope)
            current = self.store.read(
                child.spec.owner_id, child.spec.operation_id
            )
            if not acceptance.duplicate:
                self.calls.append(
                    ("callback", envelope.operation_id, current.lane_id)
                )
            return SimpleNamespace(
                record=current,
                action=(
                    "callback-duplicate"
                    if acceptance.duplicate
                    else "callback-accepted"
                ),
            )
        key = next(
            key
            for key, record in self.records.items()
            if record.spec.operation_id == envelope.operation_id
        )
        current = self.records[key]
        updated = replace(
            current,
            state="verifying",
            revision=current.revision + 1,
            accepted_callback_id=envelope.callback_id,
            accepted_callback_kind=envelope.kind,
            accepted_callback_sha256=envelope.payload_sha256,
        )
        self.records[key] = updated
        self.calls.append(("callback", envelope.operation_id, current.lane_id))
        return SimpleNamespace(record=updated)

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> object:
        key = (owner_id, operation_id)
        current = self.records[key]
        check(
            "continue uses captured provider checkpoint",
            checkpoint == self.checkpoints[key],
        )
        check(
            "continue prompt is an owner-relative pointer",
            not Path(prompt_pointer).is_absolute()
            and (self.cwds[key] / prompt_pointer).is_file(),
        )
        updated = replace(current, state="running", revision=current.revision + 1)
        self.records[key] = updated
        self.calls.append(("continue", operation_id, current.lane_id))
        return SimpleNamespace(record=updated, checkpoint=checkpoint)

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        key = (owner_id, operation_id)
        current = self.records[key]
        if current.spec.kind == "dispatch":
            current = self.store.read(owner_id, operation_id)
            if current.state != "exiting":
                self.store.transition(owner_id, operation_id, "exiting")
            current = self.store.read(owner_id, operation_id)
        updated = replace(current, state="exiting", revision=current.revision + 1)
        self.records[key] = (
            current if current.spec.kind == "dispatch" else updated
        )
        self.calls.append(("exit", operation_id, current.lane_id))
        return SimpleNamespace(record=self.records[key])

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        key = (owner_id, operation_id)
        current = self.records[key]
        if current.spec.kind == "dispatch":
            current = self.store.read(owner_id, operation_id)
        attempt = self.cleanup_attempts.get(key, 0) + 1
        self.cleanup_attempts[key] = attempt
        if attempt == 1:
            self.calls.append(("cleanup-wait", operation_id, current.lane_id))
            return SimpleNamespace(
                record=current,
                action=(
                    "wait-for-supervisor"
                    if current.spec.route.runtime == "codex"
                    else "wait-for-ownership"
                ),
            )
        if current.spec.kind == "dispatch":
            self.store.transition(owner_id, operation_id, "complete")
            current = self.store.read(owner_id, operation_id)
            updated = replace(
                current,
                resources=OwnedResources(),
                revision=current.revision + 1,
            )
            self.store.save(
                updated, expected_revision=current.revision
            )
        else:
            updated = replace(
                current,
                state="complete",
                revision=current.revision + 1,
                resources=OwnedResources(),
            )
        self.records[key] = updated
        self.calls.append(("cleanup", operation_id, current.lane_id))
        return SimpleNamespace(record=updated)

    def status(self, owner_id: str, operation_id: str) -> object:
        record = self.records.get((owner_id, operation_id))
        if record is not None and record.spec.kind == "dispatch":
            record = self.store.read(owner_id, operation_id)
        if record is None:
            record = self.store.read(owner_id, operation_id)
        return SimpleNamespace(record=record)


with tempfile.TemporaryDirectory(prefix="live-cell-driver.") as raw:
    live_root = Path(raw)
    (live_root / "config").mkdir()
    shutil.copy2(
        ROOT / "config/model-routing.toml",
        live_root / "config/model-routing.toml",
    )
    for contract_cell in CONTRACT_CELLS:
        manager = FakeRuntimeSessions(live_root)
        facade_calls: list[str] = []
        original_dispatch = dispatch_workflow.start_dispatch
        original_begin = review_gate_workflow.ReviewGateController.begin
        original_authorize = review_gate_workflow.authorize_task_finalization
        original_reap = reap_workflow.run_reap
        if contract_cell["cell_id"] == "cross-runtime-composition":
            def traced_dispatch(*args: object, **kwargs: object) -> object:
                facade_calls.append("dispatch.start_dispatch")
                return original_dispatch(*args, **kwargs)

            def traced_begin(self: object, *args: object, **kwargs: object) -> object:
                facade_calls.append("review_gate.begin")
                return original_begin(self, *args, **kwargs)

            def traced_authorize(*args: object, **kwargs: object) -> object:
                facade_calls.append("review_gate.authorize")
                return original_authorize(*args, **kwargs)

            def traced_reap(*args: object, **kwargs: object) -> object:
                facade_calls.append("reap.run_reap")
                return original_reap(*args, **kwargs)

            dispatch_workflow.start_dispatch = traced_dispatch
            review_gate_workflow.ReviewGateController.begin = traced_begin
            review_gate_workflow.authorize_task_finalization = traced_authorize
            reap_workflow.run_reap = traced_reap
        try:
            actual = driver.run_cell(
                live_root,
                {**contract_cell, "commit_sha": COMMIT},
                timeout=17,
                session_manager=manager,
                origin_surface=SURFACE,
                sleep=lambda _seconds: None,
            )
        finally:
            dispatch_workflow.start_dispatch = original_dispatch
            review_gate_workflow.ReviewGateController.begin = original_begin
            review_gate_workflow.authorize_task_finalization = original_authorize
            reap_workflow.run_reap = original_reap
        driver.validate_cell_evidence(contract_cell, actual, commit_sha=COMMIT)
        if contract_cell["cell_id"] == "cross-runtime-composition":
            check(
                "composition calls production dispatch review gate and reap facades",
                facade_calls
                == [
                    "dispatch.start_dispatch",
                    "review_gate.begin",
                    "review_gate.authorize",
                    "reap.run_reap",
                ],
            )
        check(
            f"{contract_cell['cell_id']} starts every declared operation through runtime sessions",
            len([call for call in manager.calls if call[0] == "start"])
            == len(actual["operations"]),
        )
        check(
            f"{contract_cell['cell_id']} proves callback and cleanup for every operation",
            len([call for call in manager.calls if call[0] == "callback"])
            == (
                len(actual["operations"]) - 1
                if contract_cell["cell_id"]
                == "cross-runtime-composition"
                else len(actual["operations"])
            )
            and len([call for call in manager.calls if call[0] == "cleanup"])
            == len(actual["operations"]),
        )
        check(
            f"{contract_cell['cell_id']} waits for provider exit before exact cleanup",
            len([call for call in manager.calls if call[0] == "cleanup-wait"])
            == len(actual["operations"]),
        )
        expected_review_lanes = (
            1
            if contract_cell["cell_id"] == "cross-runtime-composition"
            else 2
            if contract_cell["cell_id"] == "deep-review"
            else 0
        )
        check(
            f"{contract_cell['cell_id']} routes reviews through child callback receipts",
            len(
                [
                    call
                    for call in manager.calls
                    if call[0] == "register-review-callback"
                ]
            )
            == expected_review_lanes,
        )
        review_records = [
            record
            for record in manager.records.values()
            if "review" in record.spec.kind
        ]
        check(
            f"{contract_cell['cell_id']} keeps product read-only with owner scratch",
            len(review_records) == expected_review_lanes
            and all(
                record.spec.route.profile == "reviewer-callback"
                and manager.cwds[
                    (record.spec.owner_id, record.spec.operation_id)
                ]
                != live_root
                and (
                    manager.cwds[
                        (record.spec.owner_id, record.spec.operation_id)
                    ].stat().st_mode
                    & 0o077
                )
                == 0
                for record in review_records
            ),
        )
        if contract_cell["cell_id"] in {"claude-lifecycle", "codex-lifecycle"}:
            check(
                f"{contract_cell['cell_id']} continues the exact opened operation",
                [call[1:] for call in manager.calls if call[0] == "continue"]
                == [manager.calls[0][1:]],
            )

    replay_manager = FakeRuntimeSessions(live_root)
    replay_request = {**CONTRACT_CELLS[0], "commit_sha": COMMIT}
    first_replay = driver.run_cell(
        live_root,
        replay_request,
        timeout=17,
        session_manager=replay_manager,
        origin_surface=SURFACE,
        sleep=lambda _seconds: None,
    )
    second_replay = driver.run_cell(
        live_root,
        replay_request,
        timeout=17,
        session_manager=replay_manager,
        origin_surface=SURFACE,
        sleep=lambda _seconds: None,
    )
    check(
        "terminal lifecycle replay performs no duplicate provider effects",
        first_replay["operations"] == second_replay["operations"]
        and len(
            [
                call
                for call in replay_manager.calls
                if call[0] in {"callback", "continue", "exit", "cleanup"}
            ]
        )
        == 4
        and len(
            [
                call
                for call in replay_manager.calls
                if call[0] == "start-replay"
            ]
        )
        == 1,
    )

    class InterruptedCleanupSessions(FakeRuntimeSessions):
        def __init__(self, root: Path):
            super().__init__(root)
            self.interrupt_once = True

        def cleanup(self, owner_id: str, operation_id: str) -> object:
            if self.interrupt_once:
                self.interrupt_once = False
                self.calls.append(("cleanup-interrupted", operation_id, ""))
                raise RuntimeError("simulated coordinator interruption")
            return super().cleanup(owner_id, operation_id)

    interrupted_manager = InterruptedCleanupSessions(live_root)
    try:
        driver.run_cell(
            live_root,
            replay_request,
            timeout=17,
            session_manager=interrupted_manager,
            origin_surface=SURFACE,
            sleep=lambda _seconds: None,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("interrupted lifecycle leaves a resumable exit boundary")
    resumed = driver.run_cell(
        live_root,
        replay_request,
        timeout=17,
        session_manager=interrupted_manager,
        origin_surface=SURFACE,
        sleep=lambda _seconds: None,
    )
    check(
        "interrupted exiting lifecycle resumes cleanup without duplicate model work",
        resumed["status"] == "passed"
        and len(
            [
                call
                for call in interrupted_manager.calls
                if call[0] in {"callback", "continue", "exit"}
            ]
        )
        == 3,
    )

    class TransientExitOwnershipSessions(FakeRuntimeSessions):
        def __init__(self, root: Path):
            super().__init__(root)
            self.ambiguous_once = True

        def request_exit(self, owner_id: str, operation_id: str) -> object:
            if self.ambiguous_once:
                self.ambiguous_once = False
                key = (owner_id, operation_id)
                current = self.records[key]
                updated = replace(
                    current,
                    state="attention-required",
                    revision=current.revision + 1,
                    attention_reason=AttentionReason.CLEANUP_INCOMPLETE,
                )
                self.records[key] = updated
                self.calls.append(("exit-ambiguous", operation_id, current.lane_id))
                return SimpleNamespace(
                    record=updated,
                    action="attention-required",
                )
            return super().request_exit(owner_id, operation_id)

    transient_exit_manager = TransientExitOwnershipSessions(live_root)
    recovered = driver.run_cell(
        live_root,
        replay_request,
        timeout=17,
        session_manager=transient_exit_manager,
        origin_surface=SURFACE,
        sleep=lambda _seconds: None,
    )
    check(
        "transient exit ownership is re-probed before cleanup",
        recovered["status"] == "passed"
        and len(
            [
                call
                for call in transient_exit_manager.calls
                if call[0] == "exit-ambiguous"
            ]
        )
        == 1
        and len(
            [
                call
                for call in transient_exit_manager.calls
                if call[0] == "exit"
            ]
        )
        == 1,
    )

    class CleanupAttentionRaceSessions(FakeRuntimeSessions):
        def __init__(self, root: Path):
            super().__init__(root)
            self.raced_once = False

        def cleanup(self, owner_id: str, operation_id: str) -> object:
            key = (owner_id, operation_id)
            if (
                not self.raced_once
                and self.cleanup_attempts.get(key, 0) == 1
            ):
                self.raced_once = True
                current = self.records[key]
                updated = replace(
                    current,
                    state="attention-required",
                    revision=current.revision + 1,
                    attention_reason=AttentionReason.CLEANUP_INCOMPLETE,
                )
                self.records[key] = updated
                self.calls.append(
                    ("cleanup-attention-race", operation_id, current.lane_id)
                )
                return SimpleNamespace(
                    record=updated,
                    action="attention-required",
                )
            return super().cleanup(owner_id, operation_id)

    cleanup_race_manager = CleanupAttentionRaceSessions(live_root)
    cleanup_race_recovered = driver.run_cell(
        live_root,
        replay_request,
        timeout=17,
        session_manager=cleanup_race_manager,
        origin_surface=SURFACE,
        sleep=lambda _seconds: None,
    )
    check(
        "cleanup attention race returns to the bounded exit probe",
        cleanup_race_recovered["status"] == "passed"
        and len(
            [
                call
                for call in cleanup_race_manager.calls
                if call[0] == "cleanup-attention-race"
            ]
        )
        == 1
        and len(
            [
                call
                for call in cleanup_race_manager.calls
                if call[0] == "exit"
            ]
        )
        == 2,
    )

    class NonCleanupAttentionSessions(FakeRuntimeSessions):
        def cleanup(self, owner_id: str, operation_id: str) -> object:
            key = (owner_id, operation_id)
            current = self.records[key]
            updated = replace(
                current,
                state="attention-required",
                revision=current.revision + 1,
                attention_reason=AttentionReason.CALLBACK_INVALID,
            )
            self.records[key] = updated
            self.calls.append(
                ("cleanup-invalid-attention", operation_id, current.lane_id)
            )
            return SimpleNamespace(
                record=updated,
                action="attention-required",
            )

    invalid_attention_manager = NonCleanupAttentionSessions(live_root)
    try:
        driver.run_cell(
            live_root,
            replay_request,
            timeout=17,
            session_manager=invalid_attention_manager,
            origin_surface=SURFACE,
            sleep=lambda _seconds: None,
        )
    except driver.LiveDriverError:
        pass
    else:
        raise AssertionError(
            "non-cleanup attention must remain a fail-closed boundary"
        )
    check(
        "cleanup recovery preserves unrelated attention reasons",
        len(
            [
                call
                for call in invalid_attention_manager.calls
                if call[0] == "cleanup-invalid-attention"
            ]
        )
        == 1
        and len(
            [
                call
                for call in invalid_attention_manager.calls
                if call[0] == "exit"
            ]
        )
        == 1,
    )

    class PersistentExitAmbiguitySessions(FakeRuntimeSessions):
        def request_exit(self, owner_id: str, operation_id: str) -> object:
            key = (owner_id, operation_id)
            current = self.records[key]
            updated = replace(
                current,
                state="attention-required",
                revision=current.revision + 1,
                attention_reason=AttentionReason.ATTENTION_REQUIRED,
            )
            self.records[key] = updated
            self.calls.append(("exit-ambiguous", operation_id, current.lane_id))
            return SimpleNamespace(
                record=updated,
                action="attention-required",
            )

    persistent_exit_manager = PersistentExitAmbiguitySessions(live_root)
    try:
        driver.run_cell(
            live_root,
            replay_request,
            timeout=17,
            session_manager=persistent_exit_manager,
            origin_surface=SURFACE,
            sleep=lambda _seconds: None,
        )
    except driver.LiveDriverError:
        pass
    else:
        raise AssertionError("persistent exit ambiguity must exhaust its probe budget")
    check(
        "persistent exit ownership stops after three total probes",
        len(
            [
                call
                for call in persistent_exit_manager.calls
                if call[0] == "exit-ambiguous"
            ]
        )
        == 3
        and not persistent_exit_manager.cleanup_attempts,
    )

with tempfile.TemporaryDirectory(prefix="live-acceptance-test.") as raw:
    tmp = Path(raw)
    state = tmp / "state.json"
    report = tmp / "report.json"
    calls: list[str] = []
    preflight = {"calls": 0}

    def fake_preflight(
        _root: Path,
        release: dict[str, object],
        *,
        timeout: int,
    ) -> dict[str, object]:
        preflight["calls"] += 1
        check(
            "global route preflight sees exact release before model cells",
            release["commit_sha"] == COMMIT and timeout == 17,
        )
        return preflight_evidence()

    def fake_cell(
        _root: Path,
        row: dict[str, object],
        *,
        timeout: int,
    ) -> dict[str, object]:
        check(
            "global route preflight completed before first cell",
            preflight["calls"] >= 1,
        )
        check("timeout reaches the in-process driver port", timeout == 17)
        calls.append(str(row["cell_id"]))
        return evidence(row)

    first = runner.execute_release(
        ROOT,
        RELEASE,
        state_path=state,
        report_path=report,
        selected=set(runner.CELL_IDS),
        restart=False,
        timeout=17,
        cell_driver=fake_cell,
        release_preflight=fake_preflight,
    )
    check("four-cell in-process run succeeds", len(first["cells"]) == 4)
    check(
        "exact-SHA report persists global capability preflight",
        first["schema_version"] == 3
        and first["preflight"] == preflight_evidence()
        and first["failures"] == []
        and json.loads(report.read_text())["preflight"] == preflight_evidence(),
    )
    check("each cell ran once", calls == list(runner.CELL_IDS))
    second = runner.execute_release(
        ROOT,
        RELEASE,
        state_path=state,
        report_path=report,
        selected=set(runner.CELL_IDS),
        restart=False,
        timeout=17,
        cell_driver=fake_cell,
        release_preflight=fake_preflight,
    )
    check("green typed cells resume without rerun", len(second["cells"]) == 4 and len(calls) == 4)
    check("green resume still performs zero-effect route preflight", preflight["calls"] == 2)
    check("checkpoint uses typed commit binding", json.loads(state.read_text())["commit_sha"] == COMMIT)

    failed_state = tmp / "failed-state.json"
    failed_report = tmp / "failed-report.json"
    failed_calls: list[str] = []
    fail_once = {"armed": True}

    def classified_cell(
        _root: Path,
        row: dict[str, object],
        *,
        timeout: int,
    ) -> dict[str, object]:
        del timeout
        cell_id = str(row["cell_id"])
        failed_calls.append(cell_id)
        if cell_id == "codex-lifecycle" and fail_once["armed"]:
            fail_once["armed"] = False
            raise driver.LiveDriverError("bounded provider callback failed")
        return evidence(row)

    try:
        runner.execute_release(
            ROOT,
            RELEASE,
            state_path=failed_state,
            report_path=failed_report,
            selected=set(runner.CELL_IDS),
            restart=False,
            timeout=17,
            cell_driver=classified_cell,
            release_preflight=fake_preflight,
        )
    except driver.LiveDriverError:
        pass
    else:
        raise AssertionError("failed live cell must remain a typed failure")
    failed_value = json.loads(failed_report.read_text(encoding="utf-8"))
    check(
        "failed cell classification is persisted without error content",
        failed_value["failures"]
        == [
            {
                "cell_id": "codex-lifecycle",
                "status": "failed",
                "classification": "runtime-contract",
                "attempt": 1,
            }
        ]
        and failed_value["cells"][0]["cell_id"] == "claude-lifecycle",
    )
    resumed_failed = runner.execute_release(
        ROOT,
        RELEASE,
        state_path=failed_state,
        report_path=failed_report,
        selected=set(runner.CELL_IDS),
        restart=False,
        timeout=17,
        cell_driver=classified_cell,
        release_preflight=fake_preflight,
    )
    check(
        "resume executes only the explicitly classified failed cell",
        failed_calls
        == ["claude-lifecycle", "codex-lifecycle", "codex-lifecycle"]
        and [row["cell_id"] for row in resumed_failed["cells"]]
        == ["claude-lifecycle", "codex-lifecycle"]
        and resumed_failed["failures"] == [],
    )
    completed_after_failure = runner.execute_release(
        ROOT,
        RELEASE,
        state_path=failed_state,
        report_path=failed_report,
        selected=set(runner.CELL_IDS),
        restart=False,
        timeout=17,
        cell_driver=classified_cell,
        release_preflight=fake_preflight,
    )
    check(
        "later invocation may run remaining cells after classified recovery",
        [row["cell_id"] for row in completed_after_failure["cells"]]
        == list(runner.CELL_IDS),
    )

    mutation_state = tmp / "mutation-state.json"
    mutation_report = tmp / "mutation-report.json"
    mutation = {"driver_ran": False}

    def mutation_driver(
        root: Path,
        row: dict[str, object],
        *,
        timeout: int,
    ) -> dict[str, object]:
        result = fake_cell(root, row, timeout=timeout)
        mutation["driver_ran"] = True
        return result

    try:
        runner.execute_release(
            ROOT,
            RELEASE,
            state_path=mutation_state,
            report_path=mutation_report,
            selected={"claude-lifecycle"},
            restart=False,
            timeout=17,
            cell_driver=mutation_driver,
            verify_clean_head=True,
            contract_loader=lambda _root: (
                {**RELEASE, "commit_sha": "c" * 40}
                if mutation["driver_ran"]
                else RELEASE
            ),
        )
    except runner.AcceptanceError:
        check(
            "release mutation during driver call is rejected before persistence",
            not mutation_state.exists() and not mutation_report.exists(),
        )
    else:
        raise AssertionError("release mutation during driver call is rejected before persistence")

    malicious = tmp / "malicious-driver.py"
    marker = tmp / "fabricated"
    malicious.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('pass')\n",
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--root",
            str(ROOT),
            "--driver",
            f"{sys.executable} {malicious}",
            "--state",
            str(state),
            "--report",
            str(report),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("external live driver selector is rejected", rejected.returncode == 2 and not marker.exists())

    alternate = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--root",
            str(tmp),
            "--state",
            str(state),
            "--report",
            str(report),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "live runner rejects an alternate code checkout",
        alternate.returncode == 3 and "same checkout" in alternate.stderr,
    )

    bootstrap_root = tmp / "bootstrap-checkout"
    bootstrap_scripts = bootstrap_root / "scripts"
    bootstrap_scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, bootstrap_scripts / SCRIPT.name)
    import_marker = tmp / "driver-imported-before-clean-check"
    (bootstrap_scripts / "live_acceptance_driver.py").write_text(
        f"from pathlib import Path\nPath({str(import_marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(bootstrap_root)], check=True)
    subprocess.run(
        ["git", "-C", str(bootstrap_root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(bootstrap_root), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(bootstrap_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(bootstrap_root), "commit", "-qm", "seed"], check=True)
    (bootstrap_root / ".task-origin-session").write_text(
        "runtime-only coordinator identity\n", encoding="utf-8"
    )
    check(
        "bootstrap clean-head ignores the task origin runtime marker",
        runner.bootstrap_clean_head(bootstrap_root)
        == runner.bootstrap_head(bootstrap_root),
    )
    (bootstrap_root / "config").mkdir()
    (bootstrap_root / "config/dirty.toml").write_text("dirty = true\n", encoding="utf-8")
    dirty_bootstrap = subprocess.run(
        [
            sys.executable,
            str(bootstrap_scripts / SCRIPT.name),
            "run",
            "--root",
            str(bootstrap_root),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "dirty checkout is rejected before importing repo-owned driver code",
        dirty_bootstrap.returncode == 3 and not import_marker.exists(),
    )

print("live acceptance runner tests passed")
