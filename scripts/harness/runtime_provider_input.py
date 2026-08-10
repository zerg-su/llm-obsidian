"""Provider input composition with one durable interactive send authority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .contracts import EffectOutcome, OperationRecord
from .runtime_provider_events import (
    RuntimeProviderEventError,
    RuntimeProviderEventStream,
)
from .runtime_session_contracts import RuntimeSessionError, continuation_effect_id


def initial_provider_argv(
    driver: object,
    request: object,
    *,
    callback_path: Path,
    prompt: str,
) -> tuple[tuple[str, ...], bool]:
    """Keep interactive input out of argv while ephemeral modes stay one-shot."""

    provider_argv = driver.command(
        request.spec.route,
        resume=request.checkpoint,
        callback_pointer=callback_path,
        product_root=request.product_root,
        session_root=request.cwd,
    )
    deferred = request.callback_mode not in {
        "research-fetch",
        "research-synth",
    }
    return (
        tuple(provider_argv) if deferred else (*provider_argv, prompt),
        deferred,
    )


def interactive_provider_input(
    runtime: str,
    prompt_path: Path,
    prompt: str,
) -> str:
    """Keep supported interactive editors compact without weakening identity."""

    if runtime not in {"claude", "codex"}:
        return prompt
    if not prompt_path.is_absolute():
        raise RuntimeSessionError("interactive prompt pointer must be absolute")
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return (
        "Read and follow the complete task contract at "
        f"`{prompt_path}` (SHA-256 `{digest}`)."
    )


@dataclass(frozen=True)
class RuntimeContinuationInput:
    """Optional typed stream; legacy sessions remain byte-for-byte compatible."""

    stream: RuntimeProviderEventStream | None

    def accepted(self) -> None:
        if self.stream is not None:
            self.stream.accept_input()

    def ambiguous(self) -> None:
        if self.stream is not None:
            self.stream.ambiguous_input()


def bound_continuation_effect_id(
    record: OperationRecord,
    prompt: str,
    target: Mapping[str, object],
) -> str:
    """Preserve historical effect identity only for its exact durable replay."""

    legacy = continuation_effect_id(prompt)
    bound = continuation_effect_id(
        "\n".join(
            (
                prompt,
                str(target["generation"]),
                str(target["operation_id"]),
                str(target["run_id"]),
            )
        )
    )
    return (
        legacy
        if record.effect_id == legacy
        and record.effect_outcome
        in {EffectOutcome.PENDING, EffectOutcome.SUCCEEDED}
        else bound
    )


def reserve_continuation_input(
    provider_root: Path,
    *,
    record: OperationRecord,
    target: Mapping[str, object],
    workspace_id: str,
    prompt: str,
    attention_state: Callable[[], str],
) -> RuntimeContinuationInput:
    """Reserve a new generation before its continuation can reach cmux."""

    typed = any(
        provider_root.glob("generation-*/delivery/delivery-state.json")
    )
    if not typed:
        return RuntimeContinuationInput(None)
    try:
        stream = RuntimeProviderEventStream.create(
            provider_root,
            owner_id=record.spec.owner_id,
            operation_id=record.spec.operation_id,
            run_id=record.run_id,
            generation=int(target["generation"]),
            process_identity=record.resources.process_identity,
            workspace_id=workspace_id,
            surface_id=record.resources.surface_id,
            input_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        )
        stream.start()
        decision = stream.reserve_input()
    except (RuntimeProviderEventError, TypeError, ValueError) as exc:
        state = attention_state()
        raise RuntimeSessionError(
            f"continuation delivery authority requires attention: {state}"
        ) from exc
    if decision.action != "send":
        state = attention_state()
        raise RuntimeSessionError(
            "continuation input is already reserved or accepted without "
            f"an acknowledged receipt: {state}"
        )
    return RuntimeContinuationInput(stream)
