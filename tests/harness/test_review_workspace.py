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

from harness.contracts import AttentionReason, CapabilityReport, RuntimeRoute  # noqa: E402
from harness.review_workspace import ReviewWorkspaceBinding  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewOperationRequest,
    ReviewRequest,
    start_review,
)


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


if __name__ == "__main__":
    unittest.main()
