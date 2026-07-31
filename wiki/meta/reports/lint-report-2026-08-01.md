---
type: meta
title: "Lint Report 2026-08-01"
created: 2026-08-01
updated: 2026-08-01
tags:
  - meta
  - lint
  - audit
status: solid
related:
  - "[[dashboard]]"
  - "[[backlog]]"
sessions:
  - 019fab00-3160-7380-8920-4b20183afb76
---

# Lint Report: 2026-08-01

> [!abstract] Audit scope
> RT09 audited the exact task snapshot at `13291a6` on `task/df250-real-rt09-vault-health-audit`. The audit scanned the writable integration snapshot only; the live source vault was read-only context.

## Summary

- Pages scanned: 70 Markdown pages.
- Distinct findings: 16 — 4 bounded/actionable and 12 retained for review.
- Auto-fixed: 4 — linked both actionable orphans, added the index freshness marker, and regenerated the stale meta folder index.
- Needs review: 12 — 7 explicit legacy-unknown provenance markers, 3 frontmatter-density hints, and 2 stale DragonScale claims.
- Machine result before repair: 0 FAIL, 8 WARN.
- Semantic tiling: 0 error pairs, 0 review pairs; thresholds are not calibrated.

## Deterministic Validator Baseline

```text
WARN: index: no AUTO-DATE marker (header freshness is unverifiable)
WARN: schema/provenance: wiki/meta/reviews/Cross-model review — 232fe53b-d609-4467-9a9d-13e45326368b — ffe07464dfd5.md: sessions is legacy-unknown []
WARN: schema/provenance: wiki/meta/reviews/Cross-model review — 789b3188-0916-4d59-9bcd-6cddcdcc64ac — 1ab0c0f125cc.md: sessions is legacy-unknown []
WARN: schema/provenance: wiki/meta/reviews/Cross-model review — 7ccfc543-9d3f-4e0d-9a44-89378ec073cd — 7d7bbd1d3ef8.md: sessions is legacy-unknown []
WARN: schema/provenance: wiki/meta/reviews/Cross-model review — 8d9f0b29-0f04-4f32-8484-f203ab212b81 — d877febeeedb.md: sessions is legacy-unknown []
WARN: schema/provenance: wiki/meta/reviews/Cross-model review — 95db707f-3219-45cf-84d1-715b7344c411 — c50e8025e5d5.md: sessions is legacy-unknown []
WARN: schema/provenance: wiki/meta/reviews/Cross-model review — e5197527-44dc-4df7-b686-a6ce6f092e3d — b4bc43525bf3.md: sessions is legacy-unknown []
WARN: schema/provenance: wiki/plans/_index.md: sessions is legacy-unknown []
validate-vault: 0 FAIL, 8 WARN (hot, fold, index, questions, schema, plans, panic, skills, guide; 0.1s)
```

No deterministic FAIL lines existed to carry forward as red items.

## Orphan Pages

- Baseline: [[backlog]] and [[meta/_index|meta index]] had no inbound wikilinks after excluding plans, session archives, and lint reports as required by the skill.
- Repair: [[dashboard]] now links the capture inbox and path-qualified meta index in navigation and `related` metadata.
- Post-repair actionable orphans: none.

## Dead Links

- Genuine dead wikilinks after the required exclusions: 0.
- The strict catalog resolves Markdown, Canvas, and Base targets; asset-backed links were not misclassified as missing pages.
- Renamed/moved: 0. Frontier: 0. Obsolete intent: 0.

## Missing Pages and Cross-References

- No high-confidence repeated concept lacked a canonical page.
- The bounded cross-reference gaps were the unlinked capture inbox and meta index; both were repaired through [[dashboard]].

## Frontmatter Gaps

- Required fields missing: 0.
- Empty tags: 0.
- Missing `sessions`: 0.
- Explicit legacy-unknown `sessions: []`: 7 — six historical review artifacts and `wiki/plans/_index.md`. These were retained because the correct provenance cannot be inferred safely.

## Frontmatter Discipline

- `wiki/index.md`: `related` has 12 entries (>8); retain until navigation ownership is redesigned.
- `wiki/meta/sessions/LLM Obsidian v2.1.2 semantic acceptance refactor.md`: `related` has 10 entries (>8); these are review provenance and were not compacted.
- `wiki/concepts/DragonScale Memory.md`: frontmatter is 26 lines (>25); one line over the advisory threshold, so no unrelated rewrite was justified.
- Stale `status: developing` pages older than 30 days: 0.

## Stale Claims

- [[DragonScale on macOS]] still names `nomic-embed-text`, while the current helper requested `bge-m3`.
- [[DragonScale Memory]] documents historical `nomic-embed-text` defaults and older thresholds; the current configuration is `bge-m3` with error/review bands `0.92/0.85`.
- These claims are durable follow-up candidates, not bounded frontmatter/link/orphan repairs, so RT09 did not rewrite the concept pages.

## Empty Sections

- Empty leaf sections: 0 after manual review.
- Headings surfaced by the structural pass were container headings with populated subsections or intentionally empty review findings; they were not rewritten.

## Index Health

- Main-index unresolved entries: 0.
- Baseline folder-index finding: `wiki/meta/_index.md` listed 23 of 34 meta pages and was 11 pages behind.
- Repairs: added `<!-- AUTO-DATE --> 2026-08-01` to `wiki/index.md`, updated its page date, and regenerated the meta AUTO-INDEX block to all 34 current pages.

## Address Validation

- Counter state: `52`.
- Highest observed creation address: `c-000051`.
- Post-rollout pages checked: 51 (51 passing, 0 errors).
- Legacy pages pending backfill: 6 (informational).
- Excluded meta/fold/daily/navigation pages: 13.
- Format, uniqueness, counter drift, and required-address errors: 0.
- `.raw/.manifest.json`: absent, so address-map consistency there is not applicable; `.vault-meta/address-map.tsv` passed the strict validator.

## Semantic Tiling

- Model: `bge-m3`; local Ollama was reachable and the model was present.
- Pages scanned: 70; embedded: 20; excluded: 50.
- Cache hits: 0; recomputed: 20; orphaned cache entries pruned: 0.
- Errors (`>=0.92`): 0 pairs.
- Review (`0.85–0.92`): 0 pairs.
- Calibrated: false; results use the configured uncalibrated defaults and therefore remain advisory.

## Pipeline Usage Stats

```text
# Pipeline stats — last 30d (prompts in project: 0)

## Runtime-neutral observed operations

These are content-free events from shared scripts. They measure executed operations, not skill invocation or hook parity.

no runtime-neutral operations captured

## Model turn timing by session role

Completed and incomplete turns are content-free counters. Duration percentiles use completed turns only.

no model turn timing captured

## Unattended lifecycle dogfood

These counters describe orchestration mechanics only. They never contain task text, review prose, commands, queries, or error messages.

no unattended lifecycle events captured in this window

## Skill telemetry coverage

Skill invocations are counted from Claude history and transcripts only. Every other skill-capable runtime executes the same skills without leaving evidence here, so its row below is absence of measurement, not absence of use. Evidence records count per-source activity markers (prompts, transcript tool calls, router hits, script events); Claude draws on four sources and the others on fewer, so the counts are a presence test and are not comparable across runtimes.

| Runtime | Evidence records | Skill invocations observable |
|---|---:|---|
| claude | 0 | yes (history + transcripts) |
| codex | 0 | no — invocations invisible to this report |

## Claude-only skill telemetry

Typed/Auto come from Claude history and transcripts; they do not measure Codex skill usage. Router hints are runtime-tagged and cross-runtime, but record prompt intent rather than invocation.

| Skill | Typed | Auto | Total | Last used | Router hints | Hint→use ≤1h |
|---|---:|---:|---:|---|---:|---:|

## Skill usage evidence unavailable (32 installed)

No skill invocation of any kind was observed in 30d, so this window cannot rank skills. Check that Claude history/transcripts cover this project path and window before reading any zero as unused.

## Agents usage (Task tool, transcripts)

no Task-tool calls found in transcripts

## Retrieval assists (command-log.jsonl, Bash capture)

no retrieval-assist invocations captured

> Границы интерпретации: (1) Typed = history.jsonl (что напечатал user), Auto = Skill tool_use из транскриптов (что Claude вызвал сам) — источники комплементарны; (2) покрытие транскриптов ограничено их retention (~30д); (3) hint-precision грубая (окно 1ч, без привязки к сессии); (4) reference-скиллы (obsidian-markdown/bases) и замороженные (canvas, wiki) по нулям — это норма; (5) вызовы скиллов видны только в Claude-источниках — ноль означает «нет Claude-евиденса», а не «не используется», пока в окне наблюдался Codex.
```

Verdict: usage evidence is unavailable, so no skill or router rule is a dead-weight candidate from this audit.

## Skill Size & Quality

```text
skill descriptions: 7343/7500 bytes across 32 skills
skill bodies: 155734 bytes; normal closure: 199565 bytes
   439  distill-runbook
   422  save
   392  save-plan
   378  obsidian-markdown
   363  defuddle
skill audit: 32 audited, 0 errors, 0 warnings
```

## Plans and Drift Candidates

- Stale pending plans: none reported by the deterministic validator.
- Oldest `type: service`, `status: solid` pages: none in this snapshot.

## Review Resolution

- `holistic-01-stale-folder-index-claimed-clean` — applied: regenerated `wiki/meta/_index.md` to 34 pages and corrected the Index Health baseline/resolution.
- `holistic-02-corrupted-verbatim-validator-evidence` — applied: restored the captured 12-hex identifier `6cddcdcc64ac` in the validator block.
- `holistic-03-mutating-command-in-reproduction-block` — applied: separated read-only checks from the explicitly labeled mutating folder-index refresh.
- `holistic-04-audit-absent-from-operations-log` — applied: appended this audit to [[log]] through the dedicated transactional log operation.

## Reproduction

Run the read-only checks from the repository root:

```bash
python3 scripts/validate-vault.py
./scripts/allocate-address.sh --peek
./scripts/with-timeout 8 ./scripts/tiling-check.py --peek
./scripts/with-timeout 60 ./scripts/tiling-check.py
python3 scripts/pipeline-stats.py --days 30
python3 scripts/check-skill-budget.py
python3 skills/improve-skills/scripts/audit_skills.py --strict
python3 scripts/boundary-score.py --json --include-score-zero --top 200
```

> [!warning] Index refresh writes files
> `python3 scripts/reindex.py --folder-indexes` rewrites derived `.vault-meta/` indexes and folder `_index.md` blocks, and may restamp `wiki/index.md`. Run it only when intentionally refreshing indexes.

The orphan/dead-link pass resolves pathless links by unique filename, includes aliases and non-Markdown Obsidian targets for dead-link classification, ignores fenced examples, and applies the exclusions documented in the repository `wiki-lint` skill.
