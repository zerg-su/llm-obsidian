#!/usr/bin/env python3
"""Regression checks for code-owned verification resubmission."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pipeline-verification-resubmit.py"


with tempfile.TemporaryDirectory(prefix="verification-resubmit.") as raw:
    worktree = Path(raw)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=worktree, check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=worktree, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Verification Resubmit"],
        cwd=worktree, check=True,
    )
    product = worktree / "product.txt"
    product.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "before"], cwd=worktree, check=True,
        capture_output=True,
    )
    failed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    packet = {
        "schema_version": 1,
        "operation_id": "parent-operation",
        "verification_operation_id": "verify-operation",
        "verification_lane_id": "verify-lane",
        "verification_run_id": "verify-run",
        "definition_sha256": "a" * 64,
        "step_id": "verify",
        "head_sha": failed_head,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": ["fix-and-resubmit", "escalate"],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": "/trusted/receipt.json",
        "evidence": [],
    }
    packet_path = worktree / ".task-verification.json"
    packet_path.write_text(
        json.dumps(packet, sort_keys=True) + "\n", encoding="utf-8"
    )
    blocked = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if blocked.returncode == 0 or "commit a new HEAD" not in blocked.stderr:
        raise AssertionError(blocked)
    product.write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "after"], cwd=worktree, check=True,
        capture_output=True,
    )
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    stale = worktree / ".task-verification-response.json"
    stale.write_text("{}\n", encoding="utf-8")
    ready = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if ready.returncode != 0:
        raise AssertionError(ready)
    response = json.loads(stale.read_text(encoding="utf-8"))
    canonical = json.dumps(
        packet, sort_keys=True, separators=(",", ":")
    ).encode()
    expected = {
        "schema_version": 1,
        "operation_id": "parent-operation",
        "verification_operation_id": "verify-operation",
        "failed_head_sha": failed_head,
        "packet_sha256": hashlib.sha256(canonical).hexdigest(),
        "response": "fix-and-resubmit",
        "resubmitted_head_sha": current_head,
    }
    if response != expected:
        raise AssertionError((response, expected))
    repeated = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if repeated.returncode != 0 or "already-ready" not in repeated.stdout:
        raise AssertionError(repeated)

print("OK   verification resubmission is exact, code-owned, and idempotent")
