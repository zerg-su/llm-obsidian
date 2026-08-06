#!/usr/bin/env python3
"""Cheap smoke probe for the finite provider/review transport shapes."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace


def require(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
args = parser.parse_args()
repo = args.repo.resolve()
sys.path.insert(0, str(repo / "scripts"))

provider_input = importlib.import_module("harness.runtime_provider_input")
prompts = importlib.import_module("harness.prompts")
review_transport = importlib.import_module("task_review_transport")

body = "# Probe\nReturn exactly TRANSPORT_OK.\n"
pointer = Path("/private/tmp/transport-smoke/task.md")
digest = hashlib.sha256(body.encode()).hexdigest()
codex = provider_input.interactive_provider_input("codex", pointer, body)
require(
    "Codex interactive transport is one digest-bound pointer",
    "\n" not in codex
    and str(pointer) in codex
    and digest in codex
    and body not in codex,
)
require(
    "Claude interactive transport remains verbatim",
    provider_input.interactive_provider_input("claude", pointer, body) == body,
)


class Driver:
    def command(self, *_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("provider", "--fixed-route")


route = SimpleNamespace(runtime="codex")
interactive_request = SimpleNamespace(
    spec=SimpleNamespace(route=route),
    checkpoint="",
    product_root=repo,
    cwd=repo,
    callback_mode="reviewer-callback",
)
argv, deferred = provider_input.initial_provider_argv(
    Driver(), interactive_request, callback_path=pointer, prompt=body
)
require(
    "interactive provider launch keeps prompt out of argv",
    deferred and argv == ("provider", "--fixed-route"),
)
ephemeral_request = SimpleNamespace(
    **{**interactive_request.__dict__, "callback_mode": "research-fetch"}
)
argv, deferred = provider_input.initial_provider_argv(
    Driver(), ephemeral_request, callback_path=pointer, prompt=body
)
require(
    "ephemeral provider launch receives one bounded prompt argument",
    not deferred and argv == ("provider", "--fixed-route", body),
)

update = "\n".join(
    (
        "Update available! 0.146.0 -> 0.146.1",
        "1. Update now",
        "2. Skip",
        "3. Skip until next version",
        "Press enter to continue",
    )
)
decision = prompts.classify("codex", update)
require(
    "Codex update prompt selects current-launch Skip only",
    decision.recognized
    and decision.family == "update-skip-current"
    and decision.keys == ("down", "Enter"),
)

policy = {
    "mode": "simple",
    "cross_model": False,
    "runtime": "codex",
    "model": "sol",
    "effort": "high",
    "purpose": "implementation",
}
meta = {
    "lifecycle": "current-checkout",
    "review_policy": policy,
    "plan_file": "/private/tmp/synthetic-current-review-scope.md",
}
wake = review_transport._callback_wake(meta, repo, repo)
wake_argv = shlex.split(
    wake.removeprefix(
        "Typed current-review callback is ready. Run this exact command: "
    )
)
require(
    "current callback wake is executable without legacy plan",
    wake_argv[2] == "current"
    and "--runtime" in wake_argv
    and "--model" in wake_argv
    and "--plan" not in wake_argv,
)

boundary = "/private/tmp/review-boundary-input.json"
bounded_meta = {
    **meta,
    "review_policy": {**policy, "purpose": "release"},
    "review_boundary_input_file": boundary,
}
bounded_wake = shlex.split(
    review_transport._callback_wake(bounded_meta, repo, repo).removeprefix(
        "Typed current-review callback is ready. Run this exact command: "
    )
)
require(
    "purpose-bound wake preserves purpose and exact boundary",
    bounded_wake[bounded_wake.index("--purpose") + 1] == "release"
    and bounded_wake[bounded_wake.index("--boundary-input") + 1] == boundary
    and "--plan" not in bounded_wake,
)

print("\ntransport smoke: PASS")
