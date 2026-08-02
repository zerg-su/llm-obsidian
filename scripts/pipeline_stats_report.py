#!/usr/bin/env python3
"""Optional vault report persistence for pipeline statistics."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


VAULT_ROOT = Path(__file__).resolve().parent.parent


def write_report(output: str) -> int:
    today = dt.date.today().isoformat()
    path = VAULT_ROOT / "wiki" / "meta" / "reports" / f"pipeline-stats-{today}.md"
    frontmatter = (
        f"---\ntype: meta\ntitle: \"Pipeline Stats {today}\"\ncreated: {today}\n"
        f"updated: {today}\ntags: [meta, pipeline, stats]\nstatus: developing\n"
        "sessions: []\n---\n\n"
    )
    page = {
        "op": "update" if path.is_file() else "create",
        "path": str(path.relative_to(VAULT_ROOT)),
        "content": frontmatter + output + "\n",
    }
    if path.is_file():
        page["expected_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "actor": "pipeline-stats",
        "session": os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or "unknown",
        "pages": [page],
    }
    result = subprocess.run(
        [sys.executable, str(VAULT_ROOT / "scripts" / "vault-write.py")],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=VAULT_ROOT,
    )
    if result.returncode:
        print(result.stderr or result.stdout, end="", file=sys.stderr)
        return result.returncode
    print(f"\nreport written: {path.relative_to(VAULT_ROOT)}", file=sys.stderr)
    return 0
