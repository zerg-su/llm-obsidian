#!/usr/bin/env python3
"""Resolve a pending reap effect from one exact durable completion receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reap_effect_reconciliation import (
    ReapEffectRecoveryError,
    parse_recovery_request,
    reconcile_completed_reap_effect,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = json.loads(args.request_file.read_text(encoding="utf-8"))
        request = parse_recovery_request(value)
        result = reconcile_completed_reap_effect(
            args.vault_root,
            args.worktree,
            request,
        )
    except (
        OSError,
        json.JSONDecodeError,
        ReapEffectRecoveryError,
    ) as exc:
        print(f"reap-effect-reconcile: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
