#!/usr/bin/env python3
"""Best-effort fingerprint-aware worker for deferred dense refresh."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[1]).resolve()
META = ROOT / ".vault-meta"
PENDING = META / "dense-refresh.pending.json"
sys.path.insert(0, str(ROOT / "scripts"))

from pipeline_events import emit_event  # noqa: E402
import retrieve  # noqa: E402


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_pending() -> dict:
    try:
        value = json.loads(PENDING.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    marker = load_pending()
    if not marker:
        return 0
    if float(marker.get("next_retry_at", 0) or 0) > time.time():
        return 0
    requested = str(marker.get("source_fingerprint") or "")
    try:
        index, _ = retrieve.ensure_sparse()
        target = str(index.get("source_fingerprint") or "")
        if target != requested:
            current = load_pending()
            if str(current.get("source_fingerprint") or "") != requested:
                return 0
            atomic_json(
                PENDING,
                {
                    "schema_version": 2,
                    "requested_at": current.get("requested_at") or datetime.now(timezone.utc).isoformat(),
                    "source_fingerprint": target,
                    "next_retry_at": 0,
                },
            )
            requested = target
        retrieve.refresh_dense(index, quiet=True)
    except (OSError, RuntimeError, TimeoutError, ValueError):
        current = load_pending()
        if str(current.get("source_fingerprint") or "") == requested:
            atomic_json(
                PENDING,
                {
                    "schema_version": 2,
                    "requested_at": current.get("requested_at") or marker.get("requested_at") or datetime.now(timezone.utc).isoformat(),
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "source_fingerprint": requested,
                    "next_retry_at": int(time.time()) + 900,
                    "exit_code": 10,
                },
            )
        emit_event("dense-refresh", actor="deferred-worker", counts={"exit_code": 10}, status="degraded", root=ROOT)
        return 10

    current = load_pending()
    if str(current.get("source_fingerprint") or "") == target:
        PENDING.unlink(missing_ok=True)
    emit_event(
        "dense-refresh",
        actor="deferred-worker",
        counts={"exit_code": 0, "chunks": int(index.get("chunk_count", 0))},
        root=ROOT,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
