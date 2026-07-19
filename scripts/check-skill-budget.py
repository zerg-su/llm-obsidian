#!/usr/bin/env python3
"""Fail when repo skill descriptions exceed Codex's initial registry budget."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


DEFAULT_LIMIT = 7500
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "config" / "skill-body-baseline.json"
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


def body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: missing YAML frontmatter")
    return parts[2].lstrip("\n")


def estimated_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def metrics(path: Path) -> dict[str, int]:
    text = body(path)
    closure = text
    references = path.parent / "references"
    if references.is_dir():
        for reference in sorted(references.glob("*.md")):
            rel = reference.relative_to(path.parent).as_posix()
            mentioning_lines = [line for line in text.splitlines() if rel in line or reference.name in line]
            conditional = any("context:conditional" in line for line in mentioning_lines)
            if mentioning_lines and not conditional:
                closure += "\n" + reference.read_text(encoding="utf-8")
    return {
        "body_bytes": len(text.encode("utf-8")),
        "body_lines": len(text.splitlines()),
        "token_estimate": estimated_tokens(text),
        "closure_bytes": len(closure.encode("utf-8")),
        "closure_token_estimate": estimated_tokens(closure),
    }


def baseline_payload(rows: list[tuple[str, int, dict[str, int]]]) -> dict:
    return {
        "schema_version": 1,
        "basis": "v2.1.0 skill bodies and directly referenced Markdown",
        "skills": {name: values for name, _description_size, values in rows},
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read body baseline {path}: {exc}") from exc
    skills = value.get("skills") if isinstance(value, dict) and value.get("schema_version") == 1 else None
    if not isinstance(skills, dict):
        raise ValueError(f"{path}: invalid body baseline")
    return skills


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills", type=Path, default=ROOT / "skills")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    rows = []
    try:
        for path in sorted(args.skills.glob("*/SKILL.md")):
            size = len(description(path).encode("utf-8"))
            rows.append((path.parent.name, size, metrics(path)))
    except (OSError, ValueError) as exc:
        print(f"skill-budget: {exc}", file=sys.stderr)
        return 3
    if args.update_baseline:
        write_json(args.baseline, baseline_payload(rows))
        print(f"updated skill body baseline: {args.baseline}")
        return 0
    try:
        baseline = load_baseline(args.baseline)
    except ValueError as exc:
        print(f"skill-budget: {exc}", file=sys.stderr)
        return 3
    current_names = {name for name, _size, _values in rows}
    if current_names != set(baseline):
        print("skill-budget: body baseline skill inventory drift; use --update-baseline", file=sys.stderr)
        return 1
    increases: list[str] = []
    for name, _size, values in rows:
        prior = baseline[name]
        for key in ("body_bytes", "body_lines", "token_estimate", "closure_bytes", "closure_token_estimate"):
            if values[key] > int(prior.get(key, -1)):
                increases.append(f"{name}:{key}+{values[key] - int(prior.get(key, 0))}")
    total = sum(size for _name, size, _values in rows)
    body_bytes = sum(values["body_bytes"] for _name, _size, values in rows)
    closure_bytes = sum(values["closure_bytes"] for _name, _size, values in rows)
    print(f"skill descriptions: {total}/{args.limit} bytes across {len(rows)} skills")
    print(f"skill bodies: {body_bytes} bytes; normal closure: {closure_bytes} bytes")
    for name, size, _values in sorted(rows, key=lambda row: row[1], reverse=True)[:5]:
        print(f"  {size:4d}  {name}")
    if total > args.limit:
        print(f"SKILL_BUDGET_EXCEEDED: trim {total - args.limit} bytes", file=sys.stderr)
        return 1
    if increases:
        print("SKILL_BODY_BASELINE_EXCEEDED: " + ", ".join(increases), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
