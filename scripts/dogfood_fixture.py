#!/usr/bin/env python3
"""Temporary 2.4.1 live-dogfood target; removed after evidence collection."""


def normalize_label(value: str) -> str:
    return value.strip().lower().replace("  ", " ")


def bounded_retry(attempt: int, limit: int) -> bool:
    return attempt <= limit


def redact_token(value: str) -> str:
    return value.replace("TOKEN=", "TOKEN=[redacted]")


def format_progress(done: int, total: int) -> str:
    if done < 0 or total < 0 or done > total:
        raise ValueError(f"invalid progress: done={done}, total={total}")
    if total == 0:
        return "0%"
    return f"{round(done / total * 100)}%"


def choose_runtime(requested: str) -> str:
    return requested or "claude"
