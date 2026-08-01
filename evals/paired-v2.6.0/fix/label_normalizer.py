"""Frozen paired-eval target; intentionally incomplete for Unicode labels."""

from __future__ import annotations

import re


def normalize_label(value: str) -> str:
    """Return a stable lowercase task label."""

    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "untitled"
