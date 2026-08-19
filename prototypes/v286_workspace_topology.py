#!/usr/bin/env python3
"""Provider-free oracle for the v2.8.6 task-centric cmux topology."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Surface:
    surface_id: str
    role: str
    workspace_id: str
    window_id: str


@dataclass
class Workspace:
    workspace_id: str
    window_id: str
    surfaces: dict[str, Surface] = field(default_factory=dict)
    anchor_surface_id: str = ""
    closed: bool = False

    def add(self, surface_id: str, role: str) -> Surface:
        if self.closed:
            raise ValueError("cannot add a surface to a closed workspace")
        if surface_id in self.surfaces:
            raise ValueError(f"duplicate surface identity: {surface_id}")
        surface = Surface(surface_id, role, self.workspace_id, self.window_id)
        self.surfaces[surface_id] = surface
        if not self.anchor_surface_id:
            self.anchor_surface_id = surface_id
        return surface

    def roles(self) -> set[str]:
        return {surface.role for surface in self.surfaces.values()}


@dataclass
class TaskTopology:
    task_id: str
    primary: Workspace
    review_cycle: int = 0
    review: Workspace | None = None

    def live_workspaces(self) -> tuple[Workspace, ...]:
        candidates = (self.primary, self.review)
        return tuple(item for item in candidates if item is not None and not item.closed)

    def start_review(self, lane_count: int) -> Workspace:
        if lane_count not in {1, 2, 4}:
            raise ValueError("review programs have exactly one, two, or four lanes")
        if self.review is not None and not self.review.closed:
            raise ValueError("a review program is already active")
        self.review_cycle += 1
        review = Workspace(
            workspace_id=f"workspace:review:{self.task_id}:{self.review_cycle}",
            window_id=f"window:review:{self.task_id}:{self.review_cycle}",
        )
        for lane in range(1, lane_count + 1):
            review.add(f"surface:review:{self.review_cycle}:lane:{lane}", "reviewer")
        self.review = review
        return review

    def add_review_surface(self, role: str) -> Surface:
        if self.review is None or self.review.closed:
            raise ValueError("no active review workspace")
        ordinal = len(self.review.surfaces) + 1
        return self.review.add(
            f"surface:review:{self.review_cycle}:{role}:{ordinal}", role
        )

    def add_non_review_surface(self, role: str) -> Surface:
        ordinal = len(self.primary.surfaces) + 1
        return self.primary.add(f"surface:primary:{role}:{ordinal}", role)

    def close_review_workspace(self, workspace_id: str) -> bool:
        if self.review is None:
            return False
        if workspace_id != self.review.workspace_id:
            return False
        self.review.closed = True
        return True

    def finish_review(self, verdict: str) -> bool:
        if verdict not in {"approved", "changes-requested"}:
            raise ValueError(f"non-terminal review verdict: {verdict}")
        if self.review is None:
            return False
        return self.close_review_workspace(self.review.workspace_id)

    def retain_review_for_attention(self) -> None:
        if self.review is None:
            raise ValueError("no active review workspace")

    def reap(self, *, success: bool, workspace_id: str) -> bool:
        if not success or workspace_id != self.primary.workspace_id:
            return False
        self.primary.closed = True
        return True


class Topology:
    def __init__(self) -> None:
        self._coordinator_surfaces = ("surface:coordinator",)
        self.tasks: dict[str, TaskTopology] = {}

    def coordinator_snapshot(self) -> tuple[str, ...]:
        return self._coordinator_surfaces

    def dispatch(self, task_id: str) -> TaskTopology:
        if task_id in self.tasks:
            raise ValueError(f"duplicate task identity: {task_id}")
        primary = Workspace(
            workspace_id=f"workspace:primary:{task_id}",
            window_id=f"window:primary:{task_id}",
        )
        primary.add(f"surface:primary:{task_id}:executor", "executor")
        primary.add(f"surface:primary:{task_id}:dashboard", "dashboard")
        task = TaskTopology(task_id=task_id, primary=primary)
        self.tasks[task_id] = task
        return task


def run_matrix() -> None:
    for lanes in (1, 2, 4):
        task = Topology().dispatch(f"fanout-{lanes}")
        review = task.start_review(lanes)
        assert len(review.surfaces) == lanes
        assert {surface.workspace_id for surface in review.surfaces.values()} == {
            review.workspace_id
        }

    task = Topology().dispatch("cycles")
    first = task.start_review(2)
    task.finish_review("changes-requested")
    assert task.add_non_review_surface("fix").workspace_id == task.primary.workspace_id
    second = task.start_review(2)
    assert first.workspace_id != second.workspace_id
    task.finish_review("approved")
    assert task.reap(success=True, workspace_id=task.primary.workspace_id)
    assert not task.live_workspaces()

    world = Topology()
    left = world.dispatch("left")
    right = world.dispatch("right")
    assert left.primary.workspace_id != right.primary.workspace_id


if __name__ == "__main__":
    run_matrix()
    print("v2.8.6 provider-free workspace topology matrix passed")
