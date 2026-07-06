#!/usr/bin/env python3
"""Print compact Codex rate-limit reset status from the latest session JSONL."""

import argparse
import glob
import json
import os
import subprocess
import sys
import time


CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def fmt_remaining(reset_at, now=None):
    if reset_at is None:
        return "?"
    now = int(time.time() if now is None else now)
    try:
        seconds = int(float(reset_at)) - now
    except (TypeError, ValueError):
        return "?"
    if seconds <= 0:
        return "0m"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}"
    return f"{minutes}m"


def session_files():
    pattern = os.path.join(CODEX_HOME, "sessions", "*", "*", "*", "*.jsonl")
    return sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)


def pick_session():
    thread_id = os.environ.get("CODEX_THREAD_ID")
    files = session_files()
    if not thread_id:
        return files[0] if files else None
    needle = f'"id":"{thread_id}"'
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                head = fh.readline()
        except OSError:
            continue
        if needle in head:
            return path
    return files[0] if files else None


def latest_limits(path):
    found = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if '"rate_limits"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") or {}
                limits = obj.get("rate_limits") or payload.get("rate_limits")
                if limits:
                    found = limits
    except OSError:
        return None
    return found


def pct_value(win):
    value = (win or {}).get("used_percent")
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def pct_text(win):
    value = pct_value(win)
    if value is None:
        return "?"
    return str(value)


def status_color(limits):
    primary = (limits or {}).get("primary") or {}
    secondary = (limits or {}).get("secondary") or {}
    used_values = [v for v in (pct_value(primary), pct_value(secondary)) if v is not None]
    if not used_values:
        return "#7f8793"
    used = max(used_values)
    if used >= 90:
        return "#c46a6a"
    if used >= 80:
        return "#b8875f"
    if used >= 65:
        return "#a9955f"
    return "#7f9db7"


def render(limits, include_pct, compact=False):
    primary = (limits or {}).get("primary") or {}
    secondary = (limits or {}).get("secondary") or {}
    p_left = fmt_remaining(primary.get("resets_at"))
    s_left = fmt_remaining(secondary.get("resets_at"))
    if compact and include_pct:
        return f"5h {pct_text(primary)}% used | 7d {pct_text(secondary)}% used"
    if compact:
        return f"5h {p_left} | 7d {s_left}"
    if include_pct:
        return f"5h {pct_text(primary)}% used/{p_left} | 7d {pct_text(secondary)}% used/{s_left}"
    return f"5h {p_left} | 7d {s_left}"


def cmux_set(value, color):
    cmux = os.environ.get("CMUX_BUNDLED_CLI_PATH") or "cmux"
    cmd = [
        cmux,
        "set-status",
        "codex-limits",
        value,
        "--icon",
        "clock",
        "--color",
        color,
    ]
    return subprocess.run(cmd, check=False).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-pct", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--cmux-set", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()

    while True:
        path = pick_session()
        if not path:
            limits = None
            value = "5h u?%/? | 7d u?%/?"
            status = 1
        else:
            limits = latest_limits(path)
            value = render(limits, include_pct=args.with_pct, compact=args.compact)
            status = 0
        if args.cmux_set:
            status = cmux_set(value, status_color(limits))
        else:
            print(value, flush=True)
        if not args.watch:
            return status
        time.sleep(max(5, args.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
