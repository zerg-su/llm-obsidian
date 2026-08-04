#!/usr/bin/env python3
"""Keep the Makefile harness suite in exact sync with test files."""

from __future__ import annotations

import re
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
    print(f"harness suite registration: ok ({len(expected)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
