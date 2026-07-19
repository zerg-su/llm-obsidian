#!/usr/bin/env python3
"""Queue graceful /exit into this exact cmux surface; never close the surface."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


SURFACE_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def runtime() -> str:
    return "codex" if any(os.environ.get(key) for key in ("CODEX_THREAD_ID", "CODEX_CI", "CODEX_MANAGED_BY_NPM")) else "claude"


def run(cmux: str, *args: str) -> bool:
    result = subprocess.run([cmux, *args], text=True, capture_output=True, check=False)
    return result.returncode == 0


def queue_exit() -> dict[str, object]:
    selected_runtime = runtime()
    surface = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    configured = str(os.environ.get("CMUX_BUNDLED_CLI_PATH") or "").strip()
    cmux = configured if configured and Path(configured).is_file() else shutil.which("cmux")
    base = {
        "schema_version": 1,
        "runtime": selected_runtime,
        "surface_closed": False,
        "manual_fallback": selected_runtime == "codex",
    }
    if not surface or not SURFACE_RE.fullmatch(surface) or not cmux:
        return {**base, "status": "manual", "reason": "exact cmux surface unavailable"}
    if selected_runtime == "claude":
        ok = run(cmux, "send", "--surface", surface, "/exit\n")
    else:
        for _index in range(40):
            run(cmux, "send-key", "--surface", surface, "backspace")
        ok = run(cmux, "send", "--surface", surface, "/exit")
        ok = run(cmux, "send-key", "--surface", surface, "tab") and ok
    return {
        **base,
        "status": "queued" if ok else "manual",
        "reason": "exact-surface graceful exit queued" if ok else "cmux queue failed",
    }


def main() -> int:
    print(json.dumps(queue_exit(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
