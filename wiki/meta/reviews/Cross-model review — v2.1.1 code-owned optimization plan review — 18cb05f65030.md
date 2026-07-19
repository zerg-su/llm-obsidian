---
type: review
title: "Cross-model review — v2.1.1 code-owned optimization plan review — 18cb05f65030"
address: c-000012
created: 2026-07-19
updated: 2026-07-19
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "05d2b16c-a4ca-418f-8582-54f170e00fc4"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 3
verdict: approve
---

# Cross-model review — v2.1.1 code-owned optimization plan review — 18cb05f65030

> [!abstract] Outcome
> **Task:** v2.1.1 code-owned optimization plan review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 3
> **Started:** 2026-07-19T20:11:23Z
> **Updated:** 2026-07-19T20:21:22Z

## Review request

Review the implementation for **v2.1.1 code-owned optimization plan review** in `task/v2.1.1-code-owned-optimizations` against `v2.1.0` using the `full` cross-model gate.

> [!quote] Original task request
> # Fable review: possible v2.1.1 code-owned optimizations
>
> Review only the committed plan at
> `docs/plans/v2.1.1-code-owned-optimization-plan.md` (commit `7c4ea8d`).
> Do not review v2.1.0 as an implementation release gate and do not propose or
> edit product code.
>
> The user has frozen v2.1.0 at `94cc6cf`. The question is whether the proposed
> v2.1.1 optimizations move all safely deterministic work into code while
> preserving or improving result quality:
>
> 1. provider-native, content-free message/request telemetry for Claude Code and
>    Codex plus stage latency aggregation;
> 2. content-addressed acceptance-cell reuse across commits;
> 3. runner-first skill compaction with enforceable body budgets.
>
> Critically inspect the plan for:
>
> - any quality regression, lost context, stale evidence, or unsafe cache reuse;
> - whether built-in Claude/Codex OpenTelemetry supports the proposed data and
>   configuration path without parsing conversation text;
> - privacy, local attack surface, config ownership, installer reversibility,
>   portability, and fail-open observability boundaries;
> - missing dependencies, schemas, migrations, compatibility constraints, unit
>   tests, live tests, or rollback conditions;
> - which tasks should remain semantic/model-owned rather than code-owned;
> - unnecessary complexity or a simpler design with equivalent evidence;
> - whether the three workstreams should be reordered, split, or narrowed.
>
> Return actionable findings against plan lines. A blocking finding means the
> plan must change before implementation. Warnings and nits should explain a
> concrete quality-preserving improvement. Approve only if the plan is safe,
> implementable, and sufficiently testable.

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `05d2b16c-a4ca-418f-8582-54f170e00fc4`
- Received: 2026-07-19T20:15:24Z

### Findings

#### 1. blocking — Collector topology and cross-project event scoping are unspecified for global provider configuration and multi-worktree checkouts

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:79`
- Evidence:
> Design item 5 merges user-level Codex OTel configuration (and managed Claude environment), so every Codex/Claude session on the machine — including unrelated work projects — will export telemetry to this collector, yet the plan never says whether unbound events are dropped or persisted into this vault's .vault-meta/model-events.jsonl. It also does not say which checkout owns the launchd service, how the loopback port is allocated, or what happens when two vaults/worktrees (this repo's normal workflow, e.g. /Users/zak/Projects/worktrees/*) both run `install --apply` and fight over the single user-level Codex endpoint. The repo already has a stated precedent for exactly this problem (mcp-gateway: one process set per machine, port only in local runtime.env), which the plan ignores. This is squarely the privacy/config-ownership/portability surface the review was asked to gate.
- Recommendation:
> Add a topology section: one collector per machine (mcp-gateway pattern), port/config location, single-owner semantics with explicit behavior when a second vault installs, and a hard rule that events whose session/conversation binding does not resolve to a known task/vault binding are counted (for coverage stats) but never persisted with their identifiers. State explicitly whether the Claude environment merge is project-scoped (.claude/settings.json env) or user-scoped.

#### 2. warning — Load-bearing provider-fact claims are unverified and one citation looks wrong

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:44`
- Evidence:
> The schema and correlation design (design item 3, 'hashed prompt/request correlation') depend on attributes the plan asserts as fact: a 'content-redacted prompt correlation ID' on claude_code.user_prompt and 'request ID, and query source' on claude_code.api_request are not attributes I can confirm against documented stable Claude Code OTel events (documented api_request attributes are model, cost, duration, and token/cache counts). The claim that project-scope Codex config cannot set `otel` drives the whole user-level installer design. The cited Codex manual URL https://learn.chatgpt.com/docs/config-file/config-advanced does not match known Codex documentation hosts (developers.openai.com/codex or the openai/codex repo docs) and may be hallucinated.
- Recommendation:
> Add an explicit gate to implementation-order step 2: capture live OTLP/HTTP JSON payloads from both current runtimes first, reconcile every 'Provider facts' claim against them, and derive golden fixtures from those captures rather than from the doc claims. Define the correlation fallback (session/conversation ID plus monotonic sequence and timestamps) for the case where no prompt/request correlation ID exists. Replace the Codex reference with the authoritative config documentation URL.

#### 3. warning — Loopback receiver has no authentication, allowing any local process to forge accounting and latency evidence

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:67`
- Evidence:
> Design item 2 restricts by source address only. Any local process can POST well-formed OTLP JSON to 127.0.0.1 and poison model-events.jsonl, pipeline-stats summaries, and the stage-latency reports that acceptance criterion 6 turns into release analysis. Both runtimes support OTLP headers (OTEL_EXPORTER_OTLP_HEADERS for Claude Code; Codex otel exporter headers), so a shared-secret check is cheap and fits the existing secrets-outside-repo pattern.
- Recommendation:
> Mint a per-install random bearer token at `install --apply`, store it outside the repo (config/secrets pattern already used by mcp-gateway), configure both runtimes to send it, and have the receiver reject unauthenticated posts. Alternatively, document forged-telemetry acceptance explicitly as a residual risk in the plan.

#### 4. warning — Plan does not state that .vault-meta/model-events.jsonl is gitignored, rotation-capped, and excluded from the Stop-hook scoped commit

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:71`
- Evidence:
> .gitignore enumerates .vault-meta files individually (.gitignore:119-162) rather than ignoring the directory, and the Stop hook scope-commits vault-owned paths. Without an explicit ignore entry the new events file (session IDs, models, costs, machine metadata, potentially from other projects) could be auto-committed and later pushed. The pipeline-events.jsonl precedent (gitignored plus .jsonl.1 rotation plus lock file) exists but the plan never binds the new file to it.
- Recommendation:
> State in workstream 1 that model-events.jsonl follows the pipeline-events.jsonl contract exactly: gitignored (file plus rotation sibling plus lock), size-capped rotation, and never included in scoped commits; add a test asserting the ignore entry exists.

#### 5. nit — Freshness window is unspecified and fingerprints ignore provider-side model drift

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:114`
- Evidence:
> Workstream 2 item 4 says 'bounded freshness window' without a default, and the fingerprint keys on 'resolved model/effort' even though hosted model aliases can change behavior server-side without any local input changing.
- Recommendation:
> Name a default window (e.g., 7-14 days, overridable) in the plan, and include a provider model version/snapshot identifier in the fingerprint whenever the runtime or API response exposes one.

#### 6. nit — Installer behavior on Linux is unspecified despite invariant 5

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:81`
- Evidence:
> Design item 5 is launchd-only. Invariant 5 promises 'Linux remains basic', but the plan does not say what check/install/status do off macOS.
- Recommendation:
> State that on Linux `check`/`status` report unsupported-service gracefully and `install --apply` either refuses or offers a documented foreground/manual mode; no partial config writes.

#### 7. nit — Rollback says 'uncompressed skill entry files' and omits provider-config restore

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:214`
- Evidence:
> The skills are compacted, not compressed, and the rollback section does not mention that `uninstall` restores user-level Claude/Codex configuration from the timestamped backups created at install.
- Recommendation:
> Reword to 'pre-compaction skill entry files' and add one sentence: uninstall removes the launchd service and restores or reverts the managed Claude/Codex config keys from the recorded backups.

### Executor resolution

> [!note] Resolution snapshot
> # Executor resolution
>
> 1. **applied — collector topology and scoping.** Added an explicit one-collector-per-machine topology, machine-local installed code/config ownership, compatible second-vault reuse, fail-before-mutation drift handling, authenticated session binding, bounded in-memory pre-binding buffer, and a hard rule that unbound events persist no identifiers or payload attributes.
> 2. **applied — provider facts and capture gate.** Current official Claude documentation confirms `prompt.id`, API request token/duration/cache fields, optional request ID, and query source; the current Codex manual confirms user-scope-only OTel configuration and response-completion token events. The plan now treats those as capture-gated hypotheses, requires live current-runtime OTLP capture before schema freeze, defines a session+sequence+timestamp fallback, and uses the canonical manual URL including `.md`.
> 3. **applied — receiver authentication.** Added a random per-install bearer token stored outside Git at mode 0600, provider OTLP headers, constant-time verification, and unauthenticated-request tests.
> 4. **applied — gitignore/rotation.** Bound the new event log, rotation sibling, and lock to explicit gitignore, size-cap, Stop-hook exclusion, and regression tests.
> 5. **applied — freshness/model drift.** Set a seven-day default, a reviewed-contract requirement above 14 days, and provider snapshot/version inclusion when exposed.
> 6. **applied — Linux behavior.** Check/status are read-only; install refuses without partial writes and gives a foreground/manual path until a tested service manager exists.
> 7. **applied — rollback wording/config restoration.** Reworded to pre-compaction files and made uninstall remove launchd plus reverse only managed provider keys through the install manifest/backups.
>
> Additional reviewer note applied: skill compaction explicitly invalidates the corresponding content-addressed acceptance fingerprints.

### Verification gaps

- Reviewer has no network access, so the Claude Code and Codex OpenTelemetry attribute claims (prompt correlation ID, request ID, query source, Codex project-scope otel restriction) and the four reference URLs could not be verified against live documentation; the plan should not freeze the schema until live payload capture confirms them.
- This is a plan-only branch (single docs commit, clean tree); no tests exist to run for this change, and no executable claims could be exercised.

### Residual risks

- Cached acceptance reuse can never fully protect against nondeterministic model behavior changing under an unchanged fingerprint; the freshness window plus explicit --restart is an acceptable mitigation, not a proof.
- Even content-free telemetry aggregates timing/token/cost patterns across sessions on one machine; the data stays local and gitignored only if the warning findings are adopted.

### Notes for executor

- The overall shape is strong: the semantic/code ownership split, fail-closed invariants 1-6, conservative unknown-path invalidation in workstream 2, and the required-tests matrix are all sound, and every repo extension point named in the plan (session-preflight.py, pipeline-stats.py, release-acceptance.py checkpoints/--restart, check-skill-budget.py, lifecycle_telemetry.py) actually exists.
- The changes-requested verdict is driven by the single blocking topology/scoping gap; the warnings are plan-text amendments, not redesigns. Workstream ordering (telemetry, reuse, compaction) is fine as-is.
- Workstream 3 interaction with workstream 2 is implicitly correct (compaction changes skill hashes and invalidates cells, and the final evidence gate reruns them), but a one-line acknowledgement in the plan would prevent surprise at step 5-6.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `30680259-d945-4baf-8dbe-88fba9e82dd0`
- Received: 2026-07-19T20:19:12Z

### Findings

#### 1. nit — Codex manual citation still points at an unrecognized host despite the prior finding

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:62`
- Evidence:
> The fix appended '.md' to the same URL (https://learn.chatgpt.com/docs/config-file/config-advanced.md) rather than switching to a known Codex documentation host (developers.openai.com/codex or the openai/codex repository docs). The executor asserts this is the canonical current manual; the reviewer cannot verify it offline. The claim it supports (project-scope config cannot set Codex `otel`) is now safely covered anyway: implementation-order step 2 gates every provider-fact claim on live current-runtime reconciliation, and the user-scoped installer design remains correct even if the scope restriction turns out to be wrong.
- Recommendation:
> During implementation-order step 2, actually fetch this reference; if it does not resolve, replace it with the authoritative Codex configuration documentation URL, and confirm the project-scope `otel` restriction empirically (set the key in project .codex/config.toml and observe it is ignored) as part of the provider-fact reconciliation.

### Executor resolution

> [!note] Resolution snapshot
> # Executor resolution
>
> 1. **applied — citation and implementation verification.** The current
>    official Codex manual helper emits the learn.chatgpt.com source URL, while a
>    separate web fetch did not render it reliably. The plan now requires the
>    implementation to re-fetch every provider citation through the official
>    documentation route, replace an unavailable route with the then-current
>    authoritative source, and empirically prove project-scoped Codex `otel` is
>    ignored before any user-level config mutation.
> 2. **applied — control endpoint coverage.** Added authentication,
>    size/schema/method, replay/idempotency, wrong-vault/stale-session, path
>    confinement, concurrency, and restart tests for the session-binding control
>    endpoint.

### Verification gaps

- The reviewer has no network access, so the executor's claim that current official Claude documentation confirms `prompt.id`/request-ID/query-source attributes and that learn.chatgpt.com is the canonical Codex manual could not be independently verified; the plan's capture-before-freeze gate in implementation-order step 2 is the accepted mitigation.
- This remains a plan-only branch (two docs commits, clean tree); no executable checks apply.

### Residual risks

- Cached acceptance reuse cannot fully protect against provider-side model behavior changing under an unchanged fingerprint; the seven-day window, snapshot inclusion when available, and explicit --restart are mitigations, not proof.
- Even content-free telemetry aggregates timing/token/cost patterns per machine; the topology section keeps it local, authenticated, and vault-scoped, but the collector and its control endpoint are new long-running local attack surface whose hardening now depends on the implementation matching the plan's rejection-test matrix.

### Notes for executor

- All seven prior findings are resolved in commit e84418c. Blocking finding 1 is fully addressed by the new 'Machine topology, authentication, and vault scoping' section: one collector per machine on the MCP-gateway precedent, machine-local install/config/manifest in ~/.config/llm-obsidian/model-telemetry/, compatible second-vault reuse with fail-before-mutation on drift, worktree-aware vault binding, unbound events dropped without persisting identifiers, and matching multi-vault ownership tests.
- Finding 2 is resolved in substance (claims downgraded to capture-gated hypotheses, live OTLP capture required before schema freeze, session+sequence+timestamp correlation fallback defined); only the citation host remains, now filed as the single nit above.
- Findings 3-7 verified resolved: bearer-token receiver auth with constant-time comparison and 0600 secrets outside Git; explicit gitignore/rotation/Stop-hook exclusion with a regression test; seven-day freshness default with reviewed-contract escalation above 14 days and provider snapshot in the fingerprint; Linux check/status read-only with install refusing before partial writes; rollback reworded with manifest-driven restore of managed provider keys. The workstream 3 / workstream 2 fingerprint-invalidation note was also applied as workstream 3 item 8.
- The revision introduces one new surface not in the original plan — the authenticated local control call for session binding (topology item 4). It is bounded and token-authenticated, so it is acceptable at plan level, but its request/response contract should get the same rejection-test treatment as the OTLP endpoints during implementation.

## Round 3 — approve

- Phase: `verify-fixes`
- Run ID: `6b0fedf6-b0ab-4598-8df9-6b5f61ff9bb5`
- Received: 2026-07-19T20:21:22Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The reviewer remains offline; the learn.chatgpt.com citation and the Claude attribute claims are still unverified externally, but the plan now makes both self-correcting: every cited provider source must be re-fetched during implementation-order step 2, dead routes replaced with the then-current authoritative source, and the Codex project-scope otel restriction proven empirically before any user-level config mutation.
- This remains a plan-only branch (three docs commits, clean tree); no executable checks apply.

### Residual risks

- Cached acceptance reuse cannot fully protect against provider-side model behavior changing under an unchanged fingerprint; the seven-day window, snapshot inclusion when available, and explicit --restart are mitigations, not proof.
- The collector and its session-binding control endpoint are new long-running local attack surface; the plan's rejection-test matrix now covers both, but the guarantee holds only if the implementation matches it and will need re-verification at the implementation review.

### Notes for executor

- Both items from the prior approve verdict are resolved in commit f3f5823. The citation nit is closed by the explicit re-fetch/replace caveat under the references (docs/plans/v2.1.1-code-owned-optimization-plan.md:64-67) plus the implementation-order step 2 requirement to re-fetch every cited source and empirically confirm project-scoped otel is ignored (lines 217-220). The control-endpoint note is closed by the new required-tests bullet (lines 249-251) covering authentication, size/schema/method limits, replay/idempotency, wrong-vault and stale-session bindings, path confinement, concurrency, and restart — matching the OTLP boundary treatment.
- No new surfaces, claims, or scope were introduced by this revision; it only tightened the two open items. The plan is approved for implementation as gated by its own review-acceptance status line.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
