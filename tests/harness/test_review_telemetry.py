#!/usr/bin/env python3
"""Review telemetry stays complete, content-free, and non-fatal."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import review_contract  # noqa: E402
import review_telemetry  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="review-telemetry.") as raw:
    root = Path(raw)
    worktree = root / "worktree"
    vault = root / "vault"
    worktree.mkdir()
    (vault / "wiki").mkdir(parents=True)
    common = {
        "worktree": worktree,
        "vault_root": vault,
        "axis": "openai-engineering",
        "reviewer_runtime": "codex",
        "iteration": 2,
        "started_at": "2026-08-01T00:00:00Z",
    }
    check(
        "review severity producers share the executable review vocabulary",
        review_telemetry.SEVERITIES is review_contract.SEVERITIES
        and review_telemetry.severity_counts(review_contract.SEVERITIES)
        == {
            "findings": 3,
            "critical_findings": 1,
            "important_findings": 1,
            "minor_findings": 1,
        },
    )
    check(
        "round start emits",
        review_telemetry.emit_review_event(
            **common,
            event="review-round-start",
            terminal_status="started",
        ),
    )
    check(
        "callback acceptance emits",
        review_telemetry.emit_review_event(
            **common,
            event="review-callback",
            terminal_status="accepted",
        ),
    )
    check(
        "round completion emits severity counts",
        review_telemetry.emit_review_event(
            **common,
            event="review-round-complete",
            terminal_status="changes-requested",
            severities=("critical", "important", "minor"),
        ),
    )
    events = [
        json.loads(line)
        for line in (
            vault / ".vault-meta/pipeline-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    check(
        "review lifecycle carries axis runtime duration iteration and terminal status",
        [event["op"] for event in events]
        == ["review-round-start", "review-callback", "review-round-complete"]
        and all(event["identifiers"]["axis"] == common["axis"] for event in events)
        and all(
            event["identifiers"]["reviewer_runtime"] == "codex"
            for event in events
        )
        and events[0]["counts"]["iteration"] == 2
        and events[1]["counts"]["accepted_callbacks"] == 1
        and events[1]["counts"]["duration_ms"] > 0
        and events[2]["counts"]["critical_findings"] == 1
        and events[2]["identifiers"]["terminal_status"]
        == "changes-requested",
    )
    serialized = json.dumps(events, sort_keys=True)
    check(
        "review telemetry excludes content-bearing fields",
        not any(
            f'"{key}"' in serialized
            for key in (
                "finding_id",
                "summary",
                "evidence",
                "recommendation",
                "prompt",
                "command",
                "snippet",
            )
        ),
    )
    blocked = root / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")
    sentinel = {"review": "unchanged"}
    check(
        "telemetry failure cannot change review outcome",
        not review_telemetry.emit_review_event(
            **{**common, "vault_root": blocked},
            event="review-round-complete",
            terminal_status="approve",
        )
        and sentinel == {"review": "unchanged"},
    )
    check(
        "invalid telemetry input remains non-fatal",
        not review_telemetry.emit_review_event(
            **{**common, "iteration": "not-an-iteration"},
            event="review-round-start",
            terminal_status="started",
        ),
    )

print("\nAll review telemetry tests passed.")
