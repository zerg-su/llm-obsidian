<p align="center">
  <img src="docs/assets/llm-obsidian-banner.png" alt="Claude Code and Codex exchanging reviewed work through an Obsidian knowledge vault" width="100%">
</p>

# LLM Obsidian

[![CI](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml/badge.svg)](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml)

**A local-first project workspace where Claude Code and Codex CLI share durable memory, reusable skills, visible cmux orchestration, and independent cross-model review.**

Language: **English** · [Русский](README.ru.md)
Release history: [English](CHANGELOG.md) · [Русский](CHANGELOG.ru.md)

Complete technical handbook: [Русский, version 2.6.3](docs/ru/index.md).

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
- **Quality is measured.** Hermetic tests cover the mechanics; retrieval has a RU/EN goldset; releases use four bounded provider-backed lifecycle cells on one frozen Git SHA.

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
│   ├── repo skills ──► intent + reasoning discipline         │
│   ├── retrieval ────► Obsidian Markdown + local indexes     │
│   └── harness ──────► operation ledger + typed workflows    │
└──────────────────────────┬───────────────────────────────────┘
                           │ adapters: Git, cmux, models, checks
                 ┌─────────┴─────────┐
                 ▼                   ▼
        isolated task worktree   simple/deep reviewer lanes
        Claude or Codex          same or opposite runtime
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
3. **Split when explicitly approved.** `$split` is a zero-effect manifest preview. `$split --dispatch` activates only an approved bounded DAG, launches dependency-ready workspace children through the existing dispatch adapter, and joins exact approved resource-free receipts in manifest order.
4. **Dispatch.** The restartable harness captures project/task/session IDs, the exact model route, approved plan hash, permission domain, worktree, and caller cmux surface. It opens the task to the right of the correct coordinator instead of whichever tab happens to be selected later.
5. **Execute.** Claude or Codex works in an isolated Git worktree. Same-model bounded work normally uses an internal agent; an explicit separate window creates a durable visible lane.
6. **Review.** Simple review uses one selected read-only holistic lane. A standalone Deep review compiles independent Anthropic and OpenAI holistic lanes; each checks both intent/outcome and engineering quality. Within a finalization cycle, cycles 1–3 use the primary route alone and the independent holistic lane joins in cycles 4–5 only against fresh availability evidence. Explicit single-model Deep instead splits that model into intent and engineering lanes. Full review is explicit-only and runs the four provider-by-responsibility lanes. Verification resumes the exact lane and surface.
7. **Reap.** A typed final summary, approved review archive, plan hash, result path, and session provenance are validated. One vault transaction writes the result and closes the plan.
8. **Exit safely.** `/exit` is armed only after the lifecycle is complete. The lifecycle wrapper requests graceful agent exit and closes that exact surface only after the process exits; it never guesses another tab.

Review approval finishes a **round**, not the whole task. The task remains resumable until final reap. Archived sessions are never silently attached to another task ID.

### Common workflows

The normal interface is the skill, not a low-level runner command. In Claude,
select the named plugin skill or use the natural-language phrase shown below.
In Codex, use the explicit `$llm-obsidian:<skill>` name.

| Goal | What to ask for | Skill / result |
|---|---|---|
| Clarify an idea | “Grill me before code; ask one material question at a time.” | `clarify` turns the discussion into an explicit outcome, scope, evidence, and stop conditions. |
| Save the agreed plan | “Save this approved implementation plan.” | `save-plan` writes the canonical plan; `implementation-plan` structures multi-file TDD slices and ownership. |
| Review a plan | “Review this plan for intent and outcome before dispatch.” | `review` with the intent boundary checks that the plan still serves the goal before implementation mechanics. |
| Start ordinary work | “Dispatch this approved plan with `engineering/change`.” | `dispatch` validates the exact repo, plan, route, worktree, permissions, review preset, and visible cmux session. |
| Split approved work | “Use `$split` to preview,” or explicitly “Use `$split --dispatch`.” | Preview is zero-effect. Activation binds disjoint workspace children, bounded waves and exact receipts to one sealed manifest; deterministic join never merges, releases or replays a provider. |
| Open or continue a visible session | “Open this task in a separate visible session,” or “continue the existing task session.” | `dispatch` creates the owned cmux lane when requested; later work resumes its exact stored checkpoint instead of opening an unrelated window. |
| Read cmux progress | Look at the thin workspace progress line while a pipeline is active. | It counts only live harness steps owned by the current coordinator workspace. It clears at idle and deliberately does not report project-backlog tasks, model limits, or history from missing surfaces. |
| Fix a reproducible defect | “Dispatch this approved plan with `engineering/fix`.” | The built-in bounded loop runs reproduce → root cause → regression → minimal fix and stops on architecture or budget boundaries. |
| Use a custom pipeline | “No built-in fits; propose the smallest bounded custom pipeline.” | The model may author the typed DSL only after proving the semantic gap; the harness validates allowed steps, loops, hashes, and budgets before approval. |
| Review implementation | “Run Deep review,” “use only Opus/Sol,” or explicitly “Run Full review.” | Simple is one selected holistic reviewer. Standalone Deep is two provider-independent holistic reviews, but in a finalization cycle the independent lane only joins in cycles 4–5 against fresh availability; single-model Deep is separate intent and engineering reviews. Full is the explicit-only four-lane provider/responsibility grid. |
| Finish or leave | “Reap the approved task,” or “save and close this session.” | `reap` archives the result and closes the plan; `close` saves and exits only the current agent process without guessing another cmux surface. |

Pipeline choice is frozen during clarification. `lifecycle/default` handles a
plain lifecycle, `engineering/change` is the normal TDD implementation path,
and `engineering/fix` is the reproducible-defect loop. Custom DSL is an escape
hatch for an approved semantic gap, not a more fashionable default. cmux keeps
the resulting executor and reviewer sessions visible; the harness owns their
exact IDs, callbacks, bounded retries, progress, and cleanup.

## Where code saves tokens without lowering quality

| Mechanical work | Code-owned implementation | Why it matters |
|---|---|---|
| Session readiness | `session-preflight.py`, generated-config checks, dependency detection | One fast local check replaces repeated model inspection. Missing optional components produce exact repair commands. |
| Model selection | `config/model-routing.toml` + `model_routing.py` | Concrete defaults live in one place; task metadata records the resolved route. No model-name hardcoding across dozens of skills. |
| Repository/context candidates | harness context and Git modules | IDs, manifests, and validated paths replace token-heavy guessing about repo, plan, window, or prior session. |
| Vault mutation | `vault-write.py` | One optimistic, journaled transaction replaces many fragile edits to pages, log, hot list, plan, and manifest. |
| Search | section BM25 + optional local embeddings | The model sees the best bounded sections, not whole folders or repeated page bodies. |
| Web cleanup | `defuddle` before synthesis | Navigation, ads, and boilerplate are removed before they consume context. |
| Document conversion | cached stdlib/Docling pipeline | OCR and parsing are reused by source hash instead of spending model tokens rereading unchanged binaries. |
| Review transport | typed internal callback broker | No long callback paths or free-form findings need to be copied between terminal windows. |
| Acceptance reruns | exact SHA + per-cell dependency fingerprints | Green lifecycle cells are reused only while their code-owned dependency closure is unchanged. |
| Monitoring | content-free heartbeat and numeric telemetry | The pipeline can distinguish active work from a stall without storing prompts, responses, or screen text. |
| Finalization | harness reap workflow | Review archival, result routing, plan close, reindex, validation, and exact exit happen through one fail-closed contract. |

This division is deliberate. Requirements, interpretation, synthesis, code review, and risk judgment stay with models and people. Hashing, routing, schema checks, filesystem bookkeeping, and retry policy stay in code.

## Unified review

`review` starts one selected holistic reviewer. A standalone `review --deep`
compiles independent Anthropic and OpenAI holistic lanes; each checks the
complete outcome and engineering denominator. Inside a finalization cycle that
topology is gated twice: cycles 1–3 run the primary route only (its
provider-prefixed intent and engineering lanes), and the independent holistic
lane is added in cycles 4–5 only when fresh provider availability evidence
admits it — otherwise those cycles also stay primary-only. Explicit
single-model Deep uses separate intent and engineering sessions on that model. Explicit-only `--full` starts
the four provider/responsibility lanes (`anthropic-intent`,
`anthropic-engineering`, `openai-intent`, `openai-engineering`) and is never
chosen by heuristics or risk policy. A registered model alias may override an
allowed selected route. Every resolved route is recorded, and an unavailable
provider can be excluded through an explicit single-model choice instead of
breaking the pipeline.

A review operation contains:

- opaque project, task, lane, operation, runtime, model, and permission-domain identities;
- the reviewed branch and stable baseline;
- a product-read-only mandate and a single isolated outbox write path;
- typed severity, evidence, recommendation, verification gaps, and residual risks;
- bounded safe transitions owned by the harness operation engine;
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
2. a networkless synthesizer that receives only the validated artifact and explicitly selected context.

The coordinator validates the cited result and performs the only vault write.
Persistent task lanes retain provider context only inside the exact task and
isolation domain, so follow-up research does not start from zero. Every
operation gets fresh scratch. `unsafe-research` is a separate, explicitly
authorized single-context escape hatch; it never becomes a silent fallback from
protected research.

## The 34 shipped skills

Claude invokes them through its plugin UI (`/skill`). Codex uses the generated repo-local marketplace (`$llm-obsidian:skill`). The mechanics live in `skills/<name>/SKILL.md`, so another coding agent can follow them manually even without plugin support.

| Area | Skills and purpose |
|---|---|
| **Orientation and alignment** | `wiki` bootstraps the vault; `clarify` performs one-question-at-a-time requirements/design alignment before implementation. |
| **Capture and writing** | `save`, `save-plan`, `journal`, `backlog`, `daily`, and `agenda` turn conversations and dated work into canonical vault data. |
| **Knowledge access** | `wiki-query`, `find-session`, `wiki-lint`, and `wiki-fold` retrieve, audit, and compact durable knowledge. |
| **Documents and web** | `wiki-ingest`, `defuddle`, `research`, and `unsafe-research` normalize sources and keep trust domains explicit. |
| **Engineering** | `debug`, `tdd`, `design`, `prototype`, and `resolve-conflict` keep reasoning disciplined while the harness owns lifecycle mechanics. |
| **Thinking and communication** | `draft` proposes redacted external replies; `learn` tutors from your notes; `distill-runbook` turns sanitized shell history into human-executable procedures. |
| **Skill quality** | `improve-skills` explicitly audits invocation, information hierarchy, completion criteria, and pruning while preserving behavior. |
| **Obsidian-native output** | `obsidian-markdown`, `obsidian-bases`, and `canvas` produce correct links, properties, database views, and visual canvases. |
| **Task orchestration** | `dispatch`, `review`, `reap`, and `close` expose the visible harness-owned multi-session lifecycle. |

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

In Codex, use explicit names such as `$llm-obsidian:wiki-query`, `$llm-obsidian:clarify`, and `$llm-obsidian:review`.

## Testing and release evidence

```bash
make test                 # full hermetic suite; no network or Ollama
make bench-retrieval      # measured ranking gate
make acceptance-check     # model-free four-cell harness contract
make acceptance-live      # resume only affected provider-backed cells
```

The release gate contains exactly four live cells: Claude lifecycle, Codex
lifecycle, cross-runtime dispatch/review/reap composition, and two-axis deep
review. Each cell binds the exact release SHA and a scoped dependency
fingerprint; green cells are reused only while both remain unchanged.

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
| v2.8.4 custom initial-delivery repair | [v2.8.4 release notes](docs/releases/v2.8.4.md) |
| v2.8.3 transition recovery | [v2.8.3 release notes](docs/releases/v2.8.3.md) |
| v2.8.2 semantic cmux liveness | [v2.8.2 release notes](docs/releases/v2.8.2.md) |
| v2.8.1 Harness lifecycle stabilization | [v2.8.1 release notes](docs/releases/v2.8.1.md) |
| v2.8.0 Architecture Workflow v1 | [v2.8.0 release notes](docs/releases/v2.8.0.md) |
| v2.6.7 RC4 terminal dashboard candidate | [v2.6.7 RC4 release notes](docs/releases/v2.6.7-rc4.md) |
| v2.6.7 RC3 final stabilization candidate | [v2.6.7 RC3 release notes](docs/releases/v2.6.7-rc3.md) |
| v2.6.7 RC2 root-scoped Harness observer | [v2.6.7 RC2 release notes](docs/releases/v2.6.7-rc2.md) |
| v2.6.7 RC1 bounded Harness stabilization | [v2.6.7 RC1 release notes](docs/releases/v2.6.7-rc1.md) |
| v2.6.6 RC4-fix3 target-local Codex dispatch repair | [v2.6.6 RC4-fix3 release notes](docs/releases/v2.6.6-rc4-fix3.md) |
| v2.6.6 RC4-fix2 live Harness dashboard | [v2.6.6 RC4-fix2 release notes](docs/releases/v2.6.6-rc4-fix2.md) |
| v2.6.6 RC4-fix1 bounded dogfood repairs | [v2.6.6 RC4-fix1 release notes](docs/releases/v2.6.6-rc4-fix1.md) |
| v2.6.6 RC4 deterministic review control plane | [v2.6.6 RC4 release notes](docs/releases/v2.6.6-rc4.md) |
| v2.6.6 RC3 reproducible evidence and release disposition | [v2.6.6 RC3 release notes](docs/releases/v2.6.6-rc3.md) |
| v2.6.6 RC2 repair-and-delete polishing | [v2.6.6 RC2 release notes](docs/releases/v2.6.6-rc2.md) |
| v2.6.6 RC1-fix2 exact cmux cleanup reconciliation | [v2.6.6 RC1-fix2 release notes](docs/releases/v2.6.6-rc1-fix2.md) |
| v2.6.6 RC1-fix1 dispatch startup and review-skip corrections | [v2.6.6 RC1-fix1 release notes](docs/releases/v2.6.6-rc1-fix1.md) |
| v2.6.6 RC1 deletion-first lifecycle simplification and sealed Split ancestry | [v2.6.6 RC1 release notes](docs/releases/v2.6.6-rc1.md) |
| Model inheritance and overrides | [Model routing](docs/model-routing.md) |
| Claude/Codex capability differences | [Runtime capability matrix](docs/runtime-capabilities.md) |
| Dispatch, review, harness liveness, and close | [Unattended pipeline](docs/unattended-pipeline-operations.md) |
| Persistent task/model/domain lanes | [Task sessions](docs/task-sessions.md) |
| v2.3.0 clean-cut migration | [Runtime harness migration](docs/runtime-harness-migration.md) |
| v2.4.0 compiled pipeline boundary | [Pipeline composition ADR](docs/decisions/v2.4-pipeline-composition-boundary.md) |
| Acceptance fingerprints and reuse | [Acceptance architecture](docs/acceptance-architecture.md) |
| v2.6.5 exact-HEAD lifecycle and bounded Split activation | [v2.6.5 release notes](docs/releases/v2.6.5.md) |
| v2.6.4 unattended callback continuity and durable decisions | [v2.6.4 release notes](docs/releases/v2.6.4.md) |
| v2.6.3 complete Russian technical handbook and deterministic documentation gates | [v2.6.3 release notes](docs/releases/v2.6.3.md) |
| v2.6.2 truthful cmux workspace progress and lifecycle fixes | [v2.6.2 release notes](docs/releases/v2.6.2.md) |
| v2.6.1 independent review topology and lane isolation | [v2.6.1 release notes](docs/releases/v2.6.1.md) |
| v2.6.0 outcome-preserving contracts and skill intelligence | [v2.6.0 release notes](docs/releases/v2.6.0.md) |
| v2.4.1 typed fix loops, install, upgrade, and rollback | [v2.4.1 release notes](docs/releases/v2.4.1.md) |
| v2.5.1 real-task lifecycle stabilization | [v2.5.1 release notes](docs/releases/v2.5.1.md) |
| v2.5.0 model-authored bounded pipelines and callback liveness | [v2.5.0 release notes](docs/releases/v2.5.0.md) |
| v2.4.0 baseline release | [v2.4.0 release notes](docs/releases/v2.4.0.md) |
| Numeric, content-free metrics | [Pipeline observability](docs/pipeline-observability.md) |
| Local PDF/Office/OCR path | [Document ingestion](docs/document-ingestion.md) |
| MCP service operations | [MCP gateway](docs/mcp-gateway.md) |
| Addresses, folds, and memory model | [DragonScale guide](docs/dragonscale-guide.md) |

## Credits and license

MIT; see [LICENSE](LICENSE). The upstream lineage and preserved copyright are documented in [ATTRIBUTION.md](ATTRIBUTION.md). The system was incubated in a private DevOps vault before being generalized into this repository.
