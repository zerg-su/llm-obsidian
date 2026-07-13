<p align="center">
  <img src="docs/assets/llm-obsidian-banner.png" alt="Two terminal agents exchanging work through an obsidian knowledge vault" width="100%">
</p>

# LLM Obsidian

[![CI](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml/badge.svg)](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml)

**One durable working environment for Claude Code and Codex CLI: shared memory, shared skills, bounded orchestration, and cross-model review.** It turns conversations, sources, plans, and decisions into a structured Obsidian wiki, then makes that knowledge available to the next agent session instead of starting from zero.

🇷🇺 **Читайте по-русски: [README.ru.md](README.ru.md)**

This is deliberately not a universal agent router. Claude Code and Codex CLI are the two first-class agents; Obsidian is the durable memory; plain Python and shell scripts provide the shared mechanics. The project does not replace either CLI, emulate a provider, or bypass its account limits.

The workflow grew out of months of daily use as a DevOps engineer's working memory before it was extracted from a private vault and made generic.

---

## Why LLM Obsidian?

A capable coding agent is still usually a temporary process. Its local context ends, its private memory differs from another CLI's memory, and a useful plan or review can disappear into terminal history. Running two agents side by side does not solve that by itself: they need a shared place to remember, a contract for handing work over, and a way to finish without pasting free-form text between windows.

LLM Obsidian supplies that missing layer:

- **Memory:** a local Markdown vault with provenance, links, retrieval, lifecycle, and transactional writes.
- **Skills:** the same versioned workflows for capture, research, planning, review, daily work, and maintenance.
- **Orchestration:** visible cmux task splits, isolated git worktrees, typed task contracts, watchdogs, and deterministic reap.
- **Independent review:** Claude can implement while Codex reviews, or Codex can implement while Claude reviews.
- **Guardrails:** validation, read-only reviewer roles, bounded verification loops, DCG, and explicit human escalation for scope or trust changes.

The result is one working system with several model perspectives—not a collection of unrelated chat sessions.

## Why not just Claude Code? Why not just Codex?

You should still use both. LLM Obsidian is the connective tissue around them.

| A single CLI is good at | The gap LLM Obsidian closes |
|---|---|
| Solving the task in its current context | Carrying decisions, sources, and task history into future sessions |
| Using its own commands and extensions | Exposing one versioned skill set and one vault to both agents |
| Reviewing its own implementation | Routing the result to a different model family with a read-only review contract |
| Running a long task | Supervising the task in a visible split, detecting stalls, and closing it only after a validated handoff |
| Asking for permissions as work unfolds | Moving foreseeable choices into the approved plan and escalating only material surprises |

Different models fail differently. Cross-review is valuable because the reviewer did not produce the implementation and does not share all of the executor's assumptions. It raises confidence; it does not pretend to prove correctness.

## Architecture at a glance

```text
VoiceInk (optional voice input)            keyboard input
                 \                            /
                  v                          v
        ┌────────────── LLM Obsidian workspace ──────────────┐
        │                                                     │
        │   Claude Code  ⇄  typed handoffs  ⇄  Codex CLI     │
        │         \          cmux + worktrees          /      │
        │          └── shared skills + Obsidian vault ─┘      │
        │                    │                                │
        │       sparse retrieval + optional Ollama/bge-m3     │
        │       DCG + schemas + validation + Stop pipeline    │
        └────────────────────┬────────────────────────────────┘
                             │
              Claude implements → Codex reviews
              Codex implements  → Claude reviews
```

Obsidian stores the durable state, but the automation is not locked into an Obsidian process: the canonical data is Markdown plus repository scripts. cmux is needed only for visible multi-session orchestration. Ollama is needed only for optional dense retrieval.

## Cross-model review, end to end

Example: Claude Code implements, Codex reviews.

1. The coordinator turns the request into an approved plan and asks consequential questions before execution.
2. `/dispatch` records a bounded task contract, creates an isolated worktree, and opens an interactive Claude Code session in a cmux split.
3. Claude implements and runs the task-specific checks without repeatedly asking about actions already covered by the contract.
4. `/review-dispatch` opens a read-only Codex reviewer. The callback is typed and tied to the task, review, and baseline being reviewed—not pasted terminal prose.
5. Accepted findings return to the same executor. Verification is bounded; a scope, security, trust, or destructive-action change returns to the user.
6. `/reap-send` validates the final summary and result provenance before the coordinator files the outcome. The cmux surface is armed to close only after the agent process exits.

The reverse direction uses the same protocol: Codex implements and Claude reviews. See [model routing](docs/model-routing.md) and the [unattended pipeline runbook](docs/unattended-pipeline-operations.md).

## Automatic checks before completion

| Phase | What is checked |
|---|---|
| Dispatch | approved-plan identity, task contract, worktree metadata, generated command, role-specific permission policy |
| Execution | supervised process state, heartbeat/progress evidence, observer-only stall notifications, explicit exit state |
| Review | read-only reviewer mandate, task/review IDs, baseline fingerprint, callback schema, bounded verification rounds |
| Reap | typed wiki summary, result path and hash, task provenance, terminal outcome, close-on-exit eligibility |
| Vault Stop | transaction recovery, sparse reindex/self-heal, strict vault validation, scoped commit, fingerprinted optional dense refresh |
| Dogfood report | content-free completion, callback, intervention, watchdog, surface, and p50/p95 lifecycle counters |

The watchdog does not blindly kill an agent that is visibly working. It reports a possible stall first; genuinely blocking decisions return to the coordinator instead of being guessed in a background window.

## Supported agents and companion components

| Runtime or component | Status | Role and boundary |
|---|---|---|
| **Claude Code** | First-class | Coordinator, executor, or opposite-model reviewer through the official CLI and the user's own account/subscription. Provider limits remain in force. |
| **Codex CLI** | First-class | Coordinator, executor, or opposite-model reviewer through the official CLI and the user's own access. Provider limits remain in force. |
| **Obsidian** | Core data/UX | Local Markdown vault, links, browsing, and human editing. The files remain usable without an agent. |
| **cmux** | Optional; required for orchestration | Visible task/reviewer splits, sockets, lifecycle tracking, and resumable interactive sessions. Core wiki skills work without it. |
| **Ollama + `bge-m3`** | Optional local model | Dense multilingual embeddings and duplicate detection without a hosted API or per-call model fee. It uses your own disk/RAM; sparse retrieval remains complete when Ollama is absent. |
| **DCG** | Optional, recommended | Destructive-command preflight for both CLIs. Defense in depth, not a sandbox or a proof of safety. |
| **VoiceInk** | Optional input layer | Native macOS dictation into either CLI. Not bundled and not an agent integration. |
| **Gemini CLI and other agents** | Not supported today | A future adapter is possible only after it reaches the same skill, contract, review, and test guarantees. No compatibility is claimed now. |

Model names change faster than the workflow. Defaults, subscription-only routes, and explicit overrides are documented in [docs/model-routing.md](docs/model-routing.md) rather than advertised as permanent provider support.

## Design principles

- **Durable files over hidden memory.** Important context is inspectable, diffable, and portable.
- **Plan once, execute within bounds.** Predictable choices happen before dispatch; unattended sessions inherit only that mandate.
- **Different model, independent review.** The reviewer is advisory and read-only; the executor owns fixes and commits.
- **Typed contracts over terminal paste.** IDs, hashes, schemas, and explicit terminal states make handoffs auditable.
- **Local-first retrieval.** The mandatory path needs no embedding API; dense retrieval is an optional local enhancement.
- **Fail closed at trust boundaries.** Invalid callbacks, changed baselines, missing provenance, or auth-route drift do not silently continue.
- **The user owns external effects.** Pushes, deployments, credentials, destructive actions, and material scope changes are not inferred.

## Security and trust

LLM Obsidian **does not bypass Claude, Codex, or any provider's subscriptions, rate limits, authentication, safety controls, or terms**. You install and authenticate the official CLIs yourself. Subscription-only Claude review routes reject API/provider overrides rather than silently spending an API key; model overrides remain explicit.

Reviewers are launched with read-only mandates. Secrets for MCP services live outside the repository. Vault writes use optimistic hashes and a recovery journal. DCG blocks known destructive shell patterns before execution, while the CLI sandbox/approval policy and the human-approved task contract remain separate layers. No guardrail eliminates model error, so cross-review and tests improve assurance but never turn generated work into a guarantee.

## Real workflows

- **Ship a change while away from the terminal:** approve the plan, dispatch one bounded implementation, receive opposite-model review, and return to a validated result instead of a chain of permission prompts.
- **Build a durable technical memory:** ingest docs and decisions, retrieve them by RU/EN concepts, and preserve session provenance for later agents.
- **Research without contaminating the vault context:** fetch sources in an isolated session, synthesize networklessly, then write through one vault transaction.
- **Operate by voice:** dictate prompts into Claude Code or Codex with [VoiceInk](https://github.com/beingpax/VoiceInk), while the same skills and safety gates still apply.
- **Run a personal operations loop:** journal plans, capture backlog items, generate daily summaries, find prior sessions, and distill shell history into human-executable runbooks.

## Core capabilities

- **A wiki that grows itself.** `ingest <path|URL>` turns raw material into 8-15 cross-linked typed pages; `/save` files insights from any conversation; `/autoresearch` runs autonomous research loops; every approved plan is auto-captured into `wiki/plans/`.
- **Retrieval that is measured, not vibed.** H2/H3 sections are bounded to 800 words with 100-word overlap, ranked sparsely, and deduplicated to the best heading/snippet per page; optional local `bge-m3` joins through RRF. A 48-query RU/EN goldset is half held out, and `make bench-retrieval` rejects hit@5/MRR regressions over 0.02.
- **RU-first, local-first.** The mandatory sparse channel handles Cyrillic and mixed technical vocabulary without a service. Optional `bge-m3` embeddings stay on your machine; no cloud API is required.
- **Documents become model-ready locally.** Markdown/text take a stdlib fast
  path; PDF, Office, EPUB, and scans go through pinned Docling with explicit
  `ru,en` OCR, accurate tables, content-addressed cache, and no remote services.
- **A transactional write path.** Agent-driven page create/update, manifest merge, `wiki/log.md`, and `wiki/hot.md` go through `scripts/vault-write.py`: strict frontmatter, optimistic SHA-256 updates, cap enforcement, and crash-safe roll-forward from a durable journal.
- **An industrial Stop hook.** Every turn: recover interrupted writes, reindex, self-heal sparse section retrieval, strictly validate, commit only vault-owned paths, then schedule fingerprinted dense refresh when needed without waiting. Stdlib `fcntl` serializes sessions; validation failures block the commit without hiding dirty work.
- **MCP without the process zoo.** A local HTTP gateway (one launchd service) fronts all your MCP servers: one set of long-lived processes per machine instead of per-terminal duplicates. Ships with [context7](https://context7.com) preconfigured — add one API-key line and library docs are available in every session.
- **Parallel work, optional.** `/dispatch` gives an approved plan one bounded unattended mandate in a separate git worktree + [cmux](https://github.com/wandb/cmux) split. Cross-model review, contract-bound final `/reap`, validation, observer-only 15/20-minute stall notifications, and armed close-on-process-exit run without repeated prompts; blocking/scope changes return to the coordinator. Requires cmux; everything else works without it.

## Why this fork

| | upstream claude-obsidian | llm-obsidian |
|---|---|---|
| Retrieval unit | contextual-prefix chunk cascade | H2/H3 sections, 800-word cap, 100-word overlap, best heading/snippet per unique page |
| Fusion | chunk cascade | mandatory sparse title/tag/heading/body ranking + optional local `bge-m3` RRF; explicit degraded metadata |
| Retrieval QA | one-off benchmark scripts | 48 RU/EN queries, 50% held out, committed baseline, ≤0.02 hit@5/MRR regression gate |
| vault write path | free-form model edits | transactional `vault-write.py`: pages + manifest + log/hot, optimistic hashes, recovery journal |
| Turn-end hook | inline hooks.json commands | `stop-hook.py`: `fcntl`, recovery, self-healing indexes, validation gate, scoped commit |
| Skill routing | descriptions only | data-driven `skill-router` hook (regex rules → soft hints) + `session-nudge` maintenance hints |
| MCP | per-session stdio servers | HTTP gateway: 1 process set per machine, secrets outside the repo, `doctor`/`smoke`/`update` tooling, profile pattern for heavy servers |
| Orchestration | — | `/dispatch` / `/reap` worktree + split workflow with auto plan handoff |
| Productivity layer | — | `/journal`, `/agenda`, `/daily`, `/backlog`, `/find-session`, `/draft`, `/distill-runbook`, `/save-plan` |
| Docs language | EN | EN + RU, RU-first audience |

Deliberately **not** carried over from upstream: methodology modes, `/think`, the `wiki-cli` transport layer, and the contextual-prefix chunk cascade (its best tier requires cloud API calls; our flat-subscription-friendly stack embeds locally).

DragonScale Memory (fold rollups, deterministic `c-NNNNNN` page addresses, semantic tiling duplicate lint, boundary-first autoresearch) is inherited from upstream and kept — with the tiling thresholds recalibrated for `bge-m3` and a documented per-vault recalibration procedure.

## Quick start

Requirements: macOS with Xcode Command Line Tools (the maintained and tested target), [Obsidian](https://obsidian.md), [Claude Code](https://claude.com/claude-code) or Codex CLI, a runnable Python 3.9+, and git. Optional: [Ollama](https://ollama.com) for semantic retrieval, [cmux](https://github.com/wandb/cmux) for parallel tasks, DCG for destructive-command checks, and VoiceInk for voice input. Linux and Windows ports may be feasible, but this project does not currently test or support them.

```bash
# 1. Get the vault
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-clean-machine.sh  # vault + MCP + Docling ru/en + Codex metadata

# 2. Open the folder as a vault in Obsidian, then start Claude Code in it
claude
# > /wiki                        # bootstrap: the agent walks you through personalization
```

`setup-clean-machine.sh` preserves existing Obsidian settings, MCP server entries,
and secrets. Use `--reset-obsidian` only when you intentionally want the three
managed defaults restored; it first backs up the complete `.obsidian` directory.
On macOS it verifies the Command Line Tools and Python before touching the vault.
If the tools are missing, it opens Apple's `xcode-select --install` dialog and
stops; finish the installer and rerun the bootstrap. It also rejects the inert
`/usr/bin/python3` placeholder and selects a working Homebrew Python when present.
The Excalidraw `main.js` bootstrap is pinned and checksum-verified. A different
existing build is preserved with a warning; use
`bash bin/setup-vault.sh --repair-excalidraw "$(pwd)"` (or pass the same flag
to `setup-clean-machine.sh`) to back it up and replace it explicitly.
Obsidian Tasks 8.2.2 is likewise pinned as a verified `main.js` +
`manifest.json` + `styles.css` set. It powers checkbox statuses and monthly
live agenda views, while `scripts/agenda.py` remains authoritative without the
plugin. Existing Tasks settings are preserved; `--repair-tasks` is the explicit
backup-and-replace path for mismatched release assets.
Add `--install-service` after filling `~/.config/mcp-gateway/secrets.env`, and
`--install-codex-plugin` when the Codex CLI is already installed. Docling is
installed in an isolated Python 3.12 environment and its OCR/layout/table models
are prefetched (roughly 1.8 GiB total on Apple Silicon); use `--skip-docling`
for a deliberately lightweight setup. See
[local document ingestion](docs/document-ingestion.md) for formats, cache,
security boundaries, and repair commands.

Install as a plugin (skills + hooks) instead of / in addition to cloning:

```bash
claude plugin marketplace add zerg-su/llm-obsidian
# then: /plugin install llm-obsidian
```

### Local embeddings (recommended)

Optional dense section retrieval and the tiling duplicate-lint run on a local ollama with `bge-m3` (1024-dim, 8k context, 100+ languages, ~1.2 GB):

```bash
brew install ollama
brew services start ollama       # or: ollama serve
ollama pull bge-m3
curl -s http://127.0.0.1:11434 && echo " ollama is up"
```

Without ollama nothing breaks: retrieval returns complete sparse section results with
`degraded=true`. Stop refreshes sparse indexes synchronously, records a fingerprinted
dense marker, and launches a lock-safe worker without waiting for embeddings. Failed
workers retain the marker for a later retry. Required Stop phases default to a 60-second
budget; override it with `LLM_OBSIDIAN_STOP_REQUIRED_TIMEOUT_SEC` (1–600 seconds).

### Optional agent-memory backup

Backup of Claude's separate auto-memory is off by default and never searches
neighboring vaults. To opt in, either set `CLAUDE_MEMORY_DIR` explicitly or run:

```bash
cp config/memory-backup.example.json .vault-meta/memory-backup.json
# edit source and set enabled=true
python3 scripts/memory-backup.py --status
```

Before changing `.claude-memory/`, the helper sanitizes the complete candidate
snapshot and scans both it and the existing backup. Any residual credential
pattern blocks all writes and the turn-end commit. This is pattern-based
defense-in-depth, not proof that arbitrary secrets are absent; review backup
diffs before publishing them.

### First contact

```text
ingest ~/Downloads/some-article.pdf     # source -> structured wiki pages
что ты знаешь про <тему>?               # cited answers from YOUR notes (also EN)
/save                                   # file the current conversation
/journal план на завтра: ...            # date-keyed journal
/agenda собери незавершённое             # preview + atomic carry-over + monthly report
/backlog add не забыть продлить домен   # one-line capture inbox
lint the wiki                           # health check: orphans, dead links, duplicates
```

### Optional voice input with VoiceInk

[VoiceInk](https://github.com/beingpax/VoiceInk) is a native macOS voice-to-text app that can dictate directly into a terminal, so it works equally well in front of Claude Code or Codex. It is a companion input tool, not a dependency or a privileged integration with LLM Obsidian.

VoiceInk's source is available under GPL-3.0 and can be built locally without a subscription. Its maintainer's prebuilt distribution has its own trial/license and support benefits. VoiceInk currently requires macOS 14.4 or later; follow its repository for current installation and licensing details.

## MCP HTTP gateway

Tool schemas aside, MCP's practical tax is processes: every terminal session spawns its own copy of every stdio server. The gateway runs **one** set of children per machine behind [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy); sessions connect over HTTP and survive gateway restarts. Bootstrap installs the exact artifact and SHA-256 recorded in `scripts/mcp-gateway/mcp-proxy.lock.json`—never an unreviewed `latest` release.

```bash
# one-time
bin/setup-clean-machine.sh --skip-vault

# context7 (library docs MCP): get a free key at https://context7.com and add ONE line
#   CONTEXT7_API_KEY=ctx7sk-...
# to ~/.config/mcp-gateway/secrets.env — that is the whole setup.

scripts/mcp-gateway/mcp-gateway.sh doctor    # read-only pre-flight: tells you what's missing
scripts/mcp-gateway/mcp-gateway.sh install   # launchd service, autostarts at login
scripts/mcp-gateway/mcp-gateway.sh health    # real MCP handshake against the first child
```

The one local port setting is `scripts/mcp-gateway/runtime.env`. After changing
it, run `mcp-gateway.sh sync-config --apply`; gateway and default client JSON are
updated together.

Adding your own servers is a config edit (stdio children with `command`/`args`/`env`, remote children with `url`; `${VAR}` placeholders resolve from `secrets.env`, and `doctor` derives the required key list automatically). Full guide, the add-a-server checklist, disaster recovery and hard-won gotchas: **[docs/mcp-gateway.md](docs/mcp-gateway.md)**.

One caveat to know upfront: the gateway saves processes, RAM and cold starts — it does **not** shrink tool schemas in your context. Keep heavy, rarely-used servers in `.mcp-profiles/` (see the README there) and load them per-session with `claude --mcp-config`.

For Codex, mirror the gateway HTTP pointers into TOML:

```bash
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
```

This writes repo-local `.codex/config.toml`, removes duplicate llm-obsidian MCP
servers from `~/.codex/config.toml`, and writes optional profile overlays such as
`~/.codex/llm-obsidian-mcp.config.toml`.

## Skills

| Group | Skills |
|---|---|
| Wiki core | `/wiki` (bootstrap), `/wiki-ingest`, `/wiki-query`, `/wiki-lint`, `/wiki-fold`, `/save`, `/close`, `/autoresearch`, `/canvas`, `/defuddle` |
| Productivity | `/journal` (date-keyed planner), `/agenda` (unfinished-item scan/carry-over/report), `/daily` (end-of-day status), `/backlog` (capture inbox), `/find-session`, `/draft` (reply advisor), `/distill-runbook` (session commands → copy-paste runbook), `/learn` (tutor over your notes), `/save-plan` |
| Orchestration (needs cmux) | `/dispatch`, `/review-dispatch`, `/review-send`, `/reap`, `/reap-send` (Codex: `$llm-obsidian:*`) |
| Reference | `obsidian-markdown`, `obsidian-bases` |

A `UserPromptSubmit` router suggests the right skill from regex rules in `.claude/skill-rules.json` (soft hints, never mandatory); `session-nudge` surfaces overdue maintenance (lint age, fold due, stale backups, a skill-of-the-day tip).

Running an unattended `/dispatch` task end to end (supervisor/watchdog behavior, permission boundaries, typed review callbacks, auto-close, diagnostics): **[docs/unattended-pipeline-operations.md](docs/unattended-pipeline-operations.md)**.

## Testing

Everything mechanical is covered by hermetic suites — no network, no ollama needed:

```bash
make test          # address allocator, tiling, boundary, vault-write/validate,
                   # safe stop-hook + latency, bm25 + fusion, bench harness, router
make test-gateway  # MCP gateway management layer (offline, fake MCP server)
make test-documents # live installed Docling ru/en + Office/PDF acceptance
```

GitHub Actions runs the same hermetic contract on `macos-latest` for pushes,
pull requests, release tags, and manual dispatch. It also rejects generated
Codex marketplace drift. Real unattended behavior is measured separately:

```bash
python3 scripts/pipeline-stats.py --days 30
```

See [pipeline observability](docs/pipeline-observability.md) for definitions,
the strict no-content telemetry boundary, and the 10–20-task dogfood threshold.

## Codex CLI

Codex uses a generated local plugin marketplace instead of legacy skill symlinks:

```bash
python3 scripts/codex-adapter.py --apply
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
codex plugin marketplace add "$(pwd)"
codex plugin add llm-obsidian@llm-obsidian-codex
```

Start a new Codex thread after installing or updating. Invoke skills explicitly
as `$llm-obsidian:save`, `$llm-obsidian:wiki-query`,
`$llm-obsidian:review-dispatch`,
`$llm-obsidian:close`, etc.
Claude prompt/session/tool hooks remain Claude-specific. The installed Codex
plugin does run the shared, safe `Stop` pipeline, but does not claim automatic
`SessionStart`, `UserPromptSubmit`, or `PostToolUse` parity. See the
[runtime capability matrix](docs/runtime-capabilities.md).

Codex limit helpers are bundled too:

```bash
python3 scripts/codex-limit-monitor.py --install
codex-limit-status --scope recent --once
.codex/codex-limits-status.py --with-pct --compact
```

For cmux status integration, register `.codex/update-cmux-limits.sh` from your
Codex `SessionStart`, `UserPromptSubmit`, and `Stop` hooks.

## DCG Guard

The repo ships a portable Destructive Command Guard policy and installer:

```bash
bin/setup-dcg.sh          # install dcg if missing, config, Codex + Claude hooks
bin/setup-dcg.sh --check  # report local drift without writing
bash scripts/dcg-test-suite.sh
```

The policy lives in `config/dcg/config.toml`; hook template in
`.github/hooks/dcg.json`. The installer backs up existing hook/config files with
`.bak-dcg-*` suffixes and merges only the `PreToolUse`/`Bash` dcg entry. The
smoke suite validates the repo policy in an isolated temporary HOME by default;
set `DCG_TEST_USE_USER_CONFIG=1` to test the machine's live allowlist/config.

## Limitations

- **macOS is the only maintained and exercised platform.** cmux, launchd services, status-line integration, and the unattended lifecycle are tested there. Other platforms are a porting opportunity, not a support claim.
- **Claude Code and Codex CLI are the only first-class agents.** Host hook capabilities differ; consult the [runtime capability matrix](docs/runtime-capabilities.md).
- **cmux is required for dispatch/review orchestration.** The vault, retrieval, writing, and most productivity skills work without it.
- **Ollama is optional, not invisible.** Dense retrieval consumes local RAM/disk and needs a downloaded model; sparse retrieval is the supported fallback.
- **Cross-review is bounded assurance, not formal verification.** A second model can still miss defects or share the same wrong premise.
- **No cloud model is bundled or made free.** You bring the official CLI access and subscriptions required by the routes you choose.

## Roadmap

- Stabilize the unattended pipeline with 10–20 real-task samples in both executor directions; publish measured completion, callback, intervention, surface, and p50/p95 results.
- Close safe Codex interception gaps for unified/streaming shell calls and native web search where the host exposes reliable lifecycle events.
- Publish more end-to-end macOS acceptance fixtures and operational examples from the dogfood window.
- Expand RU documentation while keeping one canonical tested behavior contract.
- Add more example MCP profiles without loading rarely used tool schemas into every session.
- Explore Linux and other-agent adapters only when they can preserve typed handoffs, permission boundaries, lifecycle supervision, and hermetic tests.

## Credits & license

MIT, with the upstream copyright preserved — see [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md). Core lineage: Andrej Karpathy's LLM Wiki pattern → [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) → a private DevOps vault where these mechanics were incubated → this repo.
