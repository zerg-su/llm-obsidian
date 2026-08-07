#!/usr/bin/env python3
"""Cheap smoke probe for the finite provider/review transport shapes."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace


def require(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


CMUX_UUID = re.compile(
    r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}"
)


def cmux_command(repo: Path, *argv: str) -> str:
    env = {**os.environ, "CMUX_QUIET": "1"}
    result = subprocess.run(
        ("cmux", *argv),
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"cmux {' '.join(argv)} failed: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def cmux_both(repo: Path, *argv: str) -> str:
    return cmux_command(repo, "--id-format", "both", *argv)


def first_cmux_id(label: str, output: str) -> str:
    ids = CMUX_UUID.findall(output)
    require(label, bool(ids))
    return ids[0] if ids else ""


def cmux_tree(repo: Path, workspace_id: str) -> str:
    return cmux_both(repo, "tree", "--workspace", workspace_id)


def close_surface(repo: Path, workspace_id: str, surface_id: str) -> None:
    cmux_command(
        repo,
        "close-surface",
        "--surface",
        surface_id,
        "--workspace",
        workspace_id,
    )


def live_cmux_probe(repo: Path) -> None:
    title = f"llm-obsidian-transport-smoke-{uuid.uuid4().hex[:12]}"
    created = cmux_command(
        repo,
        "new-workspace",
        "--name",
        title,
        "--description",
        "bounded-create-observe-close-probe",
        "--cwd",
        "/private/tmp",
        "--focus",
        "false",
    )
    match = re.search(r"OK (workspace:\d+)", created)
    require("cmux creates one bounded workspace", match is not None)
    workspace_ref = match.group(1) if match else ""
    workspace_id = ""
    try:
        listing = cmux_both(repo, "list-workspaces")
        row = next((line for line in listing.splitlines() if title in line), "")
        workspace_id = first_cmux_id(
            "cmux exposes the exact created workspace", row
        )
        before = cmux_tree(repo, workspace_id)
        anchors = {
            found
            for line in before.splitlines()
            if "surface " in line
            for found in CMUX_UUID.findall(line)
        }
        require("cmux workspace starts with an observable surface", bool(anchors))
        anchor = sorted(anchors)[0]

        tab = first_cmux_id(
            "cmux returns the new exact surface identity",
            cmux_both(
                repo,
                "new-surface",
                "--type",
                "terminal",
                "--workspace",
                workspace_id,
                "--working-directory",
                "/private/tmp",
                "--focus",
                "false",
            ),
        )
        close_surface(repo, workspace_id, tab)
        after_tab = cmux_tree(repo, workspace_id)
        require(
            "cmux closes only the exact added surface",
            tab not in after_tab and all(item in after_tab for item in anchors),
        )

        right = first_cmux_id(
            "cmux creates an exact right split",
            cmux_both(
                repo,
                "new-split",
                "right",
                "--workspace",
                workspace_id,
                "--surface",
                anchor,
                "--focus",
                "false",
            ),
        )
        left = first_cmux_id(
            "cmux creates an exact left split",
            cmux_both(
                repo,
                "new-split",
                "left",
                "--workspace",
                workspace_id,
                "--surface",
                right,
                "--focus",
                "false",
            ),
        )
        layout = cmux_tree(repo, workspace_id)
        require(
            "cmux binds left and right splits to the requested workspace",
            all(item in layout for item in (anchor, right, left))
            and len(re.findall(r"\bpane pane:\d+", layout)) == 3,
        )

        transport_marker = "CMUX_LONG_COMMAND_VISIBLE_" + "x" * 320
        cmux_command(
            repo,
            "send",
            "--surface",
            right,
            transport_marker,
        )
        screen = cmux_command(repo, "read-screen", "--surface", right)
        require(
            "cmux makes a long worker command observable before submit",
            transport_marker[:96] in " ".join(screen.split()),
        )
        cmux_command(repo, "send-key", "--surface", right, "ctrl+u")

        close_surface(repo, workspace_id, left)
        close_surface(repo, workspace_id, right)
        collapsed = cmux_tree(repo, workspace_id)
        require(
            "cmux removes both split panes without touching the anchor",
            left not in collapsed
            and right not in collapsed
            and anchor in collapsed
            and len(re.findall(r"\bpane pane:\d+", collapsed)) == 1,
        )
    finally:
        if workspace_id or workspace_ref:
            cmux_command(
                repo,
                "close-workspace",
                "--workspace",
                workspace_id or workspace_ref,
            )

    final_listing = cmux_both(repo, "list-workspaces")
    require(
        "cmux leaves no workspace or surface tail",
        title not in final_listing
        and (not workspace_id or workspace_id not in final_listing),
    )


parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument(
    "--live-cmux",
    action="store_true",
    help="create and close one isolated cmux workspace/surface without an LLM",
)
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

if args.live_cmux:
    live_cmux_probe(repo)

print("\ntransport smoke: PASS")
