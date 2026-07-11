#!/usr/bin/env python3
"""Pure helpers for deterministic plan lifecycle transitions."""

from __future__ import annotations

import re


class PlanCloseError(ValueError):
    """Raised when a plan cannot make the pending -> executed transition."""


def render_plan_close(
    text: str,
    *,
    today: str,
    result_link: str,
    exec_session: str | None,
    label: str,
) -> str:
    """Return the exact plan text produced by a successful final reap.

    Keeping this transformation pure lets the lifecycle gate bind the expected
    post-transaction plan hash before ``vault-write.py`` mutates the plan.
    """
    match = re.match(r"^---\n(.*?\n)---\n", text, flags=re.S)
    if not match:
        raise PlanCloseError(f"plan_close: {label} has no frontmatter")
    frontmatter = match.group(1)
    body = text[match.end():]

    statuses = re.findall(r"^status:\s*(\S+)", frontmatter, flags=re.M)
    if len(statuses) != 1:
        raise PlanCloseError(
            f"plan_close: {label} has {len(statuses)} status lines (expected 1)"
        )
    if statuses[0] != "pending":
        raise PlanCloseError(
            f"plan_close: {label} status is '{statuses[0]}' (expected 'pending') — "
            "already closed or wrong plan_file"
        )
    frontmatter = re.sub(
        r"^status:\s*pending\s*$",
        "status: executed",
        frontmatter,
        count=1,
        flags=re.M,
    )

    if exec_session:
        entry = [f"  - id: {exec_session}", f"    date: {today}"]
        lines = frontmatter.split("\n")
        block_start = next(
            (i for i, line in enumerate(lines) if re.match(r"sessions:\s*$", line)),
            None,
        )
        if block_start is not None:
            insert_at = block_start + 1
            for index in range(block_start + 1, len(lines)):
                if lines[index].strip() and lines[index].startswith((" ", "\t")):
                    insert_at = index + 1
                elif lines[index].strip():
                    break
            lines[insert_at:insert_at] = entry
            frontmatter = "\n".join(lines)
        elif re.search(r"^sessions:\s*\[\s*\]\s*$", frontmatter, flags=re.M):
            frontmatter = re.sub(
                r"^sessions:\s*\[\s*\]\s*$",
                "\n".join(["sessions:"] + entry),
                frontmatter,
                count=1,
                flags=re.M,
            )
        else:
            frontmatter = (
                frontmatter.rstrip("\n")
                + "\n"
                + "\n".join(["sessions:"] + entry)
                + "\n"
            )

    if not frontmatter.endswith("\n"):
        frontmatter += "\n"
    body = body.rstrip("\n") + f"\n\nРезультат: {result_link} (reaped {today})\n"
    closed = "---\n" + frontmatter + "---\n" + body
    return re.sub(
        r"^updated: .*$",
        f"updated: {today}",
        closed,
        count=1,
        flags=re.M,
    )
