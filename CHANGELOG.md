# Changelog

All notable changes to llm-obsidian. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

> llm-obsidian descends from [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) (see [ATTRIBUTION.md](ATTRIBUTION.md)); its mechanics were incubated and battle-tested in a private DevOps vault through 2026 before this generic public release. This changelog starts fresh at 1.0.0.

## [2.0.3] - 2026-07-13

### Added

- Added local document normalization: Markdown/text use a stdlib fast path, while PDF, Office, EPUB, and scans use a pinned isolated Docling runtime with explicit `ru,en` OCR, content-addressed caching, confidence signals, and fail-closed size/page/time limits.
- Added a cross-runtime failure-to-repair contract: repository-owned mechanism defects are contained and diagnosed read-only, then require explicit user consent before a narrow fix, regression test, failed-stage retry, and resume of the original task.

### Changed

- Claude reviewers in locked-down `dontAsk` can run clean cwd-relative `python3 tests/test_*.py` and `bash tests/test_*.sh` entrypoints, while composed pipe/redirect/wrapper forms remain outside the allowlist.
- Fresh-machine setup provisions the isolated Docling runtime and OCR/layout/table artifacts by default; `--skip-docling` keeps an explicit lightweight path.

### Security

- Docling conversion disables remote services and external plugins, uses offline model flags, preserves immutable source files, and returns typed user-action escalation instead of silently falling back to native model parsing.
- Reviewer permission documentation now states the wildcard boundary accurately: the allowlist is not an argv parser, and executing newly added or modified repository tests is an explicit unattended-review trade-off.

## [2.0.2] - 2026-07-12

### Fixed

- Restored the macOS bootstrap, pinned-Python, protected-research callback, and restart fixes described in 2.0.1 but accidentally omitted from that release tag.
- Completed fetch and synthesis splits now close automatically only after their durable completion marker (and, for synthesis, a valid final output) proves the work finished. `--keep-surfaces` remains an explicit debugging opt-in.
- Claude reviewers now receive cwd-relative read-only Git commands that match their locked-down allowlist; Codex reviewers retain explicit worktree-qualified commands from their isolated scratch directory.

### Security

- Protected research profiles now make the Codex deny-by-default network contract explicit: no external-domain allowlist, no upstream-proxy chaining, no broad local binding or non-loopback listeners, no arbitrary Unix sockets, and no SOCKS5/UDP. The exact cmux callback socket remains the sole exception.
- Surface cleanup is marker-gated, exact-UUID, idempotent, coordinator-safe, and retryable when cmux is temporarily unavailable.

## [2.0.1] - 2026-07-12

### Fixed

- The macOS clean-machine bootstrap now verifies Xcode Command Line Tools before mutating the vault, rejects the inert system Python placeholder, and consistently uses a runnable Python 3.9+ interpreter.
- Protected research sessions pin that exact interpreter and expose only the read-only Homebrew and Command Line Tools roots required by Python and its framework libraries inside the sandbox.
- Protected fetch/synthesis callbacks now receive one explicit cmux Unix-socket exception, write durable completion markers, and can restart networkless synthesis from the already validated artifact when callback delivery fails.

### Security

- Research command networking stays in limited mode with no external-domain allowlist; the new access is restricted to the exact cmux socket, its readable parent, and read-only local toolchain roots.
- Callback failure is recoverable rather than silently losing task progress, without granting the isolated reviewer/fetcher general vault or network access.

## [2.0.0] - 2026-07-11

### Added

- First-class Claude Code and Codex plugin packaging with generated Codex marketplace metadata, shared safe Stop processing, runtime capability documentation, and portable setup helpers.
- Contract-bound unattended orchestration for cmux task worktrees: executor supervision, observer-only stall watchdogs, typed escalation, cross-model review, bounded verification, reap gating, and surface auto-close after a verified handoff.
- Evidence-grounded daily summaries, journal/backlog workflows, research isolation, instruction linting, schema validation, operation telemetry, and crash-safe transactional page writes.
- Section-level sparse retrieval with optional local `bge-m3`, quality gates, dense refresh workers, experiment tooling, and expanded hermetic regression coverage.

### Changed

- Cross-model review defaults now use subscription-backed Claude `opus` (currently Opus 4.8) for Codex work and Codex `gpt-5.6-sol` for Claude work; Fable remains an explicit opt-in.
- Hook execution, MCP profile generation, memory backup, sanitization, and clean-machine bootstrap are hardened for repeatable multi-agent use without committing machine-local state.

### Security

- Reviewer commands, task metadata, callback payloads, lifecycle transitions, and external-effect escalation are validated against strict schemas and pinned permission boundaries.
- Personal wiki pages, session records, workspace state, credentials, runtime metadata, and private memory are intentionally excluded; committed template indexes were regenerated solely from the public seed vault.

## [1.0.0] - 2026-07-05

Initial public release.

### Retrieval

- Local dense retrieval on ollama `bge-m3` (`scripts/semantic-search.py`, `scripts/tiling-check.py`): RU-capable embeddings, zero cloud calls. On the calibration vault the dense channel scored hit@1 0.85 / MRR@10 0.904 vs 0.27 / 0.405 for the previous English-centric model.
- Scope-aware hybrid fusion (`--hybrid`): dense ranks the pages it embeds; BM25 (`scripts/bm25-index.py`, whole-page Okapi with a Unicode tokenizer and RU stopwords) injects only pages outside the dense tiling scope (meta/plans/folds). Design validated on a goldset with a held-out half after plain weighted RRF measurably destroyed BM25's coverage role.
- Tag prefilter (`scripts/tag-search.py`) over the reverse tag index.
- Permanent benchmark harness: `scripts/retrieval-bench.py` + `.vault-meta/retrieval-goldset.jsonl` (seed template included) reporting hit@1 / hit@5 / MRR@10 per channel with automatic degradation handling. House rule: no ranking change ships without moving these numbers; add new goldset queries only after tuning (held-out discipline).
- Automatic degradations: ollama down → BM25-only; BM25 index missing → dense-only.

### Write path & hooks

- `scripts/vault-write.py`: single-payload dispatcher for `wiki/log.md` + `wiki/hot.md` with deterministic caps (hot ≤800 words, Recent Changes ≤15 × 160 chars, Active Threads ≤8, narrative ≤120 words) and `plan_close` lifecycle support. `scripts/validate-vault.py` enforces the caps, frontmatter schema and plans lifecycle.
- `stop.sh` turn-end hook: reindex → sanitized memory backup → BM25 rebuild → incremental dense refresh → auto-commit, serialized under `flock` (parallel sessions cannot corrupt each other), atomic index writes, per-phase latency telemetry in `.vault-meta/stop-hook-latency.jsonl` with a `STOP_HOOK_SLOW` warning at ≥30s.
- `skill-router` (UserPromptSubmit): data-driven soft skill hints from `.claude/skill-rules.json` (12 rules shipped). `session-nudge` (SessionStart): maintenance hints — lint age, fold due, tiling age, stale memory backup, skill-of-the-day, retrieval-assist discipline (`pipeline-stats.py --nudge`).
- `command-capture` (PostToolUse[Bash]): sanitized command log (`scripts/lib_sanitize.py` masks credential-looking values) feeding `/distill-runbook`. `plan-capture` (PostToolUse[ExitPlanMode]): every approved plan auto-filed to `wiki/plans/`.

### MCP HTTP gateway

- `scripts/mcp-gateway/`: one launchd-managed [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy) service per machine fronting all MCP children; sessions connect over HTTP (`.mcp.json.example`). Secrets via env indirection (`~/.config/mcp-gateway/secrets.env`); `doctor` derives required keys, child binaries and AWS profiles from `config.json`; `smoke`/`health` do real MCP handshakes; `update`/`sync-tools` manage version pins (`tools.json`).
- Flagship example: context7 (hosted) — setup is a single `CONTEXT7_API_KEY=` line.
- `.mcp-profiles/` pattern for heavy servers (schema-budget escape hatch), documented gotchas in `docs/mcp-gateway.md`.

### Skills (23)

- Wiki core: `wiki`, `wiki-ingest`, `wiki-query` (quick/standard/deep), `wiki-lint`, `wiki-fold`, `save`, `close`, `autoresearch`, `canvas`, `defuddle`, `obsidian-markdown`, `obsidian-bases`.
- Productivity: `journal` (date-keyed planner with carry-over), `daily` (end-of-day status log), `backlog` (append-only capture inbox), `find-session`, `draft` (external-communication advisor with redaction pass), `distill-runbook`, `learn`, `save-plan`.
- Orchestration (optional, requires cmux): `dispatch` (worktree + split with approved-plan handoff, configurable `LLM_OBSIDIAN_PROJECTS_ROOT` / `LLM_OBSIDIAN_WORKTREES`), `reap` (interim/final filing with plan close), `reap-send`.

### DragonScale Memory (inherited from upstream, recalibrated)

- Fold operator, deterministic `c-NNNNNN` addresses, semantic tiling duplicate lint, boundary-first autoresearch. Tiling thresholds ship as bge-m3 defaults (error 0.92 / review 0.85, `calibrated: false`) with a documented per-vault recalibration procedure.

### Vault template

- Seeded `wiki/` skeleton: demo concept/entity/source pages, getting-started, full folder set (concepts, entities, sources, comparisons, questions, runbooks, decisions, goals, routines, daily, plans, folds, meta) with auto-generated `_index.md`, fresh `hot.md`/`log.md` matching the vault-write contract.
- `CLAUDE.md` template (RU) + `AGENTS.md` agent-agnostic contract.

### Testing

- 9 hermetic suites, no network or ollama required: address allocator, tiling, boundary, vault scripts, stop-hook (flock/opt-out/latency), BM25 + fusion, bench harness, skill router, MCP gateway management layer.

### Known limitations / roadmap

- Claude Code is the only wired agent (hooks layer); Codex adapter planned — scripts are agent-agnostic.
- Skill bodies are English (RU triggers work); RU localization planned.
- launchd autostart is macOS-only; on Linux run the gateway under systemd manually.
