#!/usr/bin/env python3
"""Pipeline usage stats: which skills are actually used, which are dead weight.

Sources (all local-only, nothing leaves the machine):
  * ~/.claude/history.jsonl       — typed prompts: display/project/timestamp/sessionId
  * ~/.claude/projects/<proj>/*.jsonl — session transcripts: Skill tool_use =
    auto-invocations by Claude, Task/Agent tool_use = sub-agent usage
  * .vault-meta/router-hits.jsonl — skill-router hint log (plus rotated .1)
  * .vault-meta/pipeline-events.jsonl — runtime-neutral script operations;
    paths/counters only, never prompts, queries, commands, or page content

v2 (2026-06-10): history.jsonl sees only what the user TYPES (/skill); skills
Claude invokes itself via trigger phrases appear only in transcripts as Skill
tool_use. The two are complementary — v1 undercounted real usage ~2x.

Reports per skill: typed + auto + total, last used, router hints, rough hint
precision (hint followed by same-skill invocation within 1h). Skills with
total 0 in the window are dead-weight candidates (their descriptions cost
system-prompt budget every session — see Size discipline in CLAUDE.md).
Also reports Task-tool agent usage (custom 9 + built-ins).

Usage:
    ./scripts/pipeline-stats.py [--days N] [--report]
    ./scripts/pipeline-stats.py --nudge [--days N]

--days: lookback window, default 30. Transcript coverage is bounded by
transcript retention (~30d), so larger windows only widen the typed source.
--report: also write wiki/meta/reports/pipeline-stats-YYYY-MM-DD.md
--nudge: cheap mode for the SessionStart nudge hook — reads ONLY router-hits +
command-log jsonl (no transcript/history scan, milliseconds). Prints one hint
line when the router matched wiki-query in the window (default 7d) but no
retrieval assist was invoked; prints nothing otherwise. Always exit 0.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
HISTORY = Path.home() / ".claude" / "history.jsonl"
# Claude Code project dir = path with every non-alphanumeric char dashed
TRANSCRIPT_DIR = (Path.home() / ".claude" / "projects"
                  / re.sub(r"[^A-Za-z0-9]", "-", str(VAULT_ROOT)))
ROUTER_LOGS = [VAULT_ROOT / ".vault-meta" / "router-hits.jsonl",
               VAULT_ROOT / ".vault-meta" / "router-hits.jsonl.1"]
COMMAND_LOGS = [VAULT_ROOT / ".vault-meta" / "command-log.jsonl",
                VAULT_ROOT / ".vault-meta" / "command-log.jsonl.1"]
EVENT_LOGS = [VAULT_ROOT / ".vault-meta" / "pipeline-events.jsonl",
              VAULT_ROOT / ".vault-meta" / "pipeline-events.jsonl.1"]

# Retrieval-assist markers in captured Bash commands (plan-trim hook writes
# command-log.jsonl). Deterministic check that /wiki-query SKILL instructions
# (tag prefilter + hybrid assist) are actually followed, not silently ignored.
ASSIST_MARKERS = [
    ("tag-search", "tag-search.py"),
    ("hybrid-search", "semantic-search.py"),
    ("bm25-query", "bm25-index.py query"),
]
SKILL_ROOTS = [VAULT_ROOT / "skills"]
# Custom Claude sub-agents shipped with this repo.
CUSTOM_AGENTS: set[str] = {"daily-summarizer"}


def installed_skills() -> set[str]:
    names = set()
    for root in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and (d / "SKILL.md").is_file() and not d.name.startswith("_"):
                names.add(d.name)
    return names


def iter_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def base_name(raw: str) -> str:
    """Strip plugin prefix: 'llm-obsidian:save' -> 'save'."""
    return raw.split(":")[-1].strip().lstrip("/")


def scan_transcripts(cutoff: dt.datetime):
    """Yield ('skill'|'agent', name, ts) events from session transcripts."""
    if not TRANSCRIPT_DIR.is_dir():
        return
    for f in sorted(TRANSCRIPT_DIR.glob("*.jsonl")):
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts_raw = (rec.get("timestamp") or "")[:19]
                try:
                    # transcript timestamps are UTC (trailing Z stripped) — convert
                    # to naive local so they compare with history/router times
                    ts = (dt.datetime.fromisoformat(ts_raw)
                          .replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None))
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                content = (rec.get("message") or {}).get("content") or []
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    tool = blk.get("name") or ""
                    inp = blk.get("input") or {}
                    if tool == "Skill":
                        yield "skill", base_name(str(inp.get("skill") or "")), ts
                    elif tool in ("Task", "Agent"):
                        yield "agent", base_name(str(inp.get("subagent_type") or "general-purpose")), ts


def parse_log_ts(raw) -> dt.datetime | None:
    """ts field from router-hits/command-log jsonl -> naive local datetime."""
    try:
        if isinstance(raw, str):
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            parsed = dt.datetime.fromtimestamp(raw / 1000 if raw > 1e12 else raw)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def nudge_check(days: int) -> str:
    """Cheap retrieval-discipline probe: wiki-query router hints vs assist calls.

    Router hints are a proxy for 'wiki lookups happened' (the hook logs a match
    whenever a prompt looks like a wiki query); command-log carries the actual
    assist invocations. Hints > 0 with assists == 0 across the window means the
    /wiki-query script steps are likely being skipped.
    """
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    hints = 0
    for log in ROUTER_LOGS:
        for rec in iter_jsonl(log):
            ts = parse_log_ts(rec.get("ts", 0))
            if ts is None or ts < cutoff:
                continue
            for m in rec.get("skill_matches", []) or []:
                name = m if isinstance(m, str) else (m.get("name") or m.get("skill") or "")
                if base_name(str(name)) == "wiki-query":
                    hints += 1
    if hints == 0:
        return ""
    for log in COMMAND_LOGS:
        for rec in iter_jsonl(log):
            ts = parse_log_ts(rec.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            cmd = str(rec.get("command", ""))
            if any(marker in cmd for _, marker in ASSIST_MARKERS):
                return ""
    return (f"router матчил wiki-query {hints}× за {days}д, но retrieval-ассисты "
            "(tag-search / semantic-search --hybrid / bm25 query) ни разу не вызывались — "
            "похоже, шаги /wiki-query пропускаются. Детали: scripts/pipeline-stats.py --days "
            f"{days}.")


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        try:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("usage: pipeline-stats.py [--days N] [--report|--nudge]", file=sys.stderr)
            return 2
    if "--nudge" in sys.argv:
        line = nudge_check(days if "--days" in sys.argv else 7)
        if line:
            print(line)
        return 0
    cutoff = dt.datetime.now() - dt.timedelta(days=days)

    skills = installed_skills()
    typed_count: dict[str, int] = defaultdict(int)
    auto_count: dict[str, int] = defaultdict(int)
    last_used: dict[str, dt.datetime] = {}
    inv_by_skill: dict[str, list[dt.datetime]] = defaultdict(list)
    agent_count: dict[str, int] = defaultdict(int)

    # Source 1: typed /skill invocations (history.jsonl)
    proj = str(VAULT_ROOT)
    total_prompts = 0
    for rec in iter_jsonl(HISTORY):
        if rec.get("project") != proj:
            continue
        ts_raw = rec.get("timestamp", 0)
        ts = dt.datetime.fromtimestamp(ts_raw / 1000 if ts_raw > 1e12 else ts_raw)
        if ts < cutoff:
            continue
        total_prompts += 1
        disp = str(rec.get("display", ""))
        if not disp.startswith("/"):
            continue
        name = base_name(disp.split()[0])
        if name in skills:
            typed_count[name] += 1
            last_used[name] = max(last_used.get(name, ts), ts)
            inv_by_skill[name].append(ts)

    # Source 2: auto-invocations + agent calls (transcripts)
    for kind, name, ts in scan_transcripts(cutoff):
        if kind == "skill":
            if name in skills:
                auto_count[name] += 1
                last_used[name] = max(last_used.get(name, ts), ts)
                inv_by_skill[name].append(ts)
        else:
            agent_count[name] += 1

    # Source 3: router hints + precision (hint -> invocation within 1h)
    hint_count: dict[str, int] = defaultdict(int)
    hint_followed: dict[str, int] = defaultdict(int)
    for log in ROUTER_LOGS:
        for rec in iter_jsonl(log):
            ts = parse_log_ts(rec.get("ts", 0))
            if ts is None or ts < cutoff:
                continue
            for m in rec.get("skill_matches", []) or []:
                name = m if isinstance(m, str) else (m.get("name") or m.get("skill") or "")
                if not name:
                    continue
                hint_count[name] += 1
                if any(0 <= (i - ts).total_seconds() <= 3600 for i in inv_by_skill.get(name, [])):
                    hint_followed[name] += 1

    # Source 4: retrieval-assist invocations (command-log.jsonl, Bash capture)
    assist_count: dict[str, int] = defaultdict(int)
    assist_last: dict[str, dt.datetime] = {}
    for log in COMMAND_LOGS:
        for rec in iter_jsonl(log):
            try:
                ts = dt.datetime.fromisoformat(str(rec.get("ts", "")))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            cmd = str(rec.get("command", ""))
            for kind, marker in ASSIST_MARKERS:
                if marker in cmd:
                    assist_count[kind] += 1
                    assist_last[kind] = max(assist_last.get(kind, ts), ts)

    # Source 5: content-free operations emitted by shared scripts in any runtime.
    operation_count: dict[tuple[str, str, str], int] = defaultdict(int)
    operation_last: dict[tuple[str, str, str], dt.datetime] = {}
    operation_durations: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for log in EVENT_LOGS:
        for rec in iter_jsonl(log):
            ts = parse_log_ts(rec.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            runtime = str(rec.get("runtime") or "unknown")
            if runtime not in {"claude", "codex", "unknown"}:
                runtime = "unknown"
            op = base_name(str(rec.get("op") or "unknown"))
            status = str(rec.get("status") or "unknown")
            key = (runtime, op, status)
            operation_count[key] += 1
            operation_last[key] = max(operation_last.get(key, ts), ts)
            counts = rec.get("counts")
            duration = counts.get("duration_ms") if isinstance(counts, dict) else None
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration >= 0
                and math.isfinite(duration)
            ):
                operation_durations[key].append(float(duration))

    totals = {n: typed_count.get(n, 0) + auto_count.get(n, 0)
              for n in set(typed_count) | set(auto_count)}
    used = sorted(totals.items(), key=lambda kv: -kv[1])
    dead = sorted(skills - set(totals))

    lines = [f"# Pipeline stats — last {days}d (prompts in project: {total_prompts})", ""]
    lines.append("## Runtime-neutral observed operations")
    lines.append("")
    lines.append(
        "These are content-free events from shared scripts. They measure executed operations, "
        "not skill invocation or hook parity."
    )
    lines.append("")
    if operation_count:
        lines.append("| Runtime | Operation | Status | Calls | P50 ms | P95 ms | Last observed |")
        lines.append("|---|---|---|---|---:|---:|---|")
        for key, count in sorted(operation_count.items(), key=lambda item: (-item[1], item[0])):
            runtime, op, status = key
            durations = sorted(operation_durations.get(key, []))
            p50 = f"{statistics.median(durations):.1f}" if durations else "-"
            p95 = f"{durations[max(0, math.ceil(len(durations) * 0.95) - 1)]:.1f}" if durations else "-"
            lines.append(
                f"| {runtime} | {op} | {status} | {count} | {p50} | {p95} | "
                f"{operation_last[key].strftime('%Y-%m-%d')} |"
            )
    else:
        lines.append("no runtime-neutral operations captured")
    lines.append("")
    lines.append("## Claude-only skill telemetry")
    lines.append("")
    lines.append(
        "Typed/Auto/router/assist columns below come from Claude history, transcripts, and "
        "Claude-specific hooks; they do not measure Codex skill usage."
    )
    lines.append("")
    lines.append("| Skill | Typed | Auto | Total | Last used | Router hints | Hint→use ≤1h |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, n in used:
        lu = last_used[name].strftime("%Y-%m-%d")
        h = hint_count.get(name, 0)
        f = hint_followed.get(name, 0)
        prec = f"{f}/{h}" if h else "-"
        lines.append(f"| /{name} | {typed_count.get(name, 0)} | {auto_count.get(name, 0)} "
                     f"| {n} | {lu} | {h} | {prec} |")
    lines.append("")
    lines.append(f"## Dead-weight candidates ({len(dead)} of {len(skills)} installed, "
                 f"0 invocations typed+auto in {days}d)")
    lines.append("")
    lines.append(", ".join(f"/{n}" for n in dead) if dead else "none")
    lines.append("")
    lines.append("## Agents usage (Task tool, transcripts)")
    lines.append("")
    if agent_count:
        lines.append("| Agent | Calls | |")
        lines.append("|---|---|---|")
        for name, n in sorted(agent_count.items(), key=lambda kv: -kv[1]):
            tag = "custom" if name in CUSTOM_AGENTS else "built-in"
            lines.append(f"| {name} | {n} | {tag} |")
        never = sorted(CUSTOM_AGENTS - set(agent_count))
        if never:
            lines.append("")
            lines.append("Custom agents with 0 calls: " + ", ".join(never))
    else:
        lines.append("no Task-tool calls found in transcripts")
    lines.append("")
    lines.append("## Retrieval assists (command-log.jsonl, Bash capture)")
    lines.append("")
    if assist_count:
        lines.append("| Assist | Calls | Last used |")
        lines.append("|---|---|---|")
        for kind, _ in ASSIST_MARKERS:
            n = assist_count.get(kind, 0)
            lu = assist_last[kind].strftime("%Y-%m-%d") if kind in assist_last else "-"
            lines.append(f"| {kind} | {n} | {lu} |")
    else:
        lines.append("no retrieval-assist invocations captured")
    wq_total = totals.get("wiki-query", 0)
    if wq_total > 0 and sum(assist_count.values()) == 0:
        lines.append("")
        lines.append(f"WARN: wiki-query ran {wq_total} times in {days}d but retrieval assists "
                     "(tag-search / semantic-search --hybrid) were never invoked — "
                     "SKILL instructions may be ignored.")
    lines.append("")
    lines.append("> Гочи интерпретации: (1) Typed = history.jsonl (что напечатал user), "
                 "Auto = Skill tool_use из транскриптов (что Claude вызвал сам) — источники "
                 "комплементарны; (2) покрытие транскриптов ограничено их retention (~30д); "
                 "(3) hint-precision грубая (окно 1ч, без привязки к сессии); (4) reference-скиллы "
                 "(obsidian-markdown/bases) и замороженные (canvas, wiki) по нулям — это норма.")

    out = "\n".join(lines)
    print(out)
    if "--report" in sys.argv:
        today = dt.date.today().isoformat()
        path = VAULT_ROOT / "wiki" / "meta" / "reports" / f"pipeline-stats-{today}.md"
        fm = (f"---\ntype: meta\ntitle: \"Pipeline Stats {today}\"\ncreated: {today}\n"
              f"updated: {today}\ntags: [meta, pipeline, stats]\nstatus: developing\n"
              "sessions: []\n---\n\n")
        content = fm + out + "\n"
        page = {
            "op": "update" if path.is_file() else "create",
            "path": str(path.relative_to(VAULT_ROOT)),
            "content": content,
        }
        if path.is_file():
            page["expected_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = {
            "actor": "pipeline-stats",
            "session": os.environ.get("CODEX_THREAD_ID")
            or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or "unknown",
            "pages": [page],
        }
        result = subprocess.run(
            [sys.executable, str(VAULT_ROOT / "scripts" / "vault-write.py")],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=VAULT_ROOT,
        )
        if result.returncode:
            print(result.stderr or result.stdout, end="", file=sys.stderr)
            return result.returncode
        print(f"\nreport written: {path.relative_to(VAULT_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
