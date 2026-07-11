# Runtime capability matrix

The vault core is script-first, but host hook surfaces are not identical. This
matrix documents implemented behavior; absence in a cell is intentional and
must not be inferred from another runtime.

| Capability | Claude Code | Codex CLI | Other agents / shell |
|---|---|---|---|
| Repo skills | Claude plugin marketplace; `/skill` UI | Generated repo-local Codex plugin; `$llm-obsidian:skill` | Read and follow `skills/<name>/SKILL.md` manually |
| Daily synthesis | Subscription preflight + read-only Sonnet low plugin agent | Read-only Terra low project agent | Same contract in the parent agent |
| Daily latency | Content-free collection/synthesis/run timings with p50/p95 | Same shared numeric events | Same shared numeric events |
| Transactional writer, retrieval, fold | Shared Python/shell scripts | Same scripts | Same scripts |
| MCP HTTP gateway | Shared local client pointers | Generated TOML/profile pointers | Any HTTP-capable MCP client |
| Turn-end Stop pipeline | Claude `Stop` hook | Codex plugin `Stop` hook opts into the same `stop.sh`; output goes to `.vault-meta/stop-hook-last.log` | Run `.claude/hooks/stop.sh` manually |
| `SessionStart` hot cache + nudges | Shared runtime adapter | Shared runtime adapter; startup/resume/clear/compact | Manual |
| `UserPromptSubmit` skill router | Shared runtime adapter, soft hints | Shared runtime adapter, soft hints | Manual |
| `PostToolUse[Bash]` command capture | Shared runtime adapter, sanitized | Shared runtime adapter for supported Bash events | Manual |
| `PostToolUse[ExitPlanMode]` plan capture | Automatic | Not provided by this plugin | Use `/save-plan` equivalent explicitly |
| Compaction recovery | PostCompact adapter + host context behavior | Valid PostCompact hint; `SessionStart(source=compact)` reloads hot cache | Manual |
| Operation telemetry | Shared scripts emit `pipeline-events.jsonl` | Same | Same |
| Router/operation telemetry | Runtime-tagged, content-free hook/script events | Runtime-tagged, content-free hook/script events | Limited to explicit scripts |

`pipeline-events.jsonl` is local and gitignored. Its schema accepts only
runtime/session identifiers, actor/operation/status, relative vault paths, and
numeric counters. Prompt text, search queries, commands, snippets, page bodies,
and error text are not accepted. `pipeline-stats.py` reports these shared
operations separately from Claude-only skill telemetry.

Codex hook parity uses the documented lifecycle wire format and fixtures in
`tests/test_runtime_hooks.py`. Current Codex interception is incomplete for
unified/streaming shell calls and does not intercept native WebSearch, so hooks
remain observability/guardrails rather than a security boundary. `ExitPlanMode`
capture remains Claude-only because Codex exposes no equivalent tool event.

Protected web flows (`autoresearch`, URL ingest, and deep-query supplements)
use separate runtime homes: a web-enabled fetch context without vault access,
then a networkless vault synthesizer. They require cmux and fail closed outside
it; see `scripts/research-isolation.py`.
