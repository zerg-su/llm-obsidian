#!/usr/bin/env python3
"""Local-only evidence collection for pipeline usage statistics."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterator

from pipeline_stats_model import (
    AGENT_DRIVEN_OPS,
    LIFECYCLE_OPS,
    StatsSnapshot,
    normalize_runtime,
)


VAULT_ROOT = Path(__file__).resolve().parent.parent
HISTORY = Path.home() / ".claude" / "history.jsonl"
TRANSCRIPT_DIR = (
    Path.home()
    / ".claude"
    / "projects"
    / re.sub(r"[^A-Za-z0-9]", "-", str(VAULT_ROOT))
)
ROUTER_LOGS = [
    VAULT_ROOT / ".vault-meta" / "router-hits.jsonl",
    VAULT_ROOT / ".vault-meta" / "router-hits.jsonl.1",
]
COMMAND_LOGS = [
    VAULT_ROOT / ".vault-meta" / "command-log.jsonl",
    VAULT_ROOT / ".vault-meta" / "command-log.jsonl.1",
]
EVENT_LOGS = [
    VAULT_ROOT / ".vault-meta" / "pipeline-events.jsonl",
    VAULT_ROOT / ".vault-meta" / "pipeline-events.jsonl.1",
]
ASSIST_MARKERS = [
    ("tag-search", "tag-search.py"),
    ("hybrid-search", "semantic-search.py"),
    ("bm25-query", "bm25-index.py query"),
]
SKILL_ROOTS = [VAULT_ROOT / "skills"]
CUSTOM_AGENTS: set[str] = {"daily-summarizer"}


def installed_skills() -> set[str]:
    names = set()
    for root in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for directory in root.iterdir():
            if (
                directory.is_dir()
                and (directory / "SKILL.md").is_file()
                and not directory.name.startswith("_")
            ):
                names.add(directory.name)
    return names


def iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def base_name(raw: str) -> str:
    """Strip plugin prefix: 'llm-obsidian:save' -> 'save'."""
    return raw.split(":")[-1].strip().lstrip("/")


def scan_transcripts(cutoff: dt.datetime) -> Iterator[tuple[str, str, dt.datetime]]:
    """Yield skill and agent events from session transcripts."""
    if not TRANSCRIPT_DIR.is_dir():
        return
    for transcript in sorted(TRANSCRIPT_DIR.glob("*.jsonl")):
        try:
            handle = transcript.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"tool_use"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                try:
                    timestamp = (
                        dt.datetime.fromisoformat((record.get("timestamp") or "")[:19])
                        .replace(tzinfo=dt.timezone.utc)
                        .astimezone()
                        .replace(tzinfo=None)
                    )
                except ValueError:
                    continue
                if timestamp < cutoff:
                    continue
                content = (record.get("message") or {}).get("content") or []
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool = block.get("name") or ""
                    tool_input = block.get("input") or {}
                    if tool == "Skill":
                        yield "skill", base_name(str(tool_input.get("skill") or "")), timestamp
                    elif tool in ("Task", "Agent"):
                        raw = str(tool_input.get("subagent_type") or "general-purpose")
                        yield "agent", base_name(raw), timestamp


def parse_log_ts(raw: object) -> dt.datetime | None:
    try:
        if isinstance(raw, str):
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            numeric = float(raw)
            parsed = dt.datetime.fromtimestamp(numeric / 1000 if numeric > 1e12 else numeric)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _collect_history(snapshot: StatsSnapshot, cutoff: dt.datetime, invocations: dict) -> None:
    for record in iter_jsonl(HISTORY):
        if record.get("project") != str(VAULT_ROOT):
            continue
        raw = record.get("timestamp", 0)
        timestamp = dt.datetime.fromtimestamp(raw / 1000 if raw > 1e12 else raw)
        if timestamp < cutoff:
            continue
        snapshot.total_prompts += 1
        snapshot.runtime_activity["claude"] += 1
        display = str(record.get("display", ""))
        if not display.startswith("/"):
            continue
        name = base_name(display.split()[0])
        if name in snapshot.skills:
            snapshot.typed_count[name] += 1
            snapshot.last_used[name] = max(snapshot.last_used.get(name, timestamp), timestamp)
            invocations[name].append(timestamp)


def _collect_transcripts(snapshot: StatsSnapshot, cutoff: dt.datetime, invocations: dict) -> None:
    for kind, name, timestamp in scan_transcripts(cutoff):
        snapshot.runtime_activity["claude"] += 1
        if kind == "skill" and name in snapshot.skills:
            snapshot.auto_count[name] += 1
            snapshot.last_used[name] = max(snapshot.last_used.get(name, timestamp), timestamp)
            invocations[name].append(timestamp)
        elif kind == "agent":
            snapshot.agent_count[name] += 1


def _collect_router(snapshot: StatsSnapshot, cutoff: dt.datetime, invocations: dict) -> None:
    for log in ROUTER_LOGS:
        for record in iter_jsonl(log):
            timestamp = parse_log_ts(record.get("ts", 0))
            if timestamp is None or timestamp < cutoff:
                continue
            runtime = normalize_runtime(record.get("runtime"))
            snapshot.runtime_activity[runtime] += 1
            for match in record.get("skill_matches", []) or []:
                name = match if isinstance(match, str) else (match.get("name") or match.get("skill") or "")
                if not name:
                    continue
                snapshot.hint_count[name] += 1
                snapshot.hint_runtimes[base_name(str(name))].add(runtime)
                if any(0 <= (used - timestamp).total_seconds() <= 3600 for used in invocations.get(name, [])):
                    snapshot.hint_followed[name] += 1


def _collect_assists(snapshot: StatsSnapshot, cutoff: dt.datetime) -> None:
    for log in COMMAND_LOGS:
        for record in iter_jsonl(log):
            try:
                timestamp = dt.datetime.fromisoformat(str(record.get("ts", "")))
            except ValueError:
                continue
            if timestamp < cutoff:
                continue
            command = str(record.get("command", ""))
            for kind, marker in ASSIST_MARKERS:
                if marker in command:
                    snapshot.assist_count[kind] += 1
                    snapshot.assist_last[kind] = max(
                        snapshot.assist_last.get(kind, timestamp), timestamp
                    )


def _collect_events(snapshot: StatsSnapshot, cutoff: dt.datetime) -> None:
    for log in EVENT_LOGS:
        for record in iter_jsonl(log):
            timestamp = parse_log_ts(record.get("ts", ""))
            if timestamp is None or timestamp < cutoff:
                continue
            runtime = normalize_runtime(record.get("runtime"))
            snapshot.runtime_activity[runtime] += 1
            operation = base_name(str(record.get("op") or "unknown"))
            status = str(record.get("status") or "unknown")
            actor = str(record.get("actor") or "unknown").split(":", 1)[0]
            role = actor if actor in {"coordinator", "task", "reviewer"} else "unknown"
            key = (runtime, operation, status)
            snapshot.operation_count[key] += 1
            snapshot.operation_last[key] = max(
                snapshot.operation_last.get(key, timestamp), timestamp
            )
            if operation in {"model-turn", "model-turn-incomplete"}:
                outcome = "completed" if operation == "model-turn" else "incomplete"
                snapshot.turn_count[(runtime, role, outcome)] += 1
            if runtime == "unknown" and operation in AGENT_DRIVEN_OPS:
                snapshot.unattributed_agent_activity += 1
            if operation in LIFECYCLE_OPS:
                snapshot.lifecycle_events.append(record)
            counts = record.get("counts")
            duration = counts.get("duration_ms") if isinstance(counts, dict) else None
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration >= 0
                and math.isfinite(duration)
            ):
                snapshot.operation_durations[key].append(float(duration))
                if operation == "model-turn":
                    snapshot.turn_durations[(runtime, role)].append(float(duration))


def collect_snapshot(days: int) -> StatsSnapshot:
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    snapshot = StatsSnapshot(days=days, skills=installed_skills())
    snapshot.runtime_activity = defaultdict(int)
    snapshot.hint_runtimes = defaultdict(set)
    snapshot.typed_count = defaultdict(int)
    snapshot.auto_count = defaultdict(int)
    snapshot.agent_count = defaultdict(int)
    snapshot.hint_count = defaultdict(int)
    snapshot.hint_followed = defaultdict(int)
    snapshot.assist_count = defaultdict(int)
    snapshot.operation_count = defaultdict(int)
    snapshot.operation_durations = defaultdict(list)
    snapshot.turn_count = defaultdict(int)
    snapshot.turn_durations = defaultdict(list)
    invocations: dict[str, list[dt.datetime]] = defaultdict(list)
    _collect_history(snapshot, cutoff, invocations)
    _collect_transcripts(snapshot, cutoff, invocations)
    _collect_router(snapshot, cutoff, invocations)
    _collect_assists(snapshot, cutoff)
    _collect_events(snapshot, cutoff)
    return snapshot


def nudge_check(days: int) -> str:
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    hints = 0
    for log in ROUTER_LOGS:
        for record in iter_jsonl(log):
            timestamp = parse_log_ts(record.get("ts", 0))
            if timestamp is None or timestamp < cutoff:
                continue
            for match in record.get("skill_matches", []) or []:
                name = match if isinstance(match, str) else (match.get("name") or match.get("skill") or "")
                if base_name(str(name)) == "wiki-query":
                    hints += 1
    if hints == 0:
        return ""
    for log in COMMAND_LOGS:
        for record in iter_jsonl(log):
            timestamp = parse_log_ts(record.get("ts", ""))
            if timestamp is None or timestamp < cutoff:
                continue
            command = str(record.get("command", ""))
            if any(marker in command for _, marker in ASSIST_MARKERS):
                return ""
    return (
        f"router матчил wiki-query {hints}× за {days}д, но retrieval-ассисты "
        "(tag-search / semantic-search --hybrid / bm25 query) ни разу не вызывались — "
        "похоже, шаги /wiki-query пропускаются. Детали: scripts/pipeline-stats.py --days "
        f"{days}."
    )
