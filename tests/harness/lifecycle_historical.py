"""Connected execution of frozen lifecycle regressions through production owners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from harness.review_drive_rearm import rearm_review_drive
from harness.liveness import LivenessController

from lifecycle_scheduler import Schedule, run_schedule
from lifecycle_simulator import LifecycleWorld


ZERO_REAL_EFFECTS = {"provider": 0, "model": 0, "cmux": 0, "network": 0}


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
        frozenset(),
    )


def run_historical_schedule(
    scenario: Mapping[str, object], schedule: Schedule, root: Path
) -> HistoricalExecution:
    """Run one exact trace from its validated causal prefix."""

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
        frozenset(str(item["effect_id"]) for item in world.provider.effects()),
        world,
    )
