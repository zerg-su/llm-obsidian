"""Argument and output adapters for the public Harness CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="harness")
    result.add_argument("--store", type=Path, default=Path(".vault-meta/harness"))
    result.add_argument("--owner", default="local")
    result.add_argument("--json", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("operation_id")
    resume = commands.add_parser("resume")
    resume.add_argument("operation_id")
    commands.add_parser("reconcile")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("operation_id")
    cancel_stale = commands.add_parser("cancel-stale")
    cancel_stale.add_argument("operation_id")
    close = commands.add_parser("close")
    close.add_argument("operation_id")
    commands.add_parser("doctor")
    commands.add_parser("diagnose")
    commands.add_parser("dashboard")
    return result


def emit(value: object, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, list):
        for row in value:
            detail = row.get("kind", row.get("action", ""))
            print(f"{row['operation_id']}\t{row['state']}\t{detail}")
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)
