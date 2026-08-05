#!/usr/bin/env python3
"""Irreversible delivery and durable time-last resource closure matrix."""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.provider_events import (  # noqa: E402
    ProviderEvent,
    ProviderEventCursor,
    ProviderEventIdentity,
)
from harness.contracts import OperationSpec, OwnedResources, RuntimeRoute  # noqa: E402
from harness.runtime_session_delivery import (  # noqa: E402
    DeliveryController,
    DeliveryError,
)
from harness.runtime_session_liveness import (  # noqa: E402
    ResourceCloseError,
    ResourceClosureLedger,
    ResourceIdentity,
    ResourceObservation,
    observe_resource_liveness,
    resource_closed_event,
)
from harness.runtime_sessions import RuntimeSessionManager  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def rejected(label: str, action, error: type[Exception] = DeliveryError) -> None:
    try:
        action()
    except error:
        check(label, True)
    else:
        check(label, False)


INTERACTIVE = ProviderEventIdentity(
    owner_id="interactive-owner",
    operation_id="review-attempt",
    run_id="review-run",
    generation=1,
    provider_session_id="provider-session",
    process_identity="a" * 64,
    source_id="provider-process-1",
    workspace_id="workspace-1",
    surface_id="surface-1",
)
EPHEMERAL = ProviderEventIdentity(
    owner_id="ephemeral-owner",
    operation_id="bounded-attempt",
    run_id="bounded-run",
    generation=1,
    provider_session_id="ephemeral-session",
    process_identity="b" * 64,
    source_id="ephemeral-process-1",
)


def provider_event(
    identity: ProviderEventIdentity, kind: str, sequence: int
) -> ProviderEvent:
    values: dict[str, object] = {}
    if kind == "input-accepted":
        values["effect_id"] = "delivery-key"
    elif kind == "result-published":
        values["result_sha256"] = "c" * 64
    elif kind == "process-exited":
        values["exit_code"] = 0
    elif kind == "event-gap":
        values["reason"] = "source-gap"
    elif kind == "resource-closed":
        values["reason"] = "owned-resources-gone"
    return ProviderEvent(kind, identity, sequence, **values)


with tempfile.TemporaryDirectory(prefix="delivery-boundary.") as raw:
    root = Path(raw)
    retry_root = root / "retry"
    retry = DeliveryController(
        retry_root,
        profile="interactive",
        identity=INTERACTIVE,
        idempotency_key="delivery-key",
    )
    first = retry.decide()
    check(
        "first send is durably reserved before its provider-facing effect",
        first.action == "send"
        and first.effect_id == "delivery-key"
        and retry.current_state().send_attempts == 1
        and retry.current_state().send_status == "reserved",
    )
    retry.record_send_outcome("delivery-key", "failed-before-input")
    second = retry.decide()
    check(
        "one exact pre-accept retry reuses the idempotency key",
        second.action == "send"
        and second.effect_id == first.effect_id
        and retry.current_state().send_attempts == 2,
    )
    retry.record_send_outcome("delivery-key", "ambiguous")
    replay = DeliveryController(
        retry_root,
        profile="interactive",
        identity=INTERACTIVE,
        idempotency_key="delivery-key",
    )
    after_crash = replay.decide(screen_changed=True)
    check(
        "ambiguous input survives restart and is never replayed blindly",
        after_crash.action == "wait"
        and replay.current_state().send_attempts == 2
        and replay.current_state().send_status == "ambiguous",
    )
    state_file = retry_root / "delivery-state.json"
    check(
        "delivery state remains owner-only and stores no prompt or screen body",
        state_file.stat().st_mode & 0o077 == 0
        and "prompt" not in state_file.read_text(encoding="utf-8")
        and "screen" not in state_file.read_text(encoding="utf-8"),
    )

    for label, cursor_changes in (
        (
            "durable reload rejects impossible terminal flags",
            {"result_published": True, "resource_closed": True},
        ),
        (
            "durable reload rejects wrong cursor scalar types",
            {"last_sequence": 1, "provider_started": 1},
        ),
        (
            "durable reload rejects result before accepted input",
            {
                "last_sequence": 2,
                "provider_started": True,
                "result_published": True,
            },
        ),
    ):
        corrupt_root = root / label.replace(" ", "-")
        corrupt = DeliveryController(
            corrupt_root,
            profile="interactive",
            identity=INTERACTIVE,
            idempotency_key="delivery-key",
        )
        assert corrupt.decide().action == "send"
        corrupt_path = corrupt_root / "delivery-state.json"
        corrupt_payload = json.loads(corrupt_path.read_text(encoding="utf-8"))
        corrupt_payload["cursor"].update(cursor_changes)
        corrupt_path.write_text(
            json.dumps(corrupt_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        corrupt_path.chmod(0o600)
        rejected(label, corrupt.current_state)
    concurrent = DeliveryController(
        root / "concurrent-send",
        profile="interactive",
        identity=INTERACTIVE,
        idempotency_key="delivery-key",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent_actions = list(pool.map(lambda _index: concurrent.decide(), range(8)))
    check(
        "concurrent first delivery linearizes to exactly one provider send",
        sum(item.action == "send" for item in concurrent_actions) == 1
        and sum(item.action == "wait" for item in concurrent_actions) == 7
        and concurrent.current_state().send_attempts == 1,
    )

    accepted = DeliveryController(
        root / "accepted",
        profile="interactive",
        identity=INTERACTIVE,
        idempotency_key="delivery-key",
    )
    assert accepted.decide().action == "send"
    accepted.record_send_outcome("delivery-key", "accepted")
    check(
        "screen repaint after accepted input can only wait",
        accepted.decide(screen_changed=True).action == "wait"
        and accepted.current_state().send_attempts == 1,
    )
    check(
        "deadline after accepted input can only terminate attention, never resend",
        accepted.decide(deadline_reached=True).action == "attention"
        and accepted.current_state().send_attempts == 1,
    )

    interactive = DeliveryController(
        root / "interactive-stop",
        profile="interactive",
        identity=INTERACTIVE,
        idempotency_key="delivery-key",
    )
    assert interactive.decide().action == "send"
    assert interactive.decide(
        event=provider_event(INTERACTIVE, "provider-started", 1)
    ).action == "wait"
    assert interactive.decide(
        event=provider_event(INTERACTIVE, "input-accepted", 2)
    ).action == "wait"
    one_stop = interactive.decide(
        event=provider_event(INTERACTIVE, "turn-stopped", 3)
    )
    check(
        "one typed interactive Stop permits one submit-only recovery",
        one_stop.action == "submit-callback"
        and one_stop.effect_id != "delivery-key"
        and interactive.current_state().callback_submits == 1,
    )
    second_stop = interactive.decide(
        event=provider_event(INTERACTIVE, "turn-stopped", 4)
    )
    check(
        "second interactive Stop becomes attention with zero second submit",
        second_stop.action == "attention"
        and interactive.current_state().callback_submits == 1,
    )

    result_flow = DeliveryController(
        root / "result",
        profile="ephemeral",
        identity=EPHEMERAL,
        idempotency_key="delivery-key",
    )
    assert result_flow.decide().action == "send"
    for kind, sequence in (
        ("provider-started", 1),
        ("input-accepted", 2),
    ):
        assert result_flow.decide(
            event=provider_event(EPHEMERAL, kind, sequence)
        ).action == "wait"
    published = result_flow.decide(
        event=provider_event(EPHEMERAL, "result-published", 3)
    )
    check(
        "schema-valid result closes business delivery without screen authority",
        published.action == "close",
    )

    for label, terminal_kind in (
        ("ephemeral exit without result is terminal attention", "process-exited"),
        ("explicit event gap is terminal attention", "event-gap"),
        ("ephemeral resource close without result preserves attention", "resource-closed"),
    ):
        controller = DeliveryController(
            root / terminal_kind,
            profile="ephemeral",
            identity=EPHEMERAL,
            idempotency_key="delivery-key",
        )
        assert controller.decide().action == "send"
        assert controller.decide(
            event=provider_event(EPHEMERAL, "provider-started", 1)
        ).action == "wait"
        assert controller.decide(
            event=provider_event(EPHEMERAL, "input-accepted", 2)
        ).action == "wait"
        terminal = controller.decide(
            event=provider_event(EPHEMERAL, terminal_kind, 3)
        )
        check(
            label,
            terminal.action == "attention"
            and controller.current_state().send_attempts == 1
            and controller.current_state().callback_submits == 0,
        )

    wrong_effect = DeliveryController(
            root / "wrong-effect",
            profile="interactive",
            identity=INTERACTIVE,
            idempotency_key="other-key",
    )
    assert wrong_effect.decide().action == "send"
    assert wrong_effect.decide(
        event=provider_event(INTERACTIVE, "provider-started", 1)
    ).action == "wait"
    rejected(
        "accepted input event must match the reserved idempotency key",
        lambda: wrong_effect.decide(
            event=provider_event(INTERACTIVE, "input-accepted", 2)
        ),
    )

    resource_identity = ResourceIdentity(
        owner_id=INTERACTIVE.owner_id,
        operation_id=INTERACTIVE.operation_id,
        run_id=INTERACTIVE.run_id,
        generation=INTERACTIVE.generation,
        provider_session_id=INTERACTIVE.provider_session_id,
        process_identity=INTERACTIVE.process_identity,
        supervisor_identity="d" * 64,
        source_id=INTERACTIVE.source_id,
        workspace_id=INTERACTIVE.workspace_id,
        surface_id=INTERACTIVE.surface_id,
    )
    still_alive = ResourceObservation(
        process_status="alive",
        supervisor_status="alive",
        surface_status="alive",
        workspace_status="alive",
        screen_changed=True,
    )
    check(
        "screen change can only request another resource observation",
        observe_resource_liveness(still_alive).action == "recheck",
    )
    check(
        "deadline can terminate attention but cannot claim resource closure",
        observe_resource_liveness(
            dataclasses.replace(still_alive, deadline_reached=True)
        ).action
        == "attention",
    )
    gone = ResourceObservation(
        process_status="dead",
        supervisor_status="dead",
        surface_status="missing",
        workspace_status="missing",
    )
    ledger_root = root / "resource-close"
    ledger = ResourceClosureLedger(ledger_root)
    first_close = ledger.close(resource_identity, gone)
    duplicate_close = ledger.close(resource_identity, gone)
    close_files = list(ledger_root.glob("resource-closed.json"))
    check(
        "disappeared owned resources converge through exactly one durable receipt",
        first_close.created
        and not duplicate_close.created
        and first_close.receipt == duplicate_close.receipt
        and len(close_files) == 1
        and close_files[0].stat().st_mode & 0o077 == 0,
    )
    concurrent_ledger = ResourceClosureLedger(root / "concurrent-close")
    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent_closes = list(
            pool.map(
                lambda _index: concurrent_ledger.close(resource_identity, gone),
                range(8),
            )
        )
    check(
        "concurrent resource disappearance publishes exactly one close receipt",
        sum(item.created for item in concurrent_closes) == 1
        and len({item.receipt.close_id for item in concurrent_closes}) == 1,
    )
    rejected(
        "durable close receipt cannot be rebound to another owner",
        lambda: ledger.close(
            dataclasses.replace(resource_identity, owner_id="owner-2"), gone
        ),
        ResourceCloseError,
    )

    cursor = ProviderEventCursor.start("interactive", INTERACTIVE)
    for kind, sequence in (
        ("provider-started", 1),
        ("input-accepted", 2),
        ("process-exited", 3),
    ):
        cursor = cursor.advance(provider_event(INTERACTIVE, kind, sequence))
    close_event = resource_closed_event(cursor, first_close.receipt)
    closed_cursor = cursor.advance(close_event)
    check(
        "durable close receipt maps to one validated resource-closed event",
        close_event.kind == "resource-closed"
        and close_event.sequence == 4
        and closed_cursor.resource_closed,
    )

    class GoneProcess:
        @staticmethod
        def process_status(_process_group: int, _identity: str) -> str:
            return "dead"

        @staticmethod
        def pid_status(_pid: int, _identity: str) -> str:
            return "dead"

    class GoneCmux:
        @staticmethod
        def status(_surface_id: str) -> str:
            return "missing"

        @staticmethod
        def workspace_status(_workspace_id: str, _window_id: str) -> str:
            return "missing"

        @staticmethod
        def close_exact(_surface_id: str) -> None:
            raise AssertionError("missing surface must not be closed twice")

        @staticmethod
        def close_workspace_exact(_workspace_id: str, _window_id: str) -> None:
            raise AssertionError("missing workspace must not be closed twice")

    store = OperationStore(root / "cleanup-store")
    cleanup_spec = OperationSpec(
        "cleanup-operation",
        "cleanup-key",
        "review",
        "cleanup-owner",
        RuntimeRoute(
            "codex", "bounded-model", "xhigh", "reviewer-callback", "e" * 64
        ),
        "context/manifest.json",
        "scoped",
    )
    store.create(cleanup_spec, lane_id="cleanup-lane", run_id="cleanup-run")
    supervisor = OperationSupervisor(
        store, cleanup_spec.owner_id, cleanup_spec.operation_id
    )
    for state in ("preflight", "starting"):
        supervisor.transition(state)
    supervisor.bind_resources(
        OwnedResources(
            "cleanup-surface",
            123,
            124,
            "f" * 64,
            "1" * 64,
        )
    )
    for state in ("running", "finalizing", "exiting"):
        supervisor.transition(state)
    manager = RuntimeSessionManager(store, GoneCmux(), GoneProcess())
    cleanup_record = supervisor.read()
    manager._write_json(  # fixture: materialize the normal start metadata contract
        manager._metadata_path(cleanup_record),
        {
            "schema_version": 1,
            "operation_id": cleanup_spec.operation_id,
            "run_id": cleanup_record.run_id,
            "placement": "split",
            "workspace_id": "cleanup-workspace",
            "window_id": "cleanup-window",
        },
    )
    manager._write_json(
        manager._callback_target_path(cleanup_record),
        {
            "schema_version": 1,
            "generation": 1,
            "operation_id": cleanup_spec.operation_id,
            "run_id": cleanup_record.run_id,
            "callback_pointer": "callbacks/result.json",
        },
    )
    cleaned = manager.cleanup(cleanup_spec.owner_id, cleanup_spec.operation_id)
    replayed_cleanup = manager.cleanup(
        cleanup_spec.owner_id, cleanup_spec.operation_id
    )
    integrated_receipt = (
        manager._state_root(cleaned.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "runtime cleanup publishes close before clearing ownership and replays once",
        cleaned.record.resources == OwnedResources()
        and cleaned.record.state == "complete"
        and replayed_cleanup.action == "terminal"
        and integrated_receipt.is_file()
        and json.loads(integrated_receipt.read_text(encoding="utf-8"))["status"]
        == "resource-closed",
    )

print("delivery and durable close matrix: ok")
