# Runtime capability matrix

The vault core is script-first, but host hook surfaces are not identical. This
matrix documents implemented behavior; absence in a cell is intentional and
must not be inferred from another runtime.

| Capability | Claude Code | Codex CLI | Other agents / shell |
|---|---|---|---|
| Repo skills | Claude plugin marketplace; `/skill` UI | Generated repo-local Codex plugin; `$llm-obsidian:skill` | Read and follow `skills/<name>/SKILL.md` manually |
| Daily synthesis | Subscription preflight + read-only Sonnet low plugin agent | Read-only Terra low project agent | Same contract in the parent agent |
| Agenda carry-over | Shared deterministic scan/collect/report scripts; optional Tasks UI | Same scripts and optional Tasks UI | Same scripts; plugin is not required for correctness |
| Daily latency | Content-free collection/synthesis/run timings with p50/p95 | Same shared numeric events | Same shared numeric events |
| Transactional writer, retrieval, fold | Shared Python/shell scripts | Same scripts | Same scripts |
| Local document normalization | Shared stdlib fast path + isolated pinned Docling | Same scripts/runtime | Same scripts/runtime |
| MCP HTTP gateway | Shared local client pointers | Generated TOML/profile pointers | Any HTTP-capable MCP client |
| Turn-end Stop pipeline | Claude `Stop` hook | Codex plugin `Stop` hook opts into the same `stop.sh`; output goes to `.vault-meta/stop-hook-last.log` | Run `.claude/hooks/stop.sh` manually |
| `SessionStart` hot cache + nudges | Shared runtime adapter | Shared runtime adapter; startup/resume/clear/compact | Manual |
| `UserPromptSubmit` skill router | Shared runtime adapter, soft hints | Shared runtime adapter, soft hints | Manual |
| `PostToolUse[Bash]` command capture | Shared runtime adapter, sanitized | Shared runtime adapter for supported Bash events | Manual |
| `PostToolUse[ExitPlanMode]` plan capture | Automatic | Not provided by this plugin | Use `/save-plan` equivalent explicitly |
| Compaction recovery | PostCompact adapter + host context behavior | Valid PostCompact hint; `SessionStart(source=compact)` reloads hot cache | Manual |
| Operation telemetry | Shared scripts emit `pipeline-events.jsonl`; task/review lifecycle adds numeric latency and outcome counters | Same | Same for explicit scripts |
| Durable review history | Explicit primary-checkout reviews archive on finish; task splits defer to coordinator `reap` | Same | Explicit `spawn_review.py archive` from the coordinator vault |
| Router/operation telemetry | Runtime-tagged, content-free hook/script events | Runtime-tagged, content-free hook/script events | Limited to explicit scripts |

`pipeline-events.jsonl` is local and gitignored. Its schema accepts only
runtime/session identifiers, actor/operation/status, relative vault paths, and
numeric counters. Prompt text, search queries, commands, snippets, page bodies,
and error text are not accepted. `pipeline-stats.py` reports these shared
operations and unattended lifecycle p50/p95 separately from Claude-only skill
telemetry. See [pipeline observability](pipeline-observability.md) for metric
definitions, sample-size limits, and the dogfood acceptance window.

Durable review pages are intentionally separate from telemetry. In unattended
final reap, the lifecycle contract hashes the coordinator-generated marker,
revalidates the archived page, and requires the archive wikilink in the task
result before it permits exact-surface close.

Local file ingestion is also runtime-neutral. `document-normalize.py` handles
text-like sources directly and invokes the isolated Docling runtime only for
binary documents. The converter accepts local paths only, explicitly disables
remote services and external plugins, runs with offline model flags, and uses
prefetched EasyOCR `ru,en` plus layout/table artifacts. Missing dependencies
return a typed coordinator escalation instead of an interactive background
prompt. See [document ingestion](document-ingestion.md).

Daily agenda operations are runtime-neutral too. `journal-write.py` creates
Tasks-compatible plan/reminder checkboxes with stable block IDs. `agenda.py`
previews all unfinished prior items read-only, then can close sources, create a
single target occurrence, and refresh the monthly live-query page in one
`vault-write.py` transaction. Obsidian Tasks 8.2.2 is a pinned UI layer; the
Python contract remains authoritative when the plugin is absent or customized.

Codex hook parity uses the documented lifecycle wire format and fixtures in
`tests/test_runtime_hooks.py`. Current Codex interception is incomplete for
unified/streaming shell calls and does not intercept native WebSearch, so hooks
remain observability/guardrails rather than a security boundary. `ExitPlanMode`
capture remains Claude-only because Codex exposes no equivalent tool event.

Protected web flows (`autoresearch`, URL ingest, and deep-query supplements)
use separate runtime homes: a web-enabled fetch context without vault access,
then a networkless vault synthesizer. They require cmux and fail closed outside
it. On macOS both profiles expose `/opt/homebrew` and Xcode Command Line Tools
as runtime roots so the selected Python and its dynamic libraries work inside
the sandbox; narrower filesystem rules keep those tool roots read-only. The
network proxy runs in limited mode with no external-domain allowlist and one
explicit Unix-socket exception for cmux callbacks. Per the official
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference#configtoml),
an omitted domain map denies every external destination until an allow rule is
added. The generated profiles also explicitly disable upstream-proxy chaining,
broad local binding, non-loopback listeners, arbitrary Unix sockets, and
SOCKS5/UDP. Hermetic tests parse and assert this configuration contract; they do
not depend on a live Internet endpoint. Completion markers make
callback delivery recoverable rather than a single point of failure. They also
authorize idempotent cleanup of only the recorded fetch/synthesis surface UUID:
fetch closes during `receive` (including rejected artifacts), synthesis during
final `status`; the coordinator surface is never a valid cleanup target. Pass
`start --keep-surfaces` only for deliberate debugging. A failed synthesis can
be resumed from its already validated artifact with
`research-isolation.py restart-synthesis`; see `scripts/research-isolation.py`.

Unattended Codex task splits use `workspace-write` with `-a never`. The only
additional writable root is the validated Git common directory needed by the
linked task branch. The network proxy allows client connections to exactly
`localhost`, `127.0.0.1`, and `::1`, plus one exact user-owned cmux Unix socket;
loopback binding is allowed, while external domains, non-loopback proxying,
upstream proxy chaining, arbitrary Unix sockets, and SOCKS5/UDP remain disabled.
This supports local MCP/services and
task-side review/escalation/reap callbacks without outbound Internet access.

Both Claude and Codex background commands receive a supervisor-generated
owner/root-controlled `PATH`. It contains the selected Python runtime plus
available Homebrew, Git, uv, Docling, cmux, Claude, and Codex directories, so a
task does not inherit a stale GUI/session path. Unattended executors also receive
the standalone `config/dcg/task.toml` through `DCG_CONFIG`. DCG 0.6.x replaces,
rather than overlays, an explicit config, so this task policy repeats all base
packs and explicit dangerous-operation blocks. The base profile allows amend,
cherry-pick, and staging but blocks rebase; the isolated task profile also
allows rebase. Both keep push, hard reset, file discard, worktree or branch
deletion, repository-wide rewriting, and infrastructure destructive actions
blocked. Tests compare the task/base policies and exercise both allowed and
denied commands.

Dispatch and cross-model review pin the current repository defaults: Claude
`fable` high and Codex `gpt-5.6-sol` high. Explicit task metadata and CLI
overrides win over those defaults and remain recorded in handoff metadata. The
Codex deep profile intentionally keeps `max` effort, and the bounded daily
summarizer intentionally keeps its specialized Terra/low route.

Reviewers remain product-read-only but are no longer toolchain-starved: Claude
gets the same trusted `PATH`, bounded test entrypoints, and the exact DCG
smoke `bash scripts/dcg-test-suite.sh`; Codex gets a
private scratch directory, exact loopback access/binding, disabled web search,
and no product writable root. `tests/test_task_lifecycle.py` and
`tests/test_review_dispatch.sh` reject command, environment, writable-root,
domain, and socket drift.

The DCG suite resolves an explicit `DCG_BIN`, then `PATH`, then the portable
user/Homebrew install locations. A GUI-launched reviewer can therefore test the
repo-shipped policy with the installed binary without inheriting the foreground
terminal's PATH, while an invalid explicit override fails closed.

This task policy is a portable repository default. It does not rewrite a
user's foreground Claude/Codex permission settings; a personal foreground
session may deliberately use fuller trust while public installs retain their
own host policy.

Background tasks still pause on a probable defect in the orchestration itself:
the executor that discovered the defect must not decide that its own repair is
safe. Its `mechanism-failure` marker tells the owning coordinator to classify
immediately. The coordinator auto-repairs only a repo-owned, local,
reproducible, reversible, in-scope defect with preserved dirty work and no new
permission, dependency, security, public-interface, migration, destructive, or
external-effect boundary; otherwise it asks the user once. Stop hooks never
self-repair. The canonical decision table is
[failure-to-repair contract](skill-references/failure-repair-contract.md).
