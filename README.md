# llm-obsidian

**A self-organizing second brain for Obsidian, operated by an LLM agent.** Your assistant builds and maintains a structured, cross-linked wiki out of your conversations, sources and decisions — and then actually *finds* things in it, in Russian or English, fully locally.

🇷🇺 **Читайте по-русски: [README.ru.md](README.ru.md)**

Based on [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) by AgriciDaniel (an implementation of Andrej Karpathy's *LLM Wiki* pattern), heavily reworked: the retrieval stack, write path, hooks and MCP integration were redesigned and battle-tested for months as the daily working memory of a DevOps engineer before being extracted into this generic release. Claude Code is the supported agent today; the scripts are agent-agnostic and a Codex adapter is on the roadmap — which is why this is *llm*-obsidian, not *claude*-obsidian.

---

## What you get

- **A wiki that grows itself.** `ingest <path|URL>` turns raw material into 8-15 cross-linked typed pages; `/save` files insights from any conversation; `/autoresearch` runs autonomous research loops; every approved plan is auto-captured into `wiki/plans/`.
- **Retrieval that is measured, not vibed.** Hybrid search = dense embeddings (local [ollama](https://ollama.com) + `bge-m3`) fused with BM25 and a tag prefilter. A benchmark harness (`make bench-retrieval`) scores hit@1 / hit@5 / MRR@10 against your own goldset — no ranking change ships without moving the numbers.
- **RU-first, local-first.** `bge-m3` handles Russian prose and transliterated tech terms that English-centric embedding models miss (on our calibration vault the dense channel went from hit@1 0.27 to **0.85** after the swap). Zero cloud API calls: everything embeds on your machine.
- **A deterministic write path.** `wiki/log.md` (append-only journal) and `wiki/hot.md` (~500-word recent-context cache loaded on session start) are written only through `scripts/vault-write.py`, which enforces hard caps so the hot cache stays a cache and never rots into a second journal.
- **An industrial Stop hook.** On every turn end: reindex, BM25 rebuild, incremental dense-embedding refresh, sanitized memory backup, auto-commit — serialized under `flock` so parallel sessions never corrupt each other, with per-phase latency telemetry and a slow-turn warning.
- **MCP without the process zoo.** A local HTTP gateway (one launchd service) fronts all your MCP servers: one set of long-lived processes per machine instead of per-terminal duplicates. Ships with [context7](https://context7.com) preconfigured — add one API-key line and library docs are available in every session.
- **Parallel work, optional.** `/dispatch` spawns a task in a separate git worktree + [cmux](https://github.com/wandb/cmux) split (with plan handoff to a cheaper model), `/reap` files its results back into the wiki. Requires cmux; everything else works without it.

## Why this fork

| | upstream claude-obsidian | llm-obsidian |
|---|---|---|
| Dense retrieval | contextual-prefix cascade, cloud API tier for best results | local ollama `bge-m3`, no cloud calls, RU-capable (dense hit@1 0.27 → 0.85 on a RU/EN goldset) |
| Fusion | chunk cascade | scope-aware fusion: dense ranks what it embeds, BM25 *injects only* pages outside the dense scope — tuned on a goldset with a held-out half after plain weighted RRF measurably failed |
| Retrieval QA | one-off benchmark scripts | permanent harness: goldset + `make bench-retrieval`, hit@1/hit@5/MRR@10 per channel, degradation testing |
| hot/log write path | free-form model edits | `vault-write.py` payload API with deterministic caps + `validate-vault.py` |
| Turn-end hook | inline hooks.json commands | `stop.sh`: flock serialization, atomic writes, incremental index refresh, latency telemetry (`STOP_HOOK_SLOW`) |
| Skill routing | descriptions only | data-driven `skill-router` hook (regex rules → soft hints) + `session-nudge` maintenance hints |
| MCP | per-session stdio servers | HTTP gateway: 1 process set per machine, secrets outside the repo, `doctor`/`smoke`/`update` tooling, profile pattern for heavy servers |
| Orchestration | — | `/dispatch` / `/reap` worktree + split workflow with auto plan handoff |
| Productivity layer | — | `/journal`, `/daily`, `/backlog`, `/find-session`, `/draft`, `/distill-runbook`, `/save-plan` |
| Docs language | EN | EN + RU, RU-first audience |

Deliberately **not** carried over from upstream: methodology modes, `/think`, the `wiki-cli` transport layer, and the contextual-prefix chunk cascade (its best tier requires cloud API calls; our flat-subscription-friendly stack embeds locally).

DragonScale Memory (fold rollups, deterministic `c-NNNNNN` page addresses, semantic tiling duplicate lint, boundary-first autoresearch) is inherited from upstream and kept — with the tiling thresholds recalibrated for `bge-m3` and a documented per-vault recalibration procedure.

## Quick start

Requirements: macOS (Linux mostly works, launchd bits are macOS-only), [Obsidian](https://obsidian.md), [Claude Code](https://claude.com/claude-code), Python 3.9+, git. Optional: [ollama](https://ollama.com) for semantic retrieval (recommended), cmux for parallel tasks.

```bash
# 1. Get the vault
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-vault.sh          # one-time provisioning (downloads Excalidraw plugin binary)

# 2. Open the folder as a vault in Obsidian, then start Claude Code in it
claude
# > /wiki                        # bootstrap: the agent walks you through personalization
```

Install as a plugin (skills + hooks) instead of / in addition to cloning:

```bash
claude plugin marketplace add zerg-su/llm-obsidian
# then: /plugin install llm-obsidian
```

### Local embeddings (recommended)

Dense retrieval and the tiling duplicate-lint run on a local ollama with `bge-m3` (1024-dim, 8k context, 100+ languages, ~1.2 GB):

```bash
brew install ollama
brew services start ollama       # or: ollama serve
ollama pull bge-m3
curl -s http://127.0.0.1:11434 && echo " ollama is up"
```

Without ollama nothing breaks: hybrid search degrades to BM25-only automatically, and the Stop hook skips the embedding refresh.

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

Tool schemas aside, MCP's practical tax is processes: every terminal session spawns its own copy of every stdio server. The gateway runs **one** set of children per machine behind [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy); sessions connect over HTTP and survive gateway restarts.

```bash
# one-time
mkdir -p ~/.local/bin ~/.config/mcp-gateway
# download the mcp-proxy release binary for your platform -> ~/.local/bin/mcp-proxy && chmod +x
cp scripts/mcp-gateway/config.json.example scripts/mcp-gateway/config.json
cp scripts/mcp-gateway/secrets.env.example ~/.config/mcp-gateway/secrets.env
chmod 600 ~/.config/mcp-gateway/secrets.env
cp .mcp.json.example .mcp.json

# context7 (library docs MCP): get a free key at https://context7.com and add ONE line
#   CONTEXT7_API_KEY=ctx7sk-...
# to ~/.config/mcp-gateway/secrets.env — that is the whole setup.

scripts/mcp-gateway/mcp-gateway.sh doctor    # read-only pre-flight: tells you what's missing
scripts/mcp-gateway/mcp-gateway.sh install   # launchd service, autostarts at login
scripts/mcp-gateway/mcp-gateway.sh health    # real MCP handshake against the first child
```

Adding your own servers is a config edit (stdio children with `command`/`args`/`env`, remote children with `url`; `${VAR}` placeholders resolve from `secrets.env`, and `doctor` derives the required key list automatically). Full guide, the add-a-server checklist, disaster recovery and hard-won gotchas: **[docs/mcp-gateway.md](docs/mcp-gateway.md)**.

One caveat to know upfront: the gateway saves processes, RAM and cold starts — it does **not** shrink tool schemas in your context. Keep heavy, rarely-used servers in `.mcp-profiles/` (see the README there) and load them per-session with `claude --mcp-config`.

## Skills

| Group | Skills |
|---|---|
| Wiki core | `/wiki` (bootstrap), `/wiki-ingest`, `/wiki-query`, `/wiki-lint`, `/wiki-fold`, `/save`, `/close`, `/autoresearch`, `/canvas`, `/defuddle` |
| Productivity | `/journal` (date-keyed planner), `/daily` (end-of-day status), `/backlog` (capture inbox), `/find-session`, `/draft` (reply advisor), `/distill-runbook` (session commands → copy-paste runbook), `/learn` (tutor over your notes), `/save-plan` |
| Orchestration (needs cmux) | `/dispatch`, `/reap`, `/reap-send` |
| Reference | `obsidian-markdown`, `obsidian-bases` |

A `UserPromptSubmit` router suggests the right skill from regex rules in `.claude/skill-rules.json` (soft hints, never mandatory); `session-nudge` surfaces overdue maintenance (lint age, fold due, stale backups, a skill-of-the-day tip).

## Testing

Everything mechanical is covered by hermetic suites — no network, no ollama needed:

```bash
make test          # address allocator, tiling, boundary, vault-write/validate,
                   # stop-hook (flock + latency), bm25 + fusion, bench harness, router
make test-gateway  # MCP gateway management layer (offline, fake MCP server)
```

## Roadmap

- **Codex adapter**: the scripts are already agent-agnostic; port the hook layer (`hooks.json` is Claude Code specific) and validate the skill contracts under Codex.
- RU localization of skill bodies (currently EN — measurably better instruction-following; RU trigger words already work).
- More example MCP servers in the gateway config.
- Generic ports of the remaining incubator skills (morning briefing, commit digest, verification gates, debug discipline).

## Credits & license

MIT, with the upstream copyright preserved — see [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md). Core lineage: Andrej Karpathy's LLM Wiki pattern → [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) → a private DevOps vault where these mechanics were incubated → this repo.
