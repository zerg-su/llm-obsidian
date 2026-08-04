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
{"schema_version":1,"purpose":"Закрыть подтверждённые unattended gaps, из-за которых полностью выполненная моделью работа или безопасно восстанавливаемая ошибка vault validation не продолжают pipeline без присутствия пользователя.","desired_outcome":"Harness самостоятельно и безопасно доводит ожидающий typed callback до следующей стадии, когда точно принадлежащая ему reviewer-сессия закончила содержательную работу, вернулась к idle prompt, но не создала или не отправила callback: он использует durable generation-aware evidence, выполняет не более одного same-session submit-only nudge, принимает появившийся callback без повторного review и оставляет typed attention только после исчерпания bounded recovery. Stop hook также делает одну детерминированную попытку исправить unresolved wikilink, когда его target однозначно совпадает с title или H1 ровно одной существующей страницы, проводит изменение через vault-write и повторяет strict validation. Работа пользователя офлайн не должна останавливаться из-за забытого submit или однозначно исправимой адресации wikilink. Каждый incidental defect, обнаруженный вне frozen task scope, получает durable typed entry ближайшего будущего релиза и не теряется при смене attention marker.","success_evidence":[{"evidence_id":"E1-reproduce-missing-submit","observable":"Детерминированный full-runtime fixture воспроизводит точный инцидент: reviewer завершает вывод, возвращается к idle prompt, текущая callback generation остаётся без input/callback/receipt, а существующий harness до исправления не продолжает pipeline."},{"evidence_id":"E2-generation-aware-detection","observable":"Code-owned observer отличает стабильный idle-without-submit от активной модели по exact operation ownership, callback target generation, durable files/receipts и bounded content-free screen classification; raw review text не сохраняется и не интерпретируется."},{"evidence_id":"E3-bounded-auto-recovery","observable":"После короткого stable-idle grace harness ровно один раз отправляет в ту же exact session submit-only nudge, не просит повторить review, не создаёт новый reviewer/surface и продолжает pipeline после принятия callback."},{"evidence_id":"E4-race-and-replay-safety","observable":"Callback/input/receipt, появившиеся до или одновременно с nudge, побеждают recovery; повторный reconcile/restart не дублирует prompt, submit, callback, review или provider effect и сохраняет exact generation identity."},{"evidence_id":"E5-fail-closed-matrix","observable":"Active spinner/progress, permission prompt, unknown screen, stale generation, missing ownership, terminal parent, exhausted budget и malformed typed artifacts не вызывают nudge или synthetic callback; результатом остаётся bounded typed attention."},{"evidence_id":"E6-unattended-continuity-dogfood","observable":"Один изолированный end-to-end dogfood с недоступным координатором проходит от намеренно пропущенного reviewer submit через автоматическое same-session recovery к следующей pipeline стадии без ручной команды и без повторного review."},{"evidence_id":"E7-vault-link-self-heal","observable":"Stop hook воспроизводит текущий incident с unresolved ссылками на существующие страницы по уникальному title, атомарно канонизирует их через vault-write, повторно запускает strict validation и завершает штатный scoped commit; ambiguous, missing, anchor-only, malformed и concurrently changed targets не мутируются и остаются typed validation failure."},{"evidence_id":"E8-no-regression","observable":"Полные harness, transition, callback, review, permission, provider, vault-writer, wikilink, status/telemetry и release gates зелёные; существующие liveness nudge/restart ceilings и model budgets не расширены."},{"evidence_id":"E9-deferred-defect-ledger","observable":"Каждый подтверждённый внеплановый дефект имеет durable ID, reproducer/evidence, impact, ownership, предлагаемую release boundary и disposition included/deferred; новая escalation не перезаписывает историю предыдущих coordinator decisions, а release review проверяет отсутствие потерянных unresolved entries."}],"non_goals":["Парсить свободный текст review с экрана и превращать его в доверенный callback.","Синтезировать verdict или callback без typed reviewer artifact и штатного submit validator.","Добавлять новый scheduler, второй lifecycle owner или model-owned watchdog.","Делать периодические model calls, пока reviewer активно работает.","Увеличивать review, verification, nudge, restart или provider budgets.","Обходить dontAsk, sandbox, callback ownership, exact-generation или permission policy.","Создавать отсутствующие wiki-страницы, угадывать неоднозначные ссылки, менять смысл ссылки или редактировать vault в обход vault-write.","Переделывать PipelineSpec DSL, систему проектов 2.7 или общую task orchestration.","Включать исправление в уже финализируемый 2.6.3.","Автоматически расширять frozen task scope или чинить incidental defect внутри текущей задачи без отдельной release disposition и authority."]}
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

## 6. Реализация TDD-срезами

### Slice 0 — точный RED reproducer

- Зафиксировать incident fixture: exact reviewer parent, verification round generation N, live exact surface, stable idle prompt, отсутствующие input/callback/receipt.
- Доказать, что текущий observer достигает общего timeout/attention, но не выполняет submit-only recovery.
- Добавить отрицательный контроль: active spinner не классифицируется как idle.

### Slice 1 — content-free callback progress evidence

- Добавить компактный typed contract для callback-generation progress и stable idle observations.
- Расширить существующую liveness telemetry только identifiers/counters/classification values; body и screen content запрещены schema test.
- Сделать classifier provider-neutral и покрыть Claude/Codex prompt variants только существующими adapter evidence seams.

### Slice 2 — exact typed-artifact fast paths

- Если input стабилен, использовать существующий review submit validator без модели.
- Если callback существует, использовать существующий broker/reconcile.
- Доказать idempotency, symlink rejection, size/digest bounds и race callback-before-effect.

### Slice 3 — one-shot same-session nudge

- Добавить per-generation write-ahead effect/receipt и один фиксированный submit-only prompt.
- Проверить exact checkpointless Claude reviewer ownership и checkpoint-bound Codex ownership через существующий `RuntimeSessionManager`.
- Запретить новую lane/surface/provider identity и повтор effect на replay.

### Slice 4 — runtime integration и terminal policy

- Подключить detector к существующему runtime worker review bridge/liveness loop.
- После accepted receipt продолжить gate с первой missing stage.
- После исчерпания существующих ceilings создать отдельную typed reason `callback-submit-missing`, сохранив exact evidence pointers.
- Не считать простую process liveness или свежий screen hash durable pipeline progress для другой callback generation.

### Slice 5 — детерминированная transition matrix

Обязательные cases:

- idle два probes → один nudge → callback accepted → next stage;
- callback появляется до reservation, между reservation/send и после send;
- typed input появляется без callback;
- accepted receipt replay;
- stale generation и changed run/lane;
- active spinner, changing output, permission prompt, unknown/missing surface;
- dead provider с допустимым/исчерпанным restart budget;
- terminal parent;
- symlink/oversize/malformed input/callback;
- concurrent reconcile;
- process identity mismatch;
- coordinator offline на всём пути.

### Slice 6 — unattended dogfood и release evidence

- Запустить один изолированный full-runtime fixture, где fake reviewer намеренно возвращается к idle prompt без submit.
- Не выполнять ручной `current`, `resume`, cmux send или callback write.
- Дождаться ровно одного harness nudge, штатного typed submit, accepted receipt и автоматического перехода к следующей стадии.
- Дополнительно выполнить один обычный live review smoke, подтверждающий отсутствие ложного nudge при нормальном submit; новый намеренно сломанный live provider run не требуется.
- Сохранить exact-HEAD receipt, transition trace без content и release-readiness disposition.

### Slice 7 — bounded self-heal для unresolved wikilink

- Зафиксировать RED fixture из реального Stop failure: `[[LLM Obsidian 2.4 — Typed Pipeline Composition]]` и `[[LLM Obsidian 2.6.0 — единый релиз technical foundation и skill intelligence]]` не совпадают с filename, но однозначно совпадают с frontmatter `title` существующих страниц.
- Построить code-owned индекс `filename stem → page`, `frontmatter title → candidates`, `H1 → candidates` на валидных Markdown-страницах vault; title/H1 используется только при единственном совпадении.
- Канонизировать basic/aliased/heading wikilink с сохранением видимого текста и anchor, например `[[Title]]` → `[[canonical filename|Title]]`; embed и неоднозначные формы не расширять без отдельного RED.
- Сформировать одну bounded `vault-write.py` update transaction с optimistic source hash; прямой Edit из Stop hook запрещён.
- После успешной записи один раз повторить strict validation и штатный scoped commit. На collision, ambiguity, missing candidate, malformed page или повторный failure прекратить recovery и оставить исходный `VAULT_LINT_FAIL`/`COMMIT_BLOCKED` с typed repair classification.
- Не создавать страницы, не выбирать «ближайшее» fuzzy-совпадение и не чинить unrelated validation failures.

## 7. Предполагаемые файлы ответственности

Точные границы уточняются после RED, но предпочтительный ownership:

- `scripts/harness/liveness.py` или малый новый callback-progress contract module — чистая классификация;
- `scripts/harness/runtime_worker_liveness.py` — probes, grace и bounded decision;
- `scripts/harness/runtime_worker_review_bridge.py` — review-specific submit/nudge integration;
- `scripts/harness/runtime_session_checkpoint.py` / существующий continuation seam — только reuse, без нового ownership;
- `scripts/harness/runtime_callback_io.py` — typed input/callback stable-read fast path при необходимости;
- `scripts/harness/contracts.py` — только новый bounded attention reason/evidence shape, если существующий тип не выражает его;
- `tests/harness/test_liveness.py`, `test_runtime_task_summary.py`, `test_runtime_sessions.py`, `test_review_vertical.py` — unit/integration matrix;
- `.claude/hooks/stop.sh` и малый code-owned vault-link repair module — одна pre-commit repair attempt между первым strict lint failure и повторной validation;
- `scripts/vault-write.py`/существующий writer contract — reuse optimistic update transaction, без второго writer;
- `tests/test_stop_hook.sh`, vault schema/wikilink unit tests — однозначное исправление и fail-closed отрицательная матрица;
- `docs/pipeline-observability.md`, `docs/runtime-capabilities.md`, release notes/readiness — user-visible semantics.

Не создавать крупный новый controller и не расширять public callback schema без доказанного contract gap.

## 8. Проверка

Минимальный gate:

```bash
python3 tests/harness/test_liveness.py
python3 tests/harness/test_runtime_sessions.py
python3 tests/harness/test_runtime_task_summary.py
python3 tests/harness/test_review_vertical.py
bash tests/test_stop_hook.sh
make test-harness
make test-harness-coverage
make test
python3 scripts/runtime-harness-lint.py
python3 scripts/validate-vault.py --summary
git diff --check v2.6.3..HEAD
```

Дополнительно:

- mutation test удаляет generation check и обязан падать;
- mutation test разрешает второй nudge и обязан падать;
- transition matrix подтверждает zero duplicate effects;
- telemetry schema запрещает prompt/review/screen body;
- mutation test выбирает один из двух title-кандидатов и обязан падать;
- mutation test пишет repair напрямую, минуя optimistic vault-write transaction, и обязан падать;
- exact current Stop fixture после recovery проходит `scripts/validate-vault.py --summary` и не оставляет dirty writer-owned link repair;
- source snapshot/adapter checks подтверждают отсутствие provider и permission drift.

## 9. Review policy

- Intent review: unattended outcome, offline coordinator, stop conditions, no false progress.
- Engineering review: races, effect idempotency, exact ownership, security, test matrix и maintainability.
- Vault review: однозначность title/H1 resolution, сохранение alias/anchor, optimistic concurrency, single writer и fail-closed boundaries.
- Release review: один bounded dogfood, exact-HEAD receipt, отсутствие budget/provider/permission regression.
- Deep review достаточно; Full запускается только по явному запросу пользователя.

## 10. Stop conditions

- Если detector требует интерпретировать review prose, решение отвергается.
- Если recovery нельзя связать с exact generation/effect receipt, автоматический nudge не включается.
- Если one-shot nudge приводит к duplicate callback/provider effect хотя бы в одном race case, релиз блокируется.
- Если change расширяет model/restart budget или создаёт новый lifecycle owner, он переносится из patch-релиза.
- Если wikilink нельзя разрешить точным уникальным title/H1 match либо repair требует создания страницы, fuzzy matching или обхода vault-write, Stop сохраняет исходный failure и ничего не меняет.
- Если повторная strict validation после одной repair transaction не зелёная, дальнейшие автоматические попытки в этом Stop run запрещены.
- Если unattended dogfood требует ручного resume/current/send, E6 не установлен.

## 11. Incidental defect ledger и правило сохранения

Любой подтверждённый дефект, найденный во время frozen task, но не входящий в его Outcome Contract, получает durable entry в ближайшем будущем релизе до продолжения основной работы. Entry содержит ID, точный reproducer или evidence pointer, impact, затронутого owner, границу предполагаемого исправления и typed disposition `included`, `deferred` или `not-a-defect`. Запись не даёт authority чинить дефект внутри исходной задачи.

Начальный ledger 2.6.4:

| ID | Дефект/evidence | Impact | Предлагаемая граница |
|---|---|---|---|
| D-264-01 | Reviewer вернулся к idle prompt без `.review-input.json`/submit; callback generation осталась пустой | unattended pipeline полностью ждёт пользователя | core callback-submit watchdog, E1–E6 |
| D-264-02 | Accepted verification callback находится в resource-free child со state `verifying`, но gate не публикует result; idempotent facade падает `review continuation child is not resource-free and unpublished` | принятый callback не достигает resolution/finalization, повторный запуск опасен | generation-aware callback publication/reconcile в том же review owner, без новой surface |
| D-264-03 | `.task-needs-attention.json` хранит только последний marker; более раннее coordinator decision может остаться лишь в product-authored ссылках | reviewer не может независимо проверить amendment/authority trail | append-only typed coordinator decision records с pointer из latest marker; без prompt/body в content-free telemetry |
| D-264-04 | Stop strict lint блокирует commit для wikilink, который однозначно совпадает с title существующей страницы, но не с filename | безопасно исправимая адресация требует возвращения пользователя | bounded unique-title/H1 self-heal через `vault-write.py`, E7 |
| D-264-05 | Frozen plan hash запрещает вписывать позднее coordinator amendment в сам plan, а поддерживаемая отдельная authoritative decision-record операция не оформлена как явный workflow | корректный fail-closed требует ручного выбора и легко приводит к повторным невалидным попыткам | документированный code-owned amendment-record helper, не меняющий approved plan/Outcome digests |
| D-264-06 | После coordinator-authorized containment gate остаётся `awaiting-resolution`, хотя все retained parent/child operations terminal, resource-free и effect-free; публичный `task-review-runner.py recover` отвергает это состояние как `not at one stale verification boundary` до создания fresh boundary | разрешённое exact-chain recovery не может запустить fresh review без ручной gate/store mutation или изменения контракта | `included-in-2.6.3`: commit `68a13ef`, authoritative amendment [[LLM Obsidian 2.6.3 — review recovery inclusion disposition]] |
| D-264-07 | PreToolUse/DCG блокирует официальный `task_escalation.py raise` как HIGH unknown destructive pattern при восстановлении machine authorization token | callback-driven recovery снова требует возвращения пользователя, хотя команда не является destructive и уже coordinator-authorized | структурированный allow/deny contract для точного task-escalation CLI argv с fail-closed path/category validation и regression против shell/destructive variants |
| D-264-08 | Quiescent historical review rounds имеют pre-schema `OperationSpec` без `parent_operation_id`; текущий `rehydrate()` вычисляет тот же idempotency key с новой spec и падает до recovery | fresh boundary не запускается даже после корректного containment, хотя provider effect отсутствует | `included-in-2.6.3`: commit `f3212cd`, authoritative amendment [[LLM Obsidian 2.6.3 — review recovery inclusion disposition]] |
| D-264-09 | Opus reviewer при штатном review повторно вызывает `review-inspect.py` с symbolic/non-OID `--ref` и получает `--ref must be an exact lowercase Git object id`, после чего вынужден отдельно восстанавливать SHA | лишний failed tool turn, токены и риск неполного review; pipeline не падает, но bounded facade недостаточно self-describing | добавить exact base/head OID в ContextPacket/prompt или безопасный code-owned OID resolver перед facade call; сохранить строгий OID boundary внутри `review-inspect`, добавить prompt/command regression, запрещающий symbolic ref напрямую |

Release triage обязан обновить каждый entry: включённый дефект получает regression и exact-HEAD evidence; слишком широкий interface/schema change переносится в 2.7 с явным pointer, но не удаляется из ledger.

## 12. Завершение

2.6.4 считается готовым только когда работа без пользователя проходит подтверждённый missing-submit incident автоматически, normal-path review не получает лишних prompt/model effects, текущий класс однозначно исправимых unresolved wikilink восстанавливается Stop hook без ручного вмешательства и без ослабления strict validation, а все incidental defects имеют durable disposition. Публикация, tag и release выполняются отдельно после terminal approval.
