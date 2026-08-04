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

Observer принимает решение только из следующего bounded набора:

- exact owner/operation/lane/run;
- callback target `operation_id`, `run_id`, `generation`;
- текущий parent/round state и revision;
- наличие regular non-symlink `.review-input.json`, `.review-callback.json`, callback receipt и их bounded stable digest;
- exact provider/supervisor/surface ownership и process status;
- нормализованный content-free screen class: `active`, `idle-prompt`, `permission`, `unknown`, `missing`;
- два последовательных одинаковых idle observations и monotonic grace timestamp;
- durable per-generation recovery budget/receipt.

Ни prompt, ни review body, ни raw screen, ни callback payload не попадают в telemetry или decision record.

### 3.2 States

Минимальная внутренняя классификация:

1. `working` — есть activity/spinner/progress или callback generation ещё не ожидает submit;
2. `typed-input-ready` — exact input существует и стабилен, callback отсутствует;
3. `callback-ready` — exact callback существует, receipt отсутствует;
4. `idle-without-submit` — provider exact/live, экран дважды стабильно idle, current generation не имеет input/callback/receipt;
5. `recovery-sent` — submit-only nudge effect для generation принят write-ahead store;
6. `accepted` — exact callback receipt принят;
7. `attention` — ownership/evidence неизвестны либо bounded recovery исчерпан.

Это classification layer, а не новая публичная FSM. Основные operation transitions остаются существующими.

## 4. Recovery ladder

1. Если exact typed input уже существует и стабилен, harness вызывает существующий validator/submit seam code-owned способом без model call.
2. Если callback уже существует, harness принимает его через `CallbackBroker`; никакой nudge не отправляется.
3. Если current generation находится в `idle-without-submit` после двух probes и короткого grace, harness атомарно резервирует один effect вида `callback-submit-nudge:<generation>:<target-digest>`.
4. В ту же exact reviewer session отправляется фиксированный submit-only prompt: не повторять review, не менять verdict из памяти без повторной проверки, записать уже сформированный результат в точный input path и выполнить точный `review_submit.py` из сохранённого verification prompt.
5. После nudge observer ждёт typed artifact/receipt. Если callback принят, pipeline автоматически продолжает первую отсутствующую стадию.
6. Если session потеряна, используется только уже существующий identity-bound restart budget; restart воспроизводит exact текущий verification request, а не создаёт новую review lane или generation.
7. После одного nudge и разрешённого существующим budget restart отсутствие callback становится typed `callback-submit-missing` attention. Бесконечные retries запрещены.

## 5. Ownership и безопасность

- Решение о recovery принадлежит runtime worker/LivenessController, а не модели.
- Callback payload по-прежнему создаёт reviewer и проверяет `review_submit.py`; harness не выводит verdict из terminal prose.
- Nudge привязан к exact target generation и write-ahead effect receipt; смена generation делает его stale и запрещает отправку.
- Перед effect и сразу после atomic reservation повторно проверяются callback/input/receipt, чтобы закрыть race.
- `permission`, `unknown`, активный spinner или изменяющийся screen hash блокируют nudge.
- Нельзя отправлять prompt в surface без совпадения process/supervisor/surface identity и допустимого current state.
- Нельзя очищать или переиспользовать accepted callback, перезаписывать input либо вручную редактировать OperationStore.

## 6. Архитектурная карта и dependency direction

### 6.1 Callback recovery

| Модуль | Одна ответственность | Интерфейс | Зависимости | Test seam |
|---|---|---|---|---|
| `scripts/harness/callback_submit_recovery.py` (новый) | Чистая generation-aware классификация и детерминированный action/effect identity | immutable evidence/state/decision values + `classify_callback_submit(...)` | только bounded contract values; не знает cmux, filesystem и provider | новый `tests/harness/test_callback_submit_recovery.py` |
| `scripts/harness/runtime_callback_io.py` | Bounded stable read и validation typed input/callback/receipt | существующие callback target/read/submit helpers, расширенные без side-effect policy | filesystem + callback schema | callback IO unit cases и vertical fixture |
| `scripts/harness/runtime_worker_liveness.py` | Собрать evidence, вызвать pure policy и исполнить уже разрешённый effect | существующий `inspect_liveness()`; generic budgets из `liveness.py` не меняются | OperationStore, RuntimeSessionManager, cmux adapter, pure policy | `test_runtime_sessions.py`, `test_runtime_task_summary.py` |
| `scripts/harness/runtime_worker_review_bridge.py` | Review-specific reconcile и публикация уже принятого callback | существующие review bridge methods | callback IO + review gate | `test_review_vertical.py`, `test_review_gate.py` |

`liveness.py` остаётся владельцем общих one-nudge/one-restart ceilings. Новый модуль не создаёт вторую FSM: он скрывает только правила «можно ли применить существующую recovery ladder к exact callback generation». Runtime worker не интерпретирует screen body и не принимает policy-решений по строкам.

### 6.2 Decision history и amendment records

| Модуль | Одна ответственность | Интерфейс | Зависимости | Test seam |
|---|---|---|---|---|
| `scripts/task_escalation_records.py` (новый) | Append-only identity/digest validation и optimistic запись coordinator decisions | `append_raise`, `append_resolution`, `append_amendment`, `load_chain` | filesystem + task/plan identity values; без cmux | новый `tests/test_task_escalation_records.py` |
| `scripts/task_escalation.py` | CLI и доставка typed escalation/decision | текущие `raise`/`resolve` + узкий `record-amendment` | records module + cmux adapter | `tests/test_task_lifecycle.py` |
| `.task-needs-attention.json` | Только latest mutable pointer/status для совместимости | `record_id`, `record_sha256`, current status | append-only record chain | lifecycle matrix |

История не хранится в content-free telemetry и не подменяет OperationStore. Текст reason/decision остаётся owner-only task evidence; pipeline events получают только IDs/counters.

### 6.3 Reviewer inspection и task DCG

`task_review_request.py` владеет copy-paste reviewer instructions и включает exact base/head OID из уже доверенного ContextPacket. `review-inspect.py` сохраняет строгий lowercase-OID ingress; symbolic refs не разрешаются. `config/dcg/task.toml` получает только anchored allow для точной repo-owned escalation команды, если установленная версия DCG действительно поддерживает fail-closed allow semantics; иначе D-264-07 останавливается как security boundary, а не заменяется широким allow.

### 6.4 Wiki link repair

Новый `scripts/vault_link_repair.py` владеет pure discovery/render plan: exact filename, unique frontmatter title и unique H1. `stop-hook.py` лишь запускает одну bounded repair attempt после первого wikilink-only validation failure. Mutation выполняет только `vault-write.py` с expected SHA каждого source page; после неё Stop один раз повторяет полный validator.

### 6.5 Cohesion decisions

- Не добавлять callback policy в уже смешанный `runtime_worker_liveness.py`: новый pure module уменьшает temporal coupling и тестируется без процессов.
- Не расширять `vault_schema.py` mutation-логикой: schema остаётся read-only authority, repair planner зависит от неё.
- Не превращать `task_escalation_records.py` в общий event store: он обслуживает только durable coordinator authority chain.
- Файлы более ~500 строк трогаются через малые интерфейсы; новый unrelated growth в них запрещён. Экстракция допускается только если RED доказывает необходимый seam и сохраняет public identity.

## 7. Execution topology

После Slice 0 независимы и могут выполняться отдельными task worktrees:

- Workstream A: Slices 1–5, callback continuity;
- Workstream B: Slice 6, decision/amendment history;
- Workstream C: Slice 7, reviewer OID + DCG ergonomics;
- Workstream D: Slice 8, Wiki self-heal.

Они не меняют общие файлы, кроме заранее закреплённых интеграционных owners. Join выполняется только после зелёных focused gates; Slice 9 владеет общей матрицей/manifest, Slice 10 — dogfood и release evidence. Параллельные исполнители не редактируют release notes, readiness или общую transition matrix до join.

## 8. TDD-срезы

### Slice 0 — frozen baseline и defect triage

- **files/responsibility:** `docs/acceptance/v2.6.4-baseline.md` — зафиксировать exact `v2.6.3` base, наблюдаемые incidents и D-264 disposition; `config/harness-audit-manifest.json` — только если новый planned module должен войти в denominator.
- **consumes:** release `v2.6.3`, текущий план, retained callbacks/screenshots/escalation evidence без изменения runtime state.
- **produces:** таблица D-264 с owner и `included/already-shipped/not-a-defect/deferred`; frozen base SHA и команды RED.
- **failing evidence:** D-264-01, 02, 03, 04, 05, 07, 09 имеют воспроизводимый failing contract; D-264-06/08 отмечены already-shipped; tuple `.keys()` probe отмечен `not-a-defect`, пока нет repo command/API, обещающего dict.
- **minimal green:** только evidence document; production code не меняется.
- **refactor seam:** отсутствует.
- **focused verification:** `python3 scripts/validate-vault.py --summary`, `git diff --check`; покрывает E1, E8–E10 baseline.

### Slice 1 — pure callback-generation classifier

- **files/responsibility:** новый `scripts/harness/callback_submit_recovery.py`; новый `tests/harness/test_callback_submit_recovery.py`; manifest registration при необходимости.
- **consumes:** exact operation/run/lane/generation, current target digest, stable input/callback/receipt digests, process/surface ownership, content-free prompt class, generic liveness counters.
- **produces:** immutable states `working`, `typed-input-ready`, `callback-ready`, `idle-without-submit`, `recovery-reserved`, `accepted`, `attention` и deterministic action ID bound to generation/target digest.
- **failing evidence:** v2.6.3 classifies stable idle without typed artifact only as generic timeout; active/permission/unknown negative fixtures begin red against the new contract.
- **minimal green:** pure validation/classification; no IO, cmux, provider or OperationStore mutation.
- **refactor seam:** extract shared SHA/ID validation only if it removes duplicated knowledge without changing `harness.contracts` public values.
- **focused verification:** new unit file + mutation removing generation equality must fail; E1, E2, E5.

### Slice 2 — stable typed-artifact fast paths

- **files/responsibility:** `scripts/harness/runtime_callback_io.py` — stable bounded reads and exact target validation; `scripts/harness/runtime_worker_review_bridge.py` — invoke existing submit/broker seams; `tests/harness/test_review_vertical.py`.
- **consumes:** Slice 1 decisions `typed-input-ready`/`callback-ready`, existing review submit validator and callback broker.
- **produces:** input reconciled or callback accepted without model call; durable receipt linked to exact generation.
- **failing evidence:** input-without-callback and callback-without-receipt fixtures remain stuck before change; symlink/oversize/malformed controls fail closed.
- **minimal green:** reuse current validators/broker; no new callback schema and no synthetic verdict.
- **refactor seam:** centralize bounded stable-file read only if all existing callers retain exact errors.
- **focused verification:** `python3 tests/harness/test_review_vertical.py`; race callback-before-action; E3, E4, E5.

### Slice 3 — one-shot same-session submit nudge

- **files/responsibility:** `scripts/harness/runtime_worker_liveness.py` — evidence assembly/action execution; `scripts/harness/runtime_session_contracts.py` only if exact session identity needs a typed value; `tests/harness/test_runtime_sessions.py`.
- **consumes:** Slice 1 `idle-without-submit`, exact live provider/surface ownership, existing `LivenessController` max_nudges=1, callback target path and generation.
- **produces:** write-ahead `callback-submit-nudge:<generation>:<target-digest>` receipt followed by one fixed submit-only message to the same surface.
- **failing evidence:** stable idle fixture reaches timeout with zero submit-specific effect; concurrent callback arrival exposes race.
- **minimal green:** reserve once, re-read input/callback/receipt immediately before send, send one fixed prompt+Enter; do not change generic budgets.
- **refactor seam:** keep message rendering private and deterministic; do not create a provider abstraction.
- **focused verification:** runtime-session unit cases; mutation allowing second nudge must fail; E3–E5.

### Slice 4 — terminal policy и accepted-child publication (D-264-02)

- **files/responsibility:** `scripts/harness/runtime_worker_review_bridge.py`, `scripts/task_review_verification.py`, `scripts/task_review_resolution_bundle.py`; `tests/harness/test_runtime_task_summary.py`, `tests/harness/test_review_resolution_bundle.py`.
- **consumes:** exact resource-free child with accepted callback/receipt, retained parent/gate identity and Slice 2/3 effects.
- **produces:** idempotent publication into the existing gate and continuation from the first missing stage; distinct `callback-submit-missing` attention after existing ceilings.
- **failing evidence:** preserved D-264-02 fixture rejects an accepted resource-free child or requires replay; terminal/stale/mixed-identity controls remain rejected.
- **minimal green:** reconcile accepted evidence once; no fresh reviewer, surface, provider, callback or generation.
- **refactor seam:** share exact-chain validation with existing resolution bundle only when one owner remains explicit.
- **focused verification:** focused summary/bundle tests and replay twice with identical bytes; E3–E5.

### Slice 5 — callback transition matrix

- **files/responsibility:** `tests/harness/test_contract_state_edge_matrix.py`, new callback fixtures under `tests/harness/fixtures/`, and only the smallest audit-manifest update.
- **consumes:** Slices 1–4 public decisions/effects.
- **produces:** table-driven proof for callback before reservation, between reservation/send, after send, accepted replay, concurrent reconcile, stale generation, changed lane/run, active/changing screen, permission, unknown/missing surface, dead provider, exhausted restart, terminal parent and identity mismatch.
- **failing evidence:** each case is first added against preserved pre-fix base and must fail for the intended effect/state mismatch.
- **minimal green:** no production logic beyond gaps exposed by a case.
- **refactor seam:** merge fixtures only when independent expectations remain readable.
- **focused verification:** transition oracle plus `make test-harness-coverage`; E2–E5, E11.

### Slice 6 — append-only coordinator authority (D-264-03/05)

- **files/responsibility:** new `scripts/task_escalation_records.py`; `scripts/task_escalation.py`; new `tests/test_task_escalation_records.py`; `tests/test_task_lifecycle.py`; operator docs.
- **consumes:** current `.task-needs-attention.json`, task origin identity, plan/Outcome digests and existing raise/resolve CLI.
- **produces:** immutable per-record JSON chain in task-owned state, latest pointer marker, idempotent `record-amendment` command and chain verification.
- **failing evidence:** two escalations overwrite the first decision; frozen-plan amendment has no supported durable owner.
- **minimal green:** append record before notification, update latest pointer optimistically, append resolution/amendment without editing approved plan; retain reader compatibility for legacy full marker.
- **refactor seam:** CLI parsing/delivery stays in `task_escalation.py`; records module owns all chain validation/writes.
- **focused verification:** lost-wakeup, duplicate resolve, stale pointer, tampered chain, origin mismatch, legacy marker; E8, E10.

### Slice 7 — reviewer command affordances (D-264-07/09)

- **files/responsibility:** `scripts/task_review_request.py`, `scripts/task_review_context.py`, `scripts/review-inspect.py` only if an additional bounded metadata operation is proven necessary, `config/dcg/task.toml`, `scripts/dcg-test-suite.sh`, `tests/harness/test_review_inspect.py`, `tests/harness/test_review_gate.py`, `tests/test_dcg_assets.sh`.
- **consumes:** trusted ContextPacket head/base OIDs, existing lowercase-OID validator, exact repo-owned `task_escalation.py raise` argv.
- **produces:** prompt/packet with literal copy-paste commands using exact OIDs; narrow anchored DCG allow only for validated escalation CLI.
- **failing evidence:** reviewer prompt permits/elicits symbolic `--ref`; exact escalation raise is classified HIGH while shell-composed/destructive variants are controls.
- **minimal green:** first fix the prompt/packet; add resolver only if prompt regression still reproduces. Add DCG allow only when tests prove exact whole-command anchoring and arguments are revalidated by CLI.
- **refactor seam:** no general shell wrapper and no symbolic ref support inside `review-inspect`.
- **focused verification:** review prompt snapshot, inspect negative matrix, task/base DCG suites; E9, E11.

### Slice 8 — one-shot Wiki link self-heal (D-264-04)

- **files/responsibility:** new `scripts/vault_link_repair.py`; `scripts/stop-hook.py`; `scripts/vault_schema.py` only for reusable read-only catalog API; `tests/test_vault_schema.py`, `tests/test_stop_hook.sh`, `tests/test_vault_scripts.sh`.
- **consumes:** first strict validator output, current source-page SHA, exact filename/frontmatter title/H1 catalog and canonical `vault-write.py` update transaction.
- **produces:** repair plan only for unique exact title/H1 match; one optimistic transaction; one complete revalidation.
- **failing evidence:** preserved two-link Stop fixture blocks commit; ambiguous/missing/concurrent cases prove zero mutation.
- **minimal green:** support normal and aliased links with optional heading while preserving display/anchor; embeds and other forms stay unsupported unless RED requires them.
- **refactor seam:** catalog extraction may move from `vault_schema.py`, but validator remains mutation-free.
- **focused verification:** Stop, schema, writer tests; mutation choosing first of two candidates and direct-write mutation must fail; E7, E11.

### Slice 9 — integration join и honest coverage

- **files/responsibility:** `config/harness-audit-manifest.json`, `tests/harness/state_transition_oracle.json`, release transition tests; no feature code ownership.
- **consumes:** green commits from Workstreams A–D.
- **produces:** combined exact-HEAD matrix and honest coverage denominator for every new production module.
- **failing evidence:** join starts red if any module is unobserved, a shared transition conflicts, or a callback/vault/decision invariant is missing.
- **minimal green:** manifest/fixture registration and only conflict corrections; no new feature.
- **refactor seam:** none after join except behavior-preserving cleanup while all focused suites remain green.
- **focused verification:** `make test-harness`, `make test-harness-coverage`, release transition matrix; E10, E11.

### Slice 10 — unattended dogfood, docs и release candidate

- **files/responsibility:** `docs/pipeline-observability.md`, `docs/runtime-capabilities.md`, `docs/acceptance/v2.6.4-release-readiness.md`, `docs/releases/v2.6.4.md`, changelogs/version manifests.
- **consumes:** one clean integrated candidate HEAD and all prior receipts.
- **produces:** fake-provider full-runtime dogfood with intentionally omitted submit, normal live Opus review smoke with zero false nudge, exact evidence map and release disposition for every D-264 item.
- **failing evidence:** any manual `current/resume/send/callback write`, repeated provider effect, missing disposition or stale evidence blocks readiness.
- **minimal green:** evidence/docs/version metadata only; product fix after release review requires a new HEAD and rerun of affected gates.
- **refactor seam:** none; release review is approval-or-stop.
- **focused verification:** E6 dogfood, full gate below, final Opus Deep implementation review and single release-purpose review; E6, E10, E11.

## 9. Полная verification ladder

Cheap-first порядок:

```bash
python3 tests/harness/test_callback_submit_recovery.py
python3 tests/harness/test_review_resolution_bundle.py
python3 tests/harness/test_review_vertical.py
python3 tests/harness/test_runtime_sessions.py
python3 tests/harness/test_runtime_task_summary.py
python3 tests/test_task_escalation_records.py
python3 tests/test_task_lifecycle.py
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

Правила evidence:

- каждый новый production module наблюдается in-process, а не только subprocess trace;
- mutation checks обязаны ломать generation binding, second-nudge ceiling, unique-title ambiguity и escalation command anchoring;
- telemetry/schema tests запрещают prompt, query, command, screen/review body и reason text в content-free events;
- full-runtime dogfood использует fake provider для намеренного missing-submit; один normal live Opus review доказывает provider/callback/statusline compatibility без искусственного сбоя;
- review/permission/provider snapshots сравниваются с v2.6.3; любое расширение требует отдельного public-interface/security решения.

## 10. Review policy

1. **До реализации:** single-model Opus xhigh `purpose=intent` проверяет этот план, Outcome Contract, границы patch-релиза и полноту D-264.
2. **После каждого независимого workstream:** focused self-review + соответствующие unit/transition gates; cross-model provider effect не нужен.
3. **После join:** Opus single-model Deep (`intent` + `engineering`) на exact candidate HEAD, `max_verify_iterations` не расширяется.
4. **Перед релизом:** отдельный `purpose=release` approval-or-stop на integration HEAD и evidence map.
5. Full review не запускается автоматически; только по явному запросу пользователя.

Материальные findings применяются в том же approved scope и получают regression. Scope/public-interface/security/migration forks останавливают task и попадают в append-only decision chain; review не запускает скрытый fix loop.

## 11. Stop conditions и rollback

- Любая необходимость читать review prose, синтезировать verdict или посылать prompt без exact generation/effect receipt блокирует callback capability.
- Duplicate nudge/callback/provider effect в одном race case блокирует релиз.
- Если DCG exact allow нельзя доказать whole-command negative matrix, D-264-07 остаётся typed deferred; broad Bash allow запрещён.
- Если link не разрешается точным unique title/H1 или repair требует создать страницу/fuzzy match/direct write, Stop ничего не меняет.
- Если повторная strict validation после одной transaction не green, повторной repair попытки в том же Stop run нет.
- Если append-only record chain нельзя совместить с legacy marker без потери решения, migration требует отдельного решения и релиз останавливается.
- Если dogfood требует ручной lifecycle command, E6 отсутствует.
- До publish rollback — удалить feature branch/worktree через штатный lifecycle и оставить main/v2.6.3 неизменным. После merge откат — один revert commit; durable evidence/decision chain не удаляется.

## 12. Incidental defect ledger

| ID | Дефект/evidence | Owner | Disposition 2.6.4 |
|---|---|---|---|
| D-264-01 | Reviewer idle без `.review-input.json`/submit | callback recovery | `included`, Slices 1–5 |
| D-264-02 | Accepted resource-free verification child не публикуется в gate | review bridge/resolution | `included`, Slice 4 |
| D-264-03 | Latest attention marker перезаписывает прежнее coordinator decision | task escalation records | `included`, Slice 6 |
| D-264-04 | Unique title/H1 wikilink блокирует Stop commit | vault/Stop | `included`, Slice 8 |
| D-264-05 | Нет supported amendment record для frozen plan digest | task escalation records | `included`, Slice 6 |
| D-264-06 | Quiescent `awaiting-resolution` recovery отвергался | review recovery | `already-shipped-in-2.6.3`, commit `68a13ef` |
| D-264-07 | Exact `task_escalation.py raise` блокируется task DCG | task permission policy | `included-if-security-matrix-green`, Slice 7; иначе explicit deferred |
| D-264-08 | Legacy review spec rehydrate несовместим с новым parent field | review recovery | `already-shipped-in-2.6.3`, commit `f3212cd` |
| D-264-09 | Reviewer передаёт symbolic/non-OID `--ref` в strict facade | review ContextPacket/prompt | `included`, Slice 7; strict facade не ослабляется |
| D-264-10 | Одноразовый inline Python probe вызвал `tuple has no attribute keys` | reviewer-authored ad-hoc script | `not-a-defect`: нет repo CLI/API contract; повторный product reproducer создаст новый ID |

Новый defect получает следующую D-264 identity до продолжения task. Ledger entry сам по себе не разрешает fix и не меняет frozen Outcome Contract; disposition меняется только через Slice 6 authority record.

## 13. Requirement-to-slice coverage

| Evidence | Срезы |
|---|---|
| E1 | 0, 1 |
| E2 | 1, 5 |
| E3 | 2, 3, 4 |
| E4 | 2–5 |
| E5 | 1, 3–5 |
| E6 | 10 |
| E7 | 8 |
| E8 | 6 |
| E9 | 7 |
| E10 | 0, 6, 9, 10 |
| E11 | 5, 8–10 |

Все 11 evidence items покрыты; каждый production slice имеет RED, minimal GREEN, refactor seam и runnable verification. Workstreams A–D не делят product files и могут исполняться параллельно только как отдельные task-level pipelines; внутри одного workstream порядок последовательный.

## 14. Завершение

2.6.4 считается release candidate только когда E1–E11 установлены на одном exact HEAD, каждый D-264 имеет durable disposition, offline missing-submit dogfood продолжается без ручного вмешательства, normal review не получает лишний effect, unique wikilink self-heal проходит через единственного writer, а exact provider/permission/model budgets совпадают с v2.6.3. Push, tag и GitHub release остаются отдельным явным действием пользователя после terminal approval.
