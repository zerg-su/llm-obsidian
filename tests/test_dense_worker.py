#!/usr/bin/env python3
"""Deferred dense worker marker and stale-request tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def run(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(root), TEST_DENSE_MODE=mode)
    return subprocess.run([sys.executable, str(root / "scripts/dense-refresh-worker.py")], cwd=root, env=env, text=True, capture_output=True)


with tempfile.TemporaryDirectory(prefix="dense-worker-test.") as raw:
    root = Path(raw)
    (root / "scripts").mkdir()
    (root / ".vault-meta").mkdir()
    shutil.copy2(ROOT / "scripts/dense-refresh-worker.py", root / "scripts/dense-refresh-worker.py")
    (root / "scripts/pipeline_events.py").write_text("def emit_event(*a, **k): pass\n", encoding="utf-8")
    (root / "scripts/retrieve.py").write_text(
        "import json,os\n"
        "from pathlib import Path\n"
        "ROOT=Path(os.environ['CLAUDE_PROJECT_DIR'])\n"
        "def ensure_sparse(): return ({'source_fingerprint':'target-fp','chunk_count':7},False)\n"
        "def refresh_dense(index,quiet=True):\n"
        " mode=os.environ.get('TEST_DENSE_MODE')\n"
        " if mode=='fail': raise RuntimeError('offline')\n"
        " if mode=='newer_fail':\n"
        "  p=ROOT/'.vault-meta/dense-refresh.pending.json'\n"
        "  p.write_text(json.dumps({'schema_version':2,'source_fingerprint':'newer-fp','next_retry_at':0}))\n"
        "  raise RuntimeError('old worker failed')\n"
        " if mode=='newer':\n"
        "  p=ROOT/'.vault-meta/dense-refresh.pending.json'\n"
        "  p.write_text(json.dumps({'schema_version':2,'source_fingerprint':'newer-fp','next_retry_at':0}))\n"
        " return {'complete':True}\n",
        encoding="utf-8",
    )
    marker = root / ".vault-meta/dense-refresh.pending.json"

    marker.write_text(json.dumps({"schema_version": 2, "source_fingerprint": "target-fp", "next_retry_at": 0}), encoding="utf-8")
    result = run(root, "success")
    check("successful worker exits zero", result.returncode == 0, result.stderr)
    check("matching marker removed", not marker.exists())

    marker.write_text(json.dumps({"schema_version": 2, "source_fingerprint": "older-fp", "next_retry_at": 0}), encoding="utf-8")
    result = run(root, "success")
    check("corpus drift refresh exits zero", result.returncode == 0, result.stderr)
    check("drifted marker advanced then removed", not marker.exists())

    marker.write_text(json.dumps({"schema_version": 2, "source_fingerprint": "target-fp", "next_retry_at": 0}), encoding="utf-8")
    result = run(root, "newer")
    check("worker tolerates newer request", result.returncode == 0, result.stderr)
    check("newer marker preserved", json.loads(marker.read_text())["source_fingerprint"] == "newer-fp")

    marker.write_text(json.dumps({"schema_version": 2, "source_fingerprint": "target-fp", "next_retry_at": 0}), encoding="utf-8")
    result = run(root, "fail")
    failed = json.loads(marker.read_text())
    check("failed worker is nonzero", result.returncode == 10)
    check("failed marker retained", failed["source_fingerprint"] == "target-fp" and failed["next_retry_at"] > 0)
    check("failed marker contains no error text", "reason" not in failed)

    marker.write_text(json.dumps({"schema_version": 2, "source_fingerprint": "target-fp", "next_retry_at": 0}), encoding="utf-8")
    result = run(root, "newer_fail")
    newer = json.loads(marker.read_text())
    check("old failure does not delay new request", result.returncode == 10 and newer["source_fingerprint"] == "newer-fp" and newer["next_retry_at"] == 0)

print("\nAll deferred dense worker tests passed.")
