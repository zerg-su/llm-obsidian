#!/usr/bin/env python3
"""Keep the Makefile harness suite in exact sync with test files."""

from __future__ import annotations

import re
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
HARNESS_TESTS = ROOT / "tests" / "harness"
COMMAND_RE = re.compile(r"python3\s+(tests/harness/test_[A-Za-z0-9_]+\.py)")


def main() -> int:
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in HARNESS_TESTS.glob("test_*.py")
    }
    registered = set(COMMAND_RE.findall(MAKEFILE.read_text(encoding="utf-8")))
    missing = sorted(expected - registered)
    unknown = sorted(registered - expected)
    assert not missing and not unknown, (
        "Makefile test-harness registration drift: "
        f"missing={missing}, unknown={unknown}"
    )
    lifecycle = sorted(
        path.relative_to(ROOT).as_posix()
        for path in HARNESS_TESTS.glob("test_lifecycle_*.py")
    )
    fast_dry_run = subprocess.run(
        ["make", "-n", "test-lifecycle-simulator"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert all(path in fast_dry_run for path in lifecycle), (
        "fast lifecycle target does not own every ordinary lifecycle test: "
        f"expected={lifecycle}"
    )
    dry_run = subprocess.run(
        ["make", "-n", "test-lifecycle-simulator-extended"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "lifecycle_gate.py --mode fast" in dry_run
    assert "lifecycle_gate.py --mode extended" in dry_run
    assert "./scripts/with-timeout 60" in dry_run
    assert "./scripts/with-timeout 300" in dry_run
    summaries = [
        subprocess.run(
            [sys.executable, "tests/harness/lifecycle_gate.py", "--mode", "fast"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        for _ in range(2)
    ]
    payloads = [json.loads(item) for item in summaries]
    logical = [
        {key: value for key, value in item.items() if key != "wall_seconds"}
        for item in payloads
    ]
    assert logical[0] == logical[1], "fast lifecycle logical summary is not deterministic"
    assert all(0 < item["wall_seconds"] <= 60.0 for item in payloads)
    print(f"harness suite registration: ok ({len(expected)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
