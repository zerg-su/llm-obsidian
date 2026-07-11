# llm-obsidian

**A self-organizing second brain for Obsidian, operated by an LLM agent.** Your assistant builds and maintains a structured, cross-linked wiki out of your conversations, sources and decisions — and then actually *finds* things in it, in Russian or English, fully locally.

🇷🇺 **Читайте по-русски: [README.ru.md](README.ru.md)**

Based on [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) by AgriciDaniel (an implementation of Andrej Karpathy's *LLM Wiki* pattern), heavily reworked: the retrieval stack, write path, hooks and MCP integration were redesigned and battle-tested for months as the daily working memory of a DevOps engineer before being extracted into this generic release. Claude Code and Codex CLI are supported first-class agents — which is why this is *llm*-obsidian, not *claude*-obsidian.

---

## What you get

- **A wiki that grows itself.** `ingest <path|URL>` turns raw material into 8-15 cross-linked typed pages; `/save` files insights from any conversation; `/autoresearch` runs autonomous research loops; every approved plan is auto-captured into `wiki/plans/`.
- **Retrieval that is measured, not vibed.** H2/H3 sections are bounded to 800 words with 100-word overlap, ranked sparsely, and deduplicated to the best heading/snippet per page; optional local `bge-m3` joins through RRF. A 48-query RU/EN goldset is half held out, and `make bench-retrieval` rejects hit@5/MRR regressions over 0.02.
- **RU-first, local-first.** The mandatory sparse channel handles Cyrillic and mixed technical vocabulary without a service. Optional `bge-m3` embeddings stay on your machine; no cloud API is required.
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
| Productivity layer | — | `/journal`, `/daily`, `/backlog`, `/find-session`, `/draft`, `/distill-runbook`, `/save-plan` |
| Docs language | EN | EN + RU, RU-first audience |

Deliberately **not** carried over from upstream: methodology modes, `/think`, the `wiki-cli` transport layer, and the contextual-prefix chunk cascade (its best tier requires cloud API calls; our flat-subscription-friendly stack embeds locally).

DragonScale Memory (fold rollups, deterministic `c-NNNNNN` page addresses, semantic tiling duplicate lint, boundary-first autoresearch) is inherited from upstream and kept — with the tiling thresholds recalibrated for `bge-m3` and a documented per-vault recalibration procedure.

## Quick start

Requirements: macOS (Linux mostly works, launchd bits are macOS-only), [Obsidian](https://obsidian.md), [Claude Code](https://claude.com/claude-code) or Codex CLI, Python 3.9+, git. Optional: [ollama](https://ollama.com) for semantic retrieval (recommended), cmux for parallel tasks.

```bash
# 1. Get the vault
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-clean-machine.sh  # vault + MCP gateway config + Codex metadata

# 2. Open the folder as a vault in Obsidian, then start Claude Code in it
claude
# > /wiki                        # bootstrap: the agent walks you through personalization
```

`setup-clean-machine.sh` preserves existing Obsidian settings, MCP server entries,
and secrets. Use `--reset-obsidian` only when you intentionally want the three
managed defaults restored; it first backs up the complete `.obsidian` directory.
The Excalidraw `main.js` bootstrap is pinned and checksum-verified. A different
existing build is preserved with a warning; use
`bash bin/setup-vault.sh --repair-excalidraw "$(pwd)"` (or pass the same flag
to `setup-clean-machine.sh`) to back it up and replace it explicitly.
Add `--install-service` after filling `~/.config/mcp-gateway/secrets.env`, and
`--install-codex-plugin` when the Codex CLI is already installed.

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
/backlog add не забыть продлить домен   # one-line capture inbox
lint the wiki                           # health check: orphans, dead links, duplicates
```

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
| Productivity | `/journal` (date-keyed planner), `/daily` (end-of-day status), `/backlog` (capture inbox), `/find-session`, `/draft` (reply advisor), `/distill-runbook` (session commands → copy-paste runbook), `/learn` (tutor over your notes), `/save-plan` |
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
```

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

## Roadmap

- **Codex prompt/tool hook parity**: skills, MCP sync, shared scripts, and the safe `Stop` pipeline are supported; `SessionStart`, `UserPromptSubmit`, and `PostToolUse` remain host-specific.
- RU localization of skill bodies (currently EN — measurably better instruction-following; RU trigger words already work).
- More example MCP servers in the gateway config.
- Generic ports of the remaining incubator skills (morning briefing, commit digest, verification gates, debug discipline).

## Credits & license

MIT, with the upstream copyright preserved — see [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md). Core lineage: Andrej Karpathy's LLM Wiki pattern → [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) → a private DevOps vault where these mechanics were incubated → this repo.
