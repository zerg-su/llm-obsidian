#!/usr/bin/env python3
"""Exact shared-review-workspace topology for Simple, Deep, and Full review programs."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    CapabilityReport,
    OwnedResources,
    RuntimeRoute,
)
from harness.review_workspace import (  # noqa: E402
    ReviewWorkspaceBinding,
    close_review_workspace,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewOperationRequest,
    ReviewRequest,
    start_review,
)
from harness.review_workspace import close_terminal_review_workspace  # noqa: E402


TASK_SURFACE = "11111111-1111-4111-8111-111111111111"


class FakeReviewRuntime:
    def __init__(
        self,
        store: OperationStore,
        *,
        drift_lane: int = 0,
        workspace_int: int = 101,
    ) -> None:
        self.store = store
        self.drift_lane = drift_lane
        self.workspace_int = workspace_int
        self.requests: list[object] = []
        self.provider_starts = 0
        self.preflights: list[tuple[object, ...]] = []
        self.results: dict[str, object] = {}
        self.closed_workspaces: list[tuple[str, str]] = []

    def preflight_routes(self, requests: tuple[object, ...]) -> tuple[object, ...]:
        self.preflights.append(requests)
        return tuple(
            CapabilityReport(route, True, ("provider:profile-valid",))
            for route, _callback, _surface in requests
        )

    def start(self, request: object, *, on_surface_opened=None, **_kwargs: object) -> object:
        self.requests.append(request)
        ordinal = len(self.requests)
        if ordinal == 1:
            self.assert_workspace_request(request)
            workspace = str(uuid.UUID(int=self.workspace_int))
            window = str(uuid.UUID(int=201))
        else:
            self.assert_split_request(request)
            workspace = str(uuid.UUID(int=self.workspace_int))
            window = str(uuid.UUID(int=201))
            if ordinal == self.drift_lane:
                workspace = str(uuid.UUID(int=102))
        surface = str(uuid.UUID(int=300 + ordinal))
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        record = replace(
            record,
            resources=replace(record.resources, surface_id=surface),
        )
        self.store.save(record, expected_revision=record.revision)
        result = SimpleNamespace(
            record=self.store.read(record.spec.owner_id, record.spec.operation_id),
            action="started",
            checkpoint=f"checkpoint-{ordinal}",
            checkpoint_sha256="",
            workspace_id=workspace,
            workspace_ref="workspace:1",
            window_id=window,
            window_ref="window:1",
            surface_ref=f"surface:{ordinal}",
        )
        if on_surface_opened is not None:
            on_surface_opened(result)
        self.provider_starts += 1
        self.results[record.spec.operation_id] = result
        return result

    def status(self, owner_id: str, operation_id: str) -> object:
        del owner_id
        return self.results[operation_id]

    def register_callback_target(self, *_args: object) -> None:
        return None

    def close_workspace(self, workspace_id: str, window_id: str) -> str:
        self.closed_workspaces.append((workspace_id, window_id))
        return "closed"

    @staticmethod
    def assert_workspace_request(request: object) -> None:
        if request.placement != "workspace" or request.origin_surface != TASK_SURFACE:
            raise AssertionError("first review lane must create the review workspace")

    def assert_split_request(self, request: object) -> None:
        first_surface = self.results[next(iter(self.results))].record.resources.surface_id
        if request.placement != "split" or request.origin_surface != first_surface:
            raise AssertionError("later review lane must split from the exact anchor")


def route(runtime: str) -> RuntimeRoute:
    return RuntimeRoute(
        runtime,
        "fable" if runtime == "claude" else "gpt-5.6-sol",
        "xhigh",
        "reviewer-callback",
        "a" * 64,
    )


class ReviewTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="review-topology.")
        self.root = Path(self.temporary.name)
        self.scratch = self.root / "scratch"
        self.product = self.root / "product"
        self.scratch.mkdir()
        self.product.mkdir()
        self.context = ReviewContext(
            "packets/review/manifest.json",
            "b" * 40,
            "scoped",
            "c" * 64,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, depth: str) -> ReviewOperationRequest:
        policy = ReviewRequest(
            f"review-{depth}",
            depth=depth,
            max_verify_iterations=1 if depth == "simple" else 2,
            selected_provider="anthropic" if depth == "simple" else "",
        )
        routes = {
            axis: route("claude" if axis.startswith("anthropic") else "codex")
            for axis in policy.axes
        }
        return ReviewOperationRequest(
            policy,
            "owner-1",
            routes[policy.axes[0]],
            self.context,
            axis_routes=routes,
        )

    def start(self, depth: str, *, drift_lane: int = 0):
        store = OperationStore(self.root / f"store-{depth}-{drift_lane}")
        runtime = FakeReviewRuntime(store, drift_lane=drift_lane)
        execution = start_review(
            self.request(depth),
            runtime,
            origin_surface=TASK_SURFACE,
            cwd=self.scratch,
            product_root=self.product,
            prompt_pointer="prompt.md",
            callback_root="callbacks",
            round_store=store,
        )
        return runtime, execution

    def test_simple_deep_full_use_one_exact_workspace(self) -> None:
        for depth, lane_count in (("simple", 1), ("deep", 2), ("full", 4)):
            with self.subTest(depth=depth):
                runtime, execution = self.start(depth)
                self.assertIsInstance(execution.workspace, ReviewWorkspaceBinding)
                self.assertEqual(len(execution.lanes), lane_count)
                self.assertEqual(
                    [request.placement for request in runtime.requests],
                    ["workspace", *(["split"] * (lane_count - 1))],
                )
                self.assertEqual(
                    {lane.workspace_id for lane in execution.lanes},
                    {execution.workspace.workspace_id},
                )
                self.assertEqual(
                    {lane.window_id for lane in execution.lanes},
                    {execution.workspace.window_id},
                )

    def test_workspace_drift_is_rejected_before_second_provider_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace identity changed"):
            self.start("deep", drift_lane=2)

    def test_later_program_gets_a_fresh_binding(self) -> None:
        _first_runtime, first = self.start("simple")
        second_store = OperationStore(self.root / "store-next-cycle")
        second_runtime = FakeReviewRuntime(second_store, workspace_int=103)
        second = start_review(
            replace(self.request("simple"), policy=ReviewRequest(
                "review-simple-cycle-2", selected_provider="anthropic"
            )),
            second_runtime,
            origin_surface=TASK_SURFACE,
            cwd=self.scratch,
            product_root=self.product,
            prompt_pointer="prompt.md",
            callback_root="callbacks-cycle-2",
            round_store=second_store,
        )
        self.assertNotEqual(first.workspace.workspace_id, second.workspace.workspace_id)

    def test_terminal_program_closes_exact_workspace_once_after_all_lanes(self) -> None:
        runtime, execution = self.start("deep")
        for lane in execution.lanes:
            record = runtime.store.read(lane.owner_id, lane.operation_id)
            runtime.store.save(
                replace(
                    record,
                    state="complete",
                    resources=OwnedResources(),
                    revision=record.revision + 1,
                ),
                expected_revision=record.revision,
            )
        state = {
            "owner_id": execution.request.owner_id,
            "status": "approved",
            "active_review_operation_id": execution.request.policy.operation_id,
            "review_workspace": execution.workspace.payload(),
            "lanes": [
                {
                    "axis": lane.axis,
                    "operation_id": lane.operation_id,
                    "lane_id": lane.lane_id,
                    "run_id": lane.run_id,
                    "surface_id": "",
                    "state": "complete",
                    "workspace_id": lane.workspace_id,
                    "window_id": lane.window_id,
                }
                for lane in execution.lanes
            ],
        }
        first = close_review_workspace(
            self.root / "gate", runtime, runtime.store, state
        )
        replay = close_review_workspace(
            self.root / "gate", runtime, runtime.store, state
        )
        self.assertEqual(first.status, "closed")
        self.assertEqual(replay, first)
        self.assertEqual(
            runtime.closed_workspaces,
            [(execution.workspace.workspace_id, execution.workspace.window_id)],
        )
        self.assertTrue(
            (
                self.root
                / "gate"
                / execution.request.policy.operation_id
                / "workspace-cleanup.json"
            ).is_file()
        )

    def test_incomplete_or_attention_program_retains_workspace(self) -> None:
        runtime, execution = self.start("simple")
        state = {
            "owner_id": execution.request.owner_id,
            "status": "attention-required",
            "active_review_operation_id": execution.request.policy.operation_id,
            "review_workspace": execution.workspace.payload(),
            "lanes": [
                {
                    "axis": execution.lanes[0].axis,
                    "operation_id": execution.lanes[0].operation_id,
                    "lane_id": execution.lanes[0].lane_id,
                    "run_id": execution.lanes[0].run_id,
                    "surface_id": execution.lanes[0].surface_id,
                    "state": execution.lanes[0].state,
                    "workspace_id": execution.lanes[0].workspace_id,
                    "window_id": execution.lanes[0].window_id,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "terminal"):
            close_review_workspace(
                self.root / "gate-attention", runtime, runtime.store, state
            )
        self.assertEqual(runtime.closed_workspaces, [])

    def test_third_material_failure_retains_workspace_until_pivot(self) -> None:
        class Gate:
            closes = 0

            def close_terminal_workspace(self) -> None:
                self.closes += 1

        class Ledger:
            def __init__(self, count: int) -> None:
                self.count = count

            def snapshot(self):
                return {
                    "terminal_disposition": "",
                    "cycles": [
                        {"terminal_result": "changes-requested"}
                        for _ in range(self.count)
                    ],
                }

        ordinary = Gate()
        retained = Gate()
        self.assertFalse(
            close_terminal_review_workspace(
                ordinary, Ledger(2), "changes-requested"
            )
        )
        self.assertTrue(
            close_terminal_review_workspace(
                retained, Ledger(3), "changes-requested"
            )
        )
        self.assertEqual(ordinary.closes, 1)
        self.assertEqual(retained.closes, 0)


if __name__ == "__main__":
    unittest.main()
