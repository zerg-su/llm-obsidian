#!/usr/bin/env python3
"""Fail when repo skill descriptions exceed Codex's initial registry budget."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DEFAULT_LIMIT = 7500
DESCRIPTION_RX = re.compile(
    r"(?ms)^description:\s*(.*?)(?=^[A-Za-z_][A-Za-z0-9_-]*:\s|\Z)"
)


def description(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)
    if len(frontmatter) < 3:
        raise ValueError(f"{path}: missing YAML frontmatter")
    match = DESCRIPTION_RX.search(frontmatter[1])
    if match is None:
        raise ValueError(f"{path}: missing description")
    value = match.group(1).strip()
    if value.startswith(("|", ">")):
        value = value[1:].lstrip("-+").strip()
    return re.sub(r"\s+", " ", value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills", type=Path, default=Path(__file__).resolve().parents[1] / "skills")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    rows = []
    try:
        for path in sorted(args.skills.glob("*/SKILL.md")):
            size = len(description(path).encode("utf-8"))
            rows.append((path.parent.name, size))
    except (OSError, ValueError) as exc:
        print(f"skill-budget: {exc}", file=sys.stderr)
        return 3
    total = sum(size for _name, size in rows)
    print(f"skill descriptions: {total}/{args.limit} bytes across {len(rows)} skills")
    for name, size in sorted(rows, key=lambda row: row[1], reverse=True)[:5]:
        print(f"  {size:4d}  {name}")
    if total > args.limit:
        print(f"SKILL_BUDGET_EXCEEDED: trim {total - args.limit} bytes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
