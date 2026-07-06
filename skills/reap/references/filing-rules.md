# /reap filing rules: routing + frontmatter

Detailed rules for Phase 1.5-1.6: where each type gets filed and which frontmatter is generated.

### 1.5 Routing table

| type | target path | mode |
|---|---|---|
| `session` | `wiki/meta/sessions/YYYY-MM-DD-<task-name>.md` | new |
| `decision` | `wiki/decisions/<Title>.md` | new, ADR-style |
| `runbook` | `wiki/runbooks/<Title>.md` | new |
| `incident` | `wiki/incidents/YYYY-MM-DD-<title-slug>.md` | new |
| `service-update` | `wiki/services/<title>.md` | **update existing** (if present) or new |
| `repo-touch` | `wiki/repos/<title>.md` | **update existing** (if present) or new stub |

The type enum is enforced by `scripts/parse-wiki-summary.py` — do not invent new types here without updating the script. If a target folder does not exist in the vault yet (e.g. `wiki/incidents/`, `wiki/services/`, `wiki/repos/`), create it on first use; the core shipped folders are concepts, entities, sources, questions, runbooks, decisions, goals, routines, daily, meta.

For update-mode: `Read` the existing page and merge. The body from the summary replaces the **last** `## Recent` section (if present) or is appended as a new `## <YYYY-MM-DD> <task-name>` section at the end.

### 1.6 Frontmatter

For a **new** page — generate per the schema in `wiki/<folder>/_index.md`. Minimum:

```yaml
---
type: <session|decision|runbook|incident|service|repo>
title: "<Title>"
address: c-NNNNNN
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags:
  - <relevant-tag>
status: <active|developing|...>
sessions:                          # provenance chain: dispatch origin -> executor -> reap
  - <ORIGIN_SESSION_from_task-meta> # only if .task-meta.json present and different from current
  - <EXEC_SESSION_from_summary>     # task-agent executor (Wiki Summary `session:` field), if present
  - <SESSION_ID>                    # current reap-session from ./scripts/current-session-id.sh
executor_runtime: <claude|codex>    # from .task-meta.json runtime, if present
executor_model: <model>             # from .task-meta.json model, if present/non-empty
suggested_agents:                  # from .task-meta.json (if present)
  - <agent-name-1>
  - <agent-name-2>
related:
  - "[[<wikilinks from body>]]"
---
```

If there was no `.task-meta.json` — omit the origin; if the Wiki Summary has no `session:` (an old split) — omit the exec. The minimum in `sessions:` is the current reap session.

In **interim mode** — leave the frontmatter `status:` as `developing` (the task continues). In **final mode** — the user may pick `active` / `resolved` / `solid` in the pre-flight clarification if the type calls for it.

The `c-NNNNNN` address for every new page:
```bash
./scripts/allocate-address.sh
```

(Sessions/incidents/runbooks/decisions — all need an address. Service/repo updates keep the existing address if the page already had one.)
