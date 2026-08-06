"""Connected execution of frozen lifecycle regressions through production owners."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from harness.review_drive_rearm import rearm_review_drive
from harness.liveness import LivenessController

from lifecycle_scheduler import Schedule, run_schedule
from lifecycle_simulator import LifecycleWorld
from lifecycle_simulator_oracle import load_scenario


ZERO_REAL_EFFECTS = {"provider": 0, "model": 0, "cmux": 0, "network": 0}
ROOT = Path(__file__).resolve().parents[2]


class HistoricalFixtureError(RuntimeError):
    """An admitted causal fixture no longer binds the executable prefix."""


def assert_no_forbidden_effects(
    scenario: Mapping[str, object], effect_ids: object
) -> None:
    forbidden = {str(item) for item in scenario.get("forbidden_effects", [])}
    observed = {str(item) for item in effect_ids}  # type: ignore[arg-type]
    emitted = sorted(forbidden & observed)
    if emitted:
        raise AssertionError(
            "historical scenario emitted forbidden semantic effects: "
            + ",".join(emitted)
        )


def _source_fixture(
    scenario: Mapping[str, object], repository_root: Path
) -> Mapping[str, object] | None:
    relative = str(scenario.get("source_fixture") or "")
    expected_sha256 = str(scenario.get("source_sha256") or "")
    if not relative and not expected_sha256:
        return None
    root = repository_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HistoricalFixtureError("historical fixture escaped the repository") from exc
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise HistoricalFixtureError("historical fixture is not one bounded regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise HistoricalFixtureError("historical fixture digest changed")
    try:
        fixture = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalFixtureError("historical fixture JSON is invalid") from exc
    if not isinstance(fixture, Mapping) or fixture.get("schema_version") != 1:
        raise HistoricalFixtureError("historical fixture schema is invalid")
    return fixture


def _fact(fixture: Mapping[str, object], identity: str) -> Mapping[str, object]:
    interleaving = fixture.get("interleaving")
    if not isinstance(interleaving, list):
        raise HistoricalFixtureError("historical fixture interleaving is invalid")
    matches = [
        item
        for item in interleaving
        if isinstance(item, Mapping) and item.get("fact") == identity
    ]
    if len(matches) != 1:
        raise HistoricalFixtureError("historical fixture fact is missing or duplicated")
    return matches[0]


def bind_historical_fixture(
    scenario: Mapping[str, object], repository_root: Path = ROOT
) -> dict[str, object]:
    """Digest-check admitted bytes and bind their causal facts to the replay."""

    bound = dict(scenario)
    fixture = _source_fixture(bound, repository_root)
    if fixture is None:
        return bound
    scenario_id = str(bound.get("scenario_id") or "")
    initial = bound.get("initial_snapshot")
    replay = bound.get("replay_snapshot")
    actions = bound.get("actions")
    if (
        not isinstance(initial, Mapping)
        or not isinstance(replay, Mapping)
        or not isinstance(actions, list)
    ):
        raise HistoricalFixtureError("historical causal prefix is unavailable")
    if scenario_id == "d264-73-mixed-head-terminal-boundary":
        terminal = _fact(fixture, "parent-terminal")
        changed = _fact(fixture, "head-changed")
        child = _fact(fixture, "verification-child-prepared")
        reviewed = str(fixture.get("reviewed_head") or "")
        resolved = str(fixture.get("resolved_head") or "")
        initial_head = initial.get("head_boundary")
        replay_head = replay.get("head_boundary")
        action = actions[0] if len(actions) == 1 else None
        if (
            fixture.get("defect_id") != "D-264-73"
            or terminal.get("parent_state") != "attention-required"
            or terminal.get("attention_reason") != "callback-timeout"
            or changed.get("head") != resolved
            or child.get("child_state") != "awaiting-callback"
            or child.get("verification_iteration") != 1
            or not isinstance(initial_head, Mapping)
            or not isinstance(replay_head, Mapping)
            or not isinstance(action, Mapping)
            or initial_head.get("reviewed_head_sha") != reviewed
            or initial_head.get("resolved_head_sha") != resolved
            or initial_head.get("attempt_terminal") is not True
            or initial_head.get("continuation_requested") is not True
            or replay_head.get("reviewed_head_sha") != reviewed
            or replay_head.get("resolved_head_sha") != resolved
            or replay_head.get("continuation_requested") is not False
            or action.get("reviewed_head_sha") != reviewed
            or action.get("resolved_head_sha") != resolved
        ):
            raise HistoricalFixtureError("D-264 causal projection changed")
    elif scenario_id == "d265-stale-exiting-resource-gone":
        result = _fact(fixture, "result-published")
        process = _fact(fixture, "process-physically-gone")
        workspace = _fact(fixture, "workspace-physically-gone")
        absent = _fact(fixture, "durable-close-event-absent")
        initial_operation = initial.get("operation")
        replay_operation = replay.get("operation")
        if (
            fixture.get("defect_id") != "D-265-stale-exiting"
            or result.get("result_schema_valid") is not True
            or process.get("process_present") is not False
            or workspace.get("workspace_present") is not False
            or absent.get("resource_closed_event") is not False
            or absent.get("operation_state") != "exiting"
            or absent.get("parent_state") != "awaiting-callback"
            or not isinstance(initial_operation, Mapping)
            or initial_operation.get("state") != absent.get("operation_state")
            or initial.get("resource_receipts") != []
            or not isinstance(replay_operation, Mapping)
            or replay_operation.get("state") != absent.get("operation_state")
            or len(actions) != 2
            or not isinstance(actions[0], Mapping)
            or actions[0].get("action") != "resource-disappears"
        ):
            raise HistoricalFixtureError("D-265 causal projection changed")
    else:
        raise HistoricalFixtureError("historical fixture is bound to the wrong scenario")
    return bound


def load_historical_scenario(
    path: Path, repository_root: Path = ROOT
) -> dict[str, object]:
    return bind_historical_fixture(load_scenario(path), repository_root)


@dataclass(frozen=True)
class HistoricalExecution:
    state: str
    production_paths: tuple[str, ...]
    real_effects: dict[str, int]
    effect_ids: frozenset[str]
    world: LifecycleWorld | None = None


def _run_rearm(
    scenario: Mapping[str, object], schedule: Schedule, root: Path
) -> HistoricalExecution:
    if schedule.action_ids != ("rearm", "tick"):
        raise RuntimeError("rearm historical trace changed")
    # This fixture is also the direct production regression fixture. Importing
    # it is side-effect free because its executable suite is main-guarded.
    from test_review_drive_rearm import ProductionWorkerTick, fixture

    data = fixture(root / "rearm-production")
    progress_at = 2_000_000_000.0
    receipt = rearm_review_drive(data["product"], now=progress_at)
    worker = ProductionWorkerTick(data, int(receipt["attention_revision"]))
    worker.inspect_transport()
    worker.tick_observers()
    record = data["store"].read(data["task_id"], data["task_id"])
    live = LivenessController(data["runtime_root"] / "liveness").current_state()
    if (
        receipt.get("status") != "applied"
        or live is None
        or record.state != "awaiting-callback"
        or live.operation_state != record.state
        or live.operation_revision != record.revision
        or worker.summary_attention_revision != -1
        or worker.loaded_marker is not None
    ):
        raise RuntimeError("production review-drive rearm did not converge")
    expected = set(scenario.get("expected_production_paths", []))
    observed = {"rearm_review_drive", "RuntimeWorkerLoopMixin.tick_observers"}
    if not expected <= observed:
        raise RuntimeError("rearm scenario did not reach its declared production paths")
    return HistoricalExecution(
        record.state,
        tuple(sorted(observed)),
        dict(ZERO_REAL_EFFECTS),
        frozenset({"review-drive-rearmed", "liveness-reconciled"}),
    )


def run_historical_schedule(
    scenario: Mapping[str, object], schedule: Schedule, root: Path
) -> HistoricalExecution:
    """Run one exact trace from its validated causal prefix."""

    scenario = bind_historical_fixture(scenario)

    if scenario["scenario_id"] == "v2-6-5-rearm-liveness-latch":
        return _run_rearm(scenario, schedule, root)
    world = run_schedule(
        scenario,
        schedule,
        lambda: LifecycleWorld.from_scenario(root, scenario),
    )
    expected = set(scenario.get("expected_production_paths", []))
    observed = set(world.production_paths())
    if not expected <= observed:
        missing = sorted(expected - observed)
        raise RuntimeError(f"historical production paths were not reached: {missing}")
    snapshot = world.snapshot()
    scenario_id = str(scenario["scenario_id"])
    if scenario_id == "d265-duplicate-late-callback":
        callbacks = snapshot["callbacks"]
        if (
            len(callbacks) != 2
            or [item["accepted"] for item in callbacks] != [True, False]
        ):
            raise RuntimeError("duplicate callback attempt history was not preserved")
    elif scenario_id == "d265-callback-timeout-completion-collision":
        if (
            snapshot["operation"]["accepted_callback_id"] != "sim-callback"
            or snapshot["operation"]["attention_reason"] is not None
            or snapshot["terminal_history"] != ["complete"]
            or world.provider_result_sha256s() != ("d" * 64,)
            or not {
                "callback-accepted",
                "deadline-recheck",
                "result-published",
            }
            <= world.semantic_effects()
        ):
            raise RuntimeError("accepted completion did not win the timeout collision")
    elif scenario_id == "d265-partial-summary-publication":
        if snapshot["artifacts"] != [
            {
                "kind": "summary",
                "identity_sha256": "f" * 64,
                "status": "published",
            }
        ]:
            raise RuntimeError("summary did not cross the stable atomic owner")
    elif scenario_id == "d265-reap-complete-pending-effect":
        if (
            snapshot["operation"]["pending_effect"]
            or snapshot["operation"]["effect_outcome"] != "succeeded"
        ):
            raise RuntimeError("pending reap effect did not resolve before completion")
    elif scenario_id == "d265-stale-exiting-resource-gone":
        if world.resource_close_count() != 1:
            raise RuntimeError("stale exiting cleanup lacks one durable close receipt")
    world.assert_no_real_effects()
    return HistoricalExecution(
        world.record().state,
        world.production_paths(),
        world.real_effect_counts(),
        world.semantic_effects(),
        world,
    )
