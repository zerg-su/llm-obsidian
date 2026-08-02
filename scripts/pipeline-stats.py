#!/usr/bin/env python3
"""Pipeline usage stats: which skills are actually used, which are dead weight.

Sources are local-only: Claude history/transcripts plus content-free router,
command, and pipeline event logs. Skill invocations are observable in Claude
sources only, so activity in uncovered runtimes bounds all zero-usage claims.

Usage:
    ./scripts/pipeline-stats.py [--days N] [--report]
    ./scripts/pipeline-stats.py --nudge [--days N]
"""

from __future__ import annotations

import sys

from pipeline_stats_model import (
    AGENT_DRIVEN_OPS,
    KNOWN_RUNTIMES,
    LIFECYCLE_OPS,
    SKILL_CAPABLE_RUNTIMES,
    SKILL_OBSERVABLE_RUNTIMES,
    aggregate_snapshot,
    classify_zero_usage,
    event_count,
    normalize_runtime,
    percentile,
    seconds,
)
from pipeline_stats_render import render_report
from pipeline_stats_report import write_report
from pipeline_stats_sources import (
    ASSIST_MARKERS,
    COMMAND_LOGS,
    EVENT_LOGS,
    HISTORY,
    ROUTER_LOGS,
    SKILL_ROOTS,
    TRANSCRIPT_DIR,
    VAULT_ROOT,
    base_name,
    collect_snapshot,
    installed_skills,
    iter_jsonl,
    nudge_check,
    parse_log_ts,
    scan_transcripts,
)

# Kept literal here as part of the static bounded-agent registration contract.
CUSTOM_AGENTS: set[str] = {"daily-summarizer"}


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        try:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        except (IndexError, ValueError):
            print(
                "usage: pipeline-stats.py [--days N] [--report|--nudge]",
                file=sys.stderr,
            )
            return 2
    if "--nudge" in sys.argv:
        line = nudge_check(days if "--days" in sys.argv else 7)
        if line:
            print(line)
        return 0

    report = render_report(aggregate_snapshot(collect_snapshot(days)))
    print(report)
    if "--report" in sys.argv:
        return write_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
