---
type: plan
title: "LLM Obsidian 2.6.4 — unattended callback-submit watchdog"
address: c-000099
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
  - harness
  - unattended
---

# LLM Obsidian 2.6.4 — unattended callback-submit watchdog

## Outcome Contract

```json
{"schema_version":1,"purpose":"Закрыть подтверждённые unattended gaps 2.6.3, из-за которых завершённая reviewer-сессия, принятый callback, coordinator decision или однозначно исправимая Wiki-ссылка не продолжают pipeline без присутствия пользователя.","desired_outcome":"LLM Obsidian 2.6.4 сохраняет единственного harness lifecycle owner и автоматически продолжает безопасные exact-identity случаи: принимает уже созданный typed artifact без model call, выполняет не более одного generation-bound submit-only nudge в той же reviewer-сессии, публикует принятый resource-free callback, сохраняет coordinator decisions append-only, делает одну атомарную попытку исправить только однозначный unresolved wikilink через vault-write и оставляет typed attention для всего неизвестного. Reviewer tooling получает copy-paste exact OID evidence без ослабления read-only Git boundary. Работа пользователя офлайн либо продвигается, либо останавливается один раз с полной durable причиной, но не зависает молча.","success_evidence":[{"evidence_id":"E1-missing-submit-red","observable":"Детерминированный full-runtime fixture воспроизводит reviewer generation, где exact provider жив, экран стабильно idle, input/callback/receipt отсутствуют, а v2.6.3 до исправления завершается callback-timeout без continuation."},{"evidence_id":"E2-generation-classifier","observable":"Чистый code-owned classifier принимает только exact operation/run/lane/generation, stable typed-file digests, process/surface ownership и content-free prompt class; active, permission, unknown, stale и malformed evidence никогда не становятся recovery."},{"evidence_id":"E3-bounded-submit-recovery","observable":"Stable idle current generation резервирует один write-ahead submit-only effect, отправляет один фиксированный nudge в ту же exact session, не создаёт reviewer/surface и после валидного callback автоматически продолжает pipeline."},{"evidence_id":"E4-artifact-and-race-safety","observable":"Уже существующие stable input/callback/receipt обрабатываются без model call; callback races до reservation, между reservation/send и после send, restart и concurrent reconcile не дублируют prompt, submit, callback, review или provider effect."},{"evidence_id":"E5-terminal-fail-closed","observable":"Terminal parent, exhausted existing ceilings, lost ownership, dead/unknown provider, stale generation, symlink, oversize и malformed artifacts дают отдельную typed attention reason; budgets nudges/restarts/provider calls не увеличены."},{"evidence_id":"E6-unattended-dogfood","observable":"Один изолированный end-to-end dogfood с offline coordinator проходит намеренно пропущенный reviewer submit, ровно один same-session recovery, accepted receipt и следующую pipeline stage без ручного current/resume/send/callback write и без повторного review."},{"evidence_id":"E7-wikilink-self-heal","observable":"Stop fixture с уникальным frontmatter title или H1 атомарно канонизируется через optimistic vault-write и проходит повторную strict validation; ambiguous, missing, malformed, embed-unsupported и concurrently changed cases не мутируются."},{"evidence_id":"E8-durable-decisions","observable":"Каждая escalation/decision получает append-only identity-bound record, latest marker содержит только pointer, а отдельный amendment-record workflow связывает frozen plan/Outcome digests с coordinator decision без изменения утверждённых байтов и без потери прежних решений."},{"evidence_id":"E9-reviewer-tools","observable":"Review ContextPacket/prompt предоставляет exact base/head OID и готовые bounded review-inspect invocations; symbolic/non-OID ref по-прежнему отклоняется, а точный task_escalation raise argv проходит task DCG только через узкий anchored allow contract, тогда как shell composition и destructive variants блокируются."},{"evidence_id":"E10-defect-ledger","observable":"Все D-264 entries имеют reproducer/evidence, owner, release disposition included/already-shipped/deferred/not-a-defect и regression/evidence pointer; новая escalation не перезаписывает историю, а release review доказывает отсутствие потерянных unresolved entries."},{"evidence_id":"E11-no-regression","observable":"Focused unit/transition tests, full harness coverage, make test, vault validation, Codex/MCP sync, permission/provider snapshots and release acceptance green on one exact candidate HEAD; no public DSL, model routing or permission-budget drift."}],"non_goals":["Парсить terminal prose или синтезировать verdict/callback без typed reviewer artifact и штатного validator.","Добавлять scheduler, второй lifecycle owner, новую публичную FSM или model-owned watchdog.","Делать периодические model calls во время видимой активности или увеличивать nudge/restart/review/verification/provider budgets.","Обходить dontAsk, sandbox, exact-generation ownership, callback validation или разрешать произвольный Bash через DCG.","Создавать отсутствующие Wiki-страницы, применять fuzzy matching, угадывать неоднозначную ссылку или писать vault в обход vault-write.","Переделывать PipelineSpec DSL, Project Spaces/Task Orchestration 2.7, model routing или review topology.","Повторно реализовывать D-264-06/D-264-08, уже поставленные в 2.6.3, либо превращать одноразовую ошибку ad-hoc reviewer Python probe в product fix без воспроизводимого contract seam.","Push, publish, tag, release или удаление safety branches/worktrees в рамках реализации плана."]}
```

## 1. Контекст и подтверждённый дефект

В 2.6.3 Opus закончил verification review, напечатал содержательный результат и вернулся к интерактивному `❯`, но не создал `.review-input.json` и не запустил `review_submit.py`. Exact callback target ожидал generation 3, поэтому callback broker не получил ни input, ни callback, ни receipt. Parent закономерно перешёл в `callback-timeout`, а unattended pipeline остановился до возвращения пользователя.

Это не ошибка callback broker: предыдущая generation той же lane и соседняя engineering lane успешно прошли тем же transport. Пробел находится между provider-visible окончанием работы и обязательным typed submit.

Связанные решения уже существуют:

- [[LLM Obsidian 2.6 dogfood RT1 callback watchdog architecture]] описывает passive liveness и bounded nudge/restart ownership;
- [[2026-07-30-224926-llm-obsidian-2-5-model-authored-custom-pipelines|LLM Obsidian 2.5 — Model-Authored Custom Pipelines]] требует, чтобы transport не зависел только от того, вспомнила ли модель выполнить submit;
- 2.6.3 исправляет accepted-callback cleanup, но не распознаёт `idle prompt + current generation without submit`.

## 2. Граница patch-релиза

2.6.4 добавляет два узких unattended recovery capability: для harness-owned reviewer callbacks и для однозначно исправимой адресации wikilink в Stop hook. Callback recovery использует существующие `OperationStore`, `RuntimeSessionManager`, `LivenessController`, callback target/receipt и cmux exact ownership. Vault recovery использует существующие schema/index/validator и единственный разрешённый writer. Новый scheduler, controller, state store или public DSL не создаются.

Сначала capability включается только для `reviewer-callback` и его verification continuation. Общий detector оформляется без привязки к Opus/Fable, но расширение на research, task-summary и произвольные pipeline result modes требует отдельного evidence и не входит в 2.6.4.

## 3. Целевая модель состояния

### 3.1 Durable evidence

Observer принимает решение только из bounded набора:

- exact owner/operation/lane/run и callback generation;
- callback target identity/digest и текущий parent/round revision/state;
- regular non-symlink input/callback/receipt, bounded size и stable SHA-256;
- exact provider/supervisor/surface/process ownership;
- content-free screen class `active`, `idle-prompt`, `permission`, `unknown`, `missing`;
- monotonic observation time, callback deadline и remaining seconds;
- два последовательных stable idle observations, причём Slice 0 обязан установить, какой v2.6.3 guard фактически скрывал recovery: prompt-state gate, screen-digest churn или deadline ordering;
- существующие `LivenessState.nudge_count/restart_count` и один generation-bound effect receipt.

Ни prompt, ни review body, ни raw screen, ни callback payload не попадают в telemetry/decision record. Prompt class и screen digest не являются progress сами по себе: progress определяется exact callback generation evidence.

### 3.2 Internal classification

1. `working` — active evidence либо generation ещё не ожидает submit;
2. `typed-input-ready` — exact stable input существует, callback отсутствует;
3. `callback-ready` — exact callback существует, receipt отсутствует;
4. `idle-without-submit` — exact provider/surface живы, два probes подтверждают idle, current generation не имеет typed artifacts;
5. `recovery-reserved` — единственный существующий nudge budget атомарно зарезервирован; effect receipt имеет `status=reserved`;
6. `recovery-sent` — тот же receipt атомарно переведён в `status=sent` после успешного exact-session send;
7. `accepted` — callback receipt принят;
8. `attention` — ownership/evidence неизвестны, deadline недостаточен либо существующие ceilings исчерпаны.

Это review-callback classification layer, не новая публичная FSM. `reserved` и `sent` — два состояния одного write-ahead effect, поэтому race до reservation, между reservation/send и после send наблюдаемы отдельно.

## 4. Recovery ladder и deadline policy

1. Stable exact input проходит существующий `review_submit.py` validator code-owned способом без model call.
2. Existing callback проходит `CallbackBroker`; existing receipt побеждает все recovery actions.
3. Slice 0 сначала доказывает точный v2.6.3 suppression guard. Новый classifier не может зависеть от того же ошибочного progress signal.
4. Submit recovery — специализация существующего generic liveness nudge, а не второй nudge. Для reviewer callback modes он потребляет тот же persisted `LivenessState.nudge_count` и заменяет generic message generation-aware submit-only message. Per generation/provider session возможен максимум один provider-facing liveness prompt суммарно.
5. До reservation harness требует exact ownership и минимум два production probe intervals до текущего callback deadline. Порог code-owned, derived from existing policy и не доступен DSL/model. Недостаток времени даёт `callback-submit-deadline-insufficient`, без prompt и без deadline extension.
6. После atomic `reserved` harness повторно читает input/callback/receipt. Если race не выиграл, отправляет в ту же exact session фиксированный prompt: записать уже сформированный result только в разрешённый `.review-input.json` и выполнить exact сохранённую `review_submit.py` command; callback вручную не писать. После успешного send receipt становится `sent`.
7. Если exact callback принят уже после parent `CALLBACK_TIMEOUT`, harness может ровно один раз использовать существующий `OperationStore.rearm_callback_timeout` только для ingestion/publication принятого round evidence по тем же preconditions, что `review_gate_resolution.py`. Это не даёт модели дополнительного времени и не меняет configured task budget.
8. Dead provider до accepted callback использует только существующий identity-bound restart budget. Accepted artifact никогда не вызывает restart.
9. После одного общего nudge и разрешённого существующим policy restart отсутствие callback становится typed `callback-submit-missing`. Infinite retries и ad-hoc deadline extension запрещены.

## 5. Ownership и безопасность

- Generic ceilings остаются у `LivenessController`; callback policy только уточняет, когда и какой reviewer-safe prompt исполняет уже разрешённый nudge.
- Callback создаёт reviewer и валидирует `review_submit.py`; harness не извлекает verdict из terminal prose.
- Reservation bound to operation/run/lane/generation/target digest; смена любого поля делает action stale.
- `permission`, `unknown`, active/changing screen без подтверждённого idle, missing ownership и insufficient deadline дают zero effect.
- Exact callback/input/receipt re-read выполняется до reservation и непосредственно перед send.
- Accepted callback не очищается и не переиспользуется; OperationStore/gate не редактируются вручную.
- Production `LivenessPolicy.default()` и floors остаются byte-for-byte по значениям; test-only clock/policy injection не доступна product spec/DSL.

## 6. Архитектурная карта

### 6.1 Callback recovery

| Модуль | Ответственность | Интерфейс | Test seam |
|---|---|---|---|
| `scripts/harness/callback_submit_recovery.py` (новый) | Pure generation/deadline classification и deterministic action identity | immutable evidence/state/decision + `classify_callback_submit(...)` | `tests/harness/test_callback_submit_recovery.py` |
| `scripts/harness/runtime_callback_io.py` | Stable bounded typed-artifact reads/validation | существующие target/read/submit helpers | review vertical fixtures |
| `scripts/harness/runtime_worker_liveness.py` | Evidence assembly и исполнение одного уже budgeted action | `inspect_liveness()` + injected test clock/policy seam; production defaults unchanged | `test_runtime_sessions.py`, новый full-runtime test |
| `scripts/harness/runtime_worker_review_bridge.py` | Review-specific reconcile/accepted-child publication | existing gate/broker seams | `test_review_vertical.py`, `test_review_resolution_bundle.py` |
| `scripts/harness/liveness.py` | Единственный owner generic nudge/restart counters | existing policy/state/controller | existing `test_liveness.py` + shared-ceiling cases |

Callback submit effect не имеет отдельного model-call counter. Он резервирует/читает тот же `nudge_count`; generic and submit-specific branches mutually exclusive for one generation.

### 6.2 Decision history и amendment records

| Модуль | Ответственность | Интерфейс | Test seam |
|---|---|---|---|
| `scripts/task_escalation_records.py` (новый) | Append-only chain validation и optimistic record writes | `append_raise`, `append_resolution`, `append_amendment`, `load_chain` | новый records unit suite |
| `scripts/task_escalation.py` | CLI/delivery | `raise`, `resolve`, `record-amendment` | task lifecycle suite |
| harness writers | Создать полный compatible marker + chain pointer через records helper | `runtime_worker_custom.py`, `runtime_worker_control.py` | custom/fix decision fixtures |
| `.task-needs-attention.json` | Полный latest marker для существующих readers плюс additive `record_id/record_sha256` | прежние поля не удаляются | recovery/boundary/reap/close tests |

Writer inventory frozen before change: `task_escalation.py`, `runtime_worker_custom.py`, `runtime_worker_control.py`. Reader inventory включает `task_review_mechanism_recovery.py`, `task_review_boundary_authorization.py`, `task_contract.py`, `cmux_surface_exit.py`, `task_reap_lifecycle.py`. Harness-raised marker обязан войти в chain; legacy marker without prior raise record получает один deterministic backfill при resolve. Immutable repeated writes производят те же bytes и не конфликтуют с pointer fields.

### 6.3 Plan-review, reviewer OIDs и task DCG

Новый `task-review-runner.py plan` — единственный документированный фасад вычитки планов:

- всегда задаёт `purpose=intent`;
- validates exactly one Outcome Contract;
- materializes intent boundary из plan/design/dispositions/evidence artifacts; один plan может исполнять все роли только если соответствующие sections обнаружены deterministically;
- для current single-parent plan commit выводит base=`HEAD^` только когда exact plan path изменён в этом commit; иначе требует explicit exact `--base` и fail closed;
- dispatched lifecycle берёт base из trusted `initial_head_sha`;
- записывает exact base/head OIDs в ContextPacket и literal `review-inspect status/log/diff/commit` commands;
- invalid/ambiguous boundary завершается до `RuntimeSessionManager.start`, что доказывается zero provider sessions.
- plan subject и control boundary разделены: Outcome Contract digest остаётся frozen, а resolution связывает reviewed/resolved plan digests и exact Git delta; изменение только plan subject не делает control-plan stale и verification продолжается в тех же retained lanes без нового provider/session.

Legacy `current --plan` без явного compatible purpose/boundary больше не молча default'ится в implementation: он возвращает actionable typed error до provider launch и предлагает `plan`. `review-inspect.py` сохраняет strict lowercase-OID ingress. `config/dcg/task.toml` получает anchored allow только если whole-command negative matrix доказывает безопасное поведение установленной DCG; broad Bash запрещён.

### 6.4 Wiki link repair и Stop order

`vault_schema.py` остаётся read-only authority. Его catalog расширяется unique frontmatter-title/H1 indexes, а один общий fence/inline/escaped-pipe aware rewrite primitive используется и новым `vault_link_repair.py`, и compatibility wrapper `neutralize_unresolved_wikilinks`; второй parser/rewrite implementation не создаётся.

Stop order после успешной one-shot repair transaction:

1. reindex folder/index state;
2. BM25 ensure;
3. sparse ensure;
4. recompute sparse fingerprint и retrieval-quality/dense pending decision;
5. one full strict validation;
6. scoped commit.

User-visible output сообщает repaired page paths/count; content-free event содержит только repair ID, relative paths и counters — не link text/body.

### 6.5 Cohesion decisions

- Pure callback policy не смешивается с process/cmux worker.
- Decision chain не превращается в общий event store и не подменяет OperationStore.
- Wiki repair planner зависит от validator/catalog, но mutation остаётся только у `vault-write.py`.
- `review-inspect` не получает symbolic-ref resolver; correct OIDs предоставляет lifecycle boundary.
- Рост больших files допускается только через малые owned interfaces; pass-through modules запрещены.

## 7. Execution topology

После Slice 0 независимы:

- Workstream A: Slices 1–5 callback continuity;
- Workstream B: Slice 6 decision/amendment history;
- Workstream C: Slice 7 plan-review/OID/DCG;
- Workstream D: Slice 8 Wiki self-heal.

Shared-file rules:

- `tests/harness/test_review_gate.py` принадлежит только Workstream A/Slice 4;
- Workstream C создаёт отдельный `tests/harness/test_plan_review_facade.py` и не редактирует `test_review_gate.py`;
- Workstream B эксклюзивно владеет `runtime_worker_custom.py` и `runtime_worker_control.py` в своей ветке;
- Makefile/audit manifest/release matrix/release docs принадлежат join Slices 9–10, не feature branches;
- join сравнивает expected case counts/registered suites каждой ветки и затем запускает полный harness.

## 8. TDD-срезы

### Slice 0 — frozen v2.6.3 RED и triage

- **files/responsibility:** новый `tests/harness/test_callback_submit_recovery_runtime.py` + fixture files — deterministic real-runtime/fake-provider incident; `docs/acceptance/v2.6.4-baseline.md` — failed receipt, exact base и D-264 dispositions.
- **consumes:** clean `v2.6.3`, existing fake-provider process seam, retained incident identity.
- **produces:** runnable test that fails on v2.6.3 because callback generation reaches timeout without continuation; evidence names exact suppressing guard among prompt-state, screen churn, deadline ordering and records existing generic nudge behavior.
- **failing evidence:** preserved command/exit/output digest on v2.6.3; active reviewer negative control.
- **minimal green:** none in this slice; RED commit/receipt is the deliverable and Slices 1–4 own GREEN. Production code remains unchanged.
- **refactor seam:** none.
- **focused verification:** run fixture in preserved v2.6.3 worktree without wall-clock sleeps above injected poll seam; E1, E10 baseline.

### Slice 1 — pure generation/deadline classifier

- **files/responsibility:** new `callback_submit_recovery.py`, new pure unit test.
- **consumes:** exact identities/digests, deadline remaining, content-free class, shared generic counters.
- **produces:** states including distinct `recovery-reserved`/`recovery-sent`; deterministic action bound to generation/target.
- **failing evidence:** v2.6.3 suppression guard from Slice 0 plus active/permission/unknown/stale/malformed/insufficient-deadline controls.
- **minimal green:** pure policy only; no IO/provider/store.
- **refactor seam:** shared SHA/ID validation only if contracts stay identical.
- **focused verification:** mutation removes generation/deadline/shared-counter check and must fail; E2, E5.

### Slice 2 — stable artifact fast paths

- **files/responsibility:** `runtime_callback_io.py`, `runtime_worker_review_bridge.py`, `test_review_vertical.py`.
- **consumes:** Slice 1 input/callback-ready decisions, existing validator/broker.
- **produces:** stable input submit or callback accept without model call.
- **failing evidence:** input-without-callback and callback-without-receipt stuck; symlink/oversize/malformed negative controls.
- **minimal green:** reuse existing schema/validator/broker; no synthetic callback.
- **refactor seam:** one stable-file reader only if errors remain exact.
- **focused verification:** races before action and idempotent replay; E3, E4, E5.

### Slice 3 — single shared-budget nudge и deterministic worker seam

- **files/responsibility:** `runtime_worker_liveness.py`, `runtime_worker_execution.py` for test-only dependency injection, `liveness.py` only for shared-ceiling decision if RED requires, `test_runtime_sessions.py`, Slice 0 runtime test.
- **consumes:** exact idle decision, one existing nudge counter, callback deadline, fake cmux adapter with `read/send/send_key`, injected clock/policy for tests.
- **produces:** reserved→sent receipt, one reviewer-safe prompt, normal production default/floors unchanged.
- **failing evidence:** full-runtime RED goes green; generic-first→submit refused and submit-first→generic refused; deadline insufficient→zero send.
- **minimal green:** route reviewer callback nudge through specialization while consuming same counter; no second budget.
- **refactor seam:** deterministic message renderer; no provider abstraction.
- **focused verification:** first in-process worker-liveness test, no sleeps beyond poll seam; E1, E3–E5, E11.

### Slice 4 — accepted-child publication и timeout rearm

- **files/responsibility:** `runtime_worker_review_bridge.py`, `task_review_verification.py`, `task_review_resolution_bundle.py`, `test_review_gate.py`, `test_runtime_task_summary.py`, `test_review_resolution_bundle.py`.
- **consumes:** accepted resource-free child, exact parent/gate, existing `rearm_callback_timeout` and accepted-round preconditions.
- **produces:** one publication/continuation; accepted-after-timeout ingestion-only rearm once; typed insufficient/exhausted attention.
- **failing evidence:** D-264-02, callback accepted after parent timeout, deadline expires between reservation/callback.
- **minimal green:** no fresh provider/lane/generation and no configured budget change.
- **refactor seam:** reuse exact-chain validation with one owner.
- **focused verification:** double replay identical bytes; E3–E5.

### Slice 5 — callback transition matrix

- **files/responsibility:** `test_contract_state_edge_matrix.py`, callback fixtures, no audit manifest edits yet.
- **consumes:** Slices 1–4.
- **produces:** before/between/after-send races, both generic/submit orders, deadline expiry, accepted-after-timeout, stale/terminal/ownership/concurrency cases.
- **failing evidence:** every case begins red against preserved base for intended state/effect mismatch.
- **minimal green:** only gaps revealed by table.
- **refactor seam:** fixture consolidation without losing independent expectations.
- **focused verification:** oracle and callback suites; E2, E4, E5, E11.

### Slice 6 — append-only coordinator authority

- **files/responsibility:** new `task_escalation_records.py`, `task_escalation.py`, `runtime_worker_custom.py`, `runtime_worker_control.py`, new records test, `test_task_lifecycle.py`, `test_task_review_mechanism_recovery.py`, focused custom/fix marker tests and docs.
- **consumes:** all three marker writers, full legacy marker fields, all identified readers, plan/Outcome digests.
- **produces:** additive chain pointer, immutable records, deterministic legacy backfill, idempotent amendment workflow.
- **failing evidence:** overwrite two decisions; resolve harness-raised marker without prior record; repeated immutable write; recovery/boundary readers on new marker.
- **minimal green:** keep all current marker fields; add pointer fields; route every writer through one records helper.
- **refactor seam:** delivery stays CLI/worker-owned, records module owns chain.
- **focused verification:** tamper/stale/origin/duplicate/lost-wakeup plus custom/fix/recovery cases; E8, E10.

### Slice 7 — safe plan-review facade, exact OIDs и DCG

- **files/responsibility:** `task-review-runner.py`, `task_review_current.py`, new `task_review_plan.py`, `task_review_request.py`, `task_review_context.py`, `review-inspect.py` only if bounded metadata is necessary, `config/dcg/task.toml`, `dcg-test-suite.sh`, new `test_plan_review_facade.py`, `test_review_inspect.py`, `test_dcg_assets.sh`, review skill/docs.
- **consumes:** plan bytes/Outcome Contract, exact current/dispatched lifecycle base source, existing runtime manager and strict OID validator.
- **produces:** `plan` subcommand with automatic intent boundary; distinct frozen control digest and mutable subject-plan reviewed/resolved digests; literal exact commands; pre-provider typed rejection for ambiguous legacy invocation; anchored escalation allow.
- **failing evidence:** `current --plan` defaults to implementation and starts two providers; resolving its findings changes plan bytes and current verification rejects `review program plan digest is stale`; current review missing base; dispatched/current exact command snapshots; shell/destructive negative controls.
- **minimal green:** common single-parent plan commit auto base, otherwise explicit exact base; no symbolic resolver/broad Bash.
- **refactor seam:** plan boundary compiler is deep module; current facade delegates once.
- **focused verification:** assert zero `RuntimeSessionManager.start` on invalid combinations; unchanged Outcome plus exact plan delta continues both retained lanes with zero new sessions; Outcome mutation still fails closed; E9, E11.

### Slice 8 — one-shot Wiki self-heal

- **files/responsibility:** new `vault_link_repair.py`, `stop-hook.py`, `vault_schema.py` shared rewrite/catalog primitive, schema/Stop/writer tests.
- **consumes:** wikilink-only validator failure, unique filename/title/H1 index, source SHA, vault-write.
- **produces:** one repair transaction, rerun derived-state phases, one validation, visible bounded report/content-free event.
- **failing evidence:** two real title links; ambiguity/missing/concurrent/embed/malformed zero mutation; pre-repair index mismatch.
- **minimal green:** normal/alias/heading links via shared parser; compatibility neutralizer delegates same primitive.
- **refactor seam:** remove duplicate parser knowledge; validator remains read-only.
- **focused verification:** committed index matches repaired page; fenced/inline/escaped-pipe/alias/anchor cases; E7, E11.

### Slice 9 — standing gates и integration join

- **files/responsibility:** `Makefile`, `config/harness-audit-manifest.json`, transition oracle/release matrix; no feature code.
- **consumes:** green Workstreams A–D and their expected suite counts.
- **produces:** every new suite reachable from standing `make test`/`make test-harness`; every production module in honest coverage denominator; joined counts retained.
- **failing evidence:** unregistered test/module or dropped shared assertion fails join.
- **minimal green:** registration/conflict correction only.
- **refactor seam:** none after join.
- **focused verification:** standing targets + coverage; E10, E11.

### Slice 10 — dogfood, docs и RC

- **files/responsibility:** observability/runtime docs, `v2.6.4-release-readiness.md`, release notes/changelogs/version manifests.
- **consumes:** one clean integrated HEAD and all receipts.
- **produces:** offline fake-provider missing-submit dogfood, normal live Opus review with zero false nudge, exact D-264 map.
- **failing evidence:** manual lifecycle command, deadline/budget drift, repeat effect, missing disposition or stale evidence blocks RC.
- **minimal green:** evidence/docs/metadata only; post-review product fix creates new HEAD.
- **refactor seam:** none; release approval-or-stop.
- **focused verification:** E6 dogfood + full gate + Opus Deep implementation + release review; E6, E10, E11.

## 9. Полная verification ladder

```bash
python3 tests/harness/test_callback_submit_recovery.py
python3 tests/harness/test_callback_submit_recovery_runtime.py
python3 tests/harness/test_liveness.py
python3 tests/harness/test_review_resolution_bundle.py
python3 tests/harness/test_review_vertical.py
python3 tests/harness/test_runtime_sessions.py
python3 tests/harness/test_runtime_task_summary.py
python3 tests/test_task_escalation_records.py
python3 tests/test_task_lifecycle.py
python3 tests/harness/test_task_review_mechanism_recovery.py
python3 tests/harness/test_plan_review_facade.py
python3 tests/harness/test_review_inspect.py
bash tests/test_dcg_assets.sh
bash scripts/dcg-test-suite.sh
python3 tests/test_vault_schema.py
bash tests/test_stop_hook.sh
bash tests/test_vault_scripts.sh
make test-harness
make test-harness-coverage
make test
python3 scripts/runtime-harness-lint.py
python3 scripts/validate-vault.py --summary
python3 scripts/codex-adapter.py --check
scripts/mcp-gateway/mcp-gateway.sh codex-sync --check
git diff --check v2.6.3..HEAD
```

Evidence rules:

- production timing floors/defaults compared byte-for-byte with v2.6.3; tests inject clock/policy only through non-public seam;
- mutation checks break generation, shared nudge ceiling, deadline guard, sent marker, ambiguity and DCG anchoring;
- every new test registered in Makefile/manifest and every production module traced in-process;
- telemetry contains IDs/paths/counters only;
- fake-provider dogfood intentionally omits submit; normal live review proves zero false nudge/status/provider drift.

## 10. Review policy

1. Plan: `task-review-runner.py plan --deep --runtime claude --model opus --effort xhigh`; same two Opus parents verify amendments.
2. Workstreams: focused self-review + unit/transition gates.
3. Join: Opus single-model Deep intent+engineering on exact candidate.
4. Release: separate purpose=release approval-or-stop.
5. Full only by explicit user request.

Findings use same-session verification. Scope/public-interface/security/migration forks enter the append-only decision chain; no hidden fix loop.

## 11. Stop conditions и rollback

- New callback classifier still depends on the v2.6.3 suppression guard → redesign before GREEN.
- Generic and submit-specific prompt can both consume one generation → release blocked.
- Remaining deadline below bounded submit window → zero nudge, typed attention.
- Duplicate prompt/callback/provider effect in any order → release blocked.
- DCG exact allow matrix fails → D-264-07 deferred only via Slice 6 amendment bound to frozen plan/Outcome; E9 remains partial and RC is blocked until coordinator/user changes the contract or supplies a safe design. Broad allow remains forbidden.
- Link repair cannot prove unique exact target or derived indexes are not rebuilt → zero commit.
- Decision chain cannot preserve legacy full marker readers/writers → migration decision required.
- Plan-review invalid/ambiguous input starts any provider → release blocked.
- Dogfood needs manual current/resume/send/callback write or budget change → E6 missing.
- Before publish rollback is branch/worktree lifecycle only; after merge one revert commit, evidence chain retained.

## 12. Incidental defect ledger

| ID | Defect | Owner | Disposition |
|---|---|---|---|
| D-264-01 | Reviewer idle without typed submit | callback recovery | `included`, Slices 0–5 |
| D-264-02 | Accepted resource-free child not published | review bridge | `included`, Slice 4 |
| D-264-03 | Latest attention marker overwrites decisions | escalation records | `included`, Slice 6 |
| D-264-04 | Unique title/H1 link blocks Stop | vault/Stop | `included`, Slice 8 |
| D-264-05 | No frozen-plan amendment workflow | escalation records | `included`, Slice 6 |
| D-264-06 | Quiescent recovery rejected | review recovery | `already-shipped-in-2.6.3`, `68a13ef` |
| D-264-07 | Exact escalation CLI blocked by task DCG | permission policy | `included-if-matrix-green`; otherwise amendment + RC block because E9 partial |
| D-264-08 | Legacy spec rehydrate mismatch | review recovery | `already-shipped-in-2.6.3`, `f3212cd` |
| D-264-09 | Reviewer improvises symbolic ref because exact base absent | review boundary | `included`, Slice 7 |
| D-264-10 | Inline reviewer Python assumes tuple is dict | ad-hoc probe | `not-a-defect` until repo contract reproducer exists |
| D-264-11 | Plan review defaulted to implementation, launched two expensive Opus sessions and rejected missing code | plan-review facade | `included`, Slice 7; wrong/ambiguous invocation must start zero providers |
| D-264-12 | Same-session plan finding resolution rejects the corrected plan as stale because control-plan and review subject share one digest | plan-review facade | `included`, Slice 7; frozen Outcome, exact plan delta, retained lanes, zero new sessions |

New D-264 entry is recorded before task continuation. Disposition changes only through Slice 6 authority record and never grants unrelated fix scope.

## 13. Requirement-to-slice coverage

| Evidence | Exact slices |
|---|---|
| E1 | 0, 3 |
| E2 | 1, 5 |
| E3 | 2, 3, 4 |
| E4 | 2, 3, 4, 5 |
| E5 | 1, 2, 3, 4, 5 |
| E6 | 10 |
| E7 | 8 |
| E8 | 6 |
| E9 | 7 |
| E10 | 0, 6, 9, 10 |
| E11 | 3, 5, 7, 8, 9, 10 |

Table is the authority consumed by join/release evidence. Every slice-level focused verification matches it exactly.

## 14. Завершение

2.6.4 becomes RC only when E1–E11 are established on one exact HEAD; all D-264 dispositions are durable; one shared nudge budget, callback deadline and accepted-timeout rearm are proven; offline dogfood advances without manual action; normal review has zero extra effects; plan-review mistakes fail before provider launch; Wiki repair rebuilds derived state through the sole writer; and provider/permission/model budgets match v2.6.3. Push, tag and GitHub release remain separate explicit user actions after terminal approval.
