#!/usr/bin/env python3
"""Provider-free transition matrix for the v2.8.6 cmux topology."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototypes"))

from v286_workspace_topology import Topology  # noqa: E402


class WorkspaceTopologyTests(unittest.TestCase):
    def test_dispatch_owns_executor_and_one_dashboard_outside_coordinator(self) -> None:
        world = Topology()
        coordinator = world.coordinator_snapshot()

        task = world.dispatch("task-a")

        self.assertEqual(world.coordinator_snapshot(), coordinator)
        self.assertEqual(task.primary.roles(), {"dashboard", "executor"})
        self.assertEqual(task.primary.workspace_id, "workspace:primary:task-a")
        self.assertEqual(task.primary.window_id, "window:primary:task-a")
        self.assertEqual(len(task.live_workspaces()), 1)

    def test_simple_deep_and_full_share_one_review_workspace_per_program(self) -> None:
        for lane_count in (1, 2, 4):
            with self.subTest(lane_count=lane_count):
                world = Topology()
                task = world.dispatch(f"task-{lane_count}")

                review = task.start_review(lane_count)

                self.assertEqual(len(task.live_workspaces()), 2)
                self.assertEqual(len(review.surfaces), lane_count)
                self.assertEqual(
                    {surface.workspace_id for surface in review.surfaces.values()},
                    {review.workspace_id},
                )
                self.assertEqual(
                    {surface.window_id for surface in review.surfaces.values()},
                    {review.window_id},
                )
                self.assertEqual(review.anchor_surface_id, "surface:review:1:lane:1")

    def test_changes_requested_routes_fix_to_primary_and_next_cycle_is_fresh(self) -> None:
        task = Topology().dispatch("task-a")
        first = task.start_review(2)

        task.finish_review("changes-requested")
        fix = task.add_non_review_surface("fix")
        second = task.start_review(2)

        self.assertTrue(first.closed)
        self.assertEqual(fix.workspace_id, task.primary.workspace_id)
        self.assertNotEqual(first.workspace_id, second.workspace_id)

    def test_review_activity_and_non_review_activity_route_by_role(self) -> None:
        task = Topology().dispatch("task-a")
        review = task.start_review(1)

        finalization = task.add_review_surface("finalization")
        pivot = task.add_review_surface("structural-pivot")
        verification = task.add_non_review_surface("verification")
        recovery = task.add_non_review_surface("recovery")

        self.assertEqual(finalization.workspace_id, review.workspace_id)
        self.assertEqual(pivot.workspace_id, review.workspace_id)
        self.assertEqual(verification.workspace_id, task.primary.workspace_id)
        self.assertEqual(recovery.workspace_id, task.primary.workspace_id)

    def test_terminal_cleanup_is_exact_idempotent_and_fail_closed(self) -> None:
        task = Topology().dispatch("task-a")
        review = task.start_review(1)

        self.assertFalse(task.close_review_workspace("workspace:foreign"))
        self.assertFalse(review.closed)
        task.retain_review_for_attention()
        self.assertFalse(review.closed)

        self.assertTrue(task.close_review_workspace(review.workspace_id))
        self.assertTrue(task.close_review_workspace(review.workspace_id))
        self.assertFalse(task.reap(success=False, workspace_id=task.primary.workspace_id))
        self.assertFalse(task.primary.closed)
        self.assertFalse(task.reap(success=True, workspace_id="workspace:foreign"))
        self.assertFalse(task.primary.closed)
        self.assertTrue(task.reap(success=True, workspace_id=task.primary.workspace_id))
        self.assertTrue(task.reap(success=True, workspace_id=task.primary.workspace_id))
        self.assertEqual(task.live_workspaces(), ())

    def test_independent_tasks_never_share_owned_workspace_identity(self) -> None:
        world = Topology()
        first = world.dispatch("task-a")
        second = world.dispatch("task-b")
        first_review = first.start_review(4)
        second_review = second.start_review(2)

        self.assertEqual(
            {
                first.primary.workspace_id,
                first_review.workspace_id,
                second.primary.workspace_id,
                second_review.workspace_id,
            },
            {
                "workspace:primary:task-a",
                "workspace:review:task-a:1",
                "workspace:primary:task-b",
                "workspace:review:task-b:1",
            },
        )


if __name__ == "__main__":
    unittest.main()
