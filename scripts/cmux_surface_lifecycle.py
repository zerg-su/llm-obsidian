#!/usr/bin/env python3
"""Arm agent exit and close only the exact cmux surface after process return."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event, nonnegative_int, read_object
from plan_lifecycle import PlanCloseError, render_plan_close
from task_contract import (
    ContractError,
    normalize,
    normalize_for_runtime,
    read_json as read_contract_json,
    validate_handoff,
    v3_session_is_bound,
)
from task_sessions import (
    TaskSessionError,
    TaskSessionStore,
    capture_resume,
    close_surface_exact,
    validate_checkpoint,
)
from cmux_workspace_lifecycle import close_task_container
from harness.adapters.cmux import run_cmux

# Compatibility re-exports: callers and tests keep the executable module as
# their durable seam while policy lives in the two independent deep modules.
from cmux_surface_exit import (
    HANDOFF_PREFIXES,
    SCRIPT_DIR,
    after_exit,
    arm,
    cmux,
    names,
    non_handoff_dirty,
    request_exit,
    run,
    start_next_broker_review,
    surface_and_runtime,
    telemetry_surface_context,
    transition_broker_review,
)
from task_lifecycle_state import (
    _STATE_DIR,
    current_session_id,
    die,
    lifecycle_file,
    read_json,
    require_origin_session,
    reviewer_captures_checkpoint,
    reviewer_uses_broker_state,
    root_coordinator_reviewer,
    set_state_dir,
    utc_now,
    write_marker,
)
from task_reap_lifecycle import (
    collision_safe_result_path,
    complete_reap,
    prepare_reap,
    prepared_reap_plan,
    reroute_closed_plan,
    result_wikilink,
    review_archive_records,
    validated_review_archive,
    validated_review_archives,
)


def main() -> int:
    global _STATE_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request-exit")
    request.add_argument("--worktree", default=".")
    request.add_argument("--state-dir", default="")
    request.add_argument("--kind", choices=["reviewer", "task"], required=True)
    after = sub.add_parser("after-exit")
    after.add_argument("--worktree", default=".")
    after.add_argument("--state-dir", default="")
    after.add_argument("--kind", choices=["reviewer", "task"], required=True)
    after.add_argument("--surface", required=True)
    complete = sub.add_parser("complete-reap")
    complete.add_argument("--worktree", default=".")
    complete.add_argument("--current-session", required=True)
    complete.add_argument("--result-path", required=True)
    complete.add_argument("--vault-root", default=str(Path(__file__).resolve().parents[1]))
    prepare = sub.add_parser("prepare-reap")
    prepare.add_argument("--worktree", default=".")
    prepare.add_argument("--current-session", required=True)
    prepare.add_argument("--result-path", required=True)
    prepare.add_argument("--vault-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    raw_state = str(getattr(args, "state_dir", "") or "").strip()
    _STATE_DIR = Path(raw_state).expanduser().resolve() if raw_state else None
    set_state_dir(_STATE_DIR)
    if args.command == "request-exit":
        return request_exit(worktree, args.kind)
    if args.command == "after-exit":
        return after_exit(worktree, args.kind, args.surface)
    if args.command == "prepare-reap":
        return prepare_reap(worktree, args.current_session, Path(args.result_path), Path(args.vault_root))
    return complete_reap(worktree, args.current_session, Path(args.result_path), Path(args.vault_root))


if __name__ == "__main__":
    raise SystemExit(main())
