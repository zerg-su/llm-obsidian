#!/usr/bin/env python3
"""Identity/liveness matrix for zero-provider-effect reviewer retirement."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.cli import _cancel_or_close  # noqa: E402
from harness.pre_model_reviewer_retirement import (  # noqa: E402
    retire_failed_reviewer_start,
)
from harness.store import OperationStore  # noqa: E402


class FakeCmux:
    def __init__(self, surface: str = "missing", workspace: str = "missing"):
        self.surface = surface
        self.workspace = workspace

    def status(self, _surface_id: str) -> str:
        return self.surface

    def workspace_status(self, _workspace_id: str, _window_id: str) -> str:
        return self.workspace


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def fixture(root: Path, name: str) -> tuple[OperationStore, str, Path, Path]:
    store = OperationStore(root / name / "store")
    owner = "owner-review"
    operation_id = f"review-{name}"
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "xhigh", "reviewer-callback", "e" * 64
    )
    parent = store.create(
        OperationSpec(
            operation_id,
            f"key-{name}",
            "deep-review-spec",
            owner,
            route,
            "packets/review.json",
            "scoped",
        ),
        lane_id="lane-review",
        run_id=f"run-{name}",
    )
    store.transition(owner, operation_id, "preflight")
    store.transition(owner, operation_id, "starting")
    current = store.read(owner, operation_id)
    store.save(
        replace(
            current,
            resources=OwnedResources("11111111-1111-4111-8111-111111111111"),
            revision=current.revision + 1,
        ),
        expected_revision=current.revision,
    )
    store.begin_effect(owner, operation_id, "start-provider")
    store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=AttentionReason.PROCESS_START_FAILED,
    )
    store.resolve_effect(owner, operation_id, EffectOutcome.FAILED)

    child_id = f"{operation_id}-round"
    store.create(
        OperationSpec(
            child_id,
            f"key-{name}-round",
            "review-round",
            owner,
            route,
            "packets/review.json",
            "scoped",
            parent_operation_id=operation_id,
        ),
        lane_id="lane-review",
        run_id=f"run-{name}-round",
    )
    store.transition(owner, child_id, "failed")

    runtime = store.root / "owners" / owner / "runtime" / operation_id
    runtime.mkdir(parents=True)
    scratch = root / name / "scratch"
    product = root / name / "product"
    callback = scratch / "callbacks/openai-intent/.review-callback.json"
    callback.parent.mkdir(parents=True)
    product.mkdir(parents=True)
    values = {
        "session.json": {
            "schema_version": 1,
            "operation_id": operation_id,
            "run_id": parent.run_id,
            "cwd": str(scratch.resolve()),
            "product_root": str(product.resolve()),
            "placement": "workspace",
            "workspace_id": "22222222-2222-4222-8222-222222222222",
            "workspace_ref": "workspace:1",
            "window_id": "33333333-3333-4333-8333-333333333333",
            "window_ref": "window:1",
            "surface_ref": "surface:1",
            "callback_mode": "envelope",
            "callback_pointer": "callbacks/openai-intent/.review-callback.json",
        },
        "launch.json": {
            "schema_version": 1,
            "owner_id": owner,
            "operation_id": operation_id,
            "run_id": parent.run_id,
            "runtime": "codex",
            "callback_mode": "envelope",
            "cwd": str(scratch.resolve()),
            "product_root": str(product.resolve()),
            "surface_id": "11111111-1111-4111-8111-111111111111",
            "store_root": str(store.root.resolve()),
            "ready_path": str((runtime / "ready.json").resolve()),
            "exit_path": str((runtime / "exit.json").resolve()),
            "callback_registration": str((runtime / "callback-target.json").resolve()),
            "callback_pointer": str(callback.resolve()),
        },
        "ready.json": {"schema_version": 1, "status": "failed"},
        "exit.json": {
            "schema_version": 1,
            "status": "review-input-template-invalid",
            "exit_code": 2,
        },
        "callback-target.json": {
            "schema_version": 1,
            "generation": 1,
            "operation_id": child_id,
            "run_id": f"run-{name}-round",
            "callback_pointer": "callbacks/openai-intent/.review-callback.json",
        },
    }
    for filename, value in values.items():
        (runtime / filename).write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )
    return store, operation_id, runtime, callback


with tempfile.TemporaryDirectory(prefix="pre-model-retirement.") as raw:
    root = Path(raw)
    store, operation_id, _runtime, callback = fixture(root, "green")
    before = store.read("owner-review", operation_id)
    retired = retire_failed_reviewer_start(
        store, "owner-review", operation_id, cmux_adapter=FakeCmux()
    )
    after = store.read("owner-review", operation_id)
    check(
        "proven zero-provider-effect failure retires without signaling",
        retired is not None
        and retired.changed
        and retired.previous_state == "attention-required"
        and after.state == "complete"
        and after.resources == OwnedResources()
        and after.effect_id == "start-provider"
        and after.effect_outcome == EffectOutcome.FAILED
        and not callback.exists(),
    )
    stable = store.read("owner-review", operation_id)
    replay = retire_failed_reviewer_start(
        store, "owner-review", operation_id, cmux_adapter=FakeCmux()
    )
    check(
        "retirement replay is idempotent with zero store effect",
        replay is None and store.read("owner-review", operation_id) == stable,
    )

    cli_store, cli_operation_id, _cli_runtime, _cli_callback = fixture(
        root, "cli"
    )
    cli_result = _cancel_or_close(
        cli_store,
        "owner-review",
        cli_operation_id,
        process_adapter=object(),
        cmux_adapter=FakeCmux(),
    )
    check(
        "CLI orchestration uses the signal-free retirement seam",
        cli_result.state == "complete"
        and cli_store.read("owner-review", cli_operation_id).resources
        == OwnedResources(),
    )

    for label, mutate, cmux, create_callback in (
        ("live surface", None, FakeCmux(surface="alive"), False),
        ("live workspace", None, FakeCmux(workspace="alive"), False),
        ("callback presence", None, FakeCmux(), True),
        (
            "provider effect success",
            {"effect_outcome": EffectOutcome.SUCCEEDED},
            FakeCmux(),
            False,
        ),
        ("stale run identity", None, FakeCmux(), False),
    ):
        candidate, candidate_id, runtime, candidate_callback = fixture(
            root, label.replace(" ", "-")
        )
        if mutate:
            current = candidate.read("owner-review", candidate_id)
            candidate.save(
                replace(current, revision=current.revision + 1, **mutate),
                expected_revision=current.revision,
            )
        if create_callback:
            candidate_callback.write_text("{}\n", encoding="utf-8")
        if label == "stale run identity":
            path = runtime / "session.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["run_id"] = "stale-run"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        before = candidate.read("owner-review", candidate_id)
        result = retire_failed_reviewer_start(
            candidate, "owner-review", candidate_id, cmux_adapter=cmux
        )
        check(
            f"retirement rejects {label}",
            result is None and candidate.read("owner-review", candidate_id) == before,
        )

    escaped, escaped_id, escaped_runtime, escaped_callback = fixture(
        root, "callback-ancestor-symlink"
    )
    escaped_callback.parent.rmdir()
    escaped_callback.parent.parent.rmdir()
    outside = root / "outside-callbacks" / "openai-intent"
    outside.mkdir(parents=True)
    escaped_callback.parent.parent.symlink_to(outside.parent, target_is_directory=True)
    launch_path = escaped_runtime / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    launch["callback_pointer"] = str(escaped_callback.resolve())
    launch_path.write_text(
        json.dumps(launch, sort_keys=True) + "\n", encoding="utf-8"
    )
    before_escape = escaped.read("owner-review", escaped_id)
    escaped_result = retire_failed_reviewer_start(
        escaped, "owner-review", escaped_id, cmux_adapter=FakeCmux()
    )
    check(
        "retirement rejects a callback pointer with a symlinked ancestor",
        escaped_result is None
        and escaped.read("owner-review", escaped_id) == before_escape,
    )

print("pre-model reviewer retirement matrix: ok")
