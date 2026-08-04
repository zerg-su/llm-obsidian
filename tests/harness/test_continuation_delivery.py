#!/usr/bin/env python3
"""Ordering matrix for semantic retained-session continuation delivery."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.runtime_session_continuation import deliver_continuation  # noqa: E402


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
    artifacts: list[bool] | None = None,
    retry: bool = True,
    send_prompt: bool = True,
    runtime: str = "codex",
    ownership: list[bool] | None = None,
):
    port = FakePort(screens)
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
        observe_stage=lambda stage, count: stages.append((stage, count)),
        send_prompt=send_prompt,
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
assert stages == [("transport-accepted", 0), ("submit-accepted", 1)]
print("OK   paste visibility precedes first Enter and activity acknowledges")

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

result, port, retries, _stages = run_case(
    ["› # Harness-owned review verification", "• Working"],
    send_prompt=False,
)
assert result.acknowledged and port.sent == [] and port.keys == ["Enter"]
print("OK   retained false-success recovery submits without repasting")

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
