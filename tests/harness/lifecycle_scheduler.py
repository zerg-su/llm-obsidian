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


def _dependency_map(
    actions: Sequence[Mapping[str, object]],
) -> dict[str, frozenset[str]]:
    return {
        str(action["action_id"]): frozenset(
            str(item) for item in action.get("after", [])
        )
        for action in actions
    }


def _dependency_closure(
    dependencies: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    closure: dict[str, frozenset[str]] = {}

    def ancestors(action_id: str, visiting: frozenset[str]) -> frozenset[str]:
        if action_id in closure:
            return closure[action_id]
        if action_id in visiting:
            raise ValueError("scenario action dependencies contain a cycle")
        result: set[str] = set(dependencies[action_id])
        for dependency in dependencies[action_id]:
            result.update(ancestors(dependency, visiting | {action_id}))
        closure[action_id] = frozenset(result)
        return closure[action_id]

    for action_id in dependencies:
        ancestors(action_id, frozenset())
    return closure


def _independent_pairs(
    dependencies: Mapping[str, frozenset[str]],
) -> tuple[tuple[str, str], ...]:
    closure = _dependency_closure(dependencies)
    identifiers = sorted(dependencies)
    return tuple(
        (left, right)
        for index, left in enumerate(identifiers)
        for right in identifiers[index + 1 :]
        if left not in closure[right] and right not in closure[left]
    )


def _seeded_rank(seed: int, salt: str, action_id: str) -> str:
    return hashlib.sha256(f"{seed}:{salt}:{action_id}".encode()).hexdigest()


def _topological_order(
    dependencies: Mapping[str, frozenset[str]],
    *,
    seed: int,
    salt: str,
    extra_order: tuple[str, str] | None = None,
    reverse: bool = False,
) -> tuple[str, ...]:
    selected = {key: set(value) for key, value in dependencies.items()}
    if extra_order is not None:
        before, after = extra_order
        selected[after].add(before)
    _dependency_closure(
        {key: frozenset(value) for key, value in selected.items()}
    )
    prefix: list[str] = []
    remaining = set(selected)
    while remaining:
        completed = frozenset(prefix)
        ready = [
            action_id
            for action_id in remaining
            if selected[action_id] <= completed
        ]
        if not ready:
            raise ValueError("scenario action dependencies contain a cycle")
        ready.sort(
            key=lambda action_id: (
                _seeded_rank(seed, salt, action_id),
                action_id,
            ),
            reverse=reverse,
        )
        chosen = ready[0]
        prefix.append(chosen)
        remaining.remove(chosen)
    return tuple(prefix)


def _pairwise_coverage(
    order: Sequence[str], pairs: Sequence[tuple[str, str]]
) -> frozenset[tuple[str, str]]:
    positions = {action_id: index for index, action_id in enumerate(order)}
    return frozenset(
        (left, right)
        if positions[left] < positions[right]
        else (right, left)
        for left, right in pairs
    )


def _wave_count(dependencies: Mapping[str, frozenset[str]]) -> int:
    remaining = set(dependencies)
    completed: set[str] = set()
    waves = 0
    while remaining:
        ready = {
            action_id
            for action_id in remaining
            if dependencies[action_id] <= completed
        }
        if not ready:
            raise ValueError("scenario action dependencies contain a cycle")
        waves += 1
        completed.update(ready)
        remaining.difference_update(ready)
    return waves


def _coverage_first_orders(
    actions: Sequence[Mapping[str, object]],
    *,
    seed: int,
    maximum: int,
) -> list[tuple[str, ...]]:
    by_id = {str(action["action_id"]): action for action in actions}
    dependencies = _dependency_map(actions)
    pairs = _independent_pairs(dependencies)
    candidates: list[tuple[str, ...]] = []

    def add(order: tuple[str, ...]) -> None:
        if order not in candidates:
            candidates.append(order)

    add(
        _topological_order(
            dependencies,
            seed=seed,
            salt="baseline",
        )
    )
    add(
        _topological_order(
            dependencies,
            seed=seed,
            salt="baseline",
            reverse=True,
        )
    )
    for left, right in pairs:
        for before, after in ((left, right), (right, left)):
            salt = f"pair:{before}:{after}"
            add(
                _topological_order(
                    dependencies,
                    seed=seed,
                    salt=salt,
                    extra_order=(before, after),
                )
            )
            add(
                _topological_order(
                    dependencies,
                    seed=seed,
                    salt=salt,
                    extra_order=(before, after),
                    reverse=True,
                )
            )

    obligations = {
        orientation
        for left, right in pairs
        for orientation in ((left, right), (right, left))
    }
    selected: list[tuple[str, ...]] = []
    uncovered = set(obligations)
    while candidates and (uncovered or not selected):
        candidates.sort(
            key=lambda order: (
                -len(_pairwise_coverage(order, pairs) & uncovered),
                hashlib.sha256(
                    f"{seed}:selection:{','.join(order)}".encode()
                ).hexdigest(),
                order,
            )
        )
        best = candidates.pop(0)
        gain = _pairwise_coverage(best, pairs) & uncovered
        if selected and not gain:
            break
        selected.append(best)
        uncovered.difference_update(gain)
        if len(selected) >= maximum:
            break
    if uncovered:
        missing = ",".join(f"{left}<{right}" for left, right in sorted(uncovered))
        raise ValueError(
            "schedule ceiling cannot satisfy pairwise coverage: " + missing
        )
    if not selected:
        raise ValueError("scenario has no legal schedule")
    return selected


def _trace_sha256(
    scenario_id: str,
    seed: int,
    action_ids: Sequence[str],
    actions: Sequence[Mapping[str, object]],
    source_sha256: str = "",
) -> str:
    payload = json.dumps(
        {
            "scenario_id": scenario_id,
            "seed": seed,
            "action_ids": list(action_ids),
            "actions": list(actions),
            "source_sha256": source_sha256,
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
    maximum_depth = scenario.get("max_depth", maximum_steps)
    if type(maximum_depth) is not int or len(actions) > maximum_depth:
        raise ValueError("scenario action count exceeds max_depth")
    maximum_waves = scenario.get("max_waves", len(actions))
    if (
        type(maximum_waves) is not int
        or maximum_waves < 1
        or _wave_count(_dependency_map(actions)) > maximum_waves
    ):
        raise ValueError("scenario dependency graph exceeds max_waves")
    by_id = {str(action["action_id"]): action for action in actions}
    orders = _coverage_first_orders(
        actions,
        seed=selected_seed,
        maximum=max_schedules,
    )
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
                    scenario_id,
                    selected_seed,
                    order,
                    ordered_actions,
                    str(scenario.get("source_sha256") or ""),
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
) -> dict[str, int]:
    return {
        "scenarios": len({item.scenario_id for item in schedules}),
        "schedules": len(schedules),
        "actions": actions,
        "invariants": invariants,
    }
