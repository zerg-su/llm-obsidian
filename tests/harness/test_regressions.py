#!/usr/bin/env python3
"""Validate sanitized lifecycle regression replay fixtures."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
EXPECTED = {
    "callback-delivered-twice",
    "claude-trust-footer-drift",
    "close-before-process-exit",
    "codex-trust-footer-drift",
    "duplicate-review-surfaces",
    "interrupted-write-ahead-transition",
    "process-exited-surface-remains",
    "recycled-process-group",
    "retry-uncertain-spawn",
    "same-session-verify-second-surface",
    "surface-vanished-process-remains",
    "unknown-choice-prompt",
    "upgrade-active-operation",
}
BEHAVIORAL_OWNERS = {
    "callback-delivered-twice": (
        "tests/harness/test_callbacks.py",
        "duplicate callback is an idempotent no-op",
    ),
    "claude-trust-footer-drift": (
        "tests/harness/test_callbacks.py",
        "near-match receives no input",
    ),
    "close-before-process-exit": (
        "tests/harness/test_runtime_sessions.py",
        "exit requests the exact PGID before surface close",
    ),
    "codex-trust-footer-drift": (
        "tests/harness/test_release_blocker_runtime.py",
        "clipped Codex trust footer rejects near-match",
    ),
    "duplicate-review-surfaces": (
        "tests/harness/test_review_vertical.py",
        "verification reuses exact axis lane and surface",
    ),
    "interrupted-write-ahead-transition": (
        "tests/harness/test_store.py",
        "CLI resume contains an unresolved effect as attention-required",
    ),
    "process-exited-surface-remains": (
        "tests/harness/test_callbacks.py",
        "dead process closes exact surface",
    ),
    "recycled-process-group": (
        "tests/harness/test_adapters.py",
        "recycled process group receives no mutating signal",
    ),
    "retry-uncertain-spawn": (
        "tests/harness/test_workflows.py",
        "uncertain dispatch effect reconciles instead of spawning again",
    ),
    "same-session-verify-second-surface": (
        "tests/harness/test_review_vertical.py",
        "verification reuses exact axis lane and surface",
    ),
    "surface-vanished-process-remains": (
        "tests/harness/test_callbacks.py",
        "orphan process without guardian fails closed",
    ),
    "unknown-choice-prompt": (
        "tests/harness/test_callbacks.py",
        "near-match receives no input",
    ),
    "upgrade-active-operation": (
        "tests/test_upgrade_preflight.py",
        "every active harness operation kind blocks upgrade",
    ),
}

seen: set[str] = set()
legacy_red = json.loads(
    (FIXTURES / "legacy-red.json").read_text(encoding="utf-8")
)
assert set(legacy_red) == EXPECTED
assert all(
    isinstance(reason, str) and 20 <= len(reason) <= 120
    for reason in legacy_red.values()
)
for path in sorted(FIXTURES.glob("*.json")):
    if path.name == "legacy-red.json":
        continue
    value = json.loads(path.read_text(encoding="utf-8"))
    assert set(value) == {"case", "initial", "event", "expected"}
    assert isinstance(value["initial"], dict) and isinstance(value["expected"], dict)
    assert len(path.read_bytes()) <= 2048
    assert "/" not in value["case"] and "\\" not in value["case"]
    case = value["case"]
    owner_path, marker = BEHAVIORAL_OWNERS[case]
    owner = ROOT / owner_path
    assert owner.is_file() and marker in owner.read_text(encoding="utf-8")
    assert legacy_red[case]
    seen.add(case)
    print(f"OK   RED {case} -> GREEN {owner_path}")
assert seen == EXPECTED, (seen, EXPECTED)
assert set(BEHAVIORAL_OWNERS) == EXPECTED
