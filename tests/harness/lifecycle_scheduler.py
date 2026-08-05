"""Deterministic bounded partial-order schedules and exact trace replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from lifecycle_simulator_oracle import InvariantViolation


@dataclass(frozen=True)
class Schedule:
    scenario_id: str
    seed: int
    actions: tuple[Mapping[str, object], ...]
    action_ids: tuple[str, ...]
    trace_sha256: str

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "scenario_id": self.scenario_id,
                "seed": self.seed,
                "action_ids": self.action_ids,
                "actions": self.actions,
                "trace_sha256": self.trace_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


@dataclass(frozen=True)
class ScheduleFailure(AssertionError):
    scenario_id: str
    seed: int
    trace_sha256: str
    action_index: int
    action_id: str
    invariant_id: str
    ordered_trace: tuple[str, ...]
    detail: str

    def __str__(self) -> str:
        return (
            f"{self.scenario_id} seed={self.seed} trace={self.trace_sha256} "
            f"action={self.action_index}:{self.action_id} "
            f"invariant={self.invariant_id}: {self.detail}"
        )


def _normalized_actions(
    scenario: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    raw_actions = scenario.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("scenario actions must be a non-empty array")
    normalized: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, Mapping):
            raise ValueError("scenario action must be an object")
        action = dict(raw)
        action_id = str(action.get("action_id") or f"a{index:03d}")
        if action_id in identifiers:
            raise ValueError("scenario action identity is duplicated")
        identifiers.add(action_id)
        action["action_id"] = action_id
        after = action.get("after", [])
        if not isinstance(after, list) or any(
            not isinstance(item, str) or not item for item in after
        ):
            raise ValueError("scenario action dependencies are invalid")
        action["after"] = sorted(set(after))
        normalized.append(action)
    for action in normalized:
        if any(
            dependency not in identifiers or dependency == action["action_id"]
            for dependency in action["after"]
        ):
            raise ValueError("scenario action dependency is unknown or cyclic")
    constraints = scenario.get("ordering_constraints", [])
    if not isinstance(constraints, list):
        raise ValueError("ordering constraints must be an array")
    by_id = {str(action["action_id"]): action for action in normalized}
    for constraint in constraints:
        if (
            not isinstance(constraint, list)
            or len(constraint) != 2
            or any(not isinstance(item, str) for item in constraint)
            or constraint[0] not in by_id
            or constraint[1] not in by_id
            or constraint[0] == constraint[1]
        ):
            raise ValueError("ordering constraint is invalid")
        target = by_id[constraint[1]]
        target["after"] = sorted({*target["after"], constraint[0]})
    return tuple(normalized)


def _topological_orders(
    actions: Sequence[Mapping[str, object]], maximum: int
) -> list[tuple[str, ...]]:
    by_id = {str(action["action_id"]): action for action in actions}
    dependencies = {
        action_id: frozenset(str(item) for item in action.get("after", []))
        for action_id, action in by_id.items()
    }
    orders: list[tuple[str, ...]] = []

    def visit(prefix: tuple[str, ...], remaining: frozenset[str]) -> None:
        if len(orders) >= maximum:
            return
        if not remaining:
            orders.append(prefix)
            return
        completed = frozenset(prefix)
        ready = sorted(
            action_id
            for action_id in remaining
            if dependencies[action_id] <= completed
        )
        if not ready:
            raise ValueError("scenario action dependencies contain a cycle")
        for action_id in ready:
            visit(prefix + (action_id,), remaining - {action_id})

    visit((), frozenset(by_id))
    return orders


def _trace_sha256(
    scenario_id: str,
    seed: int,
    action_ids: Sequence[str],
    actions: Sequence[Mapping[str, object]],
) -> str:
    payload = json.dumps(
        {
            "scenario_id": scenario_id,
            "seed": seed,
            "action_ids": list(action_ids),
            "actions": list(actions),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def compile_schedules(
    scenario: Mapping[str, object],
    *,
    seed: int | None = None,
    max_schedules: int = 64,
) -> tuple[Schedule, ...]:
    """Enumerate bounded legal interleavings in a stable seeded traversal."""

    if type(max_schedules) is not int or not 1 <= max_schedules <= 4096:
        raise ValueError("schedule ceiling must be between 1 and 4096")
    scenario_id = str(scenario.get("scenario_id") or "")
    selected_seed = scenario.get("seed") if seed is None else seed
    if not scenario_id or type(selected_seed) is not int or selected_seed < 0:
        raise ValueError("scenario identity and seed are required")
    actions = _normalized_actions(scenario)
    maximum_steps = scenario.get("max_steps")
    if type(maximum_steps) is not int or len(actions) > maximum_steps:
        raise ValueError("scenario action count exceeds max_steps")
    by_id = {str(action["action_id"]): action for action in actions}
    orders = _topological_orders(actions, max_schedules)
    if len(orders) > 1 and selected_seed % 2:
        orders.reverse()
    result: list[Schedule] = []
    for order in orders:
        ordered_actions = tuple(dict(by_id[action_id]) for action_id in order)
        result.append(
            Schedule(
                scenario_id,
                selected_seed,
                ordered_actions,
                order,
                _trace_sha256(
                    scenario_id, selected_seed, order, ordered_actions
                ),
            )
        )
    return tuple(result)


def replay_trace(
    scenario: Mapping[str, object], *, seed: int, trace_sha256: str
) -> Schedule:
    matches = [
        schedule
        for schedule in compile_schedules(scenario, seed=seed, max_schedules=4096)
        if schedule.trace_sha256 == trace_sha256
    ]
    if len(matches) != 1:
        raise ValueError("exact lifecycle trace is unknown or ambiguous")
    return matches[0]


def run_schedule(
    scenario: Mapping[str, object],
    schedule: Schedule,
    world_factory: Callable[[], object],
) -> object:
    """Execute one schedule and bind the first invariant failure to its trace."""

    if schedule.scenario_id != scenario.get("scenario_id"):
        raise ValueError("schedule belongs to a different scenario")
    world = world_factory()
    for index, action in enumerate(schedule.actions):
        try:
            world.apply(action)
        except InvariantViolation as exc:
            raise ScheduleFailure(
                schedule.scenario_id,
                schedule.seed,
                schedule.trace_sha256,
                index,
                schedule.action_ids[index],
                exc.invariant_id,
                schedule.action_ids,
                exc.detail,
            ) from exc
    return world


def schedule_summary(
    schedules: Sequence[Schedule], *, invariants: int, actions: int
) -> dict[str, int | float]:
    return {
        "scenarios": len({item.scenario_id for item in schedules}),
        "schedules": len(schedules),
        "actions": actions,
        "invariants": invariants,
        # Virtual pacing is the simulator's time authority.  External gate
        # runners measure their own real wall clock separately.
        "wall_seconds": 0.0,
    }
