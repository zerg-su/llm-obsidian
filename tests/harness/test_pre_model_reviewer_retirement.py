#!/usr/bin/env python3
"""Identity/liveness matrix for pre-input reviewer retirement."""

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


class FakeProcess:
    def __init__(self, process: str = "dead", supervisor: str = "dead"):
        self.process = process
        self.supervisor = supervisor

    def exact_statuses(
        self,
        _process_group: int,
        _process_identity: str,
        _supervisor_pid: int,
        _supervisor_identity: str,
    ) -> tuple[str, str]:
        return self.process, self.supervisor


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


def input_unconfirmed_fixture(
    root: Path, name: str
) -> tuple[OperationStore, str, Path, Path]:
    store, operation_id, runtime, callback = fixture(root, name)
    current = store.read("owner-review", operation_id)
    store.save(
        replace(
            current,
            state="attention-required",
            attention_reason=AttentionReason.ATTENTION_REQUIRED,
            resume_state="awaiting-callback",
            effect_outcome=EffectOutcome.SUCCEEDED,
            resources=OwnedResources(
                "11111111-1111-4111-8111-111111111111",
                41001,
                41002,
                "a" * 64,
                "b" * 64,
            ),
            revision=current.revision + 1,
        ),
        expected_revision=current.revision,
    )
    child_id = f"{operation_id}-round"
    child = store.read("owner-review", child_id)
    store.save(
        replace(child, state="cancelled", revision=child.revision + 1),
        expected_revision=child.revision,
    )
    (runtime / "exit.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "input-unconfirmed",
                "exit_code": 2,
                "reason": "initial-start-still-composing",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime / "surface-transport.json").write_text(
        '{"schema_version":1,"status":"command-submitted"}\n',
        encoding="utf-8",
    )
    delivery = runtime / "provider-events/generation-1/delivery"
    events = runtime / "provider-events/generation-1/events"
    delivery.mkdir(parents=True)
    events.mkdir(parents=True)
    identity = {
        "schema_version": 1,
        "owner_id": "owner-review",
        "operation_id": operation_id,
        "run_id": f"run-{name}",
        "generation": 1,
        "provider_session_id": f"run-{name}",
        "process_identity": "a" * 64,
        "source_id": f"process:{'a' * 64}",
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "surface_id": "11111111-1111-4111-8111-111111111111",
    }
    (events / "0001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "provider-started",
                "sequence": 1,
                "identity": identity,
                "effect_id": "",
                "exit_code": None,
                "reason": "",
                "result_sha256": "",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (delivery / "delivery-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "idempotency_key": "c" * 64,
                "profile": "interactive",
                "identity": identity,
                "send_status": "ambiguous",
                "send_attempts": 1,
                "callback_submits": 0,
                "attention_reason": "",
                "cursor": {
                    "schema_version": 1,
                    "identity": identity,
                    "profile": "interactive",
                    "last_sequence": 1,
                    "event_gap": False,
                    "provider_started": True,
                    "input_accepted": False,
                    "turn_stops": 0,
                    "result_published": False,
                    "process_exited": False,
                    "resource_closed": False,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
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

    observer_store, observer_id, _observer_runtime, _observer_callback = fixture(
        root, "observer-workspace"
    )
    observer_retired = retire_failed_reviewer_start(
        observer_store,
        "owner-review",
        observer_id,
        cmux_adapter=FakeCmux(surface="missing", workspace="alive"),
    )
    check(
        "failed reviewer retirement preserves a live observer workspace",
        observer_retired is not None
        and observer_store.read("owner-review", observer_id).state == "complete"
        and observer_store.read("owner-review", observer_id).resources
        == OwnedResources(),
    )

    semantic_store, semantic_id, _semantic_runtime, _semantic_callback = (
        input_unconfirmed_fixture(root, "input-unconfirmed")
    )
    semantic_retired = _cancel_or_close(
        semantic_store,
        "owner-review",
        semantic_id,
        process_adapter=FakeProcess(),
        cmux_adapter=FakeCmux(),
    )
    check(
        "proven input-unconfirmed reviewer retires after exact absence proof",
        semantic_retired is not None
        and semantic_store.read("owner-review", semantic_id).state == "complete"
        and semantic_store.read("owner-review", semantic_id).resources
        == OwnedResources(),
    )

    for label, process in (
        ("live semantic process", FakeProcess(process="alive")),
        ("unknown semantic supervisor", FakeProcess(supervisor="unknown")),
    ):
        guarded, guarded_id, _guarded_runtime, _guarded_callback = (
            input_unconfirmed_fixture(root, label.replace(" ", "-"))
        )
        guarded_result = _cancel_or_close(
            guarded,
            "owner-review",
            guarded_id,
            process_adapter=process,
            cmux_adapter=FakeCmux(),
        )
        check(
            f"retirement rejects {label}",
            guarded_result.state == "attention-required"
            and guarded.read("owner-review", guarded_id).state
            == "attention-required"
            and guarded.read("owner-review", guarded_id).resources
            != OwnedResources(),
        )

    for label, mutate, cmux, create_callback in (
        ("live surface", None, FakeCmux(surface="alive"), False),
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
