#!/usr/bin/env python3
"""Validate one task summary and send a deterministic reap callback."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn


CMUX_PASTE_SETTLE_SECONDS = 0.2


class SendError(ValueError):
    pass


def die(message: str) -> NoReturn:
    print(f"send-reap: {message}", file=sys.stderr)
    raise SystemExit(3)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SendError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SendError(f"JSON root must be an object: {path}")
    return value


def read_text(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SendError(f"cannot read {label}: {exc}") from exc
    if not value:
        raise SendError(f"{label} is empty")
    return value


def callback(worktree: Path) -> dict[str, str]:
    meta = read_json(worktree / ".task-meta.json")
    summary = read_json(worktree / ".task-summary.json")
    vault = Path(str(meta.get("vault_root") or "")).expanduser().resolve()
    if not (vault / "scripts" / "reap-runner.py").is_file():
        raise SendError("task metadata does not identify a coordinator reap runner")
    sys.path.insert(0, str(vault / "scripts"))
    from task_contract import ContractError, normalize  # type: ignore
    from wiki_summary_contract import WikiSummaryError, validate_summary  # type: ignore

    try:
        normalize(meta)
        typed = validate_summary(summary, allow_missing_session=False, require_schema=True)
    except (ContractError, WikiSummaryError) as exc:
        raise SendError(str(exc)) from exc
    if meta.get("version") == 3 and meta.get("interaction_policy") == "unattended":
        reap = meta.get("reap_policy")
        if not isinstance(reap, dict):
            raise SendError("unattended task is missing reap_policy")
        if typed["type"] not in reap.get("allowed_types", []) or typed["title"] != reap.get("title"):
            raise SendError("typed summary drifted from the approved reap type/title")
        command = shlex.join([
            sys.executable,
            str(vault / "scripts" / "reap-runner.py"),
            "--vault-root", str(vault),
            "--worktree", str(worktree),
        ])
        message = (
            "Typed final reap callback for an approved unattended task. "
            "Run this exact command now without re-reading task artifacts or reproducing reap phases manually: "
            f"{command}"
        )
        mode = "runner"
    else:
        task_name = str(meta.get("task_name") or "").strip()
        if not task_name:
            raise SendError("legacy task metadata is missing task_name")
        base = read_text(worktree / ".wiki-reap-command", "wiki reap command")
        message = f"{base} {task_name}"
        command = message
        mode = "compatibility"
    surface = read_text(worktree / ".wiki-cmux-surface", "wiki surface")
    if surface != str(meta.get("wiki_surface") or ""):
        raise SendError("wiki surface handoff drifted from task metadata")
    return {"surface": surface, "message": message, "command": command, "mode": mode}


def send(value: dict[str, str]) -> None:
    commands = (
        (["cmux", "send", "--surface", value["surface"], value["message"]], "callback send"),
        (["cmux", "send-key", "--surface", value["surface"], "Enter"], "callback submit"),
    )
    for index, (argv, label) in enumerate(commands):
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SendError(f"{label} failed" + (f": {detail[:300]}" if detail else ""))
        if index == 0:
            time.sleep(CMUX_PASTE_SETTLE_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        value = callback(args.worktree.expanduser().resolve())
        if not args.dry_run:
            send(value)
        print(json.dumps({"schema_version": 1, "status": "validated" if args.dry_run else "sent", **value}, sort_keys=True))
        return 0
    except (SendError, OSError, ValueError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
