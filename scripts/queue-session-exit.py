#!/usr/bin/env python3
"""Queue graceful /exit into this exact cmux surface; never close the surface."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from harness.adapters.cmux import CmuxAdapter, CmuxError


SURFACE_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def runtime() -> str:
    return "codex" if any(os.environ.get(key) for key in ("CODEX_THREAD_ID", "CODEX_CI", "CODEX_MANAGED_BY_NPM")) else "claude"


def run(cmux: CmuxAdapter, action: str, surface: str, value: str) -> bool:
    try:
        if action == "send":
            cmux.send(surface, value)
        elif action == "send-key":
            cmux.send_key(surface, value)
        else:
            raise ValueError("unsupported cmux action")
    except (CmuxError, OSError, ValueError):
        return False
    return True


def queue_exit() -> dict[str, object]:
    selected_runtime = runtime()
    surface = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    configured = str(os.environ.get("CMUX_BUNDLED_CLI_PATH") or "").strip()
    binary = configured if configured and Path(configured).is_file() else "cmux"
    cmux = CmuxAdapter(binary=binary)
    base = {
        "schema_version": 1,
        "runtime": selected_runtime,
        "surface_closed": False,
        "manual_fallback": selected_runtime == "codex",
    }
    if not surface or not SURFACE_RE.fullmatch(surface):
        return {**base, "status": "manual", "reason": "exact cmux surface unavailable"}
    if selected_runtime == "claude":
        ok = run(cmux, "send-key", surface, "ctrl+u")
        ok = run(cmux, "send", surface, "/exit") and ok
        ok = run(cmux, "send-key", surface, "Enter") and ok
    else:
        for _index in range(40):
            run(cmux, "send-key", surface, "Backspace")
        ok = run(cmux, "send", surface, "/exit")
        ok = run(cmux, "send-key", surface, "Tab") and ok
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
