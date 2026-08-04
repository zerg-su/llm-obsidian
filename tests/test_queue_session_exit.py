#!/usr/bin/env python3
"""Hermetic exact-surface graceful-exit runner checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/queue-session-exit.py"
SURFACE = "00000000-0000-0000-0000-000000000123"


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


TURN_END_MARK = "turn-end"


def stub_turn_end(vault: Path, body: str) -> None:
    """Stand in for the real turn-end pipeline, logging into the cmux trace."""
    (vault / "scripts" / "stop-hook.py").write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.environ['CMUX_TEST_LOG'], 'a', encoding='utf-8') as fh:\n"
        f"    fh.write({TURN_END_MARK!r} + '\\n')\n"
        f"{body}\n",
        encoding="utf-8",
    )


with tempfile.TemporaryDirectory(prefix="queue-session-exit-test.") as raw:
    tmp = Path(raw)
    log = tmp / "cmux.log"
    cmux = tmp / "cmux"
    cmux.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CMUX_TEST_LOG\"\n", encoding="utf-8")
    cmux.chmod(0o755)
    vault = tmp / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "scripts" / "vault-write.py").write_text("# marker\n", encoding="utf-8")
    stub_turn_end(vault, "sys.exit(0)")
    base = dict(
        os.environ,
        CMUX_BUNDLED_CLI_PATH=str(cmux),
        CMUX_SURFACE_ID=SURFACE,
        CMUX_TEST_LOG=str(log),
        LLM_OBSIDIAN_PROJECT_ROOT=str(vault),
        CLAUDE_PROJECT_DIR=str(vault),
    )

    claude_env = {key: value for key, value in base.items() if key not in {"CODEX_THREAD_ID", "CODEX_CI", "CODEX_MANAGED_BY_NPM"}}
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=claude_env, check=False)
    payload = json.loads(result.stdout)
    calls = log.read_text().splitlines()
    check(
        "Claude queues direct exit to exact surface",
        payload["status"] == "queued"
        and calls == [
            TURN_END_MARK,
            f"send-key --surface {SURFACE} ctrl+u",
            f"send --surface {SURFACE} /exit",
            f"send-key --surface {SURFACE} Enter",
        ],
    )
    check(
        "turn-end pipeline runs before the exit is queued",
        calls[0] == TURN_END_MARK and payload["turn_end"]["status"] == "clean",
    )
    check("runner never closes surface", all("close-surface" not in call for call in calls) and payload["surface_closed"] is False)

    log.write_text("", encoding="utf-8")
    codex_env = dict(base, CODEX_THREAD_ID="thread")
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=codex_env, check=False)
    payload = json.loads(result.stdout)
    calls = log.read_text().splitlines()
    check(
        "Codex uses bounded clear and Tab queue",
        len(calls) == 43
        and calls[0] == TURN_END_MARK
        and calls[1:41] == [f"send-key --surface {SURFACE} Backspace"] * 40
        and calls[-2:]
        == [
            f"send --surface {SURFACE} /exit",
            f"send-key --surface {SURFACE} Tab",
        ],
    )
    check("Codex retains manual fallback", payload["manual_fallback"] is True)

    missing_env = dict(claude_env)
    missing_env.pop("CMUX_SURFACE_ID")
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=missing_env, check=False)
    check("missing exact surface degrades without guessing", json.loads(result.stdout)["status"] == "manual")

    log.write_text("", encoding="utf-8")
    stub_turn_end(
        vault,
        "print('VAULT_LINT_FAIL: questions: 1 page(s) without status open|answered')\n"
        "print('COMMIT_BLOCKED: strict vault validation failed')\n"
        "sys.exit(0)",
    )
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=claude_env, check=False)
    payload = json.loads(result.stdout)
    check(
        "blocked turn-end keeps the session alive",
        payload["status"] == "blocked"
        and payload["turn_end"]["status"] == "blocked"
        and "COMMIT_BLOCKED" in payload["turn_end"]["reason"]
        and "VAULT_LINT_FAIL" in payload["turn_end"]["output"],
    )
    check("blocked turn-end queues no exit", log.read_text().splitlines() == [TURN_END_MARK])

    for marker in ("AUTO_COMMIT_DISABLED", "TASK_SPLIT_STOP_SKIPPED"):
        log.write_text("", encoding="utf-8")
        stub_turn_end(vault, f"print('{marker}: commit skipped on purpose')\nsys.exit(0)")
        result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=claude_env, check=False)
        payload = json.loads(result.stdout)
        check(
            f"{marker} blocks instead of exiting on an uncommitted vault",
            payload["status"] == "blocked"
            and payload["turn_end"]["status"] == "blocked"
            and marker in payload["turn_end"]["reason"],
        )
        check(f"{marker} sends no cmux command", log.read_text().splitlines() == [TURN_END_MARK])

    log.write_text("", encoding="utf-8")
    stub_turn_end(vault, "sys.exit(3)")
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=claude_env, check=False)
    payload = json.loads(result.stdout)
    check(
        "failing turn-end blocks exit without a marker line",
        payload["status"] == "blocked" and "exited 3" in payload["turn_end"]["reason"],
    )

    log.write_text("", encoding="utf-8")
    detached_env = dict(claude_env, LLM_OBSIDIAN_PROJECT_ROOT=str(tmp / "absent"), CLAUDE_PROJECT_DIR=str(tmp / "absent"))
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], text=True, capture_output=True, env=detached_env, cwd=tmp, check=False
    )
    payload = json.loads(result.stdout)
    check(
        "no vault still queues a graceful exit",
        payload["status"] == "queued" and payload["turn_end"]["status"] == "skipped",
    )

print("All session-exit runner tests passed.")
