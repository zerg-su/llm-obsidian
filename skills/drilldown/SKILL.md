---
name: drilldown
metadata:
  version: 1.0.0
description: "Render metric drilldown HTML."
allowed-tools: Read Write Bash
---

# drilldown: Multi-Panel Time-Series Drilldown

Build one self-contained HTML page for one object (pod, host, service, queue) over one
time window: stat tiles, stacked/bar/line panels on a shared time axis, synced crosshair
tooltip, threshold lines, and an hourly table. The engine is `scripts/drilldown.py`
(stdlib only). The spec schema and data formats are in `references/spec.md`.

Use it when a question is "what happened to X between T1 and T2" and the answer needs
several metrics side by side. For one number, answer in text. Not for dashboards that
must stay live — this is a snapshot.

## Workflow

1. **Scope.** Resolve object, window, timezone, and the metrics that answer the question.
   Ask only for what the prompt and the environment do not give. Domain recipes (ready
   specs for known objects) may live outside this plugin; use one when it matches.
2. **Work dir.** `${XDG_CACHE_HOME:-$HOME/.cache}/llm-obsidian/drilldown/<name>-<YYYYMMDD-HHMM>/`.
   Never write into `wiki/` or `.raw/`. Keep `spec.json`, `data.json`, extras there.
3. **Spec.** Write `spec.json` (schema: `references/spec.md`). One unit per panel —
   different units go to separate panels, never a second y-axis. At most 8 series per
   panel. Pick `step` so the window has at most ~600 points.
4. **Validate.** `python3 scripts/drilldown.py validate --spec <dir>/spec.json`.
5. **Data.**
   - Prometheus-compatible HTTP API reachable: `python3 scripts/drilldown.py fetch --spec <dir>/spec.json --out <dir>/data.json`.
     Read-only `query_range` only. A token comes from the env var named in `source.bearer_env`; never put secrets in the spec.
   - Metrics only reachable through an MCP tool: write `data.json` in the documented format
     from the tool results; do not paste raw tool output into the reply.
   - Events from another system (error counts, deploys, alerts): one small extra JSON per
     source, passed with `--extra`. ISO-8601 or unix timestamps.
6. **Render.** `python3 scripts/drilldown.py render --spec <dir>/spec.json --data <dir>/data.json [--extra <file>] [--lang ru]`.
   With `ORCA_CHAT_SCRATCH_DIR` set, the HTML goes there and the command prints the
   `::orca-visual{...}` line — put that line on its own line in the reply. Otherwise
   give the file path.
7. **Report.** Lead with the main finding. Quote numbers from the tiles/table, with the
   time zone. Name every fetch error and every series without data. Separate measured
   facts from interpretation.

Exit codes: 0 done; 1 fetch got no series; 2 invalid spec or output name/size.

## Rules

- Queries are read-only. Do not change the monitored system to get data.
- Keep the spec and data files; they let the user re-render or attach the HTML via `/save`.
- If a tile or table value contradicts an earlier manual number, recheck the query before
  reporting either.
- Do not render more than one window per HTML; compare windows with two pages or a ratio panel.
