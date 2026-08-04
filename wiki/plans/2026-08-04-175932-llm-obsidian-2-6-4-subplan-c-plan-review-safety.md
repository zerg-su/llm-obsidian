---
type: plan
title: "LLM Obsidian 2.6.4 — Subplan C — safe plan review, exact OIDs и DCG"
address: c-000113
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
  - review
  - dcg
---

# LLM Obsidian 2.6.4 — Subplan C — safe plan review, exact OIDs и DCG

## Outcome Contract

```json
{"schema_version":1,"purpose":"Выполнить Slice 7 утверждённого parent-плана 2.6.4 в отдельной ветке.","desired_outcome":"Планы ревьюятся только через code-owned purpose=intent facade: protected Outcome/disposition/evidence artifacts компилируются и хэшируются независимо, invalid/ambiguous input отклоняется до provider start, design-only delta продолжает retained lanes, exact Git OIDs и узкий DCG contract исключают дорогие ошибочные reviewer launches; context candidate resolver отдаёт готовую exact identity, которую dispatch принимает без переинтерпретации display title.","success_evidence":[{"evidence_id":"E9-reviewer-tools","observable":"ContextPacket содержит exact base/head OIDs и literal bounded review-inspect commands; symbolic refs и unsafe shell/DCG variants отклоняются."},{"evidence_id":"E12-plan-review-lifecycle","observable":"Новый plan facade валидирует один Outcome и четыре non-overlapping artifacts; invalid boundaries дают zero RuntimeSessionManager.start; design-only rebind не создаёт sessions, protected delta требует amendment/fresh boundary; resolver candidate проходит в dispatch request напрямую по exact context identity."},{"evidence_id":"E11-no-regression-c","observable":"Facade, review-inspect and DCG matrices pass without broader Bash, permission, provider, model-routing or review-topology drift."}],"non_goals":["Менять callback recovery, escalation records, Wiki self-heal, Makefile/audit manifest или release files.","Ослаблять lowercase exact-OID ingress или разрешать symbolic resolver.","Автоматически выбирать Full review либо менять существующую review topology/budgets.","Merge, push, tag, publish или release."]}
```

## Parent binding

[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]. Parent exact HEAD `9bd223ddd0e62a8b28e924169f6eeda2830c3558`, plan SHA-256 `2bcd5d57960a11afc9218f02acd20623b8d82e0c12c1fb3a2e0ae05e3b07745c`, Outcome SHA-256 `af24873e06669632c5c45e9818a8646918e3a086a358285d1781a1a5540660ae`.

Этот subplan владеет только parent Slice 7 и может исполняться параллельно A/B/D.

## Owned files and responsibilities

- `task-review-runner.py`, `task_review_current.py`, new `task_review_plan.py`, `task_review_request.py`, `task_review_context.py`;
- `dispatch-resolver.py`, `tests/test_dispatch_resolver.py` and dispatch request validation only if the round-trip RED requires it; display title remains presentation-only;
- `review-inspect.py` only for bounded exact metadata;
- `config/dcg/task.toml`, `scripts/dcg-test-suite.sh`, review skill/docs;
- new `tests/harness/test_plan_review_facade.py` (12 parent cases), +2 bounded review-inspect cases, +4 DCG assertions.

Do not edit `test_review_gate.py`, callback/recovery modules, Makefile, audit manifest, release matrix or release docs.

## TDD execution

1. RED: legacy `current --plan` defaults to implementation and starts providers; missing base and invalid/duplicated/overlapping artifacts also reach launch; design-only correction fails stale digest.
2. Add `plan` facade that always selects intent and validates exactly one Outcome Contract.
3. Compile four independent artifacts using exact semantic heading lines: Outcome, design, capability dispositions and success evidence map. Missing or overlapping protected regions fail before provider.
4. Bind exact base/head: safe single-parent plan commit may derive `HEAD^`; all other cases require trusted dispatched base or explicit lowercase OID.
5. Permit same-session retained-lane rebind only for design delta with reviewed/resolved digests and exact Git delta. Protected artifacts require amendment plus fresh boundary.
6. Add literal review-inspect commands and anchored exact escalation command allow only if the whole-command DCG matrix is green; broad Bash remains forbidden.
7. **D-264-15 — resolver/dispatch context identity.** RED reproduces direct use of a resolver candidate failing because display `title` is interpreted as filename stem. Add one explicit exact candidate identity and a round-trip test; presentation title never becomes authority, fuzzy lookup remains forbidden.

## Incidental defect

`D-264-15`: `dispatch-resolver.py` returns a human display title while dispatch request validation historically interprets `wiki_context.title` as an exact filename stem. Disposition: `included` in this subplan. Evidence: the four pre-effect validation failures for requests `35571834-a56d-4936-ad0f-c1ff00656edf`, `311bb014-c762-4ed3-ac81-224d9a309473`, `8f36c040-d134-4987-884c-375b29d27340`, and `6c5f69c7-7ef4-4926-bc88-80b50d4abd34`.

## Verification and handoff

Prove the resolver→dispatch exact-context round trip, zero `RuntimeSessionManager.start` in every invalid case, zero new sessions for design-only continuation, protected-delta rejection, exact OID behavior and negative shell/DCG controls. Final summary includes interfaces, exact commands, test/case counts and E9/E12 evidence. Leave standing test registration and release work to parent Slices 9–10. Reap closes this subplan only.
