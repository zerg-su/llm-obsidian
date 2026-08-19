#!/usr/bin/env python3
"""Explicit stale pending-effect retirement remains narrow and fail-closed."""

from __future__ import annotations

import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import harness.cli as harness_cli  # noqa: E402
from harness.contracts import (  # noqa: E402
    AttentionReason,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402


OWNER = "owner-retire-pending"
ROUTE = RuntimeRoute("codex", "gpt-5.6-sol", "low", "executor", "a" * 64)


class Cmux:
    def __init__(self, status: str = "missing", mutate=None) -> None:
        self.value = status
        self.mutate = mutate
        self.calls: list[str] = []

    def status(self, surface_id: str) -> str:
        self.calls.append(surface_id)
        if self.mutate is not None:
            self.mutate()
            self.mutate = None
        return self.value


class NoProcess:
    def __getattr__(self, name: str):
        raise AssertionError(f"process adapter must not be used: {name}")


def operation(
    store: OperationStore,
    operation_id: str,
    effect: str,
    *,
    parent: str = "",
    state: str = "attention-required",
    reason: AttentionReason = AttentionReason.ATTENTION_REQUIRED,
    surface_id: str = "",
) -> None:
    store.create(
        OperationSpec(
            operation_id,
            f"key-{operation_id}",
            "verification" if effect.startswith("pipeline-verify-") else "dispatch",
            OWNER,
            ROUTE,
            "packet.json",
            "scoped",
            parent_operation_id=parent,
        ),
        lane_id=f"lane-{operation_id}",
        run_id=f"run-{operation_id}",
    )
    store.transition(OWNER, operation_id, "preflight")
    store.transition(OWNER, operation_id, "starting")
    if surface_id:
        OperationSupervisor(store, OWNER, operation_id).bind_resources(
            OwnedResources(surface_id=surface_id)
        )
    if state == "verifying":
        store.transition(OWNER, operation_id, "running")
        store.transition(OWNER, operation_id, "verifying")
    store.begin_effect(OWNER, operation_id, effect)
    if state == "attention-required":
        store.transition(OWNER, operation_id, state, reason=reason)
    current = store.read(OWNER, operation_id)
    store.save(replace(current, deadline_at=1.0), expected_revision=current.revision)


def retire(store: OperationStore, operation_id: str, cmux: Cmux) -> object:
    return harness_cli._cancel_or_close_subtree(
        store,
        OWNER,
        operation_id,
        process_adapter=NoProcess(),
        cmux_adapter=cmux,
        bounded_cancel=True,
        retire_pending=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        opened = OperationStore(root / "opened")
        operation(
            opened,
            "open",
            "open-surface",
            reason=AttentionReason.SURFACE_OPEN_FAILED,
        )
        result = retire(opened, "open", Cmux())
        final = opened.read(OWNER, "open")
        assert result.complete and final.state == "cancelled"
        assert final.effect_outcome == EffectOutcome.FAILED
        assert not final.pending_effect and final.resources == OwnedResources()

        provider = OperationStore(root / "provider")
        operation(
            provider,
            "provider",
            "start-provider",
            reason=AttentionReason.PROCESS_START_FAILED,
            surface_id="surface-provider",
        )
        missing = Cmux()
        result = retire(provider, "provider", missing)
        final = provider.read(OWNER, "provider")
        assert result.complete and final.state == "cancelled"
        assert final.effect_outcome == EffectOutcome.SUCCEEDED
        assert final.resources == OwnedResources() and missing.calls == ["surface-provider"]
        revision = final.revision
        assert retire(provider, "provider", missing).complete
        assert provider.read(OWNER, "provider").revision == revision
        assert missing.calls == ["surface-provider"]

        local = OperationStore(root / "local")
        operation(local, "root", "pipeline-verify-root", state="verifying")
        result = retire(local, "root", Cmux())
        final = local.read(OWNER, "root")
        assert result.complete and final.state == "cancelled"
        assert final.effect_outcome == EffectOutcome.SUCCEEDED

        live = OperationStore(root / "live")
        operation(
            live,
            "live",
            "start-provider",
            reason=AttentionReason.PROCESS_START_FAILED,
            surface_id="surface-live",
        )
        result = retire(live, "live", Cmux("alive"))
        assert not result.complete
        assert live.read(OWNER, "live").pending_effect == "start-provider"

        unknown = OperationStore(root / "unknown")
        operation(unknown, "unknown", "publish-release")
        result = retire(unknown, "unknown", Cmux())
        assert not result.complete
        assert unknown.read(OWNER, "unknown").effect_outcome == EffectOutcome.PENDING

        cascade = OperationStore(root / "cascade")
        operation(cascade, "parent", "pipeline-verify-parent", state="verifying")
        operation(
            cascade,
            "child",
            "start-provider",
            parent="parent",
            reason=AttentionReason.PROCESS_START_FAILED,
            surface_id="surface-child",
        )
        result = retire(cascade, "parent", Cmux())
        assert result.complete
        assert cascade.read(OWNER, "child").state == "cancelled"
        assert cascade.read(OWNER, "parent").state == "cancelled"

        public = OperationStore(root / "public")
        operation(public, "public", "pipeline-verify-public", state="verifying")
        output = StringIO()
        with redirect_stdout(output):
            exit_code = harness_cli.main(
                (
                    "--store",
                    str(public.root),
                    "--owner",
                    OWNER,
                    "--json",
                    "cancel-stale",
                    "public",
                ),
                process_adapter=NoProcess(),
                cmux_adapter=Cmux(),
            )
        assert exit_code == 0 and '"state": "cancelled"' in output.getvalue()
        assert public.read(OWNER, "public").state == "cancelled"

        raced = OperationStore(root / "raced")
        operation(
            raced,
            "raced",
            "start-provider",
            reason=AttentionReason.PROCESS_START_FAILED,
            surface_id="surface-raced",
        )
        def mutate() -> None:
            OperationSupervisor(raced, OWNER, "raced").bind_resources(
                OwnedResources(
                    surface_id="surface-raced",
                    process_group=4101,
                    process_identity="b" * 64,
                )
            )
        result = retire(raced, "raced", Cmux(mutate=mutate))
        assert not result.complete
        assert raced.read(OWNER, "raced").pending_effect == "start-provider"

    print("pending-effect retirement regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
