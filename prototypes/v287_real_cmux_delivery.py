#!/usr/bin/env python3
"""Real-cmux, provider-free Enter delivery probe for Claude and Codex shapes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.cmux import CmuxAdapter  # noqa: E402
import task_escalation as task_escalation_cli  # noqa: E402


UUID_RE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}")
WORKSPACE_REF_RE = re.compile(r"workspace:[1-9][0-9]*")
BRACKETED_PASTE_START, BRACKETED_PASTE_END = "\x1b[200~", "\x1b[201~"


def child(runtime: str, turns: int) -> int:
    marker = "›" if runtime == "codex" else "❯"
    active = (
        "• Working (provider-free cmux probe)"
        if runtime == "codex"
        else "✻ Working…(1s · ↓10 tokens)"
    )
    print("\x1b[?2004h", end="", flush=True)
    for turn in range(turns):
        for _line in range(25):
            print("v287-editor-frame", flush=True)
        print(f"V287_READY_{runtime}_{turn:02d}", flush=True)
        print(marker, end=" ", flush=True)
        value = sys.stdin.readline().strip()
        if value.startswith(BRACKETED_PASTE_START) and value.endswith(BRACKETED_PASTE_END):
            value = value[len(BRACKETED_PASTE_START) : -len(BRACKETED_PASTE_END)]
        if value != f"V287_{runtime}_{turn:02d}":
            print(f"V287_REJECT_{turn:02d}", flush=True)
            return 7
        print(active, flush=True)
        print(f"V287_ACK_{runtime}_{turn:02d}", flush=True)
        time.sleep(0.15)
    print("\x1b[?2004l", end="", flush=True)
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


def created_workspace_ref(output: str) -> str:
    refs = set(WORKSPACE_REF_RE.findall(output))
    if len(refs) != 1:
        raise RuntimeError("real cmux probe creation identity is ambiguous")
    return refs.pop()


def _tree_ids(payload: object, workspace_ref: str) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("windows"), list):
        return None
    matches = [
        workspace
        for window in payload["windows"]
        if isinstance(window, dict) and isinstance(window.get("workspaces"), list)
        for workspace in window["workspaces"]
        if isinstance(workspace, dict) and workspace.get("ref") == workspace_ref
    ]
    if len(matches) != 1:
        return None
    workspace = matches[0]
    workspace_id = workspace.get("id")
    surfaces = [
        surface.get("id")
        for pane in workspace.get("panes", [])
        if isinstance(pane, dict) and isinstance(pane.get("surfaces"), list)
        for surface in pane["surfaces"]
        if isinstance(surface, dict)
    ]
    if (
        not isinstance(workspace_id, str)
        or not UUID_RE.fullmatch(workspace_id)
        or len(surfaces) != 1
        or not isinstance(surfaces[0], str)
        or not UUID_RE.fullmatch(surfaces[0])
    ):
        return None
    return workspace_id, surfaces[0]


def exact_ids(
    workspace_ref: str,
    *,
    observation_limit: int = 100,
    wait: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    for _observation in range(observation_limit):
        try:
            payload = json.loads(
                run_cmux(
                    "--id-format",
                    "both",
                    "tree",
                    "--workspace",
                    workspace_ref,
                    "--json",
                )
            )
        except json.JSONDecodeError:
            payload = None
        ids = _tree_ids(payload, workspace_ref)
        if ids is not None:
            return ids
        wait(0.05)
    raise RuntimeError("real cmux probe workspace identity is ambiguous")


def workspace_is_absent(title: str, workspace_ref: str) -> bool:
    try:
        payload = json.loads(run_cmux("workspace", "list", "--json"))
    except json.JSONDecodeError:
        return False
    workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
    if not isinstance(workspaces, list):
        return False
    return not any(
        isinstance(item, dict)
        and (item.get("title") == title or item.get("ref") == workspace_ref)
        for item in workspaces
    )


def coordinator_relay(
    surface_id: str,
    runtime: str,
    message: str,
    receipt_path: Path,
    identity: dict[str, object],
) -> str:
    return task_escalation_cli.send(
        surface_id,
        message,
        runtime=runtime,
        receipt_path=receipt_path,
        delivery_identity=identity,
    )


def run_runtime(adapter: CmuxAdapter, runtime: str, turns: int, title: str) -> int:
    command = f"python3 {Path(__file__).resolve()} --child {runtime} --turns {turns}"
    created = run_cmux(
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
    workspace_ref = created_workspace_ref(created)
    workspace_id = ""
    try:
        workspace_id, surface_id = exact_ids(workspace_ref)
        with tempfile.TemporaryDirectory(
            prefix=f"v2810-{runtime}-relay."
        ) as raw_receipts:
            receipt_root = Path(raw_receipts)
            for turn in range(turns):
                wait_screen(adapter, surface_id, f"V287_READY_{runtime}_{turn:02d}")
                receipt_path = receipt_root / f"turn-{turn:02d}.json"
                digest = coordinator_relay(
                    surface_id,
                    runtime,
                    f"V287_{runtime}_{turn:02d}",
                    receipt_path,
                    {
                        "probe": "v2810-real-cmux-coordinator-relay",
                        "runtime": runtime,
                        "turn": turn,
                        "surface_id": surface_id,
                    },
                )
                raw_receipt = receipt_path.read_bytes()
                delivery = json.loads(raw_receipt)
                if (
                    delivery.get("stage") != "submit-accepted"
                    or delivery.get("submit_count") != 1
                    or hashlib.sha256(raw_receipt).hexdigest() != digest
                ):
                    raise RuntimeError(
                        f"{runtime} turn {turn} lacked one durable submit: "
                        f"{delivery}"
                    )
                wait_screen(adapter, surface_id, f"V287_ACK_{runtime}_{turn:02d}")
        wait_screen(adapter, surface_id, f"V287_DONE_{runtime}")
    finally:
        run_cmux(
            "close-workspace",
            "--workspace",
            workspace_id or workspace_ref,
        )
    for _observation in range(100):
        if workspace_is_absent(title, workspace_ref):
            return turns
        time.sleep(0.05)
    raise RuntimeError("real cmux probe left its exact workspace tail")


def probe_identity(root: Path = ROOT) -> tuple[str, str]:
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    if status:
        raise RuntimeError("real cmux probe requires a clean exact-HEAD checkout")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    probe_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return head, probe_sha256


def receipt_payload(
    head: str,
    probe_sha256: str,
    turns: int,
    command: list[str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "complete",
        "gate": "v2810-real-cmux-coordinator-relay",
        "release": "2.8.10",
        "head_sha": head,
        "command": command,
        "working_directory": str(Path.cwd().resolve()),
        "probe_sha256": probe_sha256,
        "python_version": sys.version.split()[0],
        "runtime_counts": {"claude": turns, "codex": turns},
        "delivery_corridor": "task_escalation.send",
        "durable_stage": "submit-accepted",
        "workspace_tails": 0,
        "provider_calls": 0,
        "model_calls": 0,
    }


def write_receipt(path: Path, payload: dict[str, object]) -> None:
    path = path.expanduser().resolve()
    if path.exists() or path.is_symlink():
        raise RuntimeError("real cmux probe receipt target already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parent(
    turns: int,
    receipt: Path | None = None,
    command: list[str] | None = None,
) -> int:
    starting_identity = probe_identity()
    adapter = CmuxAdapter(timeout=8)
    totals = {"codex": 0, "claude": 0}
    for runtime in ("codex", "claude"):
        title = f"llm-obsidian-v287-{runtime}-{uuid.uuid4().hex[:10]}"
        totals[runtime] = run_runtime(adapter, runtime, turns, title)
    ending_identity = probe_identity()
    if ending_identity != starting_identity:
        raise RuntimeError(
            "real cmux probe checkout identity changed during the gate"
        )
    head, probe_sha256 = starting_identity
    creating_command = command or [
        sys.executable,
        str(Path(__file__).resolve()),
        "--turns",
        str(turns),
    ]
    payload = receipt_payload(head, probe_sha256, turns, creating_command)
    if receipt is not None:
        write_receipt(receipt, payload)
    print(
        "Question: does the production coordinator relay submit each real-cmux "
        "Claude/Codex message exactly once through its durable boundary?"
    )
    print(f"Evidence: codex={totals['codex']} claude={totals['claude']} tails=0")
    print("Decision: real-cmux provider-free delivery gate is green")
    print("Limitations: editor emulators were used; no model or provider ran")
    print(f"Provenance: git_head={head}; turns_per_runtime={turns}")
    if receipt is not None:
        print(f"Receipt: {receipt.expanduser().resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    creating_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=("codex", "claude"))
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    if args.turns < 1 or args.turns > 50:
        raise SystemExit("turns must be between 1 and 50")
    if args.child and args.receipt is not None:
        raise SystemExit("--receipt is available only for the parent gate")
    return (
        child(args.child, args.turns)
        if args.child
        else parent(
            args.turns,
            args.receipt,
            [sys.executable, str(Path(__file__).resolve()), *creating_argv],
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
