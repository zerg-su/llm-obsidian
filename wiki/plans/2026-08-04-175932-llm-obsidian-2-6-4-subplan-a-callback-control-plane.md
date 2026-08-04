---
type: plan
title: "LLM Obsidian 2.6.4 — Subplan A — callback recovery и Harness control plane"
address: c-000111
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
  - callback
  - harness
---

# LLM Obsidian 2.6.4 — Subplan A — callback recovery и Harness control plane

## Outcome Contract

```json
{"schema_version":1,"purpose":"Выполнить Slices 0–5 и 5b утверждённого parent-плана 2.6.4 в изолированной ветке без изменения общих join/release файлов.","desired_outcome":"Reviewer callback recovery становится generation-bound, однократным, race-safe и fail-closed; принятый callback публикуется без повторного model/provider effect; superseded exact-owned review resources закрываются идемпотентно; stage baseline доказывает и закрывает только реальные Harness/LLM control-plane gaps без изменения PipelineSpec DSL.","success_evidence":[{"evidence_id":"E1-missing-submit-red","observable":"Сохранённый v2.6.3 full-runtime fixture воспроизводит idle current generation без typed submit и исходный callback-timeout."},{"evidence_id":"E2-generation-classifier","observable":"Pure classifier принимает только exact current generation, stable typed files, ownership и достаточный deadline; active/permission/unknown/stale/malformed дают zero effect."},{"evidence_id":"E3-bounded-submit-recovery","observable":"Один shared generation-bound nudge резервируется и отправляется в retained session без нового reviewer/surface; accepted callback автоматически продолжает lifecycle."},{"evidence_id":"E4-artifact-and-race-safety","observable":"Stable input/callback/receipt fast paths и все races/replays не дублируют prompt, callback, review или provider effect."},{"evidence_id":"E5-terminal-fail-closed","observable":"Terminal, exhausted, stale, lost ownership, symlink, oversize и insufficient deadline завершаются отдельной typed attention reason без расширения budgets."},{"evidence_id":"E13-superseded-review-cleanup","observable":"После durable evidence exact superseded parents закрываются до resource-free terminal state; current/active/unknown никогда не закрываются."},{"evidence_id":"E14-harness-control-plane","observable":"Stage matrix и regressions доказывают code-owned lifecycle transitions и zero transition от model prose; подтверждённые gaps закрыты только в baseline-red owner modules."}],"non_goals":["Редактировать Makefile, harness audit manifest, release matrix, release docs или version metadata.","Реализовывать append-only escalation history, plan-review facade или Wiki self-heal из Subplans B–D.","Добавлять scheduler, PipelineSpec parallel/join, новую публичную FSM, model routing или новые budgets.","Выполнять parent Slices 9–10, merge, push, tag, publish или release."]}
```

## Parent binding

[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]. Parent exact HEAD `9bd223ddd0e62a8b28e924169f6eeda2830c3558`, plan SHA-256 `2bcd5d57960a11afc9218f02acd20623b8d82e0c12c1fb3a2e0ae05e3b07745c`, Outcome SHA-256 `af24873e06669632c5c45e9818a8646918e3a086a358285d1781a1a5540660ae`.

Этот subplan владеет parent Slices 0–5 и 5b. Другие subplans могут выполняться параллельно, но не потребляют и не редактируют его owned files. Parent join принимает только committed receipts и exact summary.

## Owned files and responsibilities

- new `scripts/harness/callback_submit_recovery.py` and pure tests;
- `runtime_callback_io.py`, `runtime_worker_liveness.py`, `runtime_worker_execution.py`, `runtime_worker_review_bridge.py`, `runtime_session_cleanup.py`, `liveness.py` only where parent RED requires;
- callback/review/runtime suites named by Slices 0–5;
- new control-plane baseline Markdown/JSON and `test_harness_control_plane.py`;
- only baseline-red owners among `runtime_worker_loop.py`, `runtime_worker_spec.py`, `runtime_worker_verification.py`, `runtime_worker_fix.py`, `runtime_session_checkpoint.py`.

Forbidden shared ownership: `Makefile`, `config/harness-audit-manifest.json`, release matrix, release notes/changelogs/version manifests.

## TDD execution

1. **Slice 0 — frozen RED and authority matrix.** Preserve the v2.6.3 runtime failure and exact four-task lifecycle receipts. Produce stage-by-stage owner/module/test/manual-ingress matrix. No production change.
2. **Slice 1 — pure classifier.** Red controls for generation, deadline, ownership, malformed evidence and shared counter; minimal pure policy GREEN.
3. **Slice 2 — typed artifact fast paths.** Stable input submit and accepted callback ingestion without model call; keep validator/broker authoritative.
4. **Slice 3 — one shared-budget submit-only nudge.** Reserve→re-read→send→sent against exact generation; no second nudge budget and no production-floor drift.
5. **Slice 4 — publication/rearm/cleanup.** Publish accepted child once, use ingestion-only timeout rearm, close only evidence-bound superseded resources.
6. **Slice 5 — complete transition matrix.** Before/between/after-send races, generic/submit orders, timeout, stale, terminal, concurrency and cleanup cases.
7. **Slice 5b — control-plane gaps.** Add one independent RED per baseline gap; change only the red owner. If fixing requires scheduler/DSL/public FSM, stop and escalate.

Every slice follows red → minimal green → refactor while green and commits independently. Tests assert observable state/effects, not implementation calls.

## Verification and handoff

Run all focused callback, liveness, review bridge, runtime session, resolution bundle, transition matrix and control-plane suites. Do not edit standing test registration. Final summary reports exact commits, new test files/case counts, production modules changed, E1–E5/E13/E14 disposition and remaining join work. Reap closes this subplan only; parent remains pending.
