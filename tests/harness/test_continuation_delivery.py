#!/usr/bin/env python3
"""Ordering matrix for semantic retained-session continuation delivery."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.runtime_session_continuation import (  # noqa: E402
    _editor_digest,
    _screen_digest,
    deliver_continuation,
)


SURFACE = "11111111-1111-1111-1111-111111111111"
PROMPT = "# Harness-owned review verification\nInspect the exact HEAD."


class FakePort:
    def __init__(self, screens: list[str]) -> None:
        self.screens = list(screens)
        self.sent: list[str] = []
        self.keys: list[str] = []

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.screens:
            return ""
        return self.screens.pop(0)

    def send(self, surface_id: str, text: str) -> None:
        assert surface_id == SURFACE
        self.sent.append(text)

    def send_key(self, surface_id: str, key: str) -> None:
        assert surface_id == SURFACE and key == "Enter"
        self.keys.append(key)


def run_case(
    screens: list[str],
    *,
    pre_screen: str = "›",
    artifacts: list[bool] | None = None,
    retry: bool = True,
    send_prompt: bool = True,
    submit_already_accepted: bool = False,
    accepted_submit_count: int = 0,
    pre_send_screen_sha256: str = "",
    pre_send_editor_sha256: str = "",
    paste_screen_sha256: str = "",
    runtime: str = "codex",
    ownership: list[bool] | None = None,
):
    port = FakePort(([pre_screen] if send_prompt else []) + screens)
    artifact_values = list(artifacts or [])
    retries: list[bool] = []
    stages: list[tuple[str, int]] = []
    ownership_values = list(ownership or [])

    def artifact_ready() -> bool:
        return artifact_values.pop(0) if artifact_values else False

    def reserve_retry() -> bool:
        retries.append(retry)
        return retry

    def ownership_ready() -> bool:
        return ownership_values.pop(0) if ownership_values else True

    result = deliver_continuation(
        port,
        surface_id=SURFACE,
        prompt=PROMPT,
        runtime=runtime,
        artifact_ready=artifact_ready,
        ownership_ready=ownership_ready,
        reserve_retry=reserve_retry,
        observe_stage=lambda stage, count, *_digests: stages.append((stage, count)),
        send_prompt=send_prompt,
        submit_already_accepted=submit_already_accepted,
        accepted_submit_count=accepted_submit_count,
        pre_send_screen_sha256=pre_send_screen_sha256,
        pre_send_editor_sha256=pre_send_editor_sha256,
        paste_screen_sha256=paste_screen_sha256,
        observation_limit=2,
        wait=lambda _seconds: None,
    )
    return result, port, retries, stages


result, port, retries, stages = run_case(
    [
        "› # Harness-owned review verification",
        "• Working (1s)",
    ]
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
assert stages == [("paste-reserved", 0), ("transport-accepted", 0), ("submit-accepted", 1)]
print("OK   paste visibility precedes first Enter and activity acknowledges")

crash_port = FakePort(["› previous editor"])
reserved: dict[str, str] = {}


def crash_after_paste(
    stage: str,
    _count: int,
    pre_send_screen_sha256: str,
    pre_send_editor_sha256: str,
    _paste_screen_sha256: str,
) -> None:
    reserved["stage"] = stage
    reserved["screen"] = pre_send_screen_sha256
    reserved["editor"] = pre_send_editor_sha256
    if stage == "transport-accepted":
        raise RuntimeError("kill point after prompt transport")


try:
    deliver_continuation(
        crash_port,
        surface_id=SURFACE,
        prompt=PROMPT,
        runtime="codex",
        artifact_ready=lambda: False,
        ownership_ready=lambda: True,
        reserve_retry=lambda: False,
        observe_stage=crash_after_paste,
        wait=lambda _seconds: None,
    )
except RuntimeError as exc:
    assert str(exc) == "kill point after prompt transport"
else:
    raise AssertionError("kill point did not interrupt continuation")
assert crash_port.sent == [PROMPT] and reserved["stage"] == "transport-accepted"
crash_port.screens = [
    "› # Harness-owned review verification",
    "• Working (recovered turn)",
]
replayed = deliver_continuation(
    crash_port,
    surface_id=SURFACE,
    prompt=PROMPT,
    runtime="codex",
    artifact_ready=lambda: False,
    ownership_ready=lambda: True,
    reserve_retry=lambda: False,
    observe_stage=lambda *_args: None,
    send_prompt=False,
    pre_send_screen_sha256=reserved["screen"],
    pre_send_editor_sha256=reserved["editor"],
    observation_limit=2,
    wait=lambda _seconds: None,
)
assert replayed.acknowledged and replayed.evidence == "provider-activity"
assert crash_port.sent == [PROMPT] and crash_port.keys == ["Enter"]
print("OK   crash after prompt transport replays without a second paste")

result, port, retries, _stages = run_case(
    [
        "› [Pasted Content 3675 chars]",
        "• Working (1s)",
    ]
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
print("OK   Codex collapsed pasted content is recognized as input-ready")

result, port, retries, _stages = run_case(
    [
        "› # Harness-owned review verification",
        "› # Harness-owned review verification",
        "› # Harness-owned review verification",
        "• Working (2s)",
    ]
)
assert result.acknowledged and result.submit_count == 2
assert port.sent == [PROMPT] and port.keys == ["Enter", "Enter"]
assert retries == [True]
print("OK   one identity-bound Enter retry never repeats the prompt")

result, port, retries, _stages = run_case(
    [
        "› # Harness-owned review verification",
        "› # Harness-owned review verification",
        "› # Harness-owned review verification",
    ],
    retry=False,
)
assert not result.acknowledged
assert result.evidence == "submit-retry-budget-unavailable"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and retries == [False]
print("OK   exhausted shared nudge budget fails closed without duplicate input")

result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification"],
    artifacts=[False, False, True],
)
assert result.acknowledged and result.evidence == "artifact"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
print("OK   callback artifact wins the delivery race")

transport_baseline = "› previous editor"
result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification", "• Working"],
    send_prompt=False,
    pre_send_screen_sha256=_screen_digest(transport_baseline),
    pre_send_editor_sha256=_editor_digest("codex", transport_baseline),
)
assert result.acknowledged and port.sent == [] and port.keys == ["Enter"]
print("OK   transport replay submits only after a baseline-bound editor change")

stale_editor = "› # Harness-owned review verification"
result, port, retries, _stages = run_case(
    [stale_editor, "• Working (stale previous turn)"],
    send_prompt=False,
    pre_send_screen_sha256=_screen_digest(stale_editor),
    pre_send_editor_sha256=_editor_digest("codex", stale_editor),
)
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert port.sent == [] and port.keys == [] and not retries
print("OK   transport replay cannot submit a stale same-heading editor")

result, port, retries, stages = run_case(
    [
        "• Working (stale previous turn)",
        "› # Harness-owned review verification",
        "• Working (current turn)",
    ]
)
assert result.acknowledged and result.submit_count == 1
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
assert stages == [("paste-reserved", 0), ("transport-accepted", 0), ("submit-accepted", 1)]
print("OK   stale pre-Enter activity cannot acknowledge the new continuation")

result, port, retries, _stages = run_case(
    ["• Working (stale previous turn)", "• Working (still stale)"],
)
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert port.sent == [PROMPT] and port.keys == [] and not retries
print("OK   stale activity without current input visibility fails closed")

result, port, retries, _stages = run_case(
    [
        "› # Harness-owned review verification",
        "• Working (stale previous turn)",
    ],
    pre_screen="› # Harness-owned review verification",
)
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert port.sent == [PROMPT] and port.keys == [] and not retries
print("OK   stale same-heading editor cannot identify the current paste")

result, port, retries, _stages = run_case(
    ["• Working (submitted continuation)"],
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=1,
    paste_screen_sha256=_screen_digest("› # Harness-owned review verification"),
)
assert result.acknowledged and result.submit_count == 1
assert port.sent == [] and port.keys == [] and not retries
print("OK   durable prior submit may acknowledge activity without another Enter")

result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification"],
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=1,
    paste_screen_sha256=_screen_digest("› # Harness-owned review verification"),
)
assert not result.acknowledged and result.evidence == "submit-effect-uncertain"
assert port.sent == [] and port.keys == [] and not retries
print("OK   accepted submit with visible editor fails closed without replay")

stale_activity = "• Working (stale previous turn)"
result, port, retries, _stages = run_case(
    [stale_activity],
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=1,
    paste_screen_sha256=_screen_digest(stale_activity),
)
assert not result.acknowledged and result.evidence == "submit-effect-uncertain"
assert port.sent == [] and port.keys == [] and not retries
print("OK   submit replay rejects activity identical to its durable baseline")

result, port, retries, _stages = run_case(
    [
        "› # Harness-owned review verification",
        "› # Harness-owned review verification\n• Working (2s)",
    ]
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.keys == ["Enter"] and not retries
print("OK   visible transcript anchor does not hide exact provider activity")

result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification", "›"]
)
assert not result.acknowledged and result.evidence == "idle"
assert port.keys == ["Enter"] and not retries
print("OK   idle repaint cannot acknowledge a continuation")

result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification", "1. Allow\n2. Deny\nEnter"]
)
assert not result.acknowledged and result.evidence == "unknown"
assert port.keys == ["Enter"] and not retries
print("OK   unknown interactive screen fails closed")

result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification"],
    ownership=[True, True, False],
)
assert not result.acknowledged and result.evidence == "ownership-lost"
assert port.keys == [] and not retries
print("OK   ownership is rechecked before Enter")

result, port, retries, _stages = run_case(
    [
        "❯ # Harness-owned review verification",
        "❯ # Harness-owned review verification\n✻ Working…(1s · ↓10 tokens)",
    ],
    runtime="claude",
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.keys == ["Enter"] and not retries
print("OK   Claude activity is classified without dropping the prompt anchor")

print("Continuation delivery matrix passed.")
