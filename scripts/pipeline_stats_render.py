#!/usr/bin/env python3
"""Markdown section rendering for pipeline usage statistics."""

from __future__ import annotations

import math
import statistics

from pipeline_stats_model import (
    AggregatedStats,
    SKILL_CAPABLE_RUNTIMES,
    SKILL_OBSERVABLE_RUNTIMES,
    event_count,
    percentile,
    seconds,
)
from pipeline_stats_sources import ASSIST_MARKERS, CUSTOM_AGENTS
from review_contract import SEVERITIES


def _operations(stats: AggregatedStats, lines: list[str]) -> None:
    snapshot = stats.snapshot
    lines.extend([
        "## Runtime-neutral observed operations",
        "",
        "These are content-free events from shared scripts. They measure executed operations, "
        "not skill invocation or hook parity.",
        "",
    ])
    if not snapshot.operation_count:
        lines.extend(["no runtime-neutral operations captured", ""])
        return
    lines.extend([
        "| Runtime | Operation | Status | Calls | P50 ms | P95 ms | Last observed |",
        "|---|---|---|---|---:|---:|---|",
    ])
    for key, count in sorted(snapshot.operation_count.items(), key=lambda item: (-item[1], item[0])):
        runtime, operation, status = key
        durations = sorted(snapshot.operation_durations.get(key, []))
        p50 = f"{statistics.median(durations):.1f}" if durations else "-"
        p95 = f"{durations[max(0, math.ceil(len(durations) * 0.95) - 1)]:.1f}" if durations else "-"
        lines.append(
            f"| {runtime} | {operation} | {status} | {count} | {p50} | {p95} | "
            f"{snapshot.operation_last[key].strftime('%Y-%m-%d')} |"
        )
    lines.append("")


def _turns(stats: AggregatedStats, lines: list[str]) -> None:
    snapshot = stats.snapshot
    lines.extend([
        "## Model turn timing by session role",
        "",
        "Completed and incomplete turns are content-free counters. Duration percentiles use "
        "completed turns only.",
        "",
    ])
    groups = sorted({(runtime, role) for runtime, role, _outcome in snapshot.turn_count})
    if not groups:
        lines.extend(["no model turn timing captured", ""])
        return
    lines.extend([
        "| Runtime | Session role | Completed | Incomplete | P50 ms | P95 ms |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for runtime, role in groups:
        durations = sorted(snapshot.turn_durations.get((runtime, role), []))
        p50 = f"{statistics.median(durations):.1f}" if durations else "-"
        p95 = f"{durations[max(0, math.ceil(len(durations) * 0.95) - 1)]:.1f}" if durations else "-"
        lines.append(
            f"| {runtime} | {role} | {snapshot.turn_count.get((runtime, role, 'completed'), 0)} | "
            f"{snapshot.turn_count.get((runtime, role, 'incomplete'), 0)} | {p50} | {p95} |"
        )
    lines.append("")


def _matching(events: list[dict], operation: str, actor_prefix: str = "") -> list[dict]:
    return [
        event
        for event in events
        if event.get("op") == operation
        and (not actor_prefix or str(event.get("actor") or "").startswith(actor_prefix))
    ]


def _lifecycle_metrics(events: list[dict]) -> tuple[list[tuple[str, object]], list[tuple[str, list[dict]]]]:
    task_runs = _matching(events, "agent-run", "task:")
    reviewer_runs = _matching(events, "agent-run", "reviewer:")
    review_starts = _matching(events, "review-round-start")
    review_callbacks = _matching(events, "review-callback")
    review_completions = _matching(events, "review-round-complete")
    legacy_rounds = _matching(events, "review-round")
    escalations = _matching(events, "task-escalation")
    surfaces = _matching(events, "surface-lifecycle")
    completions = _matching(events, "task-complete")
    accepted = sum(event_count(event, "accepted_callbacks") for event in review_callbacks)
    accepted += sum(event_count(event, "valid_callbacks") for event in legacy_rounds)
    rejected = sum(event_count(event, "rejected_callbacks") for event in review_callbacks)
    rejected += sum(event_count(event, "invalid_callbacks") for event in legacy_rounds)
    callback_total = accepted + rejected
    callback_rate = f"{accepted / callback_total * 100:.1f}%" if callback_total else "-"
    findings = review_completions + legacy_rounds
    metrics: list[tuple[str, object]] = [
        ("Task agent runs", len(task_runs)),
        ("Validated task completions", int(sum(event_count(e, "tasks") for e in completions))),
        ("Reviewer agent runs", len(reviewer_runs)),
        ("Review rounds started", int(sum(event_count(e, "rounds_started") for e in review_starts))),
        ("Review rounds completed", int(sum(event_count(e, "rounds_completed") for e in review_completions))),
        ("Accepted review callbacks", int(accepted)),
        ("Rejected review callbacks", int(rejected)),
        ("Callback acceptance rate", callback_rate),
    ]
    metrics.extend(
        (f"Findings: {severity}", int(sum(event_count(e, f"{severity}_findings") for e in findings)))
        for severity in sorted(SEVERITIES)
    )
    metrics.extend([
        ("Escalations raised", int(sum(event_count(e, "raised") for e in escalations))),
        ("Escalations resolved", int(sum(event_count(e, "resolved") for e in escalations))),
        ("Escalation delivery failures", int(sum(event_count(e, "delivery_failures") for e in escalations))),
        ("Watchdog warnings", int(sum(event_count(e, "watchdog_warnings") for e in task_runs + reviewer_runs))),
        ("Watchdog alerts", int(sum(event_count(e, "watchdog_alerts") for e in task_runs + reviewer_runs))),
        ("Watchdog degraded notices", int(sum(event_count(e, "watchdog_degraded") for e in task_runs + reviewer_runs))),
        ("Watchdog recoveries", int(sum(event_count(e, "watchdog_recoveries") for e in task_runs + reviewer_runs))),
        ("Surfaces auto-closed", int(sum(event_count(e, "closed") for e in surfaces))),
        ("Surfaces left open (expected)", int(sum(event_count(e, "left_open") for e in surfaces if event_count(e, "auto_close_expected") == 0))),
        ("Auto-close misses", int(sum(event_count(e, "left_open") for e in surfaces if event_count(e, "auto_close_expected") > 0))),
    ])
    durations = [
        ("Task end-to-end", completions),
        ("Task agent process", task_runs),
        ("Reviewer process", reviewer_runs),
        ("Review round", review_completions + legacy_rounds),
        ("Human escalation wait", [e for e in escalations if event_count(e, "resolved") > 0]),
    ]
    return metrics, durations


def _lifecycle(stats: AggregatedStats, lines: list[str]) -> None:
    events = stats.snapshot.lifecycle_events
    lines.extend([
        "## Unattended lifecycle dogfood",
        "",
        "These counters describe orchestration mechanics only. They never contain task text, "
        "review prose, commands, queries, or error messages.",
        "",
    ])
    if not events:
        lines.extend(["no unattended lifecycle events captured in this window", ""])
        return
    metrics, duration_groups = _lifecycle_metrics(events)
    lines.extend(["| Metric | Value |", "|---|---:|"])
    lines.extend(f"| {label} | {value} |" for label, value in metrics)
    lines.extend(["", "| Duration | Samples | P50 s | P95 s |", "|---|---:|---:|---:|"])
    for label, records in duration_groups:
        values = [event_count(event, "duration_ms") for event in records]
        values = [value for value in values if value > 0]
        lines.append(
            f"| {label} | {len(values)} | {seconds(percentile(values, 0.50))} | "
            f"{seconds(percentile(values, 0.95))} |"
        )
    lines.extend([
        "",
        "Treat p50/p95 as directional until each row has at least 10–20 real samples; "
        "zero-duration synthetic checks are excluded.",
        "",
    ])


def _coverage(stats: AggregatedStats, lines: list[str]) -> None:
    snapshot = stats.snapshot
    lines.extend([
        "## Skill telemetry coverage",
        "",
        "Skill invocations are counted from Claude history and transcripts only. Every other "
        "skill-capable runtime executes the same skills without leaving evidence here, so its "
        "row below is absence of measurement, not absence of use. Evidence records count "
        "per-source activity markers (prompts, transcript tool calls, router hits, script "
        "events); Claude draws on four sources and the others on fewer, so the counts are a "
        "presence test and are not comparable across runtimes.",
        "",
        "| Runtime | Evidence records | Skill invocations observable |",
        "|---|---:|---|",
    ])
    for runtime in sorted(SKILL_CAPABLE_RUNTIMES | set(snapshot.runtime_activity)):
        if runtime not in SKILL_CAPABLE_RUNTIMES and not snapshot.runtime_activity.get(runtime):
            continue
        if runtime in SKILL_OBSERVABLE_RUNTIMES:
            label, observable = runtime, "yes (history + transcripts)"
        elif runtime in SKILL_CAPABLE_RUNTIMES:
            label, observable = runtime, "no — invocations invisible to this report"
        else:
            label, observable = "unattributed", "unknown — runtime not recorded"
        lines.append(f"| {label} | {snapshot.runtime_activity.get(runtime, 0)} | {observable} |")
    lines.append("")


def _skill_table(stats: AggregatedStats, lines: list[str]) -> None:
    snapshot = stats.snapshot
    lines.extend([
        "## Claude-only skill telemetry",
        "",
        "Typed/Auto come from Claude history and transcripts; they do not measure Codex skill "
        "usage. Router hints are runtime-tagged and cross-runtime, but record prompt intent "
        "rather than invocation.",
        "",
        "| Skill | Typed | Auto | Total | Last used | Router hints | Hint→use ≤1h |",
        "|---|---|---|---|---|---|---|",
    ])
    for name, count in stats.used:
        last = snapshot.last_used[name].strftime("%Y-%m-%d")
        hints = snapshot.hint_count.get(name, 0)
        followed = snapshot.hint_followed.get(name, 0)
        precision = f"{followed}/{hints}" if hints else "-"
        lines.append(
            f"| /{name} | {snapshot.typed_count.get(name, 0)} | "
            f"{snapshot.auto_count.get(name, 0)} | {count} | {last} | {hints} | {precision} |"
        )
    lines.append("")


def _zero_usage(stats: AggregatedStats, lines: list[str]) -> None:
    snapshot = stats.snapshot
    uncovered = stats.bounds["uncovered_runtimes"]
    if not stats.totals:
        lines.extend([
            f"## Skill usage evidence unavailable ({len(snapshot.skills)} installed)",
            "",
        ])
        if snapshot.runtime_activity.get("claude"):
            lines.append(
                "Claude records exist for this project but contain no skill invocation in "
                f"{snapshot.days}d, so this window cannot rank skills. Widen --days, or take it as a "
                "real signal that no skill ran here, before reading any zero as unused."
            )
        else:
            lines.append(
                f"No skill invocation of any kind was observed in {snapshot.days}d, so this window cannot "
                "rank skills. Check that Claude history/transcripts cover this project path and "
                "window before reading any zero as unused."
            )
        if uncovered:
            lines.extend([
                "",
                "Active here without skill-invocation telemetry: " + ", ".join(uncovered)
                + ". Usage there would be invisible even with healthy Claude sources.",
            ])
    elif uncovered:
        lines.extend([
            f"## Claude-zero skills ({len(stats.dead)} of {len(snapshot.skills)} installed, "
            f"0 invocations typed+auto in {snapshot.days}d)",
            "",
            "Not a dead-weight verdict. Active in this window without skill-invocation "
            "telemetry: " + ", ".join(uncovered) + ". These zeros only prove absent Claude "
            "evidence. Confirm with the user before removing any skill below.",
            "",
            "Router intent observed in an uncovered runtime (unverified — a prompt "
            "match, not an invocation; do not treat as removable): "
            + (", ".join(f"/{name}" for name in stats.bounds["hinted_elsewhere"]) or "none"),
            "",
            "Dead-weight candidates, unproven (no invocation or router evidence in any "
            "runtime): " + (", ".join(f"/{name}" for name in stats.bounds["dead"]) or "none"),
        ])
    else:
        lines.extend([
            f"## Dead-weight candidates ({len(stats.dead)} of {len(snapshot.skills)} installed, "
            f"0 invocations typed+auto in {snapshot.days}d)",
            "",
            "Nothing was observed in this window that could invoke a skill unobserved, "
            "so these zeros are a complete verdict for it.",
            "",
            ", ".join(f"/{name}" for name in stats.dead) if stats.dead else "none",
        ])
    lines.append("")


def _agents_and_assists(stats: AggregatedStats, lines: list[str]) -> None:
    snapshot = stats.snapshot
    lines.extend(["## Agents usage (Task tool, transcripts)", ""])
    if snapshot.agent_count:
        lines.extend(["| Agent | Calls | |", "|---|---|---|"])
        for name, count in sorted(snapshot.agent_count.items(), key=lambda item: -item[1]):
            tag = "custom" if name in CUSTOM_AGENTS else "built-in"
            lines.append(f"| {name} | {count} | {tag} |")
        never = sorted(CUSTOM_AGENTS - set(snapshot.agent_count))
        if never:
            lines.extend(["", "Custom agents with 0 calls: " + ", ".join(never)])
    else:
        lines.append("no Task-tool calls found in transcripts")
    lines.extend(["", "## Retrieval assists (command-log.jsonl, Bash capture)", ""])
    if snapshot.assist_count:
        lines.extend(["| Assist | Calls | Last used |", "|---|---|---|"])
        for kind, _marker in ASSIST_MARKERS:
            count = snapshot.assist_count.get(kind, 0)
            last = snapshot.assist_last[kind].strftime("%Y-%m-%d") if kind in snapshot.assist_last else "-"
            lines.append(f"| {kind} | {count} | {last} |")
    else:
        lines.append("no retrieval-assist invocations captured")
    wiki_queries = stats.totals.get("wiki-query", 0)
    if wiki_queries > 0 and sum(snapshot.assist_count.values()) == 0:
        lines.extend([
            "",
            f"WARN: wiki-query ran {wiki_queries} times in {snapshot.days}d but retrieval assists "
            "(tag-search / semantic-search --hybrid) were never invoked — "
            "SKILL instructions may be ignored.",
        ])


def render_report(stats: AggregatedStats) -> str:
    snapshot = stats.snapshot
    lines = [
        f"# Pipeline stats — last {snapshot.days}d (prompts in project: {snapshot.total_prompts})",
        "",
    ]
    _operations(stats, lines)
    _turns(stats, lines)
    _lifecycle(stats, lines)
    _coverage(stats, lines)
    _skill_table(stats, lines)
    _zero_usage(stats, lines)
    _agents_and_assists(stats, lines)
    lines.extend([
        "",
        "> Границы интерпретации: (1) Typed = history.jsonl (что напечатал user), "
        "Auto = Skill tool_use из транскриптов (что Claude вызвал сам) — источники "
        "комплементарны; (2) покрытие транскриптов ограничено их retention (~30д); "
        "(3) hint-precision грубая (окно 1ч, без привязки к сессии); (4) reference-скиллы "
        "(obsidian-markdown/bases) и замороженные (canvas, wiki) по нулям — это норма; "
        "(5) вызовы скиллов видны только в Claude-источниках — ноль означает «нет "
        "Claude-евиденса», а не «не используется», пока в окне наблюдался Codex.",
    ])
    return "\n".join(lines)
