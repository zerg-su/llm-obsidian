---
type: plan
title: "LLM Obsidian 2.6.4 — Subplan B — append-only coordinator decisions"
address: c-000112
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-08-04
updated: 2026-08-04
tags:
  - plan
  - manual-save
  - llm-obsidian
  - v2-6-4
  - subplan
  - decisions
  - harness
---

# LLM Obsidian 2.6.4 — Subplan B — append-only coordinator decisions

## Outcome Contract

```json
{"schema_version":1,"purpose":"Выполнить Slice 6 утверждённого parent-плана 2.6.4 в отдельной ветке.","desired_outcome":"Escalation и coordinator decisions сохраняются как immutable identity-bound append-only chain; latest marker становится pointer-only, legacy full markers мигрируют детерминированно, а все readers/writers переходят атомарно без потери решений или wakeups.","success_evidence":[{"evidence_id":"E8-durable-decisions","observable":"Каждая raise/resolution/amendment имеет immutable record identity и digest; latest marker содержит только authoritative pointer; legacy backfill однократен и идемпотентен."},{"evidence_id":"E10-defect-ledger","observable":"Tamper, stale origin, duplicate, overwritten-decision и lost-wakeup regressions доказывают сохранность всей D-264 history и точные dispositions."},{"evidence_id":"E11-no-regression-b","observable":"Focused lifecycle/custom/fix/recovery suites проходят без изменения public DSL, provider routing, permissions или budgets."}],"non_goals":["Редактировать callback/review test files, Makefile, audit manifest или release files, принадлежащие другим workstreams/join.","Менять OperationStore, строить общий event store или добавлять новый lifecycle owner.","Реализовывать callback recovery, plan-review facade или Wiki self-heal.","Merge, push, tag, publish или release."]}
```

## Parent binding

[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]. Parent exact HEAD `9bd223ddd0e62a8b28e924169f6eeda2830c3558`, plan SHA-256 `2bcd5d57960a11afc9218f02acd20623b8d82e0c12c1fb3a2e0ae05e3b07745c`, Outcome SHA-256 `af24873e06669632c5c45e9818a8646918e3a086a358285d1781a1a5540660ae`.

Этот subplan владеет только parent Slice 6 и может исполняться параллельно A/C/D.

## Owned files and responsibilities

- new `scripts/task_escalation_records.py` — append-only record validation, chain load and optimistic writes;
- `scripts/task_escalation.py` — CLI/delivery and `record-amendment`;
- `runtime_worker_custom.py`, `runtime_worker_control.py` — record-first/pointer-second writers;
- focused `test_task_lifecycle.py`, `test_task_review_mechanism_recovery.py` and new Workstream-B record/marker fixtures.

Do not edit `test_runtime_task_summary.py`, callback suites, Makefile, audit manifest, transition/release matrix or release docs.

## TDD execution

1. Inventory and freeze all three writers and five readers named by the parent plan.
2. RED: current writer emits a full marker; overwrite two decisions; pointer-only reader fails; legacy resolve lacks prior immutable record; repeated write changes bytes.
3. Add a deep records module with immutable identity/digest/chain validation and optimistic record write.
4. Migrate writers record-first then pointer-second; migrate all readers atomically through one chain lookup.
5. Keep legacy full marker read-compatible only for deterministic first backfill; new writers never emit it.
6. Add tamper, stale, origin, duplicate, amendment, lost-wakeup and idempotent replay cases. Refactor only after the matrix is green.

## Verification and handoff

Run focused escalation/lifecycle/custom/fix/recovery suites. Final summary lists every migrated writer/reader, record schema/interface, exact tests/case counts and E8/E10 evidence. Leave Makefile registration and release evidence to parent Slices 9–10. Reap closes this subplan only.
