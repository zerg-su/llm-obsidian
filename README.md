<p align="center">
  <img src="docs/assets/llm-obsidian-banner.png" alt="Claude Code and Codex exchanging reviewed work through an Obsidian knowledge vault" width="100%">
</p>

# LLM Obsidian

[![CI](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml/badge.svg)](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml)

**A local-first project workspace where Claude Code and Codex CLI share durable memory, reusable skills, visible cmux orchestration, and independent cross-model review.**

Language: **English** · [Русский](README.ru.md)
Release history: [English](CHANGELOG.md) · [Русский](CHANGELOG.ru.md)

LLM Obsidian is an Obsidian vault and an agent toolkit in one repository. It turns conversations, plans, source documents, decisions, shell history, research, and completed tasks into structured Markdown that remains useful after a model session ends. The same repository then supplies both supported CLIs with versioned skills, deterministic scripts, retrieval, safety checks, and a complete dispatch → review → reap lifecycle.

It is designed for sustained work on a laptop: software projects, infrastructure, research, personal knowledge systems, and long-running operational work. It is not a hosted agent platform, a provider proxy, or a way around subscriptions and rate limits. You keep using the official Claude Code and Codex CLIs with your own access; this project gives them a shared operating system for knowledge and work.

## What makes it different

Most agent setups optimize one prompt or one coding session. LLM Obsidian optimizes the whole working loop:

- **The result survives the chat.** Plans, decisions, sources, reviews, and task outcomes become linked, diffable files rather than hidden conversation memory.
- **Claude and Codex work as one system.** They share the same vault and mechanics while retaining independent model context and different failure modes.
- **Review is a real lifecycle, not “ask another model.”** Reviewers are product-read-only, callbacks are typed and operation-scoped, fixes return to the executor, and every validated round can be archived with the result.
- **Long work stays visible.** cmux opens task and reviewer sessions beside the coordinator, preserves interactive context, watches activity, and closes only the exact surface whose process has exited.
- **Code handles repeatable mechanics.** Routing, candidate discovery, fingerprints, validation, retries, cleanup, telemetry, indexing, and transactional writes are scripts. Models spend tokens on interpretation and judgment.
- **The wiki is a data layer, not an agent side effect.** `wiki/` and its derived `.vault-meta/` indexes are intentionally separated from behavioral code, so ordinary note changes do not invalidate the pipeline.
- **Quality is measured.** Hermetic tests cover the mechanics; retrieval has a RU/EN goldset; releases have a 58-cell Claude × Codex live acceptance matrix with semantic evidence reuse.

The guiding rule is simple: if a deterministic program can perform a step without reducing result quality, the program should own it. The model should decide what the step means, not repeatedly reconstruct how to execute it.

## Who it is for

| You are… | What the repository gives you |
|---|---|
| **Software engineer** | Requirements clarification, code plans, isolated worktrees, opposite-model review, durable decisions, runbooks, and searchable project history. |
| **DevOps / platform engineer** | Visible parallel operations, command capture, incident/runbook memory, strict external-effect boundaries, task supervision, and local MCP infrastructure. |
| **Researcher or analyst** | Protected web acquisition, networkless synthesis, document normalization, source provenance, multilingual retrieval, and linked notes. |
| **Knowledge worker** | Journal, agenda, backlog, daily summaries, saved conversations, document ingestion, drafting help, and an Obsidian UI over plain files. |
| **Power user running several projects** | One cmux workspace can hold project/task tabs; each task keeps stable IDs and model-specific lanes instead of guessing by window title or recency. |

The strongest experience is on a macOS laptop with cmux. Most vault, retrieval, document, and productivity workflows are useful without cmux; visible unattended orchestration is the macOS-first part.

## The architecture

```text
keyboard / optional VoiceInk
             │
             ▼
┌──────────────────── coordinator session ────────────────────┐
│ Claude Code or Codex CLI                                    │
│   │                                                         │
│   ├── repo skills ──► deterministic Python/Bash runners     │
│   ├── retrieval ────► Obsidian Markdown + local indexes     │
│   └── cmux broker ──► task/reviewer sessions on the right   │
└──────────────────────────┬───────────────────────────────────┘
                           │ typed IDs, hashes, callbacks
                 ┌─────────┴─────────┐
                 ▼                   ▼
        isolated task worktree   opposite-model reviewer
        Claude or Codex          Codex or Claude
                 │                   │
                 └──── fixes ◄───────┘
                           │
                           ▼
                 validated reap transaction
                           │
                           ▼
             linked Obsidian result + review history
```

The canonical state is ordinary Markdown, JSON/TOML contracts, Git history, and reproducible scripts. Obsidian is the human interface and link graph; it is not required to run in the background for the scripts to work.

### A task from question to durable result

1. **Understand.** `clarify` (the built-in “grill me” workflow) inspects repository facts and asks one material question at a time before code or a plan.
2. **Plan.** Important decisions become a saved plan with provenance and a stable DragonScale address.
3. **Dispatch.** A code-owned runner captures project/task/session IDs, the exact model route, approved plan hash, permission domain, worktree, and caller cmux surface. It opens the task to the right of the correct coordinator instead of whichever tab happens to be selected later.
4. **Execute.** Claude or Codex works in an isolated Git worktree. Same-model bounded work normally uses an internal agent; an explicit separate window creates a durable visible lane.
5. **Review.** The opposite runtime receives a read-only request bound to the exact baseline. It returns typed findings; a verification round resumes the same model/task/domain lane so context is not paid for twice.
6. **Reap.** A typed final summary, approved review archive, plan hash, result path, and session provenance are validated. One vault transaction writes the result and closes the plan.
7. **Exit safely.** `/exit` is armed only after the lifecycle is complete. The supervisor closes that exact surface after the agent process exits; it never guesses another tab.

Review approval finishes a **round**, not the whole task. The task remains resumable until final reap. Archived sessions are never silently attached to another task ID.

## Where code saves tokens without lowering quality

| Mechanical work | Code-owned implementation | Why it matters |
|---|---|---|
| Session readiness | `session-preflight.py`, generated-config checks, dependency detection | One fast local check replaces repeated model inspection. Missing optional components produce exact repair commands. |
| Model selection | `config/model-routing.toml` + `model_routing.py` | Concrete defaults live in one place; task metadata records the resolved route. No model-name hardcoding across dozens of skills. |
| Repository/context candidates | dispatch resolver and task registry | IDs and validated paths replace token-heavy guessing about repo, plan, window, or prior session. |
| Vault mutation | `vault-write.py` | One optimistic, journaled transaction replaces many fragile edits to pages, log, hot list, plan, and manifest. |
| Search | section BM25 + optional local embeddings | The model sees the best bounded sections, not whole folders or repeated page bodies. |
| Web cleanup | `defuddle` before synthesis | Navigation, ads, and boilerplate are removed before they consume context. |
| Document conversion | cached stdlib/Docling pipeline | OCR and parsing are reused by source hash instead of spending model tokens rereading unchanged binaries. |
| Review transport | operation-scoped JSON outbox and deterministic `drive` | No long callback paths or free-form findings need to be copied between terminal windows. |
| Acceptance reruns | semantic per-cell fingerprints | A docs or data-only change reuses valid evidence; only behaviorally affected cells call a model again. |
| Monitoring | content-free heartbeat and numeric telemetry | The pipeline can distinguish active work from a stall without storing prompts, responses, or screen text. |
| Finalization | `reap-runner.py` | Review archival, result routing, plan close, reindex, validation, and exact exit happen through one fail-closed contract. |

This division is deliberate. Requirements, interpretation, synthesis, code review, and risk judgment stay with models and people. Hashing, routing, schema checks, filesystem bookkeeping, and retry policy stay in code.

## Cross-model review

Claude can implement while Codex reviews; Codex can implement while Claude reviews. The default reviewer route is the opposite runtime, and explicit overrides are recorded rather than silently substituted.

A review operation contains:

- opaque project, task, lane, operation, runtime, model, and permission-domain identities;
- the reviewed branch and stable baseline;
- a product-read-only mandate and a single isolated outbox write path;
- typed severity, evidence, recommendation, verification gaps, and residual risks;
- bounded safe transitions owned by `review-dispatch drive --apply-action`;
- same-session verification after executor fixes;
- a durable archive linked from the final task result.

Reviewers cannot push, publish, mutate product files, or broaden scope. A warning can be fixed and verified automatically inside the approved task. A blocking scope, security, permission, migration, destructive, or external-effect decision returns to the user.

Different models can still share a bad premise, so this is bounded assurance—not formal verification. The strength comes from combining independent context, tests, typed evidence, and an explicit human boundary.

## Obsidian as durable memory

Every durable page has typed frontmatter, timestamps, tags, session provenance, and a deterministic `c-NNNNNN` address. Pathless `[[wikilinks]]` keep the vault portable and readable in Obsidian, GitHub, a text editor, or another agent.

The write path is intentionally strict:

```text
model proposes structured content
  -> duplicate/title/link checks
  -> one JSON transaction
  -> optimistic SHA-256 validation
  -> crash-safe journal
  -> reindex
  -> whole-vault validation
  -> scoped commit by the Stop pipeline
```

The vault supports personal and non-personal knowledge equally well. A private daily journal, a team architecture corpus, a code repository's decisions, or a DevOps runbook collection use the same primitives. Credentials and machine-local runtime state are never meant to live in the wiki.

DragonScale Memory contributes deterministic addresses, content-hash fold rollups, boundary-first research, and semantic tiling checks for near-duplicate pages. `wiki/log.md` is an append-only operational history; `wiki/hot.md` is a bounded current view; derived `.vault-meta/` indexes are regenerated rather than hand-edited.

## Retrieval: hybrid by default, useful without embeddings

The supported retrieval unit is an H2/H3 section, capped at 800 words with 100-word overlap. Sparse ranking indexes title, tags, headings, and body using a Unicode tokenizer with Russian stopwords. Results are deduplicated to the best heading/snippet per page.

When local Ollama and `bge-m3` are available, the dense multilingual channel joins sparse results through rank fusion. When they are absent, search remains usable through the complete sparse path and the session preflight explains how to install the enhancement. It does not silently pretend hybrid search ran.

Retrieval changes are benchmark-gated against a committed RU/EN goldset. `make bench-retrieval` reports hit@1, hit@5, MRR@10, recall, and section NDCG and rejects material regressions.

## Documents: normalize locally, then ask the model

`wiki-ingest` accepts Markdown, text, JSON, YAML, CSV, local HTML, PDF, DOCX, PPTX, XLSX, OpenDocument, EPUB, and scans. The source remains read-only.

```text
local source
  -> format/size/page checks
  -> stdlib fast path for text-like files
     OR isolated pinned Docling + EasyOCR for binary/scanned files
  -> content-addressed normalized artifact
  -> quality gate
  -> model synthesis into linked pages
  -> one vault transaction
```

Docling runs before the LLM. The bootstrap prefetches layout, table, and `ru,en` OCR artifacts and disables remote services, external plugins, and runtime downloads during conversion. An unchanged file reuses the cache. A missing dependency or low-quality extraction stops with a typed action instead of silently sending the binary to a model.

See [document ingestion](docs/document-ingestion.md) for limits, cache layout, and recovery.

## Protected research

Networked research uses two isolated contexts:

1. a web-enabled fetcher that cannot read the vault;
2. a networkless synthesizer that receives only the validated artifact and can write through the vault contract.

Persistent task lanes retain provider context only inside the exact task and isolation domain, so follow-up research does not start from zero. Every operation gets fresh scratch. `unsafe-research` is a separate, explicitly authorized escape hatch for a single-context scenario; it never becomes a silent fallback from protected research.

## The 29 shipped skills

Claude invokes them through its plugin UI (`/skill`). Codex uses the generated repo-local marketplace (`$llm-obsidian:skill`). The mechanics live in `skills/<name>/SKILL.md`, so another coding agent can follow them manually even without plugin support.

| Area | Skills and purpose |
|---|---|
| **Orientation and alignment** | `wiki` bootstraps the vault; `clarify` performs one-question-at-a-time requirements/design alignment before implementation. |
| **Capture and writing** | `save`, `save-plan`, `journal`, `backlog`, `daily`, and `agenda` turn conversations and dated work into canonical vault data. |
| **Knowledge access** | `wiki-query`, `find-session`, `wiki-lint`, and `wiki-fold` retrieve, audit, and compact durable knowledge. |
| **Documents and web** | `wiki-ingest`, `defuddle`, `autoresearch`, and `unsafe-research` normalize sources and keep trust domains explicit. |
| **Thinking and communication** | `draft` proposes redacted external replies; `learn` tutors from your notes; `distill-runbook` turns sanitized shell history into human-executable procedures. |
| **Obsidian-native output** | `obsidian-markdown`, `obsidian-bases`, and `canvas` produce correct links, properties, database views, and visual canvases. |
| **Task orchestration** | `dispatch`, `dispatch-workspace`, `review-dispatch`, `review-send`, `reap-send`, `reap`, and `close` implement the visible multi-session lifecycle. |

The router provides soft hints for phrases such as “clarify before code” and “grill me”; it never forces a skill. Session-start nudges report missing optional dependencies, stale indexes, due folds, and other actionable degradation once per session rather than on every command.

## External tools and why they are used

| Tool | Required? | Role |
|---|---:|---|
| **macOS + Xcode Command Line Tools** | Maintained target | Git/toolchain base and the tested host for cmux, launchd, and the unattended lifecycle. |
| **Python 3.9+** | Yes | Portable deterministic core: writer, retrieval, schemas, runners, telemetry, validation, and tests. |
| **Git** | Yes | History, optimistic evidence, isolated worktrees, review baselines, and release provenance. |
| **Obsidian** | Core UX | Human browsing/editing of the Markdown vault, backlinks, Bases, Canvas, Tasks, and Excalidraw. Scripts remain usable without the app running. |
| **Claude Code** | One supported agent | Coordinator, executor, bounded subagent host, or opposite-model reviewer through the official CLI. |
| **Codex CLI** | One supported agent | Same roles through its official CLI, repo plugin marketplace, profiles, and shared Stop pipeline. |
| **cmux** | Required for multi-session orchestration | Visible splits/workspaces, exact surface IDs, interactive resume, notifications, and lifecycle cleanup. |
| **Homebrew** | Bootstrap helper | Installs missing macOS prerequisites such as `uv`; not used as an application runtime abstraction. |
| **uv** | Required for default document setup | Builds an isolated, pinned Docling environment without polluting the agent Python. |
| **Docling + EasyOCR** | Default install; optional for text-only use | Local PDF/Office/EPUB normalization, tables, and Russian/English OCR before model synthesis. |
| **Ollama + `bge-m3`** | Optional, recommended | Local multilingual dense embeddings and semantic duplicate checks; sparse search is the fallback. |
| **mcp-proxy** | Optional gateway core | One pinned local HTTP gateway process fronts MCP children instead of spawning each server per terminal. |
| **Context7 MCP** | Optional example | Current library documentation through the gateway; enabled by one user-supplied API key. |
| **DCG** | Optional, recommended | Destructive-command preflight for both CLIs. It is defense in depth, not a sandbox. |
| **Obsidian Tasks** | Optional UI layer | Displays plan/reminder checkboxes and live agenda views; Python contracts remain authoritative. |
| **Excalidraw** | Optional UI layer | Rich diagrams inside the vault; bootstrap can verify/repair the pinned plugin asset. |
| **VoiceInk** | Optional | macOS voice input into either CLI; no special agent protocol or cloud dependency is assumed by this repo. |
| **launchd** | macOS system service | Keeps the MCP gateway available across terminal sessions. |

No cloud model or commercial service is bundled. Optional services keep their credentials outside Git in user-owned configuration.

## MCP without a process zoo

The local HTTP gateway runs one pinned [mcp-proxy](https://github.com/TBXark/mcp-proxy) service per machine. Claude and Codex connect to stable `127.0.0.1` routes instead of starting another copy of every stdio server in every terminal.

```bash
cp scripts/mcp-gateway/config.json.example scripts/mcp-gateway/config.json
cp scripts/mcp-gateway/secrets.env.example ~/.config/mcp-gateway/secrets.env
chmod 600 ~/.config/mcp-gateway/secrets.env

# Optional Context7 example:
# CONTEXT7_API_KEY=...

scripts/mcp-gateway/mcp-gateway.sh doctor
scripts/mcp-gateway/mcp-gateway.sh install
scripts/mcp-gateway/mcp-gateway.sh health
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
```

The gateway reduces processes, RAM, and cold starts. It cannot reduce the number of tool schemas already loaded into a model context, so heavy servers belong in opt-in `.mcp-profiles/`. Full operations guide: [MCP gateway](docs/mcp-gateway.md).

## Quick start

Requirements: macOS, Xcode Command Line Tools, Git, a runnable Python 3.9+, Obsidian, and at least one of Claude Code or Codex CLI.

```bash
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-clean-machine.sh
```

The bootstrap:

- preserves existing Obsidian settings and secrets;
- initializes the vault and managed plugin assets;
- generates Claude/Codex plugin metadata;
- verifies or installs the pinned MCP proxy;
- installs the isolated Docling + RU/EN OCR runtime unless `--skip-docling` is given;
- prints actionable repair steps rather than guessing credentials.

Open the directory as an Obsidian vault, then start an agent in the same directory:

```bash
claude
# or
codex
```

For Claude, add the local marketplace/plugin through its plugin UI. For Codex:

```bash
python3 scripts/codex-adapter.py --apply
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
codex plugin marketplace add "$(pwd)"
codex plugin add llm-obsidian@llm-obsidian-codex
```

Start a new Codex thread after installing or updating so the host reloads the skill registry.

### Optional local embeddings

```bash
brew install ollama
brew services start ollama
ollama pull bge-m3
```

The session preflight reports when hybrid retrieval is degraded and supplies the installation command. It reminds once per session, not once per search.

### First useful commands

```text
wiki                              # understand/personalize the vault
clarify before code               # one material question at a time
save this                         # file a durable insight
ingest ~/Downloads/design.pdf     # normalize and link a document
find the earlier incident session # retrieve prior work
dispatch this approved plan       # isolated visible task (cmux)
review with the other model       # typed cross-model gate
```

In Codex, use explicit names such as `$llm-obsidian:wiki-query`, `$llm-obsidian:clarify`, and `$llm-obsidian:review-dispatch`.

## Testing and release evidence

```bash
make test                 # full hermetic suite; no network or Ollama
make bench-retrieval      # measured ranking gate
make acceptance-check     # model-free matrix/dependency contract
make acceptance-live      # resume only behaviorally affected live cells
```

The release matrix contains 29 skills × 2 runtimes = 58 cells. v2.1.2 uses a minimal committed seed vault, deterministic synthetic commits, exact runtime registrations, semantic fingerprints, atomic checkpoints, and integrity-protected reuse. A Markdown-only release-note change does not trigger 58 paid model sessions; an undeclared behavioral dependency fails closed before any model starts.

Acceptance heartbeat records only stage/status/counters/timestamps. Prompts, responses, commands, snippets, page bodies, queries, and error text are rejected from the telemetry schema. See [acceptance architecture](docs/acceptance-architecture.md) and [pipeline observability](docs/pipeline-observability.md).

## Security and trust boundaries

- Official Claude/Codex authentication, subscriptions, limits, and safety controls remain in force.
- Reviewers are product-read-only; isolated fetchers cannot read the vault; synthesizers are networkless.
- Vault writes use optimistic hashes and a durable recovery journal.
- Credentials belong in user-owned files such as `~/.config/mcp-gateway/secrets.env`, never in the repository.
- Task metadata and callbacks are validated against strict schemas and exact IDs.
- DCG, host sandboxing, review, and tests are separate layers; none is treated as proof of safety.
- Push, deployment, publication, destructive history edits, credentials, and material scope expansion require explicit authority.
- A repo-owned mechanism failure may be narrowly repaired only under the documented reversible boundary; Stop hooks remain fail-closed.

Read [unattended pipeline operations](docs/unattended-pipeline-operations.md), [task sessions](docs/task-sessions.md), and the [failure-to-repair contract](docs/skill-references/failure-repair-contract.md) for the exact rules.

## Platform scope and limitations

- **macOS is the maintained, release-gated platform.** cmux, launchd, status integration, document setup, and full unattended lifecycle are tested there.
- **Linux has basic script-level portability, not full product support.** Core Python/Bash mechanics can work, but the cmux/launchd-centered experience is not currently promised or release-gated.
- **Windows is not supported.**
- **Claude Code and Codex CLI are the only first-class runtimes.** Another adapter is useful only if it preserves the same contracts and tests.
- **cmux is required for visible dispatch/review/research lanes.** Wiki, retrieval, writing, and most productivity skills work without it.
- **Cross-model review is not formal verification.**
- **Mobile is not the primary operating surface.** Obsidian files can sync to mobile, but the workflow is designed around substantial project work on a laptop.

There is no speculative roadmap in this README. The repository describes what is implemented and tested now; future platform or runtime work should enter only with the same typed lifecycle, permission boundaries, and acceptance evidence.

## Further documentation

| Topic | Document |
|---|---|
| Model inheritance and overrides | [Model routing](docs/model-routing.md) |
| Claude/Codex capability differences | [Runtime capability matrix](docs/runtime-capabilities.md) |
| Dispatch, review, watchdog, and close | [Unattended pipeline](docs/unattended-pipeline-operations.md) |
| Persistent task/model/domain lanes | [Task sessions](docs/task-sessions.md) |
| Acceptance fingerprints and reuse | [Acceptance architecture](docs/acceptance-architecture.md) |
| Numeric, content-free metrics | [Pipeline observability](docs/pipeline-observability.md) |
| Local PDF/Office/OCR path | [Document ingestion](docs/document-ingestion.md) |
| MCP service operations | [MCP gateway](docs/mcp-gateway.md) |
| Addresses, folds, and memory model | [DragonScale guide](docs/dragonscale-guide.md) |

## Credits and license

MIT; see [LICENSE](LICENSE). The upstream lineage and preserved copyright are documented in [ATTRIBUTION.md](ATTRIBUTION.md). The system was incubated in a private DevOps vault before being generalized into this repository.
