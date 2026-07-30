#!/usr/bin/env python3
"""Exact native workspace-trust prompt recognition for cmux agent launches."""

from __future__ import annotations

import re


def native_dialog_region(
    screen: str,
    footer: str,
    *,
    footer_variants: tuple[str, ...] = (),
    max_lines: int = 24,
) -> str:
    """Return one bounded native dialog ending in a clean, possibly wrapped footer."""

    lines = screen.splitlines()
    expected = {
        re.sub(r"\s+", "", candidate)
        for candidate in (footer, *footer_variants)
    }
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return ""
    end = nonempty[-1]
    for start in range(max(0, end - 3), end + 1):
        fragments = [line.strip() for line in lines[start : end + 1]]
        if (
            not all(fragments)
            or any(re.search(r"""["'`\\]""", fragment) for fragment in fragments)
            or re.sub(r"\s+", "", "".join(fragments)) not in expected
        ):
            continue
        return "\n".join(lines[max(0, start - max_lines + 1) : end + 1])
    return ""


def workspace_trust_prompt_visible(runtime: str, screen: str) -> bool:
    """Recognize only a complete native first-run trust dialog."""

    markers = {
        "claude": (
            "Accessing workspace:",
            "Quick safety check: Is this a project you created or one you trust?",
            "Enter to confirm",
        ),
        "codex": (
            "Do you trust the contents of this directory?",
            "Yes, continue",
            "No, quit",
            "Press enter",
        ),
    }
    expected = markers.get(runtime)
    if expected is None:
        return False
    footer = "Enter to confirm" if runtime == "claude" else "Press enter"
    footer_variants = (
        ("Enter to confirm · Esc to cancel",)
        if runtime == "claude"
        else ("Press enter to continue",)
    )
    region = native_dialog_region(screen, footer, footer_variants=footer_variants)
    if not region:
        return False
    compact_screen = re.sub(r"\s+", "", region)
    if not all(re.sub(r"\s+", "", marker) in compact_screen for marker in expected):
        return False
    if runtime == "claude":
        return any(
            re.sub(r"\s+", "", option) in compact_screen
            for option in ("Yes, I trust this folder", "Yes, I trust this")
        )
    return True


def claude_background_exit_prompt_visible(screen: str) -> bool:
    """Recognize Claude's complete background-work exit dialog across wrapping."""

    markers = (
        "Background work is running",
        "The following will stop when you exit:",
        "1. Exit anyway",
        "2. Move to background and exit",
        "3. Stay",
        "Enter to confirm",
    )
    compact_screen = re.sub(r"\s+", "", screen)
    return all(re.sub(r"\s+", "", marker) in compact_screen for marker in markers)
