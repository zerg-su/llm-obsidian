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
CheckpointProbe = Callable[[str, str], str]
OwnershipProbe = Callable[[], bool]
RetryReservation = Callable[[], bool]
StageObserver = Callable[[str, int, str, str, str], None]
Waiter = Callable[[float], None]
INITIAL_IDLE_STABILITY_OBSERVATIONS = 10


def _no_artifact() -> bool:
    return False


def _no_checkpoint(surface_id: str, runtime: str) -> str:
    del surface_id, runtime
    return ""


@dataclass(frozen=True)
class ContinuationDelivery:
    acknowledged: bool
    evidence: str
    submit_count: int


def _prompt_anchor(prompt: str) -> str:
    for line in prompt.splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            return normalized[:96].rstrip()
    return ""


def _screen_digest(screen: str) -> str:
    normalized = "\n".join(line.rstrip() for line in screen.splitlines()).strip()
    return sha256(normalized.encode("utf-8")).hexdigest()


def _is_claude_primary_editor_line(line: str) -> bool:
    if line.startswith("❯"):
        return True
    marker_index = line.find("❯")
    prefix = line[:marker_index].strip() if marker_index >= 0 else ""
    return bool(prefix and set(prefix) <= {"─", "━", "═"})


def _editor_state(runtime: str, screen: str) -> tuple[str, ...]:
    marker = {"claude": "❯", "codex": "›"}.get(runtime)
    if not marker:
        return ()
    lines = [" ".join(line.strip().split()) for line in screen.splitlines()]
    editor_lines: list[str] = []
    for line in lines[-24:]:
        if line.startswith(marker):
            editor_lines.append(line)
            continue
        if runtime == "claude" and _is_claude_primary_editor_line(line):
            marker_index = line.find(marker)
            editor_lines.append(line[marker_index:])
    return tuple(editor_lines)


def _editor_digest(runtime: str, screen: str, anchor: str = "") -> str:
    editor = _editor_state(runtime, screen)
    if runtime == "claude" and _claude_alternate_input_state(
        screen, anchor
    ) == "input-ready":
        editor = _claude_alternate_editor(screen)
    return sha256("\n".join(editor).encode("utf-8")).hexdigest()


def _claude_alternate_editor(screen: str) -> tuple[str, ...]:
    """Return only Claude's final contiguous ``›`` composer block."""

    raw_tail = screen.splitlines()[-24:]
    normalized = [" ".join(line.strip().split()) for line in raw_tail]
    starts = [index for index, line in enumerate(normalized) if line.startswith("›")]
    if not starts:
        return ()
    start = starts[-1]
    primary_starts = [
        index
        for index, line in enumerate(normalized)
        if _is_claude_primary_editor_line(line)
    ]
    if primary_starts and primary_starts[-1] > start:
        return ()
    editor = [normalized[start]]
    for raw, line in zip(raw_tail[start + 1 :], normalized[start + 1 :]):
        if not line or not raw[:1].isspace() or line.startswith(("›", "❯")):
            break
        editor.append(line)
    return tuple(editor)


def _claude_alternate_input_state(screen: str, anchor: str) -> str:
    """Classify only Claude's current alternate composer, not retained scrollback."""

    editor = _claude_alternate_editor(screen)
    if not editor:
        return ""
    if re.fullmatch(r"›\s*[1-9][.)]\s+\S.*", editor[0]):
        return "unknown"
    current = " ".join(editor)
    ready = bool(anchor and anchor in current) or any(
        re.fullmatch(
            r"› \[Pasted text #[1-9][0-9]* \+[1-9][0-9]* lines?\]",
            line,
        )
        for line in editor
    )
    return "input-ready" if ready else ""


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
        and re.search(r"(?:…|\.\.\.)\s*\(", line)
        for line in tail
    ):
        return "active"
    editor_lines = [line for line in tail if line.startswith(marker)]
    if runtime == "codex" and any(
        re.fullmatch(r"›\s*[1-9][.)]\s+\S.*", line)
        for line in editor_lines
    ):
        return "unknown"
    if runtime == "codex" and any(
        re.fullmatch(r"› \[Pasted Content [1-9][0-9]* chars\]", line)
        for line in editor_lines
    ):
        return "input-ready"
    if runtime == "claude":
        # Claude's current composer paints ready-idle with ``❯`` but may paint
        # the newly typed input with ``›``.  A wrapped compact pointer must be
        # reassembled before matching; a bare ``›`` or unrelated choice stays
        # fail-closed.  The provider's native collapsed-paste token is also an
        # exact post-send editor shape and carries no submit authority itself.
        alternate_state = _claude_alternate_input_state(screen, anchor)
        if alternate_state:
            return alternate_state
    if any(anchor and anchor in line for line in editor_lines):
        return "input-ready"
    if editor_lines:
        return "idle"
    return "unknown"


def await_surface_transport_ready(
    port: ContinuationPort,
    *,
    surface_id: str,
    observation_limit: int = 600,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> bool:
    """Wait for the fresh terminal to paint before sending its first command."""

    if observation_limit < 1:
        raise ValueError("surface transport observation limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("surface transport interval cannot be negative")
    for observation in range(observation_limit):
        try:
            screen = port.read(surface_id)
        except RuntimeError:
            # A freshly created cmux surface can briefly exist before its
            # terminal transport is readable.  Treat that as an observation,
            # not as proof that the launch failed.
            screen = ""
        lines = [line.rstrip() for line in screen.splitlines() if line.strip()]
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
    idle_observations = 0
    for observation in range(observation_limit):
        screen = port.read(surface_id)
        prompt = classify(runtime, screen)
        if prompt.interactive:
            idle_observations = 0
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
            idle_observations += 1
            if idle_observations >= INITIAL_IDLE_STABILITY_OBSERVATIONS:
                return True
        else:
            idle_observations = 0
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
            if not before_editor_sha256 or (
                _editor_digest(runtime, screen, anchor) != before_editor_sha256
            ):
                return True
        if state == "permission":
            return False
        if (
            before_editor_sha256
            and _editor_state(runtime, screen)
            and _editor_digest(runtime, screen, anchor) != before_editor_sha256
        ):
            return True
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    return False


def await_initial_start_acknowledged(
    port: ContinuationPort,
    *,
    surface_id: str,
    runtime: str,
    anchor: str,
    paste_screen_sha256: str,
    artifact_ready: ArtifactProbe = _no_artifact,
    checkpoint_probe: CheckpointProbe = _no_checkpoint,
    observation_limit: int = 600,
    observation_interval_seconds: float = 0.05,
    wait: Waiter = sleep,
) -> str:
    """Classify whether the provider semantically started after one submit.

    Delivering ``Enter`` to a surface is a transport fact, not evidence that a
    turn began.  A provider held behind a rate-limit window keeps repainting a
    countdown and a spinner while still waiting for its first response, so this
    observes only signals that cannot be produced by repainting alone: a
    recognized provider activity transition away from the exact paste screen, a
    resume checkpoint, or a typed artifact.

    A cleared composer is deliberately *not* a start.  The retained RC4 failure
    cleared it and never began the task.

    Screen bodies are inspected transiently.  The return value is one bounded
    token: ``started``, ``still-composing``, ``permission``, ``unknown``, or
    ``unconfirmed``.  This never sends input.
    """

    if observation_limit < 1:
        raise ValueError("initial start observation limit must be positive")
    if observation_interval_seconds < 0:
        raise ValueError("initial start observation interval cannot be negative")
    last_state = ""
    for observation in range(observation_limit):
        if artifact_ready():
            return "started"
        if checkpoint_probe(surface_id, runtime):
            return "started"
        screen = port.read(surface_id)
        state = classify_continuation_screen(runtime, screen, anchor)
        if state == "permission":
            return "permission"
        if state == "active" and _screen_digest(screen) != paste_screen_sha256:
            return "started"
        last_state = state
        if observation + 1 < observation_limit:
            wait(observation_interval_seconds)
    if last_state == "input-ready":
        return "still-composing"
    if last_state in {"unknown", "missing"}:
        return "unknown"
    return "unconfirmed"


def resolve_recognized_provider_prompt(
    port: ContinuationPort,
    *,
    surface_id: str,
    runtime: str,
) -> str:
    """Confirm one already-allowlisted native prompt without task replay.

    The caller invokes this only after the task submit boundary.  Unknown or
    changed dialogs remain fail-closed, and this helper never sends task text.
    The returned family is a bounded content-free diagnostic token.
    """

    screen = port.read(surface_id)
    prompt = classify(runtime, screen)
    if not prompt.interactive or not prompt.recognized or not prompt.family:
        return ""
    try:
        for key in prompt.keys:
            port.send_key(surface_id, key)
    except Exception:
        return ""
    return prompt.family


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
                or _editor_digest(runtime, screen, anchor) == pre_send_editor_digest
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
        if artifact_ready():
            return ContinuationDelivery(True, "artifact", submit_count)
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
