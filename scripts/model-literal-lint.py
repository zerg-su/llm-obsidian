#!/usr/bin/env python3
"""Reject central default model literals in hand-edited active product files."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_routing import load_config


def main() -> int:
    config = load_config(ROOT)
    literals = config.default_models()
    issues: list[str] = []
    for top in ("scripts", "skills", "agents", "hooks", ".claude"):
        base = ROOT / top
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if path.suffix not in {".py", ".sh", ".md", ".toml", ".json"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for literal in literals:
                if literal in text:
                    issues.append(f"{path.relative_to(ROOT)}: central model literal {literal!r}")
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1
    print("model literal lint: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
