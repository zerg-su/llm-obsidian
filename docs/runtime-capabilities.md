# Runtime capability matrix

The vault core is script-first, but host hook surfaces are not identical. This
matrix documents implemented behavior; absence in a cell is intentional and
must not be inferred from another runtime.

| Capability | Claude Code | Codex CLI | Other agents / shell |
|---|---|---|---|
| Repo skills | Claude plugin marketplace; `/skill` UI | Generated repo-local Codex plugin; `$llm-obsidian:skill` | Read and follow `skills/<name>/SKILL.md` manually |
| Daily synthesis | Subscription preflight + read-only current-model agent at medium effort | Read-only current-model project agent at medium effort | Same evidence contract when exact routing is available |
| Agenda carry-over | Shared deterministic scan/collect/report scripts; optional Tasks UI | Same scripts and optional Tasks UI | Same scripts; plugin is not required for correctness |
| Daily latency | Content-free collection/synthesis/run timings with p50/p95 | Same shared numeric events | Same shared numeric events |
| Transactional writer, retrieval, fold | Shared Python/shell scripts | Same scripts | Same scripts |
| Local document normalization | Shared stdlib fast path + isolated pinned Docling | Same scripts/runtime | Same scripts/runtime |
| MCP HTTP gateway | Shared local client pointers | Generated TOML/profile pointers | Any HTTP-capable MCP client |
| Turn-end Stop pipeline | Claude `Stop` hook | Codex plugin `Stop` hook opts into the same `stop.sh`; output goes to `.vault-meta/stop-hook-last.log` | Run `.claude/hooks/stop.sh` manually |
| `SessionStart` hot cache + nudges | Shared runtime adapter | Shared runtime adapter; startup/resume/clear/compact | Manual |
| `UserPromptSubmit` skill router | Shared runtime adapter, soft hints | Shared runtime adapter, soft hints | Manual |
| Allowlisted shell command capture | Shared runtime adapter, sanitized | `Bash`, `exec_command`, `shell`, and strict literal `unified_exec` normalization | Typed `command_evidence.py ingest-user` |
| `PostToolUse[ExitPlanMode]` plan capture | Automatic | Not provided by this plugin | Use `/save-plan` equivalent explicitly |
| Compaction recovery | PostCompact adapter + host context behavior | Valid PostCompact hint; `SessionStart(source=compact)` reloads hot cache | Manual |
| Harness operations | Shared owner-scoped ledger; `status`, `inspect`, `resume`, `reconcile`, `cancel`, `close`, `doctor` | Same | Read-only inspection works; visible provider lifecycle requires a supported host |
| Operation telemetry | Shared scripts emit `pipeline-events.jsonl`; task/review lifecycle adds numeric latency and outcome counters | Same | Same for explicit scripts |
| Durable review history | Unified simple/deep operation archives exact HEAD/profile evidence at reap | Same | Explicit exact-operation archive from the coordinator vault |
| Persistent task lanes | Exact owner/task/model/domain cmux resume with anchored right splits | Same harness and typed checkpoint contract | Script-only state; visible cmux resume requires supported host |
| Router/operation telemetry | Runtime-tagged, content-free hook/script events | Runtime-tagged, content-free hook/script events | Limited to explicit scripts |

`pipeline-events.jsonl` is local and gitignored. Its schema accepts only
runtime/session identifiers, actor/operation/status, relative vault paths, and
numeric counters. Prompt text, search queries, commands, snippets, page bodies,
and error text are not accepted. `pipeline-stats.py` reports these shared
operations and unattended lifecycle p50/p95 separately from Claude-only skill
telemetry. Skill invocations are observable in Claude sources only, so that
report bounds its zero-usage verdict by observed runtime coverage: it names a
skill a dead-weight candidate only when skill usage was actually observed and
nothing that could have invoked a skill unobserved was active — Codex, or
orchestration whose runtime was never recorded — and it holds back skills the
runtime-tagged router matched inside that activity. Absent
Claude evidence is never rendered as zero Codex usage. See
[pipeline observability](pipeline-observability.md) for metric
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
`tests/test_runtime_hooks.py`. Unified execution is accepted only when the
allowlisted payload contains literal JSON arguments to `tools.exec_command`;
arbitrary JavaScript is never evaluated. Command records retain task-origin
wiki provenance separately from the executing worker session, store no tool
output, and deduplicate hook replay. Native WebSearch is not intercepted, so
hooks remain observability/guardrails rather than a security boundary.
`ExitPlanMode` capture remains Claude-only because Codex exposes no equivalent
tool event.

Protected web flows (`research`, URL ingest, and deep-query supplements)
use one owner-scoped root operation with separate fetch and synth child
operations. Fetch has web access without vault access; synthesis is networkless
and receives only the validated artifact, copied source files, and the explicit
minimal ContextPacket. The coordinator owns the only vault transaction. Each
root operation gets fresh private scratch and per-stage `CODEX_HOME` directories.
Research providers receive a fresh fixed environment: `HOME` and `CODEX_HOME`
point to that stage directory, temporary files stay below it, and only fixed
locale, shell, terminal, timezone, and system `PATH` values are present.
Coordinator credentials, proxy variables, plugin variables, and every `CMUX_*`
variable are excluded; authentication remains available only through the exact
stage-local auth link. The flows require an exact originating cmux surface and
fail closed outside it. On macOS both
profiles expose `/opt/homebrew` and Xcode Command Line Tools
as runtime roots so the selected Python and its dynamic libraries work inside
the sandbox; narrower filesystem rules keep those tool roots read-only. The
network proxy runs in limited mode with no external-domain allowlist and no
terminal-control socket. A code-owned watcher outside the provider sandbox
validates each typed artifact, accepts a content-free callback in the operation
ledger, records the exact checkpoint, and sends the exact bounded `advance`
command through the generic runtime adapter. Per the official
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference#configtoml),
an omitted domain map denies every external destination until an allow rule is
added. The generated profiles also explicitly disable upstream-proxy chaining,
broad local binding, non-loopback listeners, arbitrary Unix sockets, and
SOCKS5/UDP. Hermetic tests parse and assert this configuration contract; they do
not depend on a live Internet endpoint. The worker writes a completion marker
only after successful callback delivery, so replay never duplicates the origin
wake. Before synthesis starts, `advance` also records a content-free SHA-256 pin
for the validated fetch artifact in the harness runtime state. The worker
rechecks it after provider execution, and finalization checks it once more
before cleanup, so a coordinated source/manifest rewrite cannot change synth
provenance. `research-isolation.py advance` delegates exact provider exit and
cleanup to `RuntimeSessionManager` before starting the next stage or returning
the validated cited artifact. `status` is read-only. The coordinator surface
is never a cleanup target.

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

Dispatch and unified review resolve the current repository defaults from
`config/model-routing.toml`. Simple review stays on one selected holistic
route. Default Deep resolves independent Anthropic and OpenAI holistic routes; an
explicit model/runtime override instead opens intent and engineering lanes on
that model alone. Explicit Full resolves the four-lane Anthropic/OpenAI by
intent/engineering grid and rejects model/runtime overrides before launch.
Review model overrides accept registered aliases only. Every resolved route
remains recorded in operation metadata. The bounded
daily summarizer inherits the current session's exact model and changes only
effort to the centrally configured daily value.

Reviewers remain product-read-only but are no longer toolchain-starved. Review
specs, callbacks, baselines, watchdog state, and results live under exact
owner/operation/run identity, so several sessions in one project do not share
singleton files. Simple review uses one holistic lane. Default Deep uses two
independent holistic model lanes; single-model Deep and explicit Full reuse the
same independent intent and engineering specialist responsibilities. Anthropic
reviewers keep `dontAsk` but may read and run arbitrary local checks inside the
native Claude OS sandbox. The product worktree and Git metadata stay explicitly
read-only; writes are limited to operation-owned review scratch/callback roots
and one private test temp directory. Unsandboxed fallback, inherited user or
project settings, external network domains, Unix sockets, MCPs, hooks, and
credential reads are disabled fail-closed. Codex keeps its private scratch
directory, exact loopback access/binding, disabled web search, and no product
writable root. `tests/test_task_lifecycle.py` and
the harness adapter and task lifecycle suites reject command, environment,
writable-root, domain, and socket drift.

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
