#!/usr/bin/env python3
"""Research completion belongs to the generic harness runtime worker."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.workflows.research import _fetch_prompt, _synth_prompt


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


runner_source = (ROOT / "scripts/research-isolation.py").read_text(
    encoding="utf-8"
)
worker_source = (ROOT / "scripts/harness/runtime_worker.py").read_text(
    encoding="utf-8"
)

for token in (
    "deliver_watched_callback",
    "write_notifier",
    "CmuxAdapter",
    "TaskSessionStore",
):
    check(
        f"thin research CLI excludes legacy notifier token {token}",
        token not in runner_source,
    )

check(
    "generic runtime worker owns both research callback modes",
    '"research-fetch"' in worker_source
    and '"research-synth"' in worker_source
    and 'kind="research"' in worker_source,
)
check(
    "generic runtime worker persists an idempotent wake marker",
    '"research-notify.json"' in worker_source
    and '"pending"' in worker_source
    and '"sent"' in worker_source,
)

fetch_prompt = _fetch_prompt(
    "research",
    "run-fetch",
    "context/manifest.json",
    "context/question.bin",
    "a" * 64,
    str(Path(sys.executable).resolve()),
)
synth_prompt = _synth_prompt(
    "research",
    "run-synth",
    "context/manifest.json",
    str(Path(sys.executable).resolve()),
)
check(
    "provider prompts delegate completion to the code-owned worker",
    all(
        "code-owned harness worker validates and reports completion" in prompt
        and "terminal controls" in prompt
        and "callback envelope" in prompt
        for prompt in (fetch_prompt, synth_prompt)
    ),
)

print("\nGeneric research notification ownership: OK")
