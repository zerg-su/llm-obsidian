#!/usr/bin/env python3
"""Keep the Makefile harness suite in exact sync with test files."""

from __future__ import annotations

import re
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
    makefile = MAKEFILE.read_text(encoding="utf-8")
    fast_target = makefile.split("test-lifecycle-simulator:", 1)[1].split(
        "test-lifecycle-simulator-extended:", 1
    )[0]
    assert all(path in fast_target for path in lifecycle), (
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
    assert summaries[0] == summaries[1], "fast lifecycle summary is not deterministic"
    print(f"harness suite registration: ok ({len(expected)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
