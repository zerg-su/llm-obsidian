#!/usr/bin/env python3
"""Exact native workspace-trust prompt recognition for cmux agent launches."""

from __future__ import annotations

import re


def workspace_trust_prompt_visible(runtime: str, screen: str) -> bool:
    """Recognize only a complete native first-run trust dialog."""

    markers = {
        "claude": (
            "Accessing workspace:",
            "Quick safety check: Is this a project you created or one you trust?",
            "Yes, I trust this folder",
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
    compact_screen = re.sub(r"\s+", "", screen)
    return all(re.sub(r"\s+", "", marker) in compact_screen for marker in expected)
