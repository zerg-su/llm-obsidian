#!/usr/bin/env python3
"""Atomically resume one exact failed review-drive latch without model effects."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.review_drive_rearm import ReviewDriveRearmError, rearm_review_drive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True)
    args = parser.parse_args()
    try:
        receipt = rearm_review_drive(Path(args.worktree))
    except (OSError, ReviewDriveRearmError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
