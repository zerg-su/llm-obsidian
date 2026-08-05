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
updated: 2026-08-05
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
{"schema_version":1,"purpose":"Закрыть подтверждённые unattended gaps 2.6.3, из-за которых завершённая reviewer-сессия, принятый callback, coordinator decision или однозначно исправимая Wiki-ссылка не продолжают pipeline без присутствия пользователя.","desired_outcome":"LLM Obsidian 2.6.4 сохраняет единственного harness lifecycle owner и автоматически продолжает безопасные exact-identity случаи: принимает уже созданный typed artifact без model call, выполняет не более одного generation-bound submit-only nudge в той же reviewer-сессии, публикует принятый resource-free callback, сохраняет coordinator decisions append-only, делает одну атомарную попытку исправить только однозначный unresolved wikilink через vault-write и оставляет typed attention для всего неизвестного. Reviewer tooling получает copy-paste exact OID evidence без ослабления read-only Git boundary. Работа пользователя офлайн либо продвигается, либо останавливается один раз с полной durable причиной, но не зависает молча.","success_evidence":[{"evidence_id":"E1-missing-submit-red","observable":"Детерминированный full-runtime fixture воспроизводит reviewer generation, где exact provider жив, экран стабильно idle, input/callback/receipt отсутствуют, а v2.6.3 до исправления завершается callback-timeout без continuation."},{"evidence_id":"E2-generation-classifier","observable":"Чистый code-owned classifier принимает только exact operation/run/lane/generation, stable typed-file digests, process/surface ownership и content-free prompt class; active, permission, unknown, stale и malformed evidence никогда не становятся recovery."},{"evidence_id":"E3-bounded-submit-recovery","observable":"Stable idle current generation резервирует один write-ahead submit-only effect, отправляет один фиксированный nudge в ту же exact session, не создаёт reviewer/surface и после валидного callback автоматически продолжает pipeline."},{"evidence_id":"E4-artifact-and-race-safety","observable":"Уже существующие stable input/callback/receipt обрабатываются без model call; callback races до reservation, между reservation/send и после send, restart и concurrent reconcile не дублируют prompt, submit, callback, review или provider effect."},{"evidence_id":"E5-terminal-fail-closed","observable":"Terminal parent, exhausted existing ceilings, lost ownership, dead/unknown provider, stale generation, symlink, oversize и malformed artifacts дают отдельную typed attention reason; budgets nudges/restarts/provider calls не увеличены."},{"evidence_id":"E6-unattended-dogfood","observable":"Один изолированный end-to-end dogfood с offline coordinator проходит намеренно пропущенный reviewer submit, ровно один same-session recovery, accepted receipt и следующую pipeline stage без ручного current/resume/send/callback write и без повторного review."},{"evidence_id":"E7-wikilink-self-heal","observable":"Stop fixture с уникальным frontmatter title или H1 атомарно канонизируется через optimistic vault-write и проходит повторную strict validation; ambiguous, missing, malformed, embed-unsupported и concurrently changed cases не мутируются."},{"evidence_id":"E8-durable-decisions","observable":"Каждая escalation/decision получает append-only identity-bound record, latest marker содержит только authoritative chain pointer, legacy full marker читается только для deterministic backfill, а amendment-record workflow связывает frozen plan/Outcome digests с coordinator decision без потери прежних решений."},{"evidence_id":"E9-reviewer-tools","observable":"Review ContextPacket/prompt предоставляет exact base/head OID и готовые bounded review-inspect invocations; symbolic/non-OID ref по-прежнему отклоняется, а точный task_escalation raise argv проходит task DCG только через узкий anchored allow contract, тогда как shell composition и destructive variants блокируются."},{"evidence_id":"E10-defect-ledger","observable":"Все D-264 entries имеют reproducer/evidence, owner, release disposition included/already-shipped/deferred/not-a-defect и regression/evidence pointer; новая escalation не перезаписывает историю, а release review доказывает отсутствие потерянных unresolved entries."},{"evidence_id":"E11-no-regression","observable":"Focused unit/transition tests, full harness coverage, make test, vault validation, Codex/MCP sync, permission/provider snapshots and release acceptance green on one exact candidate HEAD; no public DSL, model routing or permission-budget drift."},{"evidence_id":"E12-plan-review-lifecycle","observable":"Code-owned plan-review facade строит purpose=intent boundary из валидированного плана; invalid или ambiguous invocation завершается до RuntimeSessionManager.start; Outcome, dispositions и evidence-map остаются fail-closed, а разрешённая design-only правка связывается reviewed/resolved plan digests и exact Git delta и продолжается в retained lanes без новой reviewer session."},{"evidence_id":"E13-superseded-review-cleanup","observable":"После durable callback/finding/resolution receipts terminal или identity-bound superseded review boundary автоматически закрывает только exact-owned provider process/surface и reconcile доводит parent до resource-free terminal state; active/current boundary и unknown ownership никогда не закрываются; cleanup failure становится typed attention, повтор идемпотентен и не удаляет evidence bytes."},{"evidence_id":"E14-harness-control-plane","observable":"Deterministic lifecycle trace доказывает, что после запуска утверждённого PipelineSpec Harness сам ведёт lifecycle: plan steps, loops, review, verification, bounded fix/retry, checkpoint, callback и terminal cleanup; LLM публикует только валидированные typed artifacts и не выбирает, не повторяет и не исполняет lifecycle effects; terminal prose не меняет state, а нормальный unattended path не требует ручного current/resume. Workstream integration join остаётся coordinator-owned и не объявляется PipelineSpec stage."}],"non_goals":["Парсить terminal prose или синтезировать verdict/callback без typed reviewer artifact и штатного validator.","Добавлять scheduler, второй lifecycle owner, новую публичную FSM или model-owned watchdog.","Делать периодические model calls во время видимой активности или увеличивать nudge/restart/review/verification/provider budgets.","Обходить dontAsk, sandbox, exact-generation ownership, callback validation или разрешать произвольный Bash через DCG.","Создавать отсутствующие Wiki-страницы, применять fuzzy matching, угадывать неоднозначную ссылку или писать vault в обход vault-write.","Переделывать PipelineSpec DSL, Project Spaces/Task Orchestration 2.7, model routing или review topology.","Повторно реализовывать D-264-06/D-264-08, уже поставленные в 2.6.3, либо превращать одноразовую ошибку ad-hoc reviewer Python probe в product fix без воспроизводимого contract seam.","Push, publish, tag, release или удаление safety branches/worktrees в рамках реализации плана."]}
```

## 1. Контекст и подтверждённый дефект

В 2.6.3 Opus закончил verification review, напечатал содержательный результат и вернулся к интерактивному `❯`, но не создал `.review-input.json` и не запустил `review_submit.py`. Exact callback target ожидал generation 3, поэтому callback broker не получил ни input, ни callback, ни receipt. Parent закономерно перешёл в `callback-timeout`, а unattended pipeline остановился до возвращения пользователя.

Это не ошибка callback broker: предыдущая generation той же lane и соседняя engineering lane успешно прошли тем же transport. Пробел находится между provider-visible окончанием работы и обязательным typed submit.

Связанные решения уже существуют:

- [[LLM Obsidian 2.6 dogfood RT1 callback watchdog architecture]] описывает passive liveness и bounded nudge/restart ownership;
- [[2026-07-30-224926-llm-obsidian-2-5-model-authored-custom-pipelines|LLM Obsidian 2.5 — Model-Authored Custom Pipelines]] требует, чтобы transport не зависел только от того, вспомнила ли модель выполнить submit;
- 2.6.3 исправляет accepted-callback cleanup, но не распознаёт `idle prompt + current generation without submit`.

## 2. Граница patch-релиза

2.6.4 добавляет два product recovery capability: для harness-owned reviewer callbacks и для однозначно исправимой адресации wikilink в Stop hook. Их безопасное unattended завершение требует supporting harness work, уже зафиксированного в ledger: append-only coordinator decisions, safe plan-review facade, exact superseded-review cleanup и проверяемый Harness/LLM control-plane invariant. Callback recovery использует существующие `OperationStore`, `RuntimeSessionManager`, `LivenessController`, callback target/receipt и cmux exact ownership. Vault recovery использует существующие schema/index/validator и единственный разрешённый writer. Новый scheduler, controller, state store или public DSL не создаются.

Сначала capability включается только для `reviewer-callback` и его verification continuation. Общий detector оформляется без привязки к Opus/Fable, но расширение на research, task-summary и произвольные pipeline result modes требует отдельного evidence и не входит в 2.6.4.

## 3. Целевая модель состояния

### 3.1 Durable evidence

Observer принимает решение только из bounded набора:

- exact owner/operation/lane/run и callback generation;
- callback target identity/digest и текущий parent/round revision/state;
- regular non-symlink input/callback/receipt, bounded size и stable SHA-256;
- exact provider/supervisor/surface/process ownership;
- content-free screen class `active`, `idle-prompt`, `permission`, `unknown`, `missing`;
- monotonic observation time, callback deadline, remaining seconds и elapsed seconds от последнего exact callback-generation progress;
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
4. Submit recovery — специализация существующего generic liveness nudge, а не второй nudge. Для reviewer callback modes он потребляет тот же persisted `LivenessState.nudge_count` и заменяет generic message generation-aware submit-only message. Per generation/provider session возможен максимум один provider-facing liveness prompt суммарно. Production branch запрещён раньше существующего `LivenessPolicy.nudge_after_seconds` (900 секунд, то есть AGENTS.md minimum 15 minutes), измеренного от последнего exact callback-generation progress, а не от screen/prompt churn; test-only injected policy не меняет production floor.
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

### 6.0 Ownership invariant: Harness управляет lifecycle, LLM — содержанием

Harness является единственным owner открытия/закрытия provider processes и cmux surfaces, сборки и передачи ContextPacket, checkpoint/callback identities, content-free progress observation, timeout/nudge/restart policy, supersession и cleanup. После запуска утверждённого PipelineSpec он также единолично продвигает plan steps, loops, review, verification и bounded fix/retry; checkpoint, callback, supersession и terminal cleanup остаются code-owned lifecycle effects. Workstream integration join выполняет coordinator поверх отдельных pipeline runs и не является PipelineSpec stage. LLM отвечает только за reasoning и генерацию содержательного результата внутри переданных Outcome/Context contracts и завершает шаг валидированным typed artifact: implementer result, review verdict, verification result или escalation request. LLM не выбирает lifecycle effect, окно или session continuation; Harness не выводит состояние из terminal prose и не синтезирует смысловой результат. Нормальный unattended path не требует ручного `current/resume`; такие команды остаются диагностическим/recovery ingress. Любой transition test обязан отдельно доказывать эту границу: lifecycle effects приходят только из code-owned policy, model output влияет только через валидированный typed artifact. Семантическая декомпозиция большой цели на отдельные планы остаётся scope 2.7 и не добавляет scheduler в 2.6.4.

### 6.1 Callback recovery

| Модуль | Ответственность | Интерфейс | Test seam |
|---|---|---|---|
| `scripts/harness/callback_submit_recovery.py` (новый) | Pure generation/deadline classification и deterministic action identity | immutable evidence/state/decision + `classify_callback_submit(...)` | `tests/harness/test_callback_submit_recovery.py` |
| `scripts/harness/runtime_callback_io.py` | Stable bounded typed-artifact reads/validation | существующие target/read/submit helpers | review vertical fixtures |
| `scripts/harness/runtime_worker_liveness.py` | Evidence assembly и исполнение одного уже budgeted action | `inspect_liveness()` + injected test clock/policy seam; production defaults unchanged | `test_runtime_sessions.py`, новый full-runtime test |
| `scripts/harness/runtime_worker_review_bridge.py` | Review-specific reconcile/accepted-child publication и identity-bound supersession receipt | existing gate/broker seams | `test_review_vertical.py`, `test_review_resolution_bundle.py` |
| `scripts/harness/runtime_session_cleanup.py` | Exact-owned close/reconcile после durable supersession; active/unknown fail closed | existing close/reconcile effects, no manual cmux/process path | `test_runtime_sessions.py`, transition matrix |
| `scripts/harness/liveness.py` | Единственный owner generic nudge/restart counters | existing policy/state/controller | existing `test_liveness.py` + shared-ceiling cases |

Callback submit effect не имеет отдельного model-call counter. Он резервирует/читает тот же `nudge_count`; generic and submit-specific branches mutually exclusive for one generation.

### 6.2 Decision history и amendment records

Contract amendment authority: [[LLM Obsidian 2.6.4 — amendment plan-review outcome]], [[LLM Obsidian 2.6.4 — amendment superseded review cleanup]] и [[LLM Obsidian 2.6.4 — amendment Harness control plane]]. Они добавляют E12/E13/E14 и подтверждают literal pointer-only disposition E8.

| Модуль | Ответственность | Интерфейс | Test seam |
|---|---|---|---|
| `scripts/task_escalation_records.py` (новый) | Append-only chain validation и optimistic record writes | `append_raise`, `append_resolution`, `append_amendment`, `load_chain` | новый records unit suite |
| `scripts/task_escalation.py` | CLI/delivery | `raise`, `resolve`, `record-amendment` | task lifecycle suite |
| harness writers | Сначала append immutable record, затем атомарно заменить latest marker на pointer-only receipt | `runtime_worker_custom.py`, `runtime_worker_control.py` | custom/fix decision fixtures |
| `.task-needs-attention.json` | Только authoritative `record_id/record_sha256` pointer и bounded routing identity | full reason/question/decision живут в immutable record | recovery/boundary/reap/close tests |

Writer inventory frozen before change: `task_escalation.py`, `runtime_worker_custom.py`, `runtime_worker_control.py`. Reader inventory включает `task_review_mechanism_recovery.py`, `task_review_boundary_authorization.py`, `task_contract.py`, `cmux_surface_exit.py`, `task_reap_lifecycle.py`. Все writers переходят на record-first/pointer-second; все readers атомарно мигрируют на chain lookup. Legacy full marker остаётся только read-compatible input и при первом resolve получает один deterministic backfill; новые writers никогда его не создают. Repeated writes одного record/pointer дают идентичные bytes.

### 6.3 Plan-review, reviewer OIDs и task DCG

Новый `task-review-runner.py plan` — единственный документированный фасад вычитки планов:

- всегда задаёт `purpose=intent`;
- validates exactly one Outcome Contract;
- deterministic compiler требует ровно по одному semantic exact heading anchor `## Outcome Contract`, `## Capability Dispositions and Defect Ledger` и `## Success Evidence Map`; Outcome берётся из единственного fenced JSON, dispositions и evidence-map — из соответствующих non-overlapping sections, а design artifact — из остальных plan bytes с тремя protected regions заменёнными их digests. Отсутствующий, duplicated или overlapping region даёт typed `plan-review-artifact-boundary-invalid` до provider start; plan без dispositions/evidence-map обязан передать explicit repository-relative artifact pointers, иначе тот же fail-closed result;
- для current single-parent plan commit выводит base=`HEAD^` только когда exact plan path изменён в этом commit; иначе требует explicit exact `--base` и fail closed;
- dispatched lifecycle берёт base из trusted `initial_head_sha`;
- записывает exact base/head OIDs в ContextPacket и literal `review-inspect status/log/diff/commit` commands;
- invalid/ambiguous boundary завершается до `RuntimeSessionManager.start`, что доказывается zero provider sessions.
- plan subject и control boundary разделены: same-session resolution может rebind только design artifact вместе с reviewed/resolved plan digests и exact Git delta; Outcome, `Capability Dispositions and Defect Ledger` и `Success Evidence Map` остаются frozen и любое их изменение fail closed. Их изменение разрешается только отдельным amendment record и fresh boundary; design-only resolution продолжается в retained lanes без нового provider/session.

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

- Workstream A: Slices 1–5 callback continuity, затем Slice 5b harness control-plane gap closure;
- Workstream B: Slice 6 decision/amendment history;
- Workstream C: Slice 7 plan-review/OID/DCG;
- Workstream D: Slice 8 Wiki self-heal.

Shared-file rules:

- `tests/harness/test_review_gate.py` и `tests/harness/test_runtime_task_summary.py` принадлежат только Workstream A/Slice 4; Workstream B проверяет marker payload в своей новой records fixture;
- Workstream C создаёт отдельный `tests/harness/test_plan_review_facade.py` и не редактирует `test_review_gate.py`;
- Workstream B эксклюзивно владеет `runtime_worker_custom.py` и `runtime_worker_control.py` в своей ветке;
- Makefile/audit manifest/release matrix/release docs принадлежат join Slices 9–10, не feature branches;
- join сравнивает expected case counts/registered suites каждой ветки и затем запускает полный harness;
- после объединения A–D и первичного Slice 9, но до Slice 10, coordinator выполняет только join-owned Slice 9a для D-264-16; четыре завершённых feature-потока не переоткрываются и их verification budgets не повторяются.

## 8. TDD-срезы

### Slice 0 — frozen v2.6.3 RED и triage

- **files/responsibility:** новый `tests/harness/test_callback_submit_recovery_runtime.py` + fixture files — deterministic real-runtime/fake-provider incident; `docs/acceptance/v2.6.4-baseline.md` — failed receipt, exact base и D-264 dispositions; `docs/acceptance/v2.6.4-harness-control-plane-baseline.md` + JSON — stage-by-stage authority trace.
- **consumes:** clean `v2.6.3`, existing fake-provider process seam, retained incident identity, OperationStore/current-review receipts for the four manual-current incidents.
- **produces:** runnable callback RED plus an E14 authority matrix for plan steps, loops, review, verification, bounded fix/retry, checkpoint, callback and terminal cleanup; every row records current owner, exact module/test, whether manual current/resume is required, and a durable receipt/command digest.
- **failing evidence:** preserved command/exit/output digest on v2.6.3; active reviewer negative control.
- **minimal green:** none in this slice; RED commit/receipt is the deliverable, Slices 1–4 own callback GREEN, а Slice 5b — только подтверждённые E14 gaps. Production code remains unchanged.
- **refactor seam:** none.
- **focused verification:** run fixture in preserved v2.6.3 worktree without wall-clock sleeps above injected poll seam; validate every E14 stage row has evidence and a disposition; E1, E10, E14 baseline.

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
- **failing evidence:** full-runtime RED goes green; generic-first→submit refused and submit-first→generic refused; deadline insufficient→zero send; все условия recovery true, но elapsed generation-idle меньше `nudge_after_seconds` → zero send.
- **minimal green:** route reviewer callback nudge through specialization while consuming same counter; no second budget.
- **refactor seam:** deterministic message renderer; no provider abstraction.
- **focused verification:** first in-process worker-liveness test, no sleeps beyond poll seam; E1, E3–E5, E11.

### Slice 4 — accepted-child publication и timeout rearm

- **files/responsibility:** `runtime_worker_review_bridge.py`, `runtime_session_cleanup.py`, `task_review_verification.py`, `task_review_resolution_bundle.py`, `test_review_gate.py`, `test_runtime_task_summary.py`, `test_runtime_sessions.py`, `test_review_resolution_bundle.py`.
- **consumes:** accepted resource-free child, exact parent/gate, existing `rearm_callback_timeout`, accepted-round preconditions, and a durable supersession receipt binding old owner/operation/store fingerprint to the new authorized boundary.
- **produces:** one publication/continuation; accepted-after-timeout ingestion-only rearm once; after all old evidence is durable, exact close/reconcile of superseded parents to resource-free terminal state; typed insufficient/exhausted/cleanup attention.
- **failing evidence:** D-264-02, D-264-13, callback accepted after parent timeout, deadline expires between reservation/callback, active current boundary not closed, unknown ownership not closed, cleanup retry duplicates no effect.
- **minimal green:** no fresh provider/lane/generation and no configured budget change.
- **refactor seam:** reuse exact-chain validation with one owner.
- **focused verification:** double replay identical bytes; superseded close/reconcile idempotent and evidence-preserving; E3–E5, E13.

### Slice 5 — callback transition matrix

- **files/responsibility:** `test_contract_state_edge_matrix.py`, callback fixtures, no audit manifest edits yet.
- **consumes:** Slices 1–4.
- **produces:** before/between/after-send races, both generic/submit orders, deadline expiry, accepted-after-timeout, stale/terminal/ownership/concurrency cases; superseded/current/unknown-ownership cleanup cases.
- **failing evidence:** every case begins red against preserved base for intended state/effect mismatch.
- **minimal green:** only gaps revealed by table.
- **refactor seam:** fixture consolidation without losing independent expectations.
- **focused verification:** oracle and callback suites plus model-output/lifecycle-effect separation; E2, E4, E5, E11, E13, E14.

### Slice 5b — Harness control-plane gap closure

- **files/responsibility:** new `tests/harness/test_harness_control_plane.py`; only baseline-red owners among `runtime_worker_loop.py`, `runtime_worker_spec.py`, `runtime_worker_review_bridge.py`, `runtime_worker_verification.py`, `runtime_worker_fix.py`, `runtime_session_checkpoint.py`, `runtime_callback_io.py`, `runtime_session_cleanup.py`; no PipelineSpec schema/DSL change.
- **consumes:** Slice 0 stage matrix and green Slices 1–5.
- **produces:** each baseline-red stage advances only from code-owned policy plus validated typed artifact; manual `current/resume` is recovery ingress, never normal progression; model prose alone produces zero transition. Already-green stages receive regression tests only.
- **failing evidence:** one independent RED per baseline gap; all already-green stages have prose-only and duplicate-effect negative controls.
- **minimal green:** modify only modules named by red stage rows; if a gap requires scheduler/DSL/public FSM change, stop for amendment instead of implementing it here.
- **refactor seam:** shared typed-artifact-to-transition adapter only when two real stage owners duplicate identical validation.
- **focused verification:** stage matrix and full control-plane suite; E11, E14.

### Slice 6 — append-only coordinator authority

- **files/responsibility:** new `task_escalation_records.py`, `task_escalation.py`, `runtime_worker_custom.py`, `runtime_worker_control.py`, new Workstream-B-owned records/marker fixture, `test_task_lifecycle.py`, `test_task_review_mechanism_recovery.py`, focused custom/fix marker tests and docs; it does not edit `test_runtime_task_summary.py`.
- **consumes:** all three marker writers, full legacy marker as read-only migration input, all identified readers, plan/Outcome digests.
- **produces:** pointer-only latest marker, immutable records, atomic reader/writer migration, deterministic legacy backfill, idempotent amendment workflow.
- **failing evidence:** new writer emits full marker; any of five readers cannot resolve pointer-only marker; overwrite two decisions; resolve legacy harness marker without prior record; repeated immutable write.
- **minimal green:** full payload lives only in immutable record; latest marker is pointer-only; migrate every writer and reader through one records helper while accepting legacy full markers for backfill.
- **refactor seam:** delivery stays CLI/worker-owned, records module owns chain.
- **focused verification:** tamper/stale/origin/duplicate/lost-wakeup plus custom/fix/recovery cases; E8, E10.

### Slice 7 — safe plan-review facade, exact OIDs и DCG

- **files/responsibility:** `task-review-runner.py`, `task_review_current.py`, new `task_review_plan.py`, `task_review_request.py`, `task_review_context.py`, `review-inspect.py` only if bounded metadata is necessary, `config/dcg/task.toml`, `dcg-test-suite.sh`, new `test_plan_review_facade.py` (12 cases), extend existing `test_review_inspect.py` (+2 cases) and `test_dcg_assets.sh` (+4 assertions), review skill/docs.
- **consumes:** plan bytes/Outcome Contract, exact current/dispatched lifecycle base source, existing runtime manager and strict OID validator.
- **produces:** `plan` subcommand with automatic four-artifact intent boundary; frozen Outcome/dispositions/evidence-map and mutable design subject with reviewed/resolved digests; literal exact commands; pre-provider typed rejection for ambiguous legacy invocation; anchored escalation allow.
- **failing evidence:** `current --plan` defaults to implementation and starts two providers; design-only resolution rejects `review program plan digest is stale`; Outcome/disposition/evidence-map delta is incorrectly accepted; absent/duplicated/overlapping protected regions or missing explicit artifacts are accepted; current review missing base; dispatched/current exact command snapshots; shell/destructive negative controls.
- **minimal green:** exact-heading compiler emits four independently hashed non-overlapping artifacts; absent/duplicated regions and plans without required sections/pointers fail before provider; only typed design delta may rebind in retained lanes; protected artifact changes require amendment/fresh boundary; common single-parent plan commit auto base, otherwise explicit exact base; no symbolic resolver/broad Bash.
- **refactor seam:** plan boundary compiler is deep module; current facade delegates once.
- **focused verification:** assert zero `RuntimeSessionManager.start` on invalid combinations; the 12 new facade cases include present, absent, duplicated and overlapping artifact discovery plus design-only rebind; unchanged protected artifacts plus exact design delta continue both retained lanes with zero new sessions; Outcome/disposition/evidence-map mutations fail closed; existing suites gain +2 inspect cases and +4 DCG assertions; E9, E11, E12.

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
- **produces:** every `tests/harness/test_*.py` suite, including pre-existing unregistered `test_contract_state_edge_matrix.py` and `test_task_session_store_io.py`, reachable from standing `make test`/`make test-harness`; join assertion compares the filesystem suite set to Makefile registration; every production module is in the honest coverage denominator.
- **failing evidence:** unregistered test/module or dropped shared assertion fails join.
- **minimal green:** registration/conflict correction only.
- **refactor seam:** none after join.
- **focused verification:** standing targets + coverage, including supersession cleanup and harness-only transition suites; E10, E11, E13, E14.

### Slice 9a — acknowledged same-session continuation delivery

Этот срез добавлен пользовательским решением сессии `019fab00-3160-7380-8920-4b20183afb76` после live dogfood четырёх подпланов. Он выполняется только на объединённом HEAD после Slice 9 и до финального Slice 10. После merge Workstream B join обязан записать amendment record, связывающий старый и новый plan digest; дочерние планы и их уже исчерпанные verification budgets не изменяются.

- **files/responsibility:** `scripts/harness/runtime_session_launch.py`, существующий provider/cmux acknowledgment seam, review continuation receipts и узкие runtime/review-gate regressions; без публичного DSL, новых provider calls или новой FSM.
- **consumes:** live D-264-16 trace: owner `8f36c040-d134-4987-884c-375b29d27340`, retained reviewer surface `826FDA9D-53A5-4327-8403-249EC18478F6`, verification child `8f36c040-d134-4987-884c-375b29d27340-holistic-23af2b2d-round-dcc2a687`; `continue-84dda8bcec3cd21b8b8cfc779821eafe` записан как `succeeded`, хотя verification prompt остался в input editor, generation не началась и callback отсутствует.
- **produces:** transport acceptance и provider-generation acceptance становятся разными доказательствами. Успех `cmux send` плюс немедленный `send-key Enter` не завершает continuation effect сам по себе. Code-owned bounded observation подтверждает callback/typed artifact либо content-free переход exact retained session из input-ready в generation/activity; prompt bytes и screen content не персистятся.
- **failing evidence:** fake cmux принимает оба RPC с exit 0, но применяет Enter раньше завершения paste или оставляет prompt в editor; прежний код фиксирует `succeeded`, parent остаётся `running`, resource-free child — `awaiting-callback`. Callback-before-ack, active generation, stale checkpoint, lost ownership и concurrent reconcile также входят в RED-матрицу.
- **minimal green:** write-ahead continuation identity сохраняется, но durable success появляется только после semantic acknowledgment. Если exact prompt остаётся в input-ready без activity, разрешён не более чем один identity-bound повтор submit-key без повторной вставки prompt и без нового model/provider call; он использует существующий общий liveness budget. Callback/typed artifact выигрывает race. Неоднозначность или отсутствие acknowledgment дают `continuation-submit-unconfirmed` attention, а не ложный success.
- **refactor seam:** correctness не зависит от фиксированного sleep; transport adapter возвращает только transport receipt, а lifecycle owner классифицирует acknowledgment через малый provider-stable interface.
- **focused verification:** deterministic ordering tests (`paste pending → Enter`, `Enter accepted → no generation`, callback race), exact retained-session integration fixture и replay D-264-16; доказать zero duplicate prompt/provider effect, bounded one-key retry, resource ownership и автоматическое продолжение verification; E3, E4, E6, E11, E14.

### Slice 10 — dogfood, docs и RC

- **files/responsibility:** observability/runtime docs, `v2.6.4-release-readiness.md`, release notes/changelogs/version manifests.
- **consumes:** one clean integrated HEAD after Slice 9a and all receipts.
- **produces:** offline fake-provider missing-submit dogfood, normal live Opus review with zero false nudge, exact D-264 map.
- **failing evidence:** manual lifecycle command, deadline/budget drift, repeat effect, missing disposition or stale evidence blocks RC.
- **minimal green:** evidence/docs/metadata only; post-review product fix creates new HEAD.
- **refactor seam:** none; release approval-or-stop.
- **focused verification:** E6 dogfood + full gate + Opus Deep implementation + release review; trace proves no manual current/resume and no model-owned lifecycle effect; E6, E10, E11, E14.

## 9. Полная verification ladder

```bash
python3 tests/harness/test_callback_submit_recovery.py
python3 tests/harness/test_callback_submit_recovery_runtime.py
python3 tests/harness/test_liveness.py
python3 tests/harness/test_contract_state_edge_matrix.py
python3 tests/harness/test_task_session_store_io.py
python3 tests/harness/test_harness_control_plane.py
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

1. Plan before Slice 7 exists: `task-review-runner.py current --deep --runtime claude --model opus --effort xhigh --purpose intent --boundary-input <exact-json> --plan <exact-plan>`; amendments use a fresh boundary when protected digests change. After Slice 7: `task-review-runner.py plan --deep --runtime claude --model opus --effort xhigh`; design-only findings continue the same two parents.
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

## Capability Dispositions and Defect Ledger


| ID | Defect | Owner | Disposition | Reproducer / evidence |
|---|---|---|---|---|
| D-264-01 | Reviewer idle without typed submit | callback recovery | `included` | E1 / Slice 0 full-runtime fixture |
| D-264-02 | Accepted resource-free child not published | review bridge | `included` | E4 / Slice 4 accepted-child fixture |
| D-264-03 | Latest attention marker overwrites decisions | escalation records | `included` | E8 / Slice 6 chain test |
| D-264-04 | Unique title/H1 link blocks Stop | vault/Stop | `included` | E7 / Slice 8 Stop fixture |
| D-264-05 | No frozen-plan amendment workflow | escalation records | `included` | [[LLM Obsidian 2.6.4 — amendment plan-review outcome]] |
| D-264-06 | Quiescent recovery rejected | review recovery | `already-shipped` | commit `68a13ef` |
| D-264-07 | Exact escalation CLI blocked by task DCG | permission policy | `included` | E9 whole-command matrix; red matrix requires amendment and blocks RC |
| D-264-08 | Legacy spec rehydrate mismatch | review recovery | `already-shipped` | commit `f3212cd` |
| D-264-09 | Reviewer improvises symbolic ref because exact base absent | review boundary | `included` | E9 / Slice 7 exact-OID fixture |
| D-264-10 | Inline reviewer Python assumes tuple is dict | ad-hoc probe | `not-a-defect` | two ad-hoc probe errors; no repository contract reproducer |
| D-264-11 | Plan review defaulted to implementation and launched two expensive Opus sessions | plan-review facade | `included` | task `fd726638-2f84-4dd5-b9ff-418fa0c99d1b`; Slice 7 zero-provider fixture |
| D-264-12 | Same-session plan resolution rejects corrected plan because control and subject share one digest | plan-review facade | `included` | task `15388886-a4e5-49f8-be9b-e964a0220c58`; stale-plan rejection |
| D-264-13 | Superseded boundaries retained five unusable provider surfaces | review lifecycle cleanup | `included` | [[LLM Obsidian 2.6.4 — amendment superseded review cleanup]] / E13 |
| D-264-14 | Failure paths required manual current/resume despite Harness control-plane vision | harness control plane | `included` | tasks `15388886-a4e5-49f8-be9b-e964a0220c58`, `fb1e4842-17d9-40a8-9a9e-a0bf6e0fdebc`, `7cbf9c6d-375a-4b70-b672-408a0df7e8bb`, `43e4919e-9c0e-4705-afa1-8641923bba93`; Slice 0 stage baseline |
| D-264-15 | Resolver display title was interpreted as exact context filename identity | dispatch context boundary | `included` | four pre-effect dispatch validation failures; Subplan C exact `context_path` round-trip |
| D-264-16 | Same-session continuation transport returned success while verification prompt remained unsubmitted in the input editor | review continuation delivery | `included` | owner `8f36c040-d134-4987-884c-375b29d27340`, surface `826FDA9D-53A5-4327-8403-249EC18478F6`, child `dcc2a687`, effect `continue-84dda8bcec3cd21b8b8cfc779821eafe`; Slice 9a |

New D-264 entry is recorded before task continuation. Disposition is exactly one of `included`, `already-shipped`, `deferred`, `not-a-defect`; qualifiers live only in the evidence column. Changes require Slice 6 amendment authority and never grant unrelated fix scope.

## Success Evidence Map

| Evidence | Exact slices |
|---|---|
| E1 | 0, 3 |
| E2 | 1, 5 |
| E3 | 2, 3, 4, 9a |
| E4 | 2, 3, 4, 5, 9a |
| E5 | 1, 2, 3, 4, 5 |
| E6 | 9a, 10 |
| E7 | 8 |
| E8 | 6 |
| E9 | 7 |
| E10 | 0, 6, 9, 10 |
| E11 | 3, 5, 5b, 7, 8, 9, 9a, 10 |
| E12 | 7 |
| E13 | 4, 5, 9 |
| E14 | 0, 5, 5b, 9, 9a, 10 |

E12/E13/E14 и re-freeze authority закреплены в [[LLM Obsidian 2.6.4 — amendment plan-review outcome]], [[LLM Obsidian 2.6.4 — amendment superseded review cleanup]] и [[LLM Obsidian 2.6.4 — amendment Harness control plane]]. Table is the authority consumed by join/release evidence. Every slice-level focused verification matches it exactly.

## Завершение

2.6.4 becomes RC only when E1–E14 are established on one exact HEAD; all D-264 dispositions are durable; one shared nudge budget, callback deadline and accepted-timeout rearm are proven; same-session continuation success is bound to semantic provider acknowledgment rather than transport exit alone; offline dogfood advances without manual action; normal review has zero extra effects; plan-review mistakes fail before provider launch; Wiki repair rebuilds derived state through the sole writer; and provider/permission/model budgets match v2.6.3. Push, tag and GitHub release remain separate explicit user actions after terminal approval.


## Append-only release amendment D-264-17 through D-264-34

This coordinator-owned amendment preserves the frozen Outcome Contract while recording defects discovered by later exact-HEAD dogfood and review. It is authoritative together with the original Capability Dispositions table.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-17 | harness | stale provider activity before Enter | `included` | `test_continuation_delivery.py`: current input then Enter then changed activity |
| D-264-18 | harness | crash after callback-submit reservation | `included` | runtime kill-point tests after paste and Enter |
| D-264-19 | plan review | dirty protected bytes at unchanged HEAD | `included` | `test_plan_review_facade.py` exact Git-byte guards |
| D-264-20 | permission policy | shell composition after exact escalation argv | `included` | task DCG whole-command matrix and E9 receipt |
| D-264-21 | coordinator | user-requested 2.7 planning page in 2.6.4 delta | `included` | accepted scope deviation in `docs/acceptance/v2.6.4-accepted-deviations.json`; no 2.7 runtime/DSL change |
| D-264-22 | review gate | verification evidence reread from resolved HEAD | `included` | exact reviewed-Git evidence rebind plus resolved delta binding |
| D-264-23 | callback watchdog | accepted callback misclassified stale | `included` | accepted current-generation receipt is a zero-effect terminal recovery signal |
| D-264-24 | continuation transport | stale same-heading editor before initial Enter | `included` | durable pre-send screen/editor baseline and ordering matrix |
| D-264-25 | review gate | resolution parent resumed to `running` | `included` | running rearm requires durable non-empty awaiting-resolution evidence |
| D-264-26 | continuation transport | transport replay without current-paste identity | `included` | replay requires pre-send screen/editor digests; missing or unchanged baseline has zero Enter |
| D-264-27 | callback watchdog | broker duplicate receipt ignored after crash | `included` | duplicate receipt binds generation, operation, run, callback and payload digest |
| D-264-28 | release evidence | annotated tag object used as commit evidence | `included` | fixture and dogfood bind dereferenced commit `99c4658562e868c9659c6722631f21d1228fa37a` |
| D-264-29 | release evidence | E7/E9 integration outputs not durable | `included` | `docs/acceptance/v2.6.4-integration-command-evidence.json` |
| D-264-30 | release evidence | late defect rows lacked owners | `included` | this append-only table plus readiness mirror |
| D-264-31 | coordinator | completed 2.6.3 vault history appears in the cumulative release delta | `included` | exact retained and derived paths are accepted in `docs/acceptance/v2.6.4-accepted-deviations.json`; no 2.6.4 runtime behavior |
| D-264-32 | callback watchdog | accepted receipt did not validate run, callback and payload identity | `included` | accepted and duplicate receipts now share the full fail-closed identity predicate |
| D-264-33 | continuation transport | crash after prompt paste but before transport receipt could paste twice | `included` | durable `paste-reserved` write-ahead state plus kill-point replay test proves zero second paste |
| D-264-34 | test harness | orphan verification recovery responder expired under full-suite load | `included` | bounded 10-second packet wait, explicit thread join and typed timeout assertion remove the 2-second cleanup race |
| D-264-35 | callback wake | crash or exception after coordinator wake paste/Enter replayed a second provider-facing wake | `included` | write-ahead `paste-reserved`/`transport-accepted`/`submit-accepted`, fail-closed uncertainty, exact-operation file lock, kill-point and concurrent-reconcile tests |
| D-264-36 | continuation transport | missing screen after the first Enter fell through to the retry budget and sent a second Enter | `included` | `test_continuation_delivery.py` first/later missing-screen cases prove one Enter, zero retry reservation and typed unconfirmed attention |
| D-264-37 | release evidence | E6 fixture stopped at child `finalizing` and did not prove parent publication, terminal cleanup or the following pipeline boundary | `included` | tracked dogfood receipt now binds accepted callback, complete resource-free parent/child and actual `reap-ready` reconciliation |
| D-264-38 | release evidence | historical E14 baseline retained RED/manual-ingress rows without a final integrated authority trace | `included` | `v2.6.4-harness-control-plane-final.json` plus `test_harness_control_plane.py` bind the approved PipelineSpec, all lifecycle stages, dogfood receipt and zero model/prose authority |
| D-264-39 | test harness | runtime task-summary helpers were not synchronized with their worker boundaries, causing full-suite-only missing recovery/verification receipts | `included` | ready/done handshakes, bounded eventual reads and explicit thread joins make repeated standalone and full-suite runs deterministic |
| D-264-40 | callback watchdog | a sent recovery binding survived its accepted callback and forced the next legitimate retained-session generation into stale-generation attention | `included` | exact accepted predecessor receipt retires only the generation binding, preserves the shared nudge/restart budgets, and the runtime regression accepts N+1 with zero second prompt or Enter |
| D-264-41 | vault writer | deterministic wikilink self-heal planned a valid update for writer-owned `wiki/log.md`, but the sole writer rejected its own repair payload | `included` | writer accepts only the exact current `stop-hook-link-repair` payload derived by the canonical planner; forged or ordinary log/hot page updates remain rejected |


## Append-only final review amendment D-264-42 through D-264-45

This coordinator-owned amendment records the four changes-requested findings from the independent Sol implementation review of candidate `79d999889d6877235c48da1a0c3be8680bea3eab`. It preserves the frozen Outcome Contract and narrows the implementation to crash-safe exact-identity behavior plus honest production-owned E6/E14 evidence.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-42 | continuation transport | Enter was sent before its durable reservation, so a crash could replay the same generation submit | `included` | `submit-reserved` and `submit-retry-reserved` persist before Enter; reservation-crash replay sends zero second key and either observes exact generation activity or fails closed |
| D-264-43 | callback watchdog | accepted-generation retirement lacked the complete immutable generation identity and concurrent idempotence | `included` | receipt identity binds operation/run/lane/generation/target plus expected counterparts; concurrent exact retirement converges and mismatches fail closed |
| D-264-44 | callback watchdog | an equal attention marker returned before retrying a previously failed OperationStore transition | `included` | replay re-reads authoritative state and retries the exact attention transition; fail-once store regression proves eventual attention with zero provider effect |
| D-264-45 | release evidence | E6/E14 terminal tail used fixture-owned state transitions and hard-coded manual-effect counters | `included` | dogfood now runs production review acceptance, provider exit, exact surface cleanup and pipeline advancement; adapter-derived effects prove provider prompt/Enter/callback plus zero manual lifecycle effects |

Focused continuation, callback recovery, liveness, transition-matrix, dogfood and control-plane suites are green. `make test-harness` is green. The exact committed candidate still requires the full release ladder, a fresh Sol implementation review, and the separate purpose=release audit; no push, tag, publish or release is authorized by this amendment.


## Append-only final review amendment D-264-46

This coordinator-owned amendment records the crash-replay finding from the independent Sol implementation review of candidate `c7423247c57dc6657b78b67e22aab294e469f264`. It preserves the frozen Outcome Contract and closes only the exact mixed durable phase created when acceptance is persisted before liveness retirement.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-46 | callback watchdog | a crash after persisting the accepted callback-submit receipt but before clearing the sent liveness state made exact retirement replay reject its own identity and stranded the next generation | `included` | `test_liveness.py` injects the kill point, proves `accepted receipt + sent state`, and verifies exact replay clears only binding/status while preserving consumed shared budgets; identity mismatches remain fail-closed |

The corrected candidate must repeat the complete exact-HEAD gate ladder into a content-addressed external receipt, continue the same Sol intent and engineering parent sessions for bounded verification, and then pass the separate purpose=release audit. No new reviewer sessions, automatic fix iteration, push, tag, publish or release are authorized by this amendment.


## Append-only mechanism repair amendment D-264-47

This coordinator-owned amendment records a pre-provider mechanism failure encountered while resuming the bounded Sol verification on candidate `d47c4ae83bd1c47863e883612627e870e2a5afc6`. It preserves the frozen Outcome Contract and repairs only the extracted review-context seam.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-47 | review context | a resolution-bound plan larger than 65,536 bytes entered `_bounded_input()`, whose extracted module no longer imported the existing `_atomic_bytes` owner and raised `NameError` before ContextPacket completion | `included` | import the existing sole atomic writer from `task_review_shared`; `test_review_resolution_bundle.py` passes a 65,537-byte plan and verifies pointer bytes, size and SHA-256; focused resolution/gate suites remain green |

The failed resume occurred before ContextPacket completion and before any provider, prompt, callback or reviewer effect. After the corrected exact HEAD passes the complete release ladder, the same two Sol parent sessions may receive the bounded verification continuation; no new review lane, widened budget, push, tag, publish or release is authorized.


## Append-only unattended prompt amendment D-264-48

This coordinator-owned amendment records a live unattended stall observed in both retained Sol review parents. It preserves the frozen Outcome Contract and adds only an exact provider-dialog policy.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-48 | provider prompt policy | Codex displayed the native `Approaching rate limits` model-switch dialog in both unattended Sol lanes; the unknown-choice policy supplied no safe answer, so review waited indefinitely for an operator | `included` | recognize only the exact dialog while option 1 is selected; send one `down` plus `Enter` to choose `Keep current model`, retain future reminders, preserve the route-bound Sol model, and keep changed or unknown choices at zero input; `test_release_blocker_runtime.py` and `test_adapters.py` are green |

The repair does not change routing, model budgets, retry budgets, review topology, or unknown-prompt fail-closed behavior. Both already-owned review sessions were manually unblocked once without switching model; the final exact-HEAD ladder and same-session verification must use the tracked policy before release audit.


## Append-only contract-byte repair amendment D-264-49

This coordinator-owned amendment records a pre-provider exact-boundary rejection. It restores the frozen Outcome Contract to its originally reviewed canonical bytes and makes no semantic change.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-49 | release evidence | the D-264-47 documentation transaction replaced four UTF-8 characters in one frozen non-goal with replacement characters, changing the canonical Outcome digest and correctly blocking same-session verification before provider effect | `included` | restore exactly `применять`; canonical Outcome SHA-256 again equals the initially reviewed `af24873e06669632c5c45e9818a8646918e3a086a358285d1781a1a5540660ae`; vault validation and the final exact-HEAD ladder must remain green |

No review finding, disposition, runtime behavior, budget, route, permission, or public interface changes. The failed continuation produced zero provider, prompt, callback, or review effect.

## Append-only coverage-fixture repair amendment D-264-50

This coordinator-owned amendment records the only failure in the final exact-HEAD ladder. It preserves the frozen Outcome Contract and changes no production/runtime behavior.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-50 | test harness | under stdlib trace, the summary-only review fixture allowed two untracked responder threads only two seconds to observe exact decision/refresh artifacts; the second helper could return before the fifth launch call and made honest harness coverage fail nondeterministically | `included` | track both helper threads, use the existing bounded ten-second eventual window, join them before assertions, and require zero live helper threads; standalone `test_runtime_task_summary.py` and `make test-harness-coverage` are green at 75.98% across 128 modules with all critical floors and 4,370 transition cases |

The repair is test-only. It does not change provider behavior, pipeline budgets, lifecycle state, routing, review topology, public interfaces, or release authority. The final candidate must still repeat the complete exact-HEAD ladder and continue the same two Sol verification parents before the separate purpose=release audit.

## Append-only release-scope amendment D-264-51

This coordinator-owned amendment resolves `REL-SCOPE-001` from the exact-HEAD Sol release audit. It preserves the frozen Outcome Contract and records the user-requested 2.7 material as planning-only scope rather than deleting it.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-51 | release scope | exact diff inspection found three 2.7 TaskGraph/project-task entries in `wiki/backlog.md` that were not named by the D-264-21 accepted-deviation path | `included` | `docs/acceptance/v2.6.4-accepted-deviations.json` binds `wiki/backlog.md` plus `llm-obsidian-2-7-code-owned-task-graph`, `llm-obsidian-2-7-project-task-system`, and `llm-obsidian-2-7-project-scoped-task-namespaces`; the entries are planning-only and introduce no 2.7 runtime, scheduler, DSL, routing, or review-topology behavior |

No production code, pipeline contract, provider behavior, budget, permission, public interface, push, tag, publish, or release effect is changed. The corrected documentation-only candidate must pass the exact-HEAD gates and a fresh implementation/release evidence boundary before handoff.

## Append-only crash-replay amendment D-264-52

This coordinator-owned amendment resolves `REL-CRASH-001` from the exact-HEAD Sol release audit of candidate `48a315251ef9d7a51c385c67916bc433ca321910`. It preserves the frozen Outcome Contract and closes only the durable sent-state/reserved-receipt crash phase.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-52 | callback watchdog | a crash after persisting `callback_submit_status=sent` but before advancing the separate callback-submit receipt left `state=sent + receipt=reserved`; restart classified recovery as already sent without healing the receipt, and accepted retirement could strand the next retained generation | `included` | runtime restart idempotently replays only the exact sent-receipt write before classification, performs zero provider prompt/Enter, preserves consumed budgets, and emits typed `callback-submit-evidence-malformed` attention for a corrupt receipt; `test_liveness.py` injects the exact kill point and `test_callback_submit_recovery_runtime.py` proves healing plus malformed fail-closed behavior |

No provider effect, callback, model call, retry-budget expansion, route change, review-topology change, public-interface change, migration, push, tag, publish or release is introduced. The corrected candidate must repeat the complete exact-HEAD gate ladder and fresh Sol implementation/release reviews.


## Append-only final implementation-review amendment D-264-53 through D-264-54

This coordinator-owned amendment resolves the two important engineering findings from the independent Sol implementation review of candidate `dc2c2dcbd6c403971a51848740e85661f9a5a587`. It preserves the frozen Outcome Contract and changes only exact crash-replay identity plus record-publication durability.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-53 | continuation transport | the bounded continuation Enter retry reserved liveness with the continuation effect identifier rather than the current callback-target digest; worker polling therefore observed a foreign generation and could enter stale-generation attention | `included` | retry reservation now uses the same bounded callback-target SHA-256 and complete generation identity as worker liveness; an integrated crash after the retry Enter leaves an exact reserved binding, pending-effect replay confirms provider activity, advances it to `sent`, reaches `awaiting-callback`, and sends zero duplicate prompt or Enter |
| D-264-54 | escalation records | the immutable decision file was fsynced, but its records-directory entry was not made durable before the latest pointer could be published | `included` | first-use records-directory creation is fsynced in the worktree and every immutable record entry is fsynced in its parent before pointer replacement; the ordered regression proves both directory barriers precede publication |

Focused runtime-session, continuation-delivery, callback-recovery, liveness and escalation-record suites are green. The corrected candidate must repeat the complete exact-HEAD gate ladder, continue the same Sol implementation review for verification, and then pass the separate purpose=release audit. No new review lane, provider budget, retry budget, route, permission, public interface, migration, push, tag, publish or release is authorized.


## Append-only coverage closure amendment D-264-55

This coordinator-owned amendment records the final exact-HEAD coverage rejection after D-264-53 and D-264-54. It preserves the frozen Outcome Contract and changes no production/runtime behavior.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-55 | test harness | honest stdlib tracing measured `scripts.harness.liveness` at 85.8%, below the standing 86.0% critical floor, because the fail-closed transition from an observed liveness state to `mark_callback_submit_uncertain` without an exact reservation had no direct behavioral assertion | `included` | `test_liveness.py` now proves the unreserved uncertain transition is rejected without state mutation; `make test-harness-coverage` is green at 76.02% across 128 modules, liveness is 86.1%, and all 4,370 transition cases remain complete |

The repair is test-only. It does not lower a coverage floor or change provider behavior, callback effects, budgets, routing, review topology, public interfaces, migration, push, tag, publish or release authority. The new exact HEAD must repeat the complete release ladder before bounded implementation verification and the separate purpose=release audit.


## Append-only resolution-rebind repair amendment D-264-56

This coordinator-owned amendment records a pre-provider mechanism failure while continuing the retained Sol implementation review after D-264-53 through D-264-55. It preserves the frozen Outcome Contract and tightens the existing exact-HEAD resolution path.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-56 | review context | resolution correctly rebound verification evidence to the reviewed Git HEAD, but still compared the current amended plan bytes with the original boundary plan digest; any required append-only defect amendment therefore stopped before verification with `review program plan digest is stale` | `included` | during an exact reviewed-to-resolved rebind, Outcome and original plan identity are read from `git show <reviewed-head>:<plan-path>` and checked against the frozen plan and Outcome digests; the current amended plan remains visible as the resolved plan plus exact fix delta; ordinary fresh reviews still reject dirty/stale plan bytes |

The live failure occurred before ContextPacket completion and before provider, prompt, callback or review effect. The regression changes both plan and purpose evidence after the reviewed commit, proves the rebind sources both from the exact reviewed Git HEAD, and preserves the existing stale-plan rejection outside resolution. No budget, routing, topology, permission, public interface, migration, push, tag, publish or release authority changes.


## Append-only ignored-evidence rebind amendment D-264-57

This coordinator-owned amendment records the second pre-provider boundary exposed after D-264-56. It preserves the frozen Outcome Contract and the tracked-artifact exact-Git rule.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-57 | review context | the original implementation-gate receipt is intentionally ignored under `.vault-meta/release-evidence` and therefore has no blob at the reviewed Git HEAD; resolution attempted only `git show` and stopped before ContextPacket construction | `included` | tracked boundary artifacts still come from the exact reviewed Git object; only when no reviewed blob can be materialized may the existing path-confined regular file be used, and only when its bytes exactly match the frozen boundary SHA-256; missing, symlinked, foreign-path or changed bytes remain fail-closed |

The live failure produced no provider, prompt, callback, verification or reviewer effect. The integrated regression changes the current plan and tracked purpose evidence, supplies an ignored receipt with its frozen digest, and proves resolution materializes the original Git-backed inputs plus the exact ignored receipt without accepting substitutions. No budget, routing, topology, permission, public interface, migration, push, tag, publish or release authority changes.


## Append-only verification amendments D-264-58 and D-264-59

This coordinator-owned amendment records the two exact verification findings from the retained Sol Deep review of HEAD `296f9982c15ebce3f19719cdee047e511a86fc85`. The frozen Outcome Contract, non-goals, routing, topology and budgets remain unchanged.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-58 | continuation delivery | `ENG-264-continuation-stage-order`: a crash after publishing `submit-retried` but before marking the exact generation binding `sent` could leave a more advanced delivery receipt paired with a reserved liveness effect, which replay would not promote | `included` | the exact generation binding is durably marked `sent` before `submit-retried` publication; the new late kill-point regression proves a crash leaves `submit-retry-reserved`, replay promotes the same binding without duplicate prompt or Enter, and the next callback generation remains usable |
| D-264-59 | review context | `INT-D57-TRACKED-GIT-FALLBACK` / `ENG-264-reviewed-artifact-fallback`: the ignored-evidence fallback caught every reviewed-artifact error, so a tracked reviewed blob with a stale digest could be replaced by mutable current worktree bytes | `included` | reviewed-tree absence is now a distinct typed condition; only that condition permits the existing path-confined exact-digest fallback, while tracked Git resolution, path and digest failures remain fail-closed; the negative regression supplies matching current bytes beside a mismatched tracked blob and proves rejection before provider effect |

Both findings were discovered during bounded same-session verification and produced no product-write, provider-replay, callback-replay, routing, permission, push, tag, publish or release effect. The focused review-gate and runtime-session suites pass after the fixes; the exact-HEAD full gate ladder and bounded verification are repeated before the separate purpose=`release` no-loss audit.

## Append-only lane-barrier callback amendment D-264-60

This coordinator-owned amendment records the final retained-review lifecycle defect after D-264-58 and D-264-59. It preserves the frozen Outcome Contract and changes only exact callback collection at an already active Deep/Full lane barrier.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-60 | review gate | during verification iteration 2, `round_results` still pointed to iteration 1 for the same axis; axis-only filtering therefore hid the newly accepted callback and reconciliation re-entered against its `finalizing` child | `included` | ready callbacks are filtered only when the stored result has the same axis and exact `verification_iteration`; a deterministic unit regression proves iteration 1 does not hide ready iteration 2 and exact iteration 2 remains idempotently filtered |

The defect was observed after the provider had legitimately completed and the typed callback was durably accepted. The repair does not replay provider input, widen retry/model budgets, change routing or topology, weaken callback identity, add public interfaces, or authorize push, tag, publish or release. The corrected exact HEAD repeats the complete gate ladder and uses a fresh bounded Sol review boundary before the separate purpose=`release` audit.


## Append-only final release-audit amendments D-264-61 through D-264-63

This coordinator-owned amendment records the final purpose=`release` findings against candidate `28fdbdd01abc9bc22e58bab48748a3cf487dfa03`. It preserves the frozen Outcome Contract and closes one callback crash ordering plus two evidence-quality gaps.

| ID | Owner | Reproducer / finding | Release disposition | Regression / evidence |
|---|---|---|---|---|
| D-264-61 | callback watchdog | an accepted callback receipt could trigger the fast return before the exact `callback_submit_status=sent` plus submit-receipt `reserved` crash phase was reconciled, leaving the next generation stale | `included` | `inspect_liveness` reconciles the exact sent receipt before accepted-receipt classification; the runtime regression drives send, mixed crash, provider acceptance, worker restart and the next generation with zero duplicate provider effect |
| D-264-62 | release evidence | the claimed E6/E14 terminal trace created a later child and called lifecycle helpers directly from the fixture while its manual counters ignored those calls | `included` | the release-bound E6 trace now starts through `ReviewGateController`, performs one missing-submit recovery, re-enters the gate through the callback wake, reaches exact resource-free cleanup, and enters `RuntimeWorkerSummaryMixin.finish_task_summary` to publish `reap-ready`; the former direct-helper tail remains unit coverage only |
| D-264-63 | review gate tests | D-264-60 tested the private exact-iteration predicate but not durable Deep collection and replay wiring | `included` | the public current-review facade drives two Deep lanes from iteration 0 findings through exact resolution and iteration 1 approvals; both final results persist at iteration 1 and each retained continuation effect occurs once |

No routing, review topology, public DSL, provider/model budget, retry budget, permission boundary, migration, push, tag, publish or release authority changes. The new exact HEAD must repeat the complete release ladder, fresh Sol Deep implementation review and separate purpose=`release` audit.
