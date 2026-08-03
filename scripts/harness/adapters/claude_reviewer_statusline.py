#!/usr/bin/env python3
"""Render content-free Claude reviewer model, context, and limit status."""

from __future__ import annotations

import json
import math
import sys
from typing import Any


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _percentage(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "--"
    number = float(value)
    if not math.isfinite(number):
        return "--"
    return f"{max(0.0, min(100.0, number)):.0f}%"


def render(payload: object) -> str:
    data = _mapping(payload)
    model = _mapping(data.get("model"))
    effort = _mapping(data.get("effort"))
    context = _mapping(data.get("context_window"))
    limits = _mapping(data.get("rate_limits"))
    five_hour = _mapping(limits.get("five_hour"))
    seven_day = _mapping(limits.get("seven_day"))

    display_name = str(model.get("display_name") or "Claude").strip() or "Claude"
    effort_level = str(effort.get("level") or "--").strip() or "--"
    context_used = context.get("used_percentage")
    if context_used is None:
        remaining = context.get("remaining_percentage")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            context_used = 100.0 - float(remaining)

    return " · ".join(
        (
            display_name,
            f"effort {effort_level}",
            f"CTX {_percentage(context_used)}",
            f"5H {_percentage(five_hour.get('used_percentage'))}",
            f"7D {_percentage(seven_day.get('used_percentage'))}",
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}
    print(render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
