"""Bounded semantic acknowledgement for retained-session continuations."""

from __future__ import annotations

import re
from hashlib import sha256
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
StageObserver = Callable[[str, int, str, str, str], None]
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


def _screen_digest(screen: str) -> str:
    normalized = "\n".join(line.rstrip() for line in screen.splitlines()).strip()
    return sha256(normalized.encode("utf-8")).hexdigest()


def _editor_state(runtime: str, screen: str) -> tuple[str, ...]:
    marker = {"claude": "❯", "codex": "›"}.get(runtime)
    if not marker:
        return ()
    lines = [" ".join(line.strip().split()) for line in screen.splitlines()]
    return tuple(line for line in lines[-24:] if line.startswith(marker))


def _editor_digest(runtime: str, screen: str) -> str:
    return sha256("\n".join(_editor_state(runtime, screen)).encode("utf-8")).hexdigest()


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
    if runtime == "codex" and any(
        re.fullmatch(r"› \[Pasted Content [1-9][0-9]* chars\]", line)
        for line in editor_lines
    ):
        return "input-ready"
    if any(anchor and anchor in line for line in editor_lines):
        return "input-ready"
    if editor_lines:
        return "idle"
    return "unknown"


def await_surface_transport_ready(
    port: ContinuationPort,
    *,
    surface_id: str,
    observation_limit: int = 100,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> bool:
    """Wait for the fresh terminal to paint before sending its first command."""

    if observation_limit < 1:
        raise ValueError("surface transport observation limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("surface transport interval cannot be negative")
    for observation in range(observation_limit):
        lines = [
            line.rstrip()
            for line in port.read(surface_id).splitlines()
            if line.strip()
        ]
        if lines and re.search(r"(?:[%$#>]|❯|›)\s*$", lines[-1]):
            return True
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    return False


def await_surface_transport_visible(
    port: ContinuationPort,
    *,
    surface_id: str,
    text: str,
    observation_limit: int = 40,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> bool:
    """Confirm one command is visible before submitting it to the shell."""

    anchor = _prompt_anchor(text)
    if not anchor:
        raise ValueError("surface transport text has no visible anchor")
    if observation_limit < 1:
        raise ValueError("surface paste observation limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("surface paste interval cannot be negative")
    for observation in range(observation_limit):
        screen = " ".join(port.read(surface_id).split())
        if anchor in screen:
            return True
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    return False


def await_initial_input_ready(
    port: ContinuationPort,
    *,
    surface_id: str,
    runtime: str,
    observation_limit: int = 300,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> bool:
    """Wait until the native provider editor can safely receive first input."""

    if observation_limit < 1:
        raise ValueError("initial input observation limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("initial input observation interval cannot be negative")
    handled_prompt_digest = ""
    for observation in range(observation_limit):
        screen = port.read(surface_id)
        prompt = classify(runtime, screen)
        if prompt.interactive:
            if not prompt.recognized:
                if observation + 1 < observation_limit:
                    wait(observation_interval_seconds)
                continue
            digest = _screen_digest(screen)
            if digest != handled_prompt_digest:
                try:
                    for key in prompt.keys:
                        port.send_key(surface_id, key)
                except Exception:
                    return False
                handled_prompt_digest = digest
            if observation + 1 < observation_limit:
                wait(observation_interval_seconds)
            continue
        state = classify_continuation_screen(runtime, screen, "")
        if state == "idle":
            return True
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    return False


def await_initial_input_visible(
    port: ContinuationPort,
    *,
    surface_id: str,
    runtime: str,
    text: str,
    before_editor_sha256: str = "",
    observation_limit: int = 40,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> bool:
    """Confirm the first provider input is visible before submitting it."""

    anchor = _prompt_anchor(text)
    if not anchor:
        raise ValueError("initial provider input has no visible anchor")
    if observation_limit < 1:
        raise ValueError("initial input visibility limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("initial input visibility interval cannot be negative")
    if before_editor_sha256 and not re.fullmatch(
        r"[0-9a-f]{64}", before_editor_sha256
    ):
        raise ValueError("initial editor digest is invalid")
    for observation in range(observation_limit):
        screen = port.read(surface_id)
        state = classify_continuation_screen(
            runtime, screen, anchor
        )
        if state == "input-ready":
            return True
        if state == "permission":
            return False
        if (
            before_editor_sha256
            and _editor_state(runtime, screen)
            and _editor_digest(runtime, screen) != before_editor_sha256
        ):
            return True
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    return False


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
    submit_already_accepted: bool = False,
    accepted_submit_count: int = 0,
    pre_send_screen_sha256: str = "",
    pre_send_editor_sha256: str = "",
    paste_screen_sha256: str = "",
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
    if accepted_submit_count < 0:
        raise ValueError("accepted submit count cannot be negative")
    if submit_already_accepted and accepted_submit_count < 1:
        raise ValueError("accepted submit requires a positive submit count")
    anchor = _prompt_anchor(prompt)
    if not anchor:
        raise ValueError("continuation prompt has no visible anchor")

    if artifact_ready():
        return ContinuationDelivery(True, "artifact", accepted_submit_count)
    if not ownership_ready():
        return ContinuationDelivery(False, "ownership-lost", 0)

    pre_send_digest = pre_send_screen_sha256
    pre_send_editor_digest = pre_send_editor_sha256
    if send_prompt:
        pre_send_screen = port.read(surface_id)
        pre_send_digest = _screen_digest(pre_send_screen)
        pre_send_editor_digest = _editor_digest(runtime, pre_send_screen)
        observe_stage(
            "paste-reserved", 0, pre_send_digest, pre_send_editor_digest, ""
        )
        port.send(surface_id, prompt)
        observe_stage(
            "transport-accepted", 0, pre_send_digest, pre_send_editor_digest, ""
        )
    elif not submit_already_accepted and (
        re.fullmatch(r"[0-9a-f]{64}", pre_send_digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", pre_send_editor_digest) is None
    ):
        return ContinuationDelivery(False, "replay-baseline-unavailable", 0)
    elif submit_already_accepted and re.fullmatch(
        r"[0-9a-f]{64}", paste_screen_sha256
    ) is None:
        return ContinuationDelivery(
            False, "submit-effect-uncertain", accepted_submit_count
        )

    paste_screen = ""
    paste_digest = ""
    for observation in range(observation_limit):
        if artifact_ready():
            return ContinuationDelivery(True, "artifact", accepted_submit_count)
        if not ownership_ready():
            return ContinuationDelivery(False, "ownership-lost", 0)
        screen = port.read(surface_id)
        screen_state = classify_continuation_screen(runtime, screen, anchor)
        if screen_state == "active" and submit_already_accepted:
            if _screen_digest(screen) == paste_screen_sha256:
                return ContinuationDelivery(
                    False, "submit-effect-uncertain", accepted_submit_count
                )
            return ContinuationDelivery(
                True, "provider-activity", accepted_submit_count
            )
        if screen_state == "input-ready":
            if submit_already_accepted:
                return ContinuationDelivery(
                    False, "submit-effect-uncertain", accepted_submit_count
                )
            if (
                _screen_digest(screen) == pre_send_digest
                or _editor_digest(runtime, screen) == pre_send_editor_digest
            ):
                if observation + 1 < observation_limit:
                    wait(observation_interval_seconds)
                continue
            paste_screen = screen
            paste_digest = _screen_digest(screen)
            break
        if screen_state in {"idle", "permission", "unknown"}:
            return ContinuationDelivery(False, screen_state, 0)
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    if not paste_screen:
        return ContinuationDelivery(False, "paste-unconfirmed", 0)

    submit_count = accepted_submit_count
    for submit_attempt in range(2):
        if submit_attempt and not reserve_retry():
            return ContinuationDelivery(
                False, "submit-retry-budget-unavailable", submit_count
            )
        if not ownership_ready():
            return ContinuationDelivery(False, "ownership-lost", submit_count)
        next_submit_count = submit_count + 1
        observe_stage(
            "submit-retry-reserved" if submit_attempt else "submit-reserved",
            next_submit_count,
            pre_send_digest,
            pre_send_editor_digest,
            paste_digest,
        )
        port.send_key(surface_id, "Enter")
        submit_count = next_submit_count
        observe_stage(
            "submit-retried" if submit_attempt else "submit-accepted",
            submit_count,
            pre_send_digest,
            pre_send_editor_digest,
            paste_digest,
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
            if screen_state == "active" and _screen_digest(screen) != paste_digest:
                return ContinuationDelivery(True, "provider-activity", submit_count)
            if screen_state in {"idle", "permission", "unknown", "missing"}:
                evidence = (
                    "submit-unconfirmed"
                    if screen_state == "missing"
                    else screen_state
                )
                return ContinuationDelivery(False, evidence, submit_count)
            if observation + 1 < observation_limit:
                wait(observation_interval_seconds)

    return ContinuationDelivery(False, "submit-unconfirmed", submit_count)
