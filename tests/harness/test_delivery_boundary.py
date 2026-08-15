#!/usr/bin/env python3
"""Irreversible delivery and durable time-last resource closure matrix."""

from __future__ import annotations

import dataclasses
import hashlib
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
from harness.contracts import (  # noqa: E402
    CallbackEnvelope,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.runtime_session_delivery import (  # noqa: E402
    DeliveryController,
    DeliveryError,
)
from harness.runtime_provider_events import RuntimeProviderEventStream  # noqa: E402
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
        "typed interactive Stop fails closed without a production adapter",
        one_stop.action == "attention"
        and one_stop.reason == "callback-submit-unsupported"
        and not one_stop.effect_id
        and interactive.current_state().callback_submits == 0,
    )
    second_stop = interactive.decide(
        event=provider_event(INTERACTIVE, "turn-stopped", 4)
    )
    check(
        "repeated interactive Stop remains attention with zero submit",
        second_stop.action == "attention"
        and interactive.current_state().callback_submits == 0,
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
    )
    check(
        "alive exact task surface cannot be inferred closed from workspace state",
        observe_resource_liveness(
            dataclasses.replace(
                gone,
                surface_status="alive",
            )
        ).action
        == "recheck",
    )
    check(
        "unknown exact task surface remains fail-closed",
        observe_resource_liveness(
            dataclasses.replace(gone, surface_status="unknown")
        ).action
        == "attention",
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
    runtime_stream = RuntimeProviderEventStream.create(
        root / "runtime-close-stream",
        owner_id=INTERACTIVE.owner_id,
        operation_id=INTERACTIVE.operation_id,
        run_id=INTERACTIVE.run_id,
        generation=INTERACTIVE.generation,
        process_identity=INTERACTIVE.process_identity,
        workspace_id=INTERACTIVE.workspace_id,
        surface_id=INTERACTIVE.surface_id,
        input_sha256="2" * 64,
    )
    runtime_resource_identity = dataclasses.replace(
        resource_identity,
        provider_session_id=INTERACTIVE.run_id,
        source_id=f"process:{INTERACTIVE.process_identity}",
    )
    runtime_resource_close = ResourceClosureLedger(
        root / "runtime-close-receipt"
    ).close(runtime_resource_identity, gone)
    assert runtime_stream.start().action == "wait"
    assert runtime_stream.reserve_input().action == "send"
    assert runtime_stream.accept_input().action == "wait"
    assert runtime_stream.process_exited(0).action == "attention"
    runtime_close = runtime_stream.resource_closed_receipt(
        runtime_resource_close.receipt
    )
    runtime_close_event = json.loads(
        (
            root
            / "runtime-close-stream"
            / "generation-1"
            / "events"
            / "0004.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "production stream consumes the exact close receipt as its next event",
        runtime_close.action == "attention"
        and runtime_close_event["kind"] == "resource-closed"
        and runtime_close_event["reason"] == "owned-resources-gone",
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
    for state in ("running", "finalizing"):
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
    missing_result_stream = RuntimeProviderEventStream.create(
        manager._state_root(cleanup_record) / "provider-events",
        owner_id=cleanup_spec.owner_id,
        operation_id=cleanup_spec.operation_id,
        run_id=cleanup_record.run_id,
        generation=1,
        process_identity="f" * 64,
        workspace_id="cleanup-workspace",
        surface_id="cleanup-surface",
        input_sha256="3" * 64,
    )
    assert missing_result_stream.start().action == "wait"
    assert missing_result_stream.reserve_input().action == "send"
    assert missing_result_stream.accept_input().action == "wait"
    manager.request_exit(cleanup_spec.owner_id, cleanup_spec.operation_id)
    assert missing_result_stream.process_exited(0).action == "attention"
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
        "missing provider result cannot publish close or clear exact ownership",
        cleaned.record.resources.surface_id == "cleanup-surface"
        and cleaned.record.state == "attention-required"
        and replayed_cleanup.action == "attention-required"
        and not integrated_receipt.exists(),
    )

    def prepare_modern_cleanup_case(
        case: str,
        *,
        target_generation: int,
    ) -> tuple[
        RuntimeSessionManager,
        OperationSupervisor,
        OperationSpec,
    ]:
        case_store = OperationStore(root / f"{case}-store")
        case_spec = dataclasses.replace(
            cleanup_spec,
            operation_id=f"{case}-operation",
            idempotency_key=f"{case}-key",
        )
        case_store.create(
            case_spec,
            lane_id="cleanup-lane",
            run_id=f"{case}-run",
        )
        case_supervisor = OperationSupervisor(
            case_store,
            case_spec.owner_id,
            case_spec.operation_id,
        )
        for state in ("preflight", "starting"):
            case_supervisor.transition(state)
        case_supervisor.bind_resources(
            OwnedResources(
                f"{case}-surface",
                223,
                224,
                "a" * 64,
                "b" * 64,
            )
        )
        for state in ("running", "awaiting-callback"):
            case_supervisor.transition(state)
        case_manager = RuntimeSessionManager(
            case_store,
            GoneCmux(),
            GoneProcess(),
        )
        case_record = case_supervisor.read()
        case_manager._write_json(
            case_manager._metadata_path(case_record),
            {
                "schema_version": 1,
                "operation_id": case_spec.operation_id,
                "run_id": case_record.run_id,
                "placement": "workspace",
                "workspace_id": f"{case}-workspace",
                "window_id": f"{case}-window",
            },
        )
        case_manager._write_json(
            case_manager._callback_target_path(case_record),
            {
                "schema_version": 1,
                "generation": target_generation,
                "operation_id": case_spec.operation_id,
                "run_id": case_record.run_id,
                "callback_pointer": f"callbacks/{case}.json",
            },
        )
        return case_manager, case_supervisor, case_spec

    def accepted_case_callback(
        case_manager: RuntimeSessionManager,
        case_spec: OperationSpec,
        case: str,
    ) -> str:
        payload = {"status": "complete", "case": case}
        payload_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        case_manager.accept_callback(
            CallbackEnvelope(
                f"{case}-callback",
                case_spec.operation_id,
                f"{case}-run",
                "result",
                payload,
                payload_sha256,
            )
        )
        return payload_sha256

    def case_stream(
        case_manager: RuntimeSessionManager,
        case_spec: OperationSpec,
        case: str,
        *,
        generation: int,
    ) -> RuntimeProviderEventStream:
        case_record = case_manager.store.read(
            case_spec.owner_id,
            case_spec.operation_id,
        )
        stream = RuntimeProviderEventStream.create(
            case_manager._state_root(case_record) / "provider-events",
            owner_id=case_spec.owner_id,
            operation_id=case_spec.operation_id,
            run_id=case_record.run_id,
            generation=generation,
            process_identity="a" * 64,
            workspace_id=f"{case}-workspace",
            surface_id=f"{case}-surface",
            input_sha256="c" * 64,
        )
        assert stream.start().action == "wait"
        assert stream.reserve_input().action == "send"
        assert stream.accept_input().action == "wait"
        return stream

    duplicate_manager, duplicate_supervisor, duplicate_spec = (
        prepare_modern_cleanup_case(
            "duplicate-eligible-root",
            target_generation=2,
        )
    )
    duplicate_sha256 = accepted_case_callback(
        duplicate_manager,
        duplicate_spec,
        "duplicate-eligible-root",
    )
    duplicate_root = case_stream(
        duplicate_manager,
        duplicate_spec,
        "duplicate-eligible-root",
        generation=1,
    )
    duplicate_sibling = case_stream(
        duplicate_manager,
        duplicate_spec,
        "duplicate-eligible-root",
        generation=2,
    )
    assert duplicate_root.result(duplicate_sha256).action == "close"
    assert duplicate_sibling.result(duplicate_sha256).action == "close"
    duplicate_manager.request_exit(
        duplicate_spec.owner_id,
        duplicate_spec.operation_id,
    )
    assert duplicate_sibling.process_exited(0).action == "close"
    duplicate_cleanup = duplicate_manager.cleanup(
        duplicate_spec.owner_id,
        duplicate_spec.operation_id,
    )
    duplicate_receipt = (
        duplicate_manager._state_root(duplicate_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "duplicate eligible root contour latches attention without close authority",
        duplicate_cleanup.record.state == "attention-required"
        and duplicate_cleanup.record.resources.surface_id
        == "duplicate-eligible-root-surface"
        and not duplicate_receipt.exists(),
    )

    malformed_manager, malformed_supervisor, malformed_spec = (
        prepare_modern_cleanup_case(
            "malformed-root-directory",
            target_generation=2,
        )
    )
    malformed_sha256 = accepted_case_callback(
        malformed_manager,
        malformed_spec,
        "malformed-root-directory",
    )
    malformed_stream = case_stream(
        malformed_manager,
        malformed_spec,
        "malformed-root-directory",
        generation=2,
    )
    assert malformed_stream.result(malformed_sha256).action == "close"
    malformed_root = (
        malformed_manager._state_root(malformed_supervisor.read())
        / "provider-events"
        / "generation-1"
    )
    malformed_root.write_text("{}\n", encoding="utf-8")
    malformed_manager.request_exit(
        malformed_spec.owner_id,
        malformed_spec.operation_id,
    )
    assert malformed_stream.process_exited(0).action == "close"
    malformed_cleanup = malformed_manager.cleanup(
        malformed_spec.owner_id,
        malformed_spec.operation_id,
    )
    malformed_receipt = (
        malformed_manager._state_root(malformed_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "malformed root generation directory fails closed without close authority",
        malformed_cleanup.record.state == "attention-required"
        and malformed_cleanup.record.resources.surface_id
        == "malformed-root-directory-surface"
        and not malformed_receipt.exists(),
    )

    symlink_manager, symlink_supervisor, symlink_spec = (
        prepare_modern_cleanup_case(
            "symlinked-root-generation",
            target_generation=2,
        )
    )
    symlink_sha256 = accepted_case_callback(
        symlink_manager,
        symlink_spec,
        "symlinked-root-generation",
    )
    symlink_stream = case_stream(
        symlink_manager,
        symlink_spec,
        "symlinked-root-generation",
        generation=2,
    )
    assert symlink_stream.result(symlink_sha256).action == "close"
    symlink_provider_root = (
        symlink_manager._state_root(symlink_supervisor.read())
        / "provider-events"
    )
    (symlink_provider_root / "generation-1").symlink_to(
        symlink_provider_root / "generation-2"
    )
    symlink_manager.request_exit(
        symlink_spec.owner_id,
        symlink_spec.operation_id,
    )
    assert symlink_stream.process_exited(0).action == "close"
    symlink_cleanup = symlink_manager.cleanup(
        symlink_spec.owner_id,
        symlink_spec.operation_id,
    )
    symlink_receipt = symlink_provider_root / "resource-closed.json"
    check(
        "symlinked root generation fails closed without close authority",
        symlink_cleanup.record.state == "attention-required"
        and symlink_cleanup.record.resources.surface_id
        == "symlinked-root-generation-surface"
        and not symlink_receipt.exists(),
    )

    drifted_manager, drifted_supervisor, drifted_spec = (
        prepare_modern_cleanup_case(
            "root-identity-drift",
            target_generation=2,
        )
    )
    drifted_sha256 = accepted_case_callback(
        drifted_manager,
        drifted_spec,
        "root-identity-drift",
    )
    drifted_record = drifted_manager.store.read(
        drifted_spec.owner_id,
        drifted_spec.operation_id,
    )
    drifted_root = RuntimeProviderEventStream.create(
        drifted_manager._state_root(drifted_record) / "provider-events",
        owner_id=drifted_spec.owner_id,
        operation_id=drifted_spec.operation_id,
        run_id=drifted_record.run_id,
        generation=1,
        process_identity="a" * 64,
        workspace_id="root-identity-drift-workspace",
        surface_id="root-identity-drift-foreign-surface",
        input_sha256="c" * 64,
    )
    assert drifted_root.start().action == "wait"
    assert drifted_root.reserve_input().action == "send"
    assert drifted_root.accept_input().action == "wait"
    drifted_stream = case_stream(
        drifted_manager,
        drifted_spec,
        "root-identity-drift",
        generation=2,
    )
    assert drifted_stream.result(drifted_sha256).action == "close"
    drifted_manager.request_exit(
        drifted_spec.owner_id,
        drifted_spec.operation_id,
    )
    assert drifted_stream.process_exited(0).action == "close"
    drifted_cleanup = drifted_manager.cleanup(
        drifted_spec.owner_id,
        drifted_spec.operation_id,
    )
    drifted_receipt = (
        drifted_manager._state_root(drifted_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "identity-drifted root generation fails closed despite an exact callback stream",
        drifted_cleanup.record.state == "attention-required"
        and drifted_cleanup.record.resources.surface_id
        == "root-identity-drift-surface"
        and not drifted_receipt.exists(),
    )

    authority_manager, authority_supervisor, authority_spec = (
        prepare_modern_cleanup_case(
            "root-generation-authority",
            target_generation=2,
        )
    )
    authority_sha256 = accepted_case_callback(
        authority_manager,
        authority_spec,
        "root-generation-authority",
    )
    authority_root = case_stream(
        authority_manager,
        authority_spec,
        "root-generation-authority",
        generation=1,
    )
    authority_stream = case_stream(
        authority_manager,
        authority_spec,
        "root-generation-authority",
        generation=2,
    )
    assert authority_stream.result(authority_sha256).action == "close"
    authority_manager.request_exit(
        authority_spec.owner_id,
        authority_spec.operation_id,
    )
    assert authority_stream.process_exited(0).action == "close"
    authority_cleanup = authority_manager.cleanup(
        authority_spec.owner_id,
        authority_spec.operation_id,
    )
    authority_receipt = (
        authority_manager._state_root(authority_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    authority_payload = (
        json.loads(authority_receipt.read_text(encoding="utf-8"))
        if authority_receipt.is_file()
        else {}
    )
    authority_replay = authority_manager.cleanup(
        authority_spec.owner_id,
        authority_spec.operation_id,
    )
    check(
        "valid later callback generation completes while closure binds the immutable root",
        authority_cleanup.record.state == "complete"
        and not authority_cleanup.record.resources.surface_id
        and authority_payload.get("identity", {}).get("generation") == 1
        and authority_replay.action == "terminal",
    )

    latched_manager, latched_supervisor, latched_spec = (
        prepare_modern_cleanup_case(
            "root-attention-latch",
            target_generation=2,
        )
    )
    latched_sha256 = accepted_case_callback(
        latched_manager,
        latched_spec,
        "root-attention-latch",
    )
    latched_root = case_stream(
        latched_manager,
        latched_spec,
        "root-attention-latch",
        generation=1,
    )
    assert latched_root.event_gap("test-gap").action == "attention"
    latched_stream = case_stream(
        latched_manager,
        latched_spec,
        "root-attention-latch",
        generation=2,
    )
    assert latched_stream.result(latched_sha256).action == "close"
    latched_manager.request_exit(
        latched_spec.owner_id,
        latched_spec.operation_id,
    )
    assert latched_stream.process_exited(0).action == "close"
    latched_cleanup = latched_manager.cleanup(
        latched_spec.owner_id,
        latched_spec.operation_id,
    )
    latched_receipt = (
        latched_manager._state_root(latched_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    latched_event_kinds = [
        json.loads(path.read_text(encoding="utf-8"))["kind"]
        for path in sorted(
            (
                latched_manager._state_root(latched_cleanup.record)
                / "provider-events"
                / "generation-1"
                / "events"
            ).glob("*.json")
        )
    ]
    check(
        "attention-latched root generation cannot receive a durable close receipt",
        latched_cleanup.record.state == "attention-required"
        and latched_cleanup.record.resources.surface_id
        == "root-attention-latch-surface"
        and not latched_receipt.exists()
        and "resource-closed" not in latched_event_kinds,
    )

    deadline_manager, deadline_supervisor, deadline_spec = (
        prepare_modern_cleanup_case(
            "root-deadline-latch",
            target_generation=2,
        )
    )
    deadline_sha256 = accepted_case_callback(
        deadline_manager,
        deadline_spec,
        "root-deadline-latch",
    )
    deadline_root = case_stream(
        deadline_manager,
        deadline_spec,
        "root-deadline-latch",
        generation=1,
    )
    assert (
        deadline_root.controller.decide(deadline_reached=True).action
        == "attention"
    )
    deadline_stream = case_stream(
        deadline_manager,
        deadline_spec,
        "root-deadline-latch",
        generation=2,
    )
    assert deadline_stream.result(deadline_sha256).action == "close"
    deadline_manager.request_exit(
        deadline_spec.owner_id,
        deadline_spec.operation_id,
    )
    assert deadline_stream.process_exited(0).action == "close"
    deadline_cleanup = deadline_manager.cleanup(
        deadline_spec.owner_id,
        deadline_spec.operation_id,
    )
    deadline_receipt = (
        deadline_manager._state_root(deadline_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "deadline-latched root generation cannot receive a durable close receipt",
        deadline_cleanup.record.state == "attention-required"
        and deadline_cleanup.record.resources.surface_id
        == "root-deadline-latch-surface"
        and not deadline_receipt.exists(),
    )

    escape_manager, escape_supervisor, escape_spec = (
        prepare_modern_cleanup_case(
            "symlinked-provider-root",
            target_generation=1,
        )
    )
    escape_sha256 = accepted_case_callback(
        escape_manager,
        escape_spec,
        "symlinked-provider-root",
    )
    escape_stream = case_stream(
        escape_manager,
        escape_spec,
        "symlinked-provider-root",
        generation=1,
    )
    assert escape_stream.result(escape_sha256).action == "close"
    escape_manager.request_exit(
        escape_spec.owner_id,
        escape_spec.operation_id,
    )
    assert escape_stream.process_exited(0).action == "close"
    escape_state_root = escape_manager._state_root(escape_supervisor.read())
    escape_outside = root / "symlinked-provider-root-outside"
    (escape_state_root / "provider-events").rename(escape_outside)
    (escape_state_root / "provider-events").symlink_to(escape_outside)
    escape_cleanup = escape_manager.cleanup(
        escape_spec.owner_id,
        escape_spec.operation_id,
    )
    check(
        "symlinked provider-events authority fails closed without an outside write",
        escape_cleanup.record.state == "attention-required"
        and escape_cleanup.record.resources.surface_id
        == "symlinked-provider-root-surface"
        and not (escape_outside / "resource-closed.json").exists(),
    )

    delegated_manager, delegated_supervisor, delegated_spec = (
        prepare_modern_cleanup_case(
            "symlinked-delivery-authority",
            target_generation=1,
        )
    )
    delegated_sha256 = accepted_case_callback(
        delegated_manager,
        delegated_spec,
        "symlinked-delivery-authority",
    )
    delegated_stream = case_stream(
        delegated_manager,
        delegated_spec,
        "symlinked-delivery-authority",
        generation=1,
    )
    assert delegated_stream.result(delegated_sha256).action == "close"
    delegated_manager.request_exit(
        delegated_spec.owner_id,
        delegated_spec.operation_id,
    )
    assert delegated_stream.process_exited(0).action == "close"
    delegated_generation_dir = (
        delegated_manager._state_root(delegated_supervisor.read())
        / "provider-events"
        / "generation-1"
    )
    delegated_outside = root / "symlinked-delivery-authority-outside"
    (delegated_generation_dir / "delivery").rename(delegated_outside)
    (delegated_generation_dir / "delivery").symlink_to(delegated_outside)
    delegated_cleanup = delegated_manager.cleanup(
        delegated_spec.owner_id,
        delegated_spec.operation_id,
    )
    delegated_receipt = (
        delegated_manager._state_root(delegated_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "symlinked delivery authority directory fails closed without close authority",
        delegated_cleanup.record.state == "attention-required"
        and delegated_cleanup.record.resources.surface_id
        == "symlinked-delivery-authority-surface"
        and not delegated_receipt.exists(),
    )

    ancestor_manager, ancestor_supervisor, ancestor_spec = (
        prepare_modern_cleanup_case(
            "symlinked-runtime-ancestor",
            target_generation=1,
        )
    )
    ancestor_sha256 = accepted_case_callback(
        ancestor_manager,
        ancestor_spec,
        "symlinked-runtime-ancestor",
    )
    ancestor_stream = case_stream(
        ancestor_manager,
        ancestor_spec,
        "symlinked-runtime-ancestor",
        generation=1,
    )
    assert ancestor_stream.result(ancestor_sha256).action == "close"
    ancestor_manager.request_exit(
        ancestor_spec.owner_id,
        ancestor_spec.operation_id,
    )
    assert ancestor_stream.process_exited(0).action == "close"
    ancestor_runtime_dir = ancestor_manager._state_root(
        ancestor_supervisor.read()
    ).parent
    ancestor_outside = root / "symlinked-runtime-ancestor-outside"
    ancestor_runtime_dir.rename(ancestor_outside)
    ancestor_runtime_dir.symlink_to(ancestor_outside)
    ancestor_cleanup = ancestor_manager.cleanup(
        ancestor_spec.owner_id,
        ancestor_spec.operation_id,
    )
    ancestor_escaped_receipt = (
        ancestor_outside
        / ancestor_spec.operation_id
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "symlinked runtime ancestor fails closed without an outside write",
        ancestor_cleanup.record.state == "attention-required"
        and ancestor_cleanup.record.resources.surface_id
        == "symlinked-runtime-ancestor-surface"
        and not ancestor_escaped_receipt.exists(),
    )

    staterow_manager, staterow_supervisor, staterow_spec = (
        prepare_modern_cleanup_case(
            "symlinked-state-root",
            target_generation=1,
        )
    )
    staterow_sha256 = accepted_case_callback(
        staterow_manager,
        staterow_spec,
        "symlinked-state-root",
    )
    staterow_stream = case_stream(
        staterow_manager,
        staterow_spec,
        "symlinked-state-root",
        generation=1,
    )
    assert staterow_stream.result(staterow_sha256).action == "close"
    staterow_manager.request_exit(
        staterow_spec.owner_id,
        staterow_spec.operation_id,
    )
    assert staterow_stream.process_exited(0).action == "close"
    staterow_state_root = staterow_manager._state_root(
        staterow_supervisor.read()
    )
    staterow_outside = root / "symlinked-state-root-outside"
    staterow_state_root.rename(staterow_outside)
    staterow_state_root.symlink_to(staterow_outside)
    staterow_cleanup = staterow_manager.cleanup(
        staterow_spec.owner_id,
        staterow_spec.operation_id,
    )
    check(
        "symlinked operation state root fails closed without an outside write",
        staterow_cleanup.record.state == "attention-required"
        and staterow_cleanup.record.resources.surface_id
        == "symlinked-state-root-surface"
        and not (
            staterow_outside / "provider-events" / "resource-closed.json"
        ).exists(),
    )

    drift_manager, drift_supervisor, drift_spec = prepare_modern_cleanup_case(
        "root-generation-drift",
        target_generation=2,
    )
    drift_sha256 = accepted_case_callback(
        drift_manager,
        drift_spec,
        "root-generation-drift",
    )
    drift_stream = case_stream(
        drift_manager,
        drift_spec,
        "root-generation-drift",
        generation=2,
    )
    assert drift_stream.result(drift_sha256).action == "close"
    drift_manager.request_exit(drift_spec.owner_id, drift_spec.operation_id)
    assert drift_stream.process_exited(0).action == "close"
    drift_cleanup = drift_manager.cleanup(
        drift_spec.owner_id,
        drift_spec.operation_id,
    )
    drift_receipt = (
        drift_manager._state_root(drift_cleanup.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "missing immutable root generation fails closed despite a live sibling stream",
        drift_cleanup.record.state == "attention-required"
        and drift_cleanup.record.resources.surface_id
        == "root-generation-drift-surface"
        and not drift_receipt.exists(),
    )

    conflict_manager, conflict_supervisor, conflict_spec = (
        prepare_modern_cleanup_case(
            "result-digest-conflict",
            target_generation=1,
        )
    )
    conflict_stream = case_stream(
        conflict_manager,
        conflict_spec,
        "result-digest-conflict",
        generation=1,
    )
    assert conflict_stream.result("d" * 64).action == "close"
    accepted_case_callback(
        conflict_manager,
        conflict_spec,
        "result-digest-conflict",
    )
    conflict_manager.request_exit(
        conflict_spec.owner_id,
        conflict_spec.operation_id,
    )
    assert conflict_stream.process_exited(0).action == "close"
    try:
        conflict_cleanup = conflict_manager.cleanup(
            conflict_spec.owner_id,
            conflict_spec.operation_id,
        )
    except Exception:
        conflict_cleanup = None
    conflict_receipt = (
        conflict_manager._state_root(conflict_supervisor.read())
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "result published before callback with a conflicting digest latches attention",
        conflict_cleanup is not None
        and conflict_cleanup.record.state == "attention-required"
        and conflict_cleanup.record.resources.surface_id
        == "result-digest-conflict-surface"
        and not conflict_receipt.exists(),
    )

    orphan_manager, orphan_supervisor, orphan_spec = (
        prepare_modern_cleanup_case(
            "result-without-callback",
            target_generation=1,
        )
    )
    orphan_stream = case_stream(
        orphan_manager,
        orphan_spec,
        "result-without-callback",
        generation=1,
    )
    assert orphan_stream.result("e" * 64).action == "close"
    orphan_supervisor.transition("finalizing")
    orphan_manager.request_exit(
        orphan_spec.owner_id,
        orphan_spec.operation_id,
    )
    assert orphan_stream.process_exited(0).action == "close"
    orphan_cleanup = orphan_manager.cleanup(
        orphan_spec.owner_id,
        orphan_spec.operation_id,
    )
    check(
        "published result without an accepted callback retains ownership",
        orphan_cleanup.record.state == "attention-required"
        and orphan_cleanup.record.resources.surface_id
        == "result-without-callback-surface",
    )

    receipt_manager, receipt_supervisor, receipt_spec = (
        prepare_modern_cleanup_case(
            "foreign-close-receipt",
            target_generation=1,
        )
    )
    receipt_sha256 = accepted_case_callback(
        receipt_manager,
        receipt_spec,
        "foreign-close-receipt",
    )
    receipt_stream = case_stream(
        receipt_manager,
        receipt_spec,
        "foreign-close-receipt",
        generation=1,
    )
    assert receipt_stream.result(receipt_sha256).action == "close"
    receipt_record = receipt_supervisor.read()
    foreign_identity = ResourceIdentity(
        owner_id=receipt_spec.owner_id,
        operation_id=receipt_spec.operation_id,
        run_id=receipt_record.run_id,
        generation=1,
        provider_session_id=receipt_record.run_id,
        process_identity="a" * 64,
        supervisor_identity="b" * 64,
        source_id=f"process:{'a' * 64}",
        workspace_id="foreign-close-receipt-workspace",
        surface_id="foreign-surface",
    )
    ResourceClosureLedger(
        receipt_manager._state_root(receipt_record) / "provider-events"
    ).close(foreign_identity, gone)
    receipt_manager.request_exit(
        receipt_spec.owner_id,
        receipt_spec.operation_id,
    )
    assert receipt_stream.process_exited(0).action == "close"
    receipt_cleanup = receipt_manager.cleanup(
        receipt_spec.owner_id,
        receipt_spec.operation_id,
    )
    receipt_event_kinds = [
        json.loads(path.read_text(encoding="utf-8"))["kind"]
        for path in sorted(
            (
                receipt_manager._state_root(receipt_record)
                / "provider-events"
                / "generation-1"
                / "events"
            ).glob("*.json")
        )
    ]
    check(
        "foreign durable close receipt latches attention without provider close",
        receipt_cleanup.record.state == "attention-required"
        and receipt_cleanup.record.resources.surface_id
        == "foreign-close-receipt-surface"
        and "resource-closed" not in receipt_event_kinds,
    )

print("delivery and durable close matrix: ok")
