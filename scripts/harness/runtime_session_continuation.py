"""Bounded semantic acknowledgement for retained-session continuations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import sleep
from typing import Callable, Protocol

from .prompts import classify


class ContinuationPort(Protocol):
    def read(self, surface_id: str) -> str: ...
    def send(self, surface_id: str, text: str) -> None: ...
    def send_key(self, surface_id: str, key: str) -> None: ...


ArtifactProbe = Callable[[], bool]
OwnershipProbe = Callable[[], bool]
RetryReservation = Callable[[], bool]
StageObserver = Callable[[str, int], None]
Waiter = Callable[[float], None]


@dataclass(frozen=True)
class ContinuationDelivery:
    acknowledged: bool
    evidence: str
    submit_count: int


def _prompt_anchor(prompt: str) -> str:
    for line in prompt.splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            return normalized[:96]
    return ""


def classify_continuation_screen(runtime: str, screen: str, anchor: str) -> str:
    """Classify only provider UI states that are safe for continuation delivery.

    The screen body remains transient.  We deliberately recognize a small set
    of native prompt/activity shapes instead of treating arbitrary repainting
    as provider progress.
    """

    if not screen.strip():
        return "missing"
    prompt = classify(runtime, screen)
    if prompt.interactive:
        return "permission" if prompt.recognized else "unknown"
    lines = [" ".join(line.strip().split()) for line in screen.splitlines()]
    tail = [line for line in lines[-24:] if line]
    marker = {"claude": "❯", "codex": "›"}.get(runtime)
    if not marker:
        return "unknown"
    if runtime == "codex" and any(
        re.match(r"^•\s+(?:Working|Running)\b", line) for line in tail
    ):
        return "active"
    if runtime == "claude" and any(
        ("tokens" in line or "effort" in line)
        and ("…(" in line or "...(" in line)
        for line in tail
    ):
        return "active"
    editor_lines = [line for line in tail if line.startswith(marker)]
    if any(anchor and anchor in line for line in editor_lines):
        return "input-ready"
    if editor_lines:
        return "idle"
    return "unknown"


def deliver_continuation(
    port: ContinuationPort,
    *,
    surface_id: str,
    prompt: str,
    runtime: str,
    artifact_ready: ArtifactProbe,
    ownership_ready: OwnershipProbe,
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
    if not ownership_ready():
        return ContinuationDelivery(False, "ownership-lost", 0)

    if send_prompt:
        port.send(surface_id, prompt)
        observe_stage("transport-accepted", 0)

    paste_screen = ""
    for observation in range(observation_limit):
        if artifact_ready():
            return ContinuationDelivery(True, "artifact", 0)
        if not ownership_ready():
            return ContinuationDelivery(False, "ownership-lost", 0)
        screen = port.read(surface_id)
        screen_state = classify_continuation_screen(runtime, screen, anchor)
        if screen_state == "active":
            return ContinuationDelivery(True, "provider-activity", 0)
        if screen_state == "input-ready":
            paste_screen = screen
            break
        if screen_state in {"idle", "permission", "unknown"}:
            return ContinuationDelivery(False, screen_state, 0)
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    if not paste_screen:
        return ContinuationDelivery(False, "paste-unconfirmed", 0)

    submit_count = 0
    for submit_attempt in range(2):
        if submit_attempt and not reserve_retry():
            return ContinuationDelivery(
                False, "submit-retry-budget-unavailable", submit_count
            )
        if not ownership_ready():
            return ContinuationDelivery(False, "ownership-lost", submit_count)
        port.send_key(surface_id, "Enter")
        submit_count += 1
        observe_stage(
            "submit-retried" if submit_attempt else "submit-accepted",
            submit_count,
        )
        for observation in range(observation_limit):
            if artifact_ready():
                return ContinuationDelivery(True, "artifact", submit_count)
            if not ownership_ready():
                return ContinuationDelivery(
                    False, "ownership-lost", submit_count
                )
            screen = port.read(surface_id)
            screen_state = classify_continuation_screen(runtime, screen, anchor)
            if screen_state == "active":
                return ContinuationDelivery(True, "provider-activity", submit_count)
            if screen_state in {"idle", "permission", "unknown"}:
                return ContinuationDelivery(False, screen_state, submit_count)
            if observation + 1 < observation_limit:
                wait(observation_interval_seconds)

    return ContinuationDelivery(False, "submit-unconfirmed", submit_count)
