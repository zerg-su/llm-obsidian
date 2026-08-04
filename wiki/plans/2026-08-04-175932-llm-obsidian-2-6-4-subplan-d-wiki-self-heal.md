---
type: plan
title: "LLM Obsidian 2.6.4 — Subplan D — one-shot Wiki self-heal"
address: c-000114
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: executed
created: 2026-08-04
updated: 2026-08-04
tags:
  - plan
  - manual-save
  - llm-obsidian
  - v2-6-4
  - subplan
  - wiki
  - stop-hook
---

# LLM Obsidian 2.6.4 — Subplan D — one-shot Wiki self-heal

## Outcome Contract

```json
{"schema_version":1,"purpose":"Выполнить Slice 8 утверждённого parent-плана 2.6.4 в отдельной ветке.","desired_outcome":"Stop hook делает ровно одну атомарную vault-write попытку исправить только однозначный unresolved wikilink по unique filename/frontmatter title/H1, затем в правильном порядке перестраивает derived state и повторяет strict validation; ambiguous, missing, malformed, embed и concurrent cases остаются fail-closed без mutation.","success_evidence":[{"evidence_id":"E7-wikilink-self-heal","observable":"Deterministic Stop fixture исправляет unique normal/alias/anchor link через shared parser и sole writer; повторная validation проходит, индексы соответствуют committed page."},{"evidence_id":"E11-no-regression-d","observable":"Ambiguous/missing/concurrent/embed/malformed/fenced/inline/escaped-pipe cases подтверждают zero unsafe mutation и сохраняют existing Stop/vault behavior."}],"non_goals":["Создавать отсутствующие страницы, использовать fuzzy matching или угадывать неоднозначную ссылку.","Писать Wiki/derived state вне vault-write и documented rebuild order.","Менять callback, review, escalation, Makefile/audit manifest или release files.","Merge, push, tag, publish или release."]}
```

## Parent binding

[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]. Parent exact HEAD `9bd223ddd0e62a8b28e924169f6eeda2830c3558`, plan SHA-256 `2bcd5d57960a11afc9218f02acd20623b8d82e0c12c1fb3a2e0ae05e3b07745c`, Outcome SHA-256 `af24873e06669632c5c45e9818a8646918e3a086a358285d1781a1a5540660ae`.

Этот subplan владеет только parent Slice 8 и может исполняться параллельно A/B/C.

## Owned files and responsibilities

- new `scripts/vault_link_repair.py` — pure one-shot planner around validator catalog;
- `scripts/stop-hook.py` — bounded repair transaction and exact rebuild/validation order;
- `scripts/vault_schema.py` — one shared fence/inline/escaped-pipe-aware catalog/rewrite primitive;
- schema, Stop and vault-writer focused tests/fixtures.

Do not edit callback/review/escalation modules, Makefile, audit manifest, transition/release matrix or release docs.

## TDD execution

1. RED with the observed unique unresolved title/H1 link and pre-repair index mismatch.
2. Add independent zero-mutation controls: two matching titles, missing target, concurrent source change, malformed link and unsupported embed.
3. Extract one shared parser/catalog/rewrite primitive in `vault_schema.py`; compatibility neutralizer delegates it instead of duplicating parsing knowledge.
4. Plan only unique exact filename/title/H1 normal/alias/anchor rewrites with optimistic source SHA.
5. Apply one `vault-write` transaction, then reindex folder/index, BM25, sparse, fingerprint/dense-pending decision, one strict validation and scoped commit.
6. Emit only bounded user paths/count and content-free repair ID/path/counters telemetry.

## Verification and handoff

Run schema, Stop and vault scripts suites including fenced, inline, escaped-pipe, alias and anchor cases. Verify committed indexes match the repaired page and every unsafe case has zero mutation. Final summary lists changed parser/writer seams, exact fixtures/case counts and E7 evidence. Leave standing registration and release work to parent Slices 9–10. Reap closes this subplan only.

Результат: [[LLM Obsidian 2.6.4 Subplan D result]] (reaped 2026-08-04)
