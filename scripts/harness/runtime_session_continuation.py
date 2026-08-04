"""Bounded semantic acknowledgement for retained-session continuations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from time import sleep
from typing import Callable, Protocol


class ContinuationPort(Protocol):
    def read(self, surface_id: str) -> str: ...
    def send(self, surface_id: str, text: str) -> None: ...
    def send_key(self, surface_id: str, key: str) -> None: ...


ArtifactProbe = Callable[[], bool]
RetryReservation = Callable[[], bool]
StageObserver = Callable[[str, int], None]
Waiter = Callable[[float], None]


@dataclass(frozen=True)
class ContinuationDelivery:
    acknowledged: bool
    evidence: str
    submit_count: int


def _digest(screen: str) -> str:
    return hashlib.sha256(screen.encode("utf-8", errors="replace")).hexdigest()


def _prompt_anchor(prompt: str) -> str:
    for line in prompt.splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            return normalized[:96]
    return ""


def _anchor_visible(screen: str, anchor: str) -> bool:
    compact = "\n".join(" ".join(line.strip().split()) for line in screen.splitlines())
    return bool(anchor and anchor in compact)


def deliver_continuation(
    port: ContinuationPort,
    *,
    surface_id: str,
    prompt: str,
    artifact_ready: ArtifactProbe,
    reserve_retry: RetryReservation,
    observe_stage: StageObserver,
    send_prompt: bool = True,
    observation_limit: int = 20,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> ContinuationDelivery:
    """Deliver once and require artifact or visible editor-to-activity progress.

    Screen bodies are inspected transiently only. Callers persist the returned
    content-free evidence and exact continuation identity.
    """

    if observation_limit < 1:
        raise ValueError("continuation observation limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("continuation observation interval cannot be negative")
    anchor = _prompt_anchor(prompt)
    if not anchor:
        raise ValueError("continuation prompt has no visible anchor")

    if artifact_ready():
        return ContinuationDelivery(True, "artifact", 0)

    if send_prompt:
        port.send(surface_id, prompt)
        observe_stage("transport-accepted", 0)

    paste_screen = ""
    for observation in range(observation_limit):
        if artifact_ready():
            return ContinuationDelivery(True, "artifact", 0)
        screen = port.read(surface_id)
        if _anchor_visible(screen, anchor):
            paste_screen = screen
            break
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    if not paste_screen:
        return ContinuationDelivery(False, "paste-unconfirmed", 0)

    paste_digest = _digest(paste_screen)
    submit_count = 0
    for submit_attempt in range(2):
        if submit_attempt and not reserve_retry():
            return ContinuationDelivery(
                False, "submit-retry-budget-unavailable", submit_count
            )
        port.send_key(surface_id, "Enter")
        submit_count += 1
        observe_stage(
            "submit-retried" if submit_attempt else "submit-accepted",
            submit_count,
        )
        for observation in range(observation_limit):
            if artifact_ready():
                return ContinuationDelivery(True, "artifact", submit_count)
            screen = port.read(surface_id)
            if (
                not _anchor_visible(screen, anchor)
                and _digest(screen) != paste_digest
            ):
                return ContinuationDelivery(True, "provider-activity", submit_count)
            if observation + 1 < observation_limit:
                wait(observation_interval_seconds)

    return ContinuationDelivery(False, "submit-unconfirmed", submit_count)
