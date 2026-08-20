#!/usr/bin/env python3
"""Bounded provider-free prototype for the v2.8.7 transition repair."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUESTION = (
    "Across the registered transition handoffs, does an exact successor "
    "artifact prevent every unsubmitted retained notification from producing "
    "Enter while ordinary delivery and bounded liveness recovery still run once?"
)


result = subprocess.run(
    (sys.executable, "tests/harness/test_transition_transport_stress.py"),
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
head = subprocess.run(
    ("git", "rev-parse", "HEAD"),
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()

print(f"Question: {QUESTION}")
print(f"Evidence: {(result.stdout or result.stderr).strip()}")
print(
    "Decision: provider-free transition stress is green"
    if result.returncode == 0
    else "Decision: provider-free transition stress rejected the candidate"
)
print(
    "Limitations: no provider, network, publication, or real cmux editor was "
    "used; a separate bounded real-cmux gate remains required."
)
print(f"Provenance: git_head={head}; repetitions=50; exit_code={result.returncode}")
raise SystemExit(result.returncode)
