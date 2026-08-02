#!/usr/bin/env python3
"""Frozen public facade contract for cmux surface lifecycle decomposition."""

from __future__ import annotations

import inspect
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cmux_surface_lifecycle.py"
sys.path.insert(0, str(ROOT / "scripts"))

import cmux_surface_lifecycle as lifecycle  # noqa: E402


EXPORT_SIGNATURES = {
    "lifecycle_file": "(worktree: 'Path', name: 'str', kind: 'str' = 'reviewer') -> 'Path'",
    "reviewer_uses_broker_state": "(worktree: 'Path') -> 'bool'",
    "reviewer_captures_checkpoint": "(worktree: 'Path') -> 'bool'",
    "root_coordinator_reviewer": "(worktree: 'Path', kind: 'str') -> 'bool'",
    "die": "(message: 'str', code: 'int' = 2) -> 'NoReturn'",
    "read_json": "(path: 'Path') -> 'dict[str, Any]'",
    "write_marker": "(path: 'Path', data: 'dict[str, Any]') -> 'None'",
    "utc_now": "() -> 'str'",
    "current_session_id": "() -> 'str'",
    "require_origin_session": "(worktree: 'Path', supplied: 'str' = '') -> 'None'",
    "run": "(args: 'list[str]', cwd: 'Path | None' = None) -> 'subprocess.CompletedProcess[str]'",
    "cmux": "(args: 'list[str]') -> 'subprocess.CompletedProcess[str]'",
    "names": "(kind: 'str') -> 'tuple[str, str, str]'",
    "telemetry_surface_context": "(worktree: 'Path', kind: 'str') -> 'tuple[str, int]'",
    "surface_and_runtime": "(worktree: 'Path', kind: 'str') -> 'tuple[str, str]'",
    "non_handoff_dirty": "(worktree: 'Path') -> 'list[str]'",
    "arm": "(worktree: 'Path', kind: 'str') -> 'tuple[Path, str, str]'",
    "request_exit": "(worktree: 'Path', kind: 'str') -> 'int'",
    "after_exit": "(worktree: 'Path', kind: 'str', surface: 'str') -> 'int'",
    "transition_broker_review": "(worktree: 'Path', status: 'str', *, checkpoint: 'dict[str, str] | None' = None, degradation: 'str' = '') -> 'bool'",
    "start_next_broker_review": "(worktree: 'Path') -> 'None'",
    "validated_review_archive": "(worktree: 'Path', vault: 'Path', state_dir: 'Path | None' = None) -> 'dict[str, Any] | None'",
    "validated_review_archives": "(worktree: 'Path', vault: 'Path', meta: 'dict[str, Any]') -> 'list[dict[str, Any]]'",
    "review_archive_records": "(archives: 'list[dict[str, Any]]') -> 'list[dict[str, str]]'",
    "result_wikilink": "(summary_title: 'str', result_path: 'Path') -> 'str'",
    "collision_safe_result_path": "(vault: 'Path', intended: 'Path') -> 'Path'",
    "reroute_closed_plan": "(text: 'str', old_link: 'str', new_link: 'str', *, label: 'str') -> 'str'",
    "prepared_reap_plan": "(meta: 'dict[str, Any]', text: 'str', *, today: 'str', result_link: 'str', exec_session: 'str | None', label: 'str') -> 'str'",
    "prepare_reap": "(worktree: 'Path', current_session: 'str', result_path: 'Path', vault_root: 'Path') -> 'int'",
    "complete_reap": "(worktree: 'Path', current_session: 'str', result_path: 'Path', vault_root: 'Path') -> 'int'",
    "main": "() -> 'int'",
}

CLI_OPTIONS = {
    "request-exit": {"--help", "--worktree", "--state-dir", "--kind"},
    "after-exit": {"--help", "--worktree", "--state-dir", "--kind", "--surface"},
    "prepare-reap": {"--help", "--worktree", "--current-session", "--result-path", "--vault-root"},
    "complete-reap": {"--help", "--worktree", "--current-session", "--result-path", "--vault-root"},
}


def invoke(*args: str, direct: bool = False) -> subprocess.CompletedProcess[str]:
    command = [str(SCRIPT), *args] if direct else [sys.executable, str(SCRIPT), *args]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


mode = stat.S_IMODE(SCRIPT.stat().st_mode)
assert mode == 0o755, f"facade mode drifted: expected 0755, got {mode:#o}"
assert os.access(SCRIPT, os.X_OK), "facade is not executable"
direct_help = invoke("--help", direct=True)
assert direct_help.returncode == 0, direct_help.stderr

assert len(EXPORT_SIGNATURES) == 31
for name, expected_signature in EXPORT_SIGNATURES.items():
    exported = getattr(lifecycle, name, None)
    assert callable(exported), f"missing callable compatibility export: {name}"
    actual_signature = str(inspect.signature(exported))
    assert actual_signature == expected_signature, (
        f"signature drift for {name}: expected {expected_signature}, got {actual_signature}"
    )

top_help = invoke("--help")
assert top_help.returncode == 0, top_help.stderr
choices = re.search(r"\{([^{}]+)\}", top_help.stdout)
assert choices is not None, top_help.stdout
assert set(choices.group(1).split(",")) == set(CLI_OPTIONS), top_help.stdout

for command, expected_options in CLI_OPTIONS.items():
    help_result = invoke(command, "--help")
    assert help_result.returncode == 0, (command, help_result.stderr)
    actual_options = set(re.findall(r"--[a-z][a-z-]*", help_result.stdout))
    assert actual_options == expected_options, (
        f"CLI option drift for {command}: expected {sorted(expected_options)}, "
        f"got {sorted(actual_options)}"
    )

invalid = invoke("not-a-command")
assert invalid.returncode == 2, invalid.stderr
print("cmux surface lifecycle facade contract passed")
