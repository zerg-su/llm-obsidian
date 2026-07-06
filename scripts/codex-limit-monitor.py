#!/usr/bin/env python3
"""Live Codex CLI usage/limit monitor from local session JSONL files.

Codex writes session events under ~/.codex/sessions/YYYY/MM/DD/*.jsonl. Recent
CLI builds include event_msg/token_count records with token usage and rate-limit
metadata. This script watches the newest session file and renders a small
terminal dashboard, so the main Codex TUI can stay unmodified.

Usage:
    ./scripts/codex-limit-monitor.py --install
    codex-limit-status
    ./scripts/codex-limit-monitor.py
    ./scripts/codex-limit-monitor.py --sessions 8
    ./scripts/codex-limit-monitor.py --scope recent
    ./scripts/codex-limit-monitor.py --once
    ./scripts/codex-limit-monitor.py --plain --once
    ./scripts/codex-limit-monitor.py --session ~/.codex/sessions/...jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - macOS system Python can be old
    tomllib = None


DEFAULT_INSTALL_NAME = "codex-limit-status"


@dataclass
class Snapshot:
    session_file: Path | None = None
    file_mtime: dt.datetime | None = None
    session_id: str = ""
    cwd: str = ""
    model: str = ""
    effort: str = ""
    provider: str = ""
    plan_type: str = ""
    updated_at: dt.datetime | None = None
    total_usage: dict[str, int] | None = None
    last_usage: dict[str, int] | None = None
    context_window: int | None = None
    rate_limits: dict[str, Any] | None = None
    error: str = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--install", action="store_true",
                    help=f"Install this script as {DEFAULT_INSTALL_NAME} in ~/.local/bin")
    ap.add_argument("--install-dir", default="~/.local/bin",
                    help="Directory used by --install (default: ~/.local/bin)")
    ap.add_argument("--install-name", default=DEFAULT_INSTALL_NAME,
                    help=f"Command name used by --install (default: {DEFAULT_INSTALL_NAME})")
    ap.add_argument("--force-install", action="store_true",
                    help="Replace an existing installed file or symlink")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", "~/.codex"),
                    help="Codex home directory (default: $CODEX_HOME or ~/.codex)")
    ap.add_argument("--session", type=Path,
                    help="Specific Codex session JSONL to watch")
    ap.add_argument("--sessions", type=int, default=5,
                    help="Maximum number of sessions to show in dashboard mode (default: 5)")
    ap.add_argument("--scope", choices=("open", "recent"), default="open",
                    help="Show only currently open Codex sessions, or recent files (default: open)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Refresh interval in seconds (default: 1.0)")
    ap.add_argument("--once", action="store_true",
                    help="Print one snapshot and exit")
    ap.add_argument("--plain", action="store_true",
                    help="Render plain text instead of the colored dashboard")
    ap.add_argument("--no-color", action="store_true",
                    help="Disable ANSI colors but keep the dashboard layout")
    ap.add_argument("--color", action="store_true",
                    help="Force ANSI colors even when stdout is not a TTY")
    return ap.parse_args(argv)


def install_self(install_dir: str, install_name: str, force: bool = False) -> int:
    source = Path(__file__).resolve()
    dest_dir = Path(install_dir).expanduser().resolve()
    dest = dest_dir / install_name

    if not install_name or "/" in install_name:
        print(f"ERROR: invalid install name: {install_name!r}", file=sys.stderr)
        return 2

    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        mode = source.stat().st_mode
        source.chmod(mode | 0o755)
    except OSError as exc:
        print(f"ERROR: cannot mark source executable: {exc}", file=sys.stderr)
        return 1

    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() and dest.resolve() == source:
            print(f"already installed: {dest} -> {source}")
            return warn_path(dest_dir, install_name)
        if not force:
            print(
                f"ERROR: {dest} already exists and points elsewhere. "
                "Use --force-install to replace it.",
                file=sys.stderr,
            )
            return 1
        if dest.is_dir():
            print(f"ERROR: {dest} is a directory; refusing to replace it", file=sys.stderr)
            return 1
        dest.unlink()

    dest.symlink_to(source)
    print(f"installed: {dest} -> {source}")
    return warn_path(dest_dir, install_name)


def warn_path(dest_dir: Path, install_name: str) -> int:
    path_dirs = [Path(p).expanduser().resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if dest_dir not in path_dirs:
        print(f"WARNING: {dest_dir} is not in PATH; add it to your shell config.")
    elif shutil.which(install_name):
        print(f"run: {install_name}")
    else:
        print(f"run: {dest_dir / install_name}")
    return 0


def codex_home(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def latest_session(home: Path) -> Path | None:
    sessions = latest_sessions(home, 1)
    return sessions[0] if sessions else None


def latest_sessions(home: Path, limit: int) -> list[Path]:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return []
    candidates = [p for p in sessions.glob("**/*.jsonl") if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:max(1, limit)]


def open_sessions(home: Path, limit: int) -> list[Path]:
    """Return session JSONL files currently held open by live codex processes."""
    sessions_dir = home / "sessions"
    try:
        proc = subprocess.run(
            ["lsof", "-Fn", "-c", "codex"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    for line in proc.stdout.splitlines():
        if not line.startswith("n"):
            continue
        raw = line[1:]
        if not raw.endswith(".jsonl"):
            continue
        path = Path(raw)
        try:
            path.relative_to(sessions_dir)
        except ValueError:
            continue
        if path not in seen and path.is_file():
            seen.add(path)
            paths.append(path)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:max(1, limit)]


def parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return dt.datetime.fromisoformat(raw[:-1] + "+00:00").astimezone()
        parsed = dt.datetime.fromisoformat(raw)
        return parsed.astimezone() if parsed.tzinfo else parsed
    except ValueError:
        return None


def read_config_model(home: Path) -> str:
    cfg = home / "config.toml"
    if not cfg.is_file():
        return ""
    if tomllib is not None:
        try:
            data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            return str(data.get("model") or "")
        except (OSError, tomllib.TOMLDecodeError):
            return ""
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("model"):
            return line.partition("=")[2].strip().strip("\"'")
    return ""


def parse_session(path: Path, fallback_model: str = "") -> Snapshot:
    snap = Snapshot(session_file=path, model=fallback_model)
    try:
        snap.file_mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        pass
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        snap.error = f"cannot read session file: {exc}"
        return snap

    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            snap.updated_at = parse_ts(rec.get("timestamp")) or snap.updated_at
            payload = rec.get("payload") or {}
            if rec.get("type") == "session_meta":
                snap.session_id = str(payload.get("id") or snap.session_id)
                snap.cwd = str(payload.get("cwd") or snap.cwd)
                snap.provider = str(payload.get("model_provider") or snap.provider)
                continue
            if rec.get("type") == "turn_context":
                snap.cwd = str(payload.get("cwd") or snap.cwd)
                snap.model = str(payload.get("model") or snap.model)
                snap.effort = str(payload.get("effort") or snap.effort)
                continue
            if rec.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            snap.total_usage = dict(info.get("total_token_usage") or {})
            snap.last_usage = dict(info.get("last_token_usage") or {})
            snap.context_window = info.get("model_context_window")
            snap.rate_limits = dict(payload.get("rate_limits") or {})
            snap.plan_type = str(snap.rate_limits.get("plan_type") or snap.plan_type)
    return snap


def pct(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"{float(n):.1f}%"


def fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "n/a"


def fmt_compact(n: Any) -> str:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.1f}m"
    if value >= 10_000:
        return f"{sign}{round(value / 1000):.0f}k"
    if value >= 1_000:
        return f"{sign}{value / 1000:.1f}k"
    return f"{sign}{value}"


def progress_bar(used_percent: float | None, width: int = 32,
                 fill: str = "#", empty: str = ".") -> str:
    if used_percent is None:
        return "[" + "?" * width + "]"
    used = max(0.0, min(100.0, float(used_percent)))
    fill_len = round(width * used / 100.0)
    if used > 0 and fill_len == 0:
        fill_len = 1
    return "[" + fill * fill_len + empty * (width - fill_len) + "]"


def fmt_reset(epoch: Any) -> str:
    try:
        ts = dt.datetime.fromtimestamp(float(epoch)).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return "n/a"
    remaining = ts - dt.datetime.now().astimezone()
    if remaining.total_seconds() <= 0:
        rel = "now"
    else:
        minutes = int(remaining.total_seconds() // 60)
        if minutes >= 24 * 60:
            days = minutes // (24 * 60)
            hours = (minutes % (24 * 60)) // 60
            rel = f"in {days}d {hours}h {minutes % 60:02d}m"
        elif minutes >= 60:
            rel = f"in {minutes // 60}h {minutes % 60:02d}m"
        else:
            rel = f"in {minutes}m"
    return f"{ts:%H:%M:%S} ({rel})"


def rate_line(label: str, item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return f"{label:<9} n/a"
    used = item.get("used_percent")
    window = item.get("window_minutes")
    window_text = f"{int(window / 60)}h" if isinstance(window, int) and window >= 60 else f"{window}m"
    return (f"{label:<9} {progress_bar(used)} {pct(used):>7}  "
            f"window {window_text:<5} reset {fmt_reset(item.get('resets_at'))}")


def usage_table(title: str, usage: dict[str, Any] | None) -> list[str]:
    usage = usage or {}
    return [
        title,
        f"  input     {fmt_int(usage.get('input_tokens'))}",
        f"  cached    {fmt_int(usage.get('cached_input_tokens'))}",
        f"  output    {fmt_int(usage.get('output_tokens'))}",
        f"  reasoning {fmt_int(usage.get('reasoning_output_tokens'))}",
        f"  total     {fmt_int(usage.get('total_tokens'))}",
    ]


class Palette:
    THEMES = {
        "orange": (226, 126, 76),
        "yellow": (218, 190, 86),
        "green": (79, 214, 143),
        "cyan": (78, 210, 218),
        "blue": (104, 156, 228),
        "magenta": (206, 94, 214),
        "olive": (109, 136, 92),
    }

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def c(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        # Reset foreground/intensity only; terminal background stays untouched.
        return f"\033[{code}m{text}\033[22;39m"

    def rgb(self, color: tuple[int, int, int], text: str) -> str:
        if not self.enabled:
            return text
        r, g, b = color
        return f"\033[38;2;{r};{g};{b}m{text}\033[22;39m"

    def bg(self, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[48;5;0m{text}\033[0m"

    def dim(self, text: str) -> str:
        return self.rgb((93, 97, 95), text)

    def text(self, text: str) -> str:
        return self.rgb((210, 213, 207), text)

    def label(self, text: str) -> str:
        return self.text(text)

    def cyan(self, text: str) -> str:
        return self.rgb((130, 173, 176), text)

    def blue(self, text: str) -> str:
        return self.rgb((126, 154, 168), text)

    def green(self, text: str) -> str:
        return self.rgb((163, 198, 112), text)

    def yellow(self, text: str) -> str:
        return self.rgb((226, 212, 128), text)

    def magenta(self, text: str) -> str:
        return self.rgb((199, 130, 147), text)

    def red(self, text: str) -> str:
        return self.rgb((202, 82, 82), text)

    def title(self, text: str) -> str:
        return self.rgb((178, 224, 214), text)

    def border(self, text: str) -> str:
        return self.rgb((109, 136, 92), text)

    def accent(self, text: str) -> str:
        return self.rgb((202, 82, 82), text)

    def track(self, text: str) -> str:
        return self.rgb((43, 46, 44), text)

    def bar_red(self, text: str) -> str:
        return self.rgb((181, 77, 87), text)

    def bar_red_hot(self, text: str) -> str:
        return self.rgb((219, 104, 113), text)

    def bar_green(self, text: str) -> str:
        return self.rgb((142, 188, 96), text)

    def theme(self, name: str, text: str) -> str:
        return self.rgb(self.THEMES.get(name, self.THEMES["olive"]), text)


def strip_ansi(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        if text[i:i + 2] == "\033[":
            i += 2
            while i < len(text) and text[i] != "m":
                i += 1
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def cpad(text: str, width: int, align: str = "left") -> str:
    if visible_len(text) > width:
        text = ellipsize_visible(text, width)
    pad = max(0, width - visible_len(text))
    if align == "right":
        return " " * pad + text
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    return text + " " * pad


def ellipsize_visible(text: str, limit: int) -> str:
    plain = strip_ansi(text)
    if len(plain) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return plain[:limit - 1] + "…"


def panel(title: str, body: list[str], width: int, pal: Palette, theme: str = "olive") -> list[str]:
    width = max(42, width)
    inner = width - 4
    title_text = f" {title} "
    title_text = title_text[:max(0, width - 4)]
    left = 2
    right = max(0, width - 2 - left - len(title_text))
    lines = [
        pal.theme(theme, "┌")
        + pal.theme(theme, "─" * left)
        + " "
        + pal.theme(theme, title)
        + " "
        + pal.theme(theme, "─" * right)
        + pal.theme(theme, "┐")
    ]
    for raw in body:
        if visible_len(raw) > inner:
            raw = ellipsize_visible(raw, inner)
        pad = max(0, inner - visible_len(raw))
        lines.append(pal.theme(theme, "│ ") + raw + " " * pad + pal.theme(theme, " │"))
    lines.append(pal.theme(theme, "└" + "─" * (width - 2) + "┘"))
    return lines


def paint_screen(lines: list[str], width: int, pal: Palette) -> str:
    return "\n".join(lines)


def color_for_percent(value: float | None, pal: Palette, kind: str = "context"):
    if value is None:
        return pal.dim
    v = float(value)
    if kind == "limit":
        if v >= 90:
            return pal.red
        if v >= 70:
            return pal.yellow
        return pal.green
    if v >= 80:
        return pal.red
    if v >= 60:
        return pal.yellow
    return pal.green


def meter(value: float | None, width: int, pal: Palette, kind: str = "context") -> str:
    if value is None:
        return pal.border("[") + pal.dim("?" * width) + pal.border("]")
    used = max(0.0, min(100.0, float(value)))
    fill_len = round(width * used / 100.0)
    if used > 0 and fill_len == 0:
        fill_len = 1
    fill_color = color_for_percent(used, pal, kind=kind)
    fill = fill_color("━" * fill_len)
    empty = pal.track("·" * (width - fill_len))
    return pal.border("[") + fill + empty + pal.border("]")


def colored_bar(value: float | None, width: int, pal: Palette, kind: str = "context") -> str:
    return meter(value, width, pal, kind=kind)


def percent_text(value: float | None, pal: Palette, kind: str = "context") -> str:
    if value is None:
        return pal.dim("n/a")
    color = color_for_percent(float(value), pal, kind=kind)
    return color(f"{float(value):.1f}%")


def project_name(cwd: str) -> str:
    return Path(cwd).name if cwd else "n/a"


def model_label(snap: Snapshot) -> str:
    model = snap.model or "n/a"
    if snap.effort:
        model = f"{model}:{snap.effort}"
    return model


def ellipsize(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[:limit - 3] + "..."


def context_percent(snap: Snapshot) -> float | None:
    total = (snap.last_usage or {}).get("total_tokens")
    if not total or not snap.context_window:
        return None
    return int(total) * 100.0 / int(snap.context_window)


def age_text(when: dt.datetime | None) -> str:
    if when is None:
        return "n/a"
    delta = dt.datetime.now().astimezone() - when
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def limit_body(name: str, item: dict[str, Any] | None, pal: Palette) -> list[str]:
    if not isinstance(item, dict):
        return [f"{name:<9} {pal.dim('n/a')}"]
    used = item.get("used_percent")
    return [
        f"{pal.label(name):<4} {meter(used, 30, pal, kind='limit')} "
        f"{percent_text(used, pal, kind='limit').rjust(7)}  "
        f"{pal.label('reset')} {pal.yellow(fmt_reset(item.get('resets_at')))}",
    ]


def usage_rows(title: str, usage: dict[str, Any] | None, pal: Palette) -> list[str]:
    usage = usage or {}
    return (
        f"{cpad(pal.title(title), 18)} "
        f"{cpad(pal.cyan(fmt_int(usage.get('input_tokens'))), 12, 'right')} "
        f"{cpad(pal.blue(fmt_int(usage.get('cached_input_tokens'))), 12, 'right')} "
        f"{cpad(pal.green(fmt_int(usage.get('output_tokens'))), 9, 'right')} "
        f"{cpad(pal.magenta(fmt_int(usage.get('reasoning_output_tokens'))), 9, 'right')} "
        f"{cpad(pal.yellow(fmt_int(usage.get('total_tokens'))), 12, 'right')}"
    )


def sessions_rows(snaps: list[Snapshot], pal: Palette) -> list[str]:
    rows = [
        " ".join([
            cpad(pal.dim("*"), 1),
            cpad(pal.dim("#"), 2, "right"),
            cpad(pal.dim("project"), 16),
            cpad(pal.dim("model"), 14),
            cpad(pal.dim("ctx"), 18),
            cpad(pal.dim("%"), 6, "right"),
            cpad(pal.dim("last"), 7, "right"),
            cpad(pal.dim("total"), 8, "right"),
            cpad(pal.dim("age"), 5, "right"),
        ])
    ]
    for idx, snap in enumerate(snaps, 1):
        mark = pal.accent("*") if idx == 1 else " "
        proj = pal.title(ellipsize(project_name(snap.cwd), 16))
        model = pal.green(ellipsize(model_label(snap), 14))
        ctx_pct = context_percent(snap)
        last_total = (snap.last_usage or {}).get("total_tokens")
        session_total = (snap.total_usage or {}).get("total_tokens")
        rows.append(" ".join([
            cpad(mark, 1),
            cpad(str(idx), 2, "right"),
            cpad(proj, 16),
            cpad(model, 14),
            cpad(colored_bar(ctx_pct, 16, pal, kind="context"), 18),
            cpad(percent_text(ctx_pct, pal), 6, "right"),
            cpad(pal.yellow(fmt_compact(last_total)), 7, "right"),
            cpad(pal.cyan(fmt_compact(session_total)), 8, "right"),
            cpad(pal.dim(age_text(snap.updated_at or snap.file_mtime)), 5, "right"),
        ]))
    return rows


def render_dashboard(snaps: list[Snapshot], color: bool = True, scope: str = "open") -> str:
    pal = Palette(color)
    if not snaps:
        msg = "No open Codex session files found."
        if scope == "open":
            msg += " Start Codex or use --scope recent to inspect saved sessions."
        else:
            msg = "No Codex session files found."
        return paint_screen(panel("Codex Limit Monitor", [pal.red(msg)], 96, pal), 96, pal)
    snap = snaps[0]
    if snap.error:
        return paint_screen(panel("Codex Limit Monitor", [pal.red(f"ERROR: {snap.error}")], 78, pal, "orange"), 78, pal)

    width = min(max(os.get_terminal_size().columns if sys.stdout.isatty() else 96, 78), 120)
    limits = snap.rate_limits or {}
    model = model_label(snap)
    updated = f"{snap.updated_at:%H:%M:%S %Z}" if snap.updated_at else "n/a"
    session_file = ellipsize(snap.session_file.name if snap.session_file else "n/a", 38)
    header = [
        f"{pal.label('project')} [{pal.title(project_name(snap.cwd))}]   "
        f"{pal.label('model')} {pal.green(model)}   "
        f"{pal.label('plan')} {pal.yellow(snap.plan_type or 'n/a')}   "
        f"{pal.label('provider')} {pal.text(snap.provider or 'n/a')}",
        f"{pal.label('session')} {pal.text(snap.session_id or 'n/a')}",
        f"{pal.label('updated')} {pal.text(updated)}   {pal.label(scope)} {pal.green(str(len(snaps)))} sessions   "
        f"{pal.label('file')} {pal.dim(session_file)}",
    ]

    rate = []
    rate.extend(limit_body("5h", limits.get("primary"), pal))
    rate.extend(limit_body("7d", limits.get("secondary"), pal))
    if limits.get("rate_limit_reached_type"):
        rate.append(f"{pal.red('reached')} {limits.get('rate_limit_reached_type')}")

    usage = [
        " ".join([
            cpad(pal.dim("scope"), 18),
            cpad(pal.dim("input"), 12, "right"),
            cpad(pal.dim("cached"), 12, "right"),
            cpad(pal.dim("output"), 9, "right"),
            cpad(pal.dim("reason"), 9, "right"),
            cpad(pal.dim("total"), 12, "right"),
        ]),
        usage_rows("last turn", snap.last_usage, pal),
        usage_rows("session", snap.total_usage, pal),
    ]

    lines: list[str] = []
    lines.extend(panel("Codex Limit Monitor", header, width, pal, "orange"))
    lines.extend(panel("Rate Limits", rate, width, pal, "yellow"))
    session_title = "Open Sessions" if scope == "open" else "Recent Sessions"
    lines.extend(panel(session_title, sessions_rows(snaps, pal), width, pal, "green"))
    lines.extend(panel("Tokens", usage, width, pal, "magenta"))
    lines.append(pal.dim("Refreshes every second. * = newest visible session. Use --sessions N, --scope recent, --session PATH, or --plain."))
    return paint_screen(lines, width, pal)


def render_plain(snap: Snapshot) -> str:
    if snap.error:
        return f"Codex Limit Monitor\n\nERROR: {snap.error}"
    lines = ["Codex Limit Monitor", ""]
    lines.append(f"session  {snap.session_id or 'n/a'}")
    lines.append(f"file     {snap.session_file or 'n/a'}")
    lines.append(f"cwd      {snap.cwd or 'n/a'}")
    model = snap.model or "n/a"
    if snap.effort:
        model = f"{model}:{snap.effort}"
    lines.append(f"model    {model}"
                 f"{' / ' + snap.provider if snap.provider else ''}")
    lines.append(f"plan     {snap.plan_type or 'n/a'}")
    lines.append(f"updated  {snap.updated_at:%Y-%m-%d %H:%M:%S %Z}" if snap.updated_at
                 else "updated  n/a")
    lines.append("")
    lines.append(f"context  {fmt_int((snap.last_usage or {}).get('total_tokens'))}"
                 f" / {fmt_int(snap.context_window)}")
    lines.append("")
    limits = snap.rate_limits or {}
    lines.append("Rate limits")
    lines.append(rate_line("primary", limits.get("primary")))
    lines.append(rate_line("secondary", limits.get("secondary")))
    if limits.get("rate_limit_reached_type"):
        lines.append(f"reached   {limits.get('rate_limit_reached_type')}")
    if limits.get("credits") is not None:
        lines.append(f"credits   {limits.get('credits')}")
    lines.append("")
    lines.extend(usage_table("Current session total", snap.total_usage))
    lines.append("")
    lines.extend(usage_table("Last turn", snap.last_usage))
    lines.append("")
    lines.append("Press Ctrl-C to exit. Use --once for a single snapshot.")
    return "\n".join(lines)


def snapshot(home: Path, session: Path | None, scope: str) -> Snapshot:
    if session:
        path = session.expanduser()
    elif scope == "open":
        open_paths = open_sessions(home, 1)
        path = open_paths[0] if open_paths else None
    else:
        path = latest_session(home)
    if path is None:
        if scope == "open":
            return Snapshot(error="no open Codex session files found; use --scope recent to inspect saved sessions")
        return Snapshot(error=f"no Codex sessions found under {home / 'sessions'}")
    return parse_session(path, read_config_model(home))


def snapshots(home: Path, session: Path | None, limit: int, scope: str) -> list[Snapshot]:
    fallback_model = read_config_model(home)
    if session:
        paths = [session.expanduser()]
    elif scope == "open":
        paths = open_sessions(home, limit)
    else:
        paths = latest_sessions(home, limit)
    if not paths:
        return []
    return [parse_session(path, fallback_model) for path in paths]


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.install:
        return install_self(args.install_dir, args.install_name, args.force_install)
    home = codex_home(args.codex_home)
    color = args.color or ((not args.no_color) and sys.stdout.isatty())
    if args.no_color:
        color = False
    if args.once:
        if args.plain:
            print(render_plain(snapshot(home, args.session, args.scope)))
        else:
            print(render_dashboard(
                snapshots(home, args.session, args.sessions, args.scope),
                color=color,
                scope=args.scope,
            ))
        return 0
    try:
        while True:
            print("\033[2J\033[H", end="")
            if args.plain:
                rendered = render_plain(snapshot(home, args.session, args.scope))
            else:
                rendered = render_dashboard(
                    snapshots(home, args.session, args.sessions, args.scope),
                    color=color,
                    scope=args.scope,
                )
            print(rendered, flush=True)
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
