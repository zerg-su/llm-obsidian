#!/usr/bin/env python3
"""Focused semantic contracts for the 2.6 debug/TDD skill workstream."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def skill_text(name: str, git_ref: str | None) -> str:
    relative = f"skills/{name}/SKILL.md"
    if git_ref is None:
        return (ROOT / relative).read_text(encoding="utf-8")
    result = subprocess.run(
        ["git", "show", f"{git_ref}:{relative}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"cannot read {relative} at {git_ref}")
    return result.stdout


def semantic_blocks(text: str) -> tuple[str, ...]:
    """Return normalized Markdown paragraphs/list items as instruction units."""
    blocks: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or line.startswith("#"):
            if current:
                blocks.append(" ".join(current.split()).casefold())
                current = ""
            continue
        if re.match(r"^(?:\d+\.|[-*])\s+", line):
            if current:
                blocks.append(" ".join(current.split()).casefold())
            current = re.sub(r"^(?:\d+\.|[-*])\s+", "", line)
        elif current:
            current = f"{current} {line}"
        else:
            current = line
    if current:
        blocks.append(" ".join(current.split()).casefold())
    return tuple(blocks)


def has_clause(blocks: tuple[str, ...], *concepts: str) -> bool:
    return any(all(re.search(concept, block) for concept in concepts) for block in blocks)


def check(label: str, condition: bool, failures: list[str]) -> None:
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}")
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--git-ref",
        help="read the two skill files from an exact preserved Git ref",
    )
    args = parser.parse_args()
    debug = semantic_blocks(skill_text("debug", args.git_ref))
    tdd = semantic_blocks(skill_text("tdd", args.git_ref))
    failures: list[str] = []

    check(
        "direct deterministic reproduction is sufficient red evidence",
        has_clause(debug, r"deterministic", r"command", r"red evidence"),
        failures,
    )
    check(
        "missing direct reproduction requires a red-capable loop before hypotheses",
        has_clause(debug, r"otherwise|without", r"red-capable", r"before", r"hypoth"),
        failures,
    )
    check(
        "missing reproducible evidence records a gap and stops speculative repair",
        has_clause(debug, r"evidence gap", r"stop", r"speculativ", r"fix|repair"),
        failures,
    )
    check(
        "repro-backed root cause is established before product mutation",
        has_clause(debug, r"root cause", r"repro(?:.*evidence|-backed)|evidence.*repro", r"before", r"product (?:change|mutation)"),
        failures,
    )
    check(
        "a failed fix attempt requires product mutation and the original repro rerun",
        has_clause(debug, r"failed fix attempt", r"product", r"chang|mutat", r"original", r"repro"),
        failures,
    )
    check(
        "three failed product fixes stop unconditionally before a fourth attempt",
        has_clause(debug, r"three", r"failed", r"unconditional", r"stop", r"fourth"),
        failures,
    )
    check(
        "debug completion traces the repaired defect to declared outcome evidence",
        has_clause(debug, r"repaired defect|defect repair", r"declared", r"success|outcome", r"evidence")
        and has_clause(debug, r"evidence", r"missing|absen|gap|not established", r"not claim|no .*claim|bars", r"complet"),
        failures,
    )

    check(
        "TDD names the production change that should break the test before writing it",
        has_clause(tdd, r"before", r"test", r"name", r"production change", r"fail|break"),
        failures,
    )
    check(
        "TDD asserts observable behavior and rejects source-text proxies",
        has_clause(tdd, r"observable (?:behavio|seam)", r"source[- ]text", r"not|reject|never", r"evidence|proxy|substitute"),
        failures,
    )
    check(
        "regression red uses preserved pre-fix state without destructive reset",
        has_clause(tdd, r"regression", r"red", r"pre-fix", r"disposable|saved base|preserved base", r"destructive reset"),
        failures,
    )
    check(
        "non-executable exemptions receive a recorded proportional check",
        has_clause(tdd, r"exempt", r"proportional", r"check", r"record"),
        failures,
    )
    check(
        "green is bound to declared success evidence rather than task completion",
        has_clause(tdd, r"green", r"declared", r"success evidence", r"task completion|complete the task|completion")
        and has_clause(tdd, r"missing|gap", r"explicit"),
        failures,
    )

    if failures:
        print(f"\n{len(failures)} workstream-B contract(s) failed.")
        return 1
    print("\nAll workstream-B skill contracts passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
