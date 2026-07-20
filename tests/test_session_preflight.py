#!/usr/bin/env python3
"""Hermetic checks for the non-blocking SessionStart preflight."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/session-preflight.py"


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


with tempfile.TemporaryDirectory(prefix="session-preflight-test.") as raw:
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), "--session-id", "test-session", "--runtime", "codex", "--model", "gpt-5.6-sol", "--effort", "high", "--json"],
        text=True, capture_output=True, env=env, check=False,
    )
    check("preflight never blocks session", result.returncode == 0)
    payload = json.loads(result.stdout)
    check("selected interpreter is visible", payload["interpreter"]["executable"] == sys.executable)
    check("effective route is visible", payload["routing"]["model"] == "gpt-5.6-sol" and payload["routing"]["source"] == "runtime-environment")
    check("retrieval degradation is explicit", payload["retrieval"] in {"hybrid", "sparse-fallback"})
    check("repairs are exact commands", all(item["repair"] for item in payload["issues"]))
    snapshot = ROOT / ".vault-meta/session-routing/test-session.json"
    check("session route snapshot created", snapshot.is_file())
    snapshot.unlink()

    guessed_root = Path(raw) / "guessed"
    (guessed_root / "config").mkdir(parents=True)
    (guessed_root / "config/model-routing.toml").write_text(
        (ROOT / "config/model-routing.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    guessed_env = dict(env)
    for key in (
        "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "LLM_OBSIDIAN_SESSION_RUNTIME",
        "LLM_OBSIDIAN_SESSION_MODEL", "LLM_OBSIDIAN_SESSION_EFFORT",
    ):
        guessed_env.pop(key, None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(guessed_root), "--session-id", "claude-session", "--json"],
        text=True, capture_output=True, env=guessed_env, check=False,
    )
    guessed_payload = json.loads(result.stdout)
    guessed_snapshot = json.loads(
        (guessed_root / ".vault-meta/session-routing/claude-session.json").read_text(encoding="utf-8")
    )
    check("Claude fallback snapshot is labelled", guessed_snapshot["source"] == "tracked-default")
    check("unconfirmed session routing is visible", any(item["id"] == "session-routing" for item in guessed_payload["issues"]))

    (guessed_root / "config/acceptance-cells.toml").write_text(
        (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    manifest = tomllib.loads(
        (guessed_root / "config/acceptance-cells.toml").read_text(encoding="utf-8")
    )
    for relative in manifest["non_behavioral_paths"]:
        target = guessed_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    for prefix in manifest["non_behavioral_prefixes"]:
        (guessed_root / prefix).mkdir(parents=True, exist_ok=True)
    for relative in manifest["orchestration_dependencies"]:
        target = guessed_root / relative
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    generation_file = guessed_root / ".vault-meta/acceptance/model-generations.json"
    generation_file.parent.mkdir(parents=True, exist_ok=True)
    generation_file.write_text(
        json.dumps({"schema_version": 1, "generations": {"claude": "claude:old", "codex": "codex:5.5"}}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(guessed_root), "--session-id", "generation-session", "--json"],
        text=True, capture_output=True, env=guessed_env, check=False,
    )
    generation_payload = json.loads(result.stdout)
    check(
        "major model generation drift proposes acceptance once at startup",
        any(item["id"] == "model-generation-changed" and item["repair"] == "make acceptance-live" for item in generation_payload["issues"]),
    )

    fake_bin = Path(raw) / "fake-bin"
    fake_bin.mkdir()
    fake_cmux = fake_bin / "cmux"
    fake_cmux.write_text("#!/bin/sh\necho 'legacy cmux without required flags'\n", encoding="utf-8")
    fake_cmux.chmod(0o755)
    capability_env = dict(os.environ)
    capability_env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), "--session-id", "capability-session", "--runtime", "codex", "--model", "gpt-5.6-sol", "--effort", "high", "--json"],
        text=True, capture_output=True, env=capability_env, check=False,
    )
    capability_payload = json.loads(result.stdout)
    capability_ids = {item["id"] for item in capability_payload["issues"]}
    check("missing anchored split is visible", "cmux-anchored-split" in capability_ids)
    check("missing typed resume is visible", "cmux-session-resume" in capability_ids)
    (ROOT / ".vault-meta/session-routing/capability-session.json").unlink(missing_ok=True)

print("session preflight tests passed")
