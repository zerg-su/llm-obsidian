#!/usr/bin/env python3
"""Real-cmux, provider-free Enter delivery probe for Claude and Codex shapes."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.cmux import CmuxAdapter  # noqa: E402
from harness.runtime_session_continuation import deliver_continuation  # noqa: E402


UUID_RE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}")


def child(runtime: str, turns: int) -> int:
    marker = "›" if runtime == "codex" else "❯"
    active = (
        "• Working (provider-free cmux probe)"
        if runtime == "codex"
        else "✻ Working…(1s · ↓10 tokens)"
    )
    for turn in range(turns):
        for _line in range(25):
            print("v287-editor-frame", flush=True)
        print(f"V287_READY_{runtime}_{turn:02d}", flush=True)
        print(marker, end=" ", flush=True)
        value = sys.stdin.readline().strip()
        if value != f"V287_{runtime}_{turn:02d}":
            print(f"V287_REJECT_{turn:02d}", flush=True)
            return 7
        print(active, flush=True)
        print(f"V287_ACK_{runtime}_{turn:02d}", flush=True)
        time.sleep(0.15)
    print(f"V287_DONE_{runtime}", flush=True)
    return 0


def run_cmux(*argv: str) -> str:
    result = subprocess.run(
        ("cmux", *argv),
        cwd=ROOT,
        env={**os.environ, "CMUX_QUIET": "1"},
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:1000])
    return result.stdout


def wait_screen(adapter: CmuxAdapter, surface_id: str, token: str) -> str:
    for _observation in range(100):
        screen = adapter.read(surface_id)
        if token in screen:
            return screen
        time.sleep(0.05)
    raise RuntimeError(f"real cmux probe did not observe {token}")


def exact_ids(title: str) -> tuple[str, str]:
    row = next(
        (
            line
            for line in run_cmux("--id-format", "both", "list-workspaces").splitlines()
            if title in line
        ),
        "",
    )
    workspace_ids = UUID_RE.findall(row)
    if len(workspace_ids) != 1:
        raise RuntimeError("real cmux probe workspace identity is ambiguous")
    tree = run_cmux(
        "--id-format", "both", "tree", "--workspace", workspace_ids[0]
    )
    surfaces = [
        match
        for line in tree.splitlines()
        if "surface " in line
        for match in UUID_RE.findall(line)
    ]
    if len(surfaces) != 1:
        raise RuntimeError("real cmux probe surface identity is ambiguous")
    return workspace_ids[0], surfaces[0]


def parent(turns: int) -> int:
    adapter = CmuxAdapter(timeout=8)
    totals = {"codex": 0, "claude": 0}
    for runtime in ("codex", "claude"):
        title = f"llm-obsidian-v287-{runtime}-{uuid.uuid4().hex[:10]}"
        command = (
            f"python3 {Path(__file__).resolve()} --child {runtime} --turns {turns}"
        )
        run_cmux(
            "new-workspace",
            "--name",
            title,
            "--description",
            "bounded-provider-free-v287-delivery-probe",
            "--cwd",
            str(ROOT),
            "--command",
            command,
            "--focus",
            "false",
        )
        workspace_id = ""
        try:
            workspace_id, surface_id = exact_ids(title)
            for turn in range(turns):
                wait_screen(
                    adapter, surface_id, f"V287_READY_{runtime}_{turn:02d}"
                )
                stages: list[tuple[str, int]] = []
                result = deliver_continuation(
                    adapter,
                    surface_id=surface_id,
                    prompt=f"V287_{runtime}_{turn:02d}",
                    runtime=runtime,
                    artifact_ready=lambda: False,
                    ownership_ready=lambda: True,
                    reserve_retry=lambda: False,
                    observe_stage=lambda stage, count, *_digests: stages.append(
                        (stage, count)
                    ),
                    observation_limit=40,
                    observation_interval_seconds=0.05,
                )
                if (
                    not result.acknowledged
                    or result.submit_count != 1
                    or stages.count(("submit-accepted", 1)) != 1
                ):
                    raise RuntimeError(
                        f"{runtime} turn {turn} was not acknowledged exactly once: "
                        f"{result} {stages}"
                    )
                wait_screen(adapter, surface_id, f"V287_ACK_{runtime}_{turn:02d}")
                totals[runtime] += 1
            wait_screen(adapter, surface_id, f"V287_DONE_{runtime}")
        finally:
            if workspace_id:
                run_cmux("close-workspace", "--workspace", workspace_id)
    listing = run_cmux("--id-format", "both", "list-workspaces")
    if "llm-obsidian-v287-" in listing:
        raise RuntimeError("real cmux probe left a workspace tail")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    print(
        "Question: do real cmux Claude/Codex editor transitions submit each "
        "visible prompt exactly once?"
    )
    print(f"Evidence: codex={totals['codex']} claude={totals['claude']} tails=0")
    print("Decision: real-cmux provider-free delivery gate is green")
    print("Limitations: editor emulators were used; no model or provider ran")
    print(f"Provenance: git_head={head}; turns_per_runtime={turns}")
    return 0


parser = argparse.ArgumentParser()
parser.add_argument("--child", choices=("codex", "claude"))
parser.add_argument("--turns", type=int, default=20)
args = parser.parse_args()
if args.turns < 1 or args.turns > 50:
    raise SystemExit("turns must be between 1 and 50")
raise SystemExit(child(args.child, args.turns) if args.child else parent(args.turns))
