---
type: plan
title: "LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline"
address: c-000117
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-05
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-2-6-5-coordinator"
status: pending
created: 2026-08-05
updated: 2026-08-05
tags:
  - plan
  - manual-save
  - llm-obsidian
  - v2-6-5
  - harness
  - split
---

# LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Упростить хрупкий review/finalization lifecycle 2.6.4, сохранив code-owned Pipeline DSL, а после доказательства стабильности добавить минимальный Split pipeline с переменным fan-out и детерминированным join.",
  "desired_outcome": "Каждая reviewer-сессия принадлежит одному неизменному exact HEAD и завершается terminal verdict; после изменения HEAD старые reviewers закрываются, а новый bounded finalization cycle запускает свежий review без rearm, checkpoint resurrection, verification child, mixed reviewed/resolved state или plan rebind. Harness получает typed provider/process events через узкий adapter, отделяет transport liveness от business completion, разрешает повтор только до необратимой delivery boundary и использует время только для terminal attention. Короткие bounded review и schema-producing tasks по умолчанию исполняются без cmux через равноправные ephemeral adapters: `claude -p` для Anthropic subscription route и `codex exec` для OpenAI ChatGPT route; публичный Pipeline DSL не связан с именем CLI, поэтому любой transport заменяется без изменения workflow contracts. Finalization lineage ограничена пятью циклами; после трёх однородных циклов 4–5-й review добавляет независимую разрешённую модель только при подтверждённой доступности недельного лимита. На этом устойчивом ядре Split workflow раскладывает утверждённый plan на естественное число независимых subplans, исполняет их bounded waves в локальных workspaces и закрывает parent Outcome только через deterministic join.",
  "success_evidence": [
    {
      "evidence_id": "E1-causal-ledger",
      "observable": "Версионированный defect ledger группирует остановки 2.6.4 по корневым механизмам и связывает каждый класс с воспроизводимым interleaving; очередной marker, timeout или retry не может считаться устранением причины."
    },
    {
      "evidence_id": "E2-exact-head-attempt",
      "observable": "ReviewAttempt immutable связывает plan, Outcome, policy и один exact HEAD; его допустимые terminal результаты — approved, changes-requested, blocked или attention-required. Изменение HEAD не может rearm или продолжить существующую reviewer-сессию."
    },
    {
      "evidence_id": "E3-cross-head-removal",
      "observable": "Новые review/finalization пути не создают cross-HEAD verification children, continuation receipts, resolution chains, checkpoint resurrection или plan rebind. Historical 2.6.x records доступны только для read-only inspect/archive/cleanup и не возобновляются."
    },
    {
      "evidence_id": "E4-provider-events",
      "observable": "Узкий provider adapter публикует только закрытый ProviderEvent vocabulary: provider-started, input-accepted, turn-stopped, result-published, process-exited, resource-closed и event-gap. Interactive и ephemeral profiles используют явно определённые subsets и mappings; каждый event несёт exact operation/run/generation/session/workspace/surface identity и monotonic source cursor, а duplicate, stale, gap и identity mismatch fail closed."
    },
    {
      "evidence_id": "E4b-execution-profiles",
      "observable": "Pipeline policy явно выбирает interactive либо ephemeral execution profile. Для bounded review/task Anthropic и OpenAI являются равноправными logical routes: разрешённый пользователем 2026-08-05 subscription-backed claude -p и ChatGPT-backed codex exec компилируются в provider-specific argv, но выдают один schema-validated result/event contract без cmux или session resurrection. Public DSL и durable receipts не содержат CLI transport name, поэтому Claude transport заменяется adapter-конфигурацией. Fail-closed auth/billing-profile probe не допускает silent API key, paid credits, provider или billing-path fallback; ambiguous profile возвращает typed interactive disposition до model effect."
    },
    {
      "evidence_id": "E5-irrevocable-boundary",
      "observable": "Delivery retry разрешён только до durable irreversible boundary. После подтверждённого input/Enter или неоднозначного effect Harness выполняет lookup/reconcile по idempotency key и никогда не повторяет provider-facing effect вслепую."
    },
    {
      "evidence_id": "E6-time-last",
      "observable": "Deadline может только инициировать recheck или terminal attention. Screen digest, elapsed time и repaint не разрешают progress, restart, successful continuation либо result acceptance. Только interactive turn-stopped без result допускает один same-HEAD submit-only recovery; ephemeral process exit без schema-valid result сразу terminal attention и никогда не запускает submit, restart или hidden interactive fallback."
    },
    {
      "evidence_id": "E7-five-cycle-budget",
      "observable": "Finalization lineage и номер цикла сохраняются при новом HEAD, task, worktree, provider и restart. До review effect атомарно резервируется цикл 1–5; пятый неуспешный заканчивается finalization-budget-exhausted, шестой не создаёт session или model call."
    },
    {
      "evidence_id": "E8-adaptive-independent-review",
      "observable": "Циклы 1–3 используют один frozen finalization-primary route, отдельный от публичного standalone `review --deep`; его dual-provider semantics не меняются. Перед 4–5 Harness проверяет разрешение и свежую typed availability finalization-independent route: при доступности добавляет вторую модель, при недоступности сохраняет single-model fallback с причиной; explicit user single-model никогда не переопределяется."
    },
    {
      "evidence_id": "E9-dsl-and-skill-contracts",
      "observable": "Pipeline DSL по-прежнему задаёт bounded steps, loops, review, verification, budgets, stop conditions и Split/join; code-owned ceilings не ослаблены. implementation-plan, dispatch и review skills ссылаются на тот же executable contract и изменены только через improve-skills плюс skill-creator с полным audit."
    },
    {
      "evidence_id": "E10-split-preview",
      "observable": "Skill split в zero-effect preview выбирает один либо естественное число subplans, публикует typed manifest и отказывается от fan-out, если независимость не доказана или coordination cost выше выигрыша."
    },
    {
      "evidence_id": "E11-split-validation",
      "observable": "До dispatch отклоняются stale parent/Outcome digest, непокрытое evidence, overlapping ownership, dependency cycle, отсутствующий join, ослабленный non-goal, незарегистрированный pipeline и превышение frozen budgets."
    },
    {
      "evidence_id": "E12-bounded-fanout",
      "observable": "Валидный manifest запускает ready children воспроизводимыми waves с отдельными subplan_count и max_parallel; executor и его review/verification размещаются в workspace child, а terminal cleanup не оставляет reviewer owners или helper processes."
    },
    {
      "evidence_id": "E13-deterministic-join",
      "observable": "Join фиксирует exact child branch, HEAD, summary и review receipt, объединяет их в manifest order, останавливается на failed/cancelled/attention/conflict и закрывает parent только после integrated gates и полного покрытия исходного Outcome Contract."
    },
    {
      "evidence_id": "E14-release-evidence",
      "observable": "Focused, generated event/retry, both-adapter conformance, full test, honest harness coverage, acceptance, vault, Codex/MCP sync и quality gates зелёные на одном clean exact HEAD. Обязателен один live single-model dogfood на разрешённом доступном route; второй provider smoke выполняется при typed availability, а unavailable/unknown фиксируется как E8 fallback и не скрывает conformance failure. Release review соблюдает five-cycle budget, все owned sessions terminal resource-free."
    }
  ],
  "non_goals": [
    "Не переписывать Harness целиком и не добавлять второй orchestration engine, universal event store или scheduler рядом с существующими OperationStore и Pipeline DSL.",
    "Не сохранять same-session reviewer continuity после изменения Git HEAD и не восстанавливать historical cross-HEAD verification operations.",
    "Не выполнять несовместимую миграцию Pipeline DSL, persisted review evidence или operation records; compatibility для historical records read-only и fail-closed.",
    "Не считать увеличение timeout, новый recovery marker, checkpoint layer или screen polling исправлением корневой причины.",
    "Не разрешать таймеру положительный lifecycle transition, provider restart либо повтор неоднозначного effect.",
    "Не считать исчезновение terminal/process окна доказательством durable cleanup: resource-closed event и idempotent reconciliation обязательны.",
    "Не переопределять explicit single-model и не делать отсутствие Anthropic либо OpenAI блокером всего pipeline.",
    "Не включать API-key billing, paid-credit continuation, hooks, network, workspace writes или permission bypass без отдельного typed opt-in profile; локальные subscription-backed claude -p и ChatGPT-backed codex exec разрешены как штатные ephemeral transports только после fail-closed auth preflight.",
    "Не встраивать CLIProxyAPI как обязательный daemon/dependency и не путать его account affinity с process/session identity.",
    "Не создавать Project Spaces, project backlog, динамический scheduler или model-driven replanning — это scope 2.7.",
    "Не менять skills напрямую без improve-skills audit и skill-creator workflow; новый split skill подчиняется тому же baseline-to-GREEN и verdict contract.",
    "Не выполнять push, tag, publish или установку релиза без отдельного пользовательского решения."
  ]
}
```

## 1. Контекст

2.6.4 дошёл до clean exact HEAD `3e391fc9e6aa48e1344520dbffdebba704312540` с зелёными full tests, honest coverage, 4 370 transition cases, acceptance, vault, Codex/MCP sync и quality gates. Последняя разрешённая retained verification остановилась до provider effect: один parent уже был в callback timeout, sibling ждал callback, gate оставался awaiting-resolution, а resource-free verification child был подготовлен. По D-264-73 кандидат сохранён как `RC-attention` и дальнейший review/fix loop запрещён.

Дополнительный 2.6.5 research dogfood воспроизвёл тот же класс рассогласования уже без review semantics: synthesis сохранил валидный `answer.md` и `complete.json`, процессы и cmux workspace исчезли, но operation осталась `exiting`, а parent — `awaiting-callback`. Это фиксируется в causal ledger как отдельный interleaving `resource physically gone → durable close event absent`; ручное редактирование OperationStore не считается исправлением.

Решение D-265-EPH-01 от 2026-08-05: пользователь явно подтвердил, что локальный `claude -p` работает через требуемую Claude subscription, и разрешил применять его наравне с `codex exec` для коротких bounded review/tasks. Это supersedes локальный запрет от 2026-06-10 в `.claude-memory/feedback_no_claude_p_headless.md`, но не разрешает hosted/multi-user use, API-key billing, paid credits или произвольные прямые print-mode launches. Slice 3b обязан доказать native account mode bounded acceptance probe; ambiguity → `billing-profile-unverified` и explicit interactive route до model effect. Slice 7 атомарно согласует memory, AGENTS, CLAUDE и skills после GREEN.

Проблема не в недостатке частных guards. Cross-HEAD review и callback recovery распределены минимум между OperationStore, mutable review gate, current pointer, resolution evidence, continuation receipts, verification children, checkpoints, liveness state и screen/time observations. Только явно связанный контур насчитывает около 4 530 строк production-кода и 8 900 строк специализированных тестов. Новая saga/reducer поверх всех этих представлений увеличила бы число понятий, но не убрала бы саму сложность.

Решение 2.6.5: удалить потребность переносить reviewer через новый HEAD. Сохраняется каноничная цель координаторская Harness-first Vision: DSL описывает pipeline и bounded loops, Harness исполняет lifecycle, LLM публикует typed artifacts. Меняется только нижний review execution protocol.

Связанные решения: решение «LLM Obsidian 2.6.5 — Split skill и bounded fan-out join» и решение «LLM Obsidian 2.7 — Project Spaces и Task Orchestration».

## 2. Корневые причины и удаляемая сложность

| Root class | Следствие 2.6.4 | Решение 2.6.5 |
|---|---|---|
| R1. Cross-HEAD continuity | parent относится к A, gate/child уже к B | одна ReviewAttempt принадлежит одному HEAD и никогда не rebind'ится |
| R2. Несколько writable truths | parent, child, gate, current и receipt расходятся | attempt terminal result пишется один раз; current и summaries только projections |
| R3. Необратимый effect не отделён от liveness | paste/Enter/activity/callback требуют угадывания replay | typed event + idempotency key + retry только до irreversible boundary |
| R4. Время используется как причина | idle threshold запускает nudge/restart | timer только attention; Stop event может разрешить один same-HEAD submit-only prompt |
| R5. Recovery сохраняет старую сессию любой ценой | checkpoint, rearm, rebind, verification recovery | старый attempt закрывается; исправленный HEAD получает новый cycle/session |
| R6. Бесконечная финализация | новый HEAD фактически сбрасывает число попыток | persistent lineage, максимум пять циклов |
| R7. Однородные поздние проверки | одна модель повторяет одинаковый blind spot | условная независимая модель на циклах 4–5 |
| R8. Физический cleanup расходится с durable state | окно/PID уже исчезли, operation остаётся `exiting`, parent ждёт callback | typed `process-exited`/`resource-closed`, один idempotent reconciler и terminal projection после durable close |

### 2.1. Что выводится из нового critical path

Для новых attempts больше не используются как переходные основания:

- `awaiting_resolution` и mutable reviewed/resolved HEAD chain;
- same-session parent rearm после timeout;
- verification child нового HEAD;
- `continuation_effects` и succeeded/prepared cross-HEAD receipts;
- checkpoint resurrection ради нового HEAD;
- plan/current review rebind между HEAD;
- fresh reevaluation recovery поверх старой gate identity;
- screen digest либо elapsed time как positive acknowledgement.

Historical 2.6.x records не удаляются и не мигрируют. Они доступны для inspect, evidence archive и resource cleanup; попытка resume возвращает typed `legacy-cross-head-resume-disabled`.

## 3. Минимальная целевая архитектура

### 3.1. ReviewAttempt: один HEAD, один terminal verdict

ReviewAttempt замораживает:

```text
attempt_id
finalization_lineage_id / cycle
plan_sha256 / outcome_sha256
exact_head_sha
review policy / provider routes
lane identities
terminal result
```

Допустимый state graph:

```text
pending → running → awaiting-callback → terminal

terminal = approved | changes-requested | blocked | attention-required
```

Terminal attempt не возобновляется. `changes-requested` содержит immutable findings receipt. Исправление происходит вне attempt; после нового full gate finalization coordinator резервирует следующий cycle и создаёт новый attempt для нового HEAD.

Deep/Full lanes остаются частью одного attempt. Их callbacks могут приходить независимо, но итог attempt публикуется только после terminal result всех обязательных lanes. Нет verification iteration внутри attempt: проверка исправленного кода — следующий cycle.

### 3.2. ProviderAdapter и EventBridge

Вместо универсального terminal inference provider boundary получает узкий интерфейс:

```text
start(spec) -> SessionIdentity
send(session, input, effect_id) -> delivery receipt
cancel(session, reason)
close(session) -> idempotent close receipt
capabilities() -> typed capabilities
events(after_cursor) -> ordered ProviderEvent
```

Закрытый ProviderEvent vocabulary:

```text
provider-started
input-accepted
turn-stopped
result-published
process-exited
resource-closed
event-gap
```

`result-published` — единый business-completion event: interactive adapter связывает его с принятым typed callback, ephemeral adapter — со schema-valid final artifact. `turn-stopped` допустим только для interactive profile. Ephemeral profile обязан выдать `provider-started`, `input-accepted`, затем ровно один `result-published` либо terminal `process-exited`; truncated/missing sequence нормализуется в `event-gap`. Оба profiles обязаны завершиться `process-exited` и/или `resource-closed` согласно owned-resource contract.

Каждый event связан с exact operation, run, generation, provider session, process identity, workspace/surface и monotonic source cursor. Core policy зависит только от этих typed values; cmux/Claude/Codex/возможный HTTP proxy являются adapters. Никакой adapter не добавляет второй vocabulary: provider-native stream items преобразуются в этот закрытый набор на ingress.

### 3.3. Два явных execution profile и сменяемые transports

ProviderAdapter поддерживает два разных, но одинаково typed режима; Harness выбирает их из frozen PipelineSpec, а не эвристикой модели.

| Profile | Для чего | Lifecycle |
|---|---|---|
| `ephemeral` | короткий bounded review, классификация, синтез или disposable schema-producing task, которому не нужна беседа с пользователем | один process, один input, normalized events, schema-valid final artifact, exit, idempotent close; без cmux workspace, resume и checkpoint |
| `interactive` | длинная implementation-сессия, ручное продолжение, видимый пользователю reviewer или задача с уточнениями | интерактивная terminal/cmux session, typed callback и provider events; exact-HEAD attempt всё равно terminal и не переживает новый HEAD |

Ephemeral Anthropic и OpenAI — равноправные logical provider routes:

| Logical route | Текущий local transport | Provider-specific параметры |
|---|---|---|
| `anthropic` | `claude -p` через подтверждённый Claude Pro/Max subscription OAuth | print/non-interactive mode, `json` или `stream-json`, `--json-schema`, `--no-session-persistence`, bounded turns/effort/model, deny-by-default tools/permissions, no project hooks/network/writes |
| `openai` | `codex exec` через подтверждённый ChatGPT login | `--ephemeral`, `--json`, `--output-schema`, bounded effort/model, read-only sandbox, isolated rules/config/hooks, раздельные stdout/stderr |

Оба adapters получают один provider-neutral `EphemeralRunSpec`:

```text
logical_provider / model / effort
context_packet / output_schema
capabilities / auth_profile
turn_budget / wall_clock_deadline
operation / run / generation / effect_id
```

И возвращают один `EphemeralRunResult`, выраженный закрытым ProviderEvent subset: `provider-started`, `input-accepted`, `result-published`, `process-exited`, `resource-closed`, `event-gap`. Ephemeral transport никогда не эмитит `turn-stopped`; отсутствие schema-valid `result-published` до exit является terminal failure. Provider-specific JSON и exit semantics нормализуются только внутри adapter; core review/finalization policy не читает stdout text и не знает CLI argv.

Public Pipeline DSL задаёт `provider`, `model`, `effort`, `execution: ephemeral|interactive` и capabilities, но не `claude -p`/`codex exec`. Внутренний transport registry сегодня связывает `anthropic → claude-print`, `openai → codex-exec`; позже Anthropic transport можно заменить CLIProxyAPI, официальным API либо другим локальным executor, не меняя PipelineSpec, ReviewAttempt, ledger, callback schema и archived evidence. Transport identity всё равно сохраняется в diagnostic receipt для аудита, но не становится workflow identity.

Оба ephemeral routes являются штатными по решению D-265-EPH-01 и могут выбираться frozen model policy без отдельного подтверждения на каждый вызов только после provider-specific preflight. Для Claude child environment удаляет `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, cloud/gateway overrides, подтверждает native subscription account mode и выполняет один bounded acceptance probe перед rollout; ambiguous/contrary evidence возвращает `billing-profile-unverified` без model effect в review pipeline. Для Codex проверяется активный ChatGPT login и запрещается неявный API-key profile. Несовпадение auth/billing profile останавливает вызов до review effect. [Claude authentication](https://code.claude.com/docs/en/team), [Pro/Max usage](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan), [Codex authentication](https://developers.openai.com/codex/auth).

Ни один adapter не включает paid credits/API fallback автоматически и не переключает provider после неоднозначного effect. `auth-expired`, `usage-exhausted`, `schema-invalid`, `policy-denied`, `timeout`, truncated stream и nonzero exit являются typed terminal outcomes. Интерактивный cmux выбирается только когда spec требует human continuation/visibility или capability, которой нет у ephemeral profile; ошибка ephemeral transport сама по себе не запускает скрытую terminal session.

D-265-EPH-01 supersedes старый локальный запрет, но live rules меняются только атомарно в Slice 7 после GREEN adapter и billing-profile tests: запрещён остаётся произвольный print-mode dispatch/reviewer, а разрешён — только code-owned ephemeral adapter с subscription preflight, fixed argv compiler, minimal ContextPacket, schema validation, bounded capabilities и durable receipts.

### 3.4. Retry boundary по execution profile

Retry безопасен только до названной необратимой transport boundary:

1. Для обоих profiles до `input-accepted` допустим один exact send replay по idempotency key.
2. После `input-accepted` исходный input никогда не повторяется.
3. Только interactive `turn-stopped` без `result-published` разрешает один фиксированный same-HEAD callback-submit request при неизменных session identity/generation.
4. Второй interactive Stop без result, `event-gap`, exit или unknown ownership → terminal attention без restart.
5. Ephemeral `process-exited` без schema-valid `result-published` → terminal attention; submit-callback, resume, process restart и hidden interactive fallback запрещены.
6. `result-published` после stale/duplicate/wrong identity отклоняется; timer не может заменить missing event.

Liveness отвечает только на вопрос «можно ли ещё наблюдать transport». Business completion доказывается только `result-published`. Cleanup — отдельный идемпотентный terminal action, завершающийся `resource-closed` либо typed attention.

### 3.5. FinalizationLedger

Небольшой code-owned ledger хранит только lineage и terminal attempts:

```text
lineage_id
origin_task / plan / Outcome
max_cycles = 5
cycles[] = {number, exact_head, attempt_id, terminal_result, provider policy}
terminal_disposition
```

Ledger не дублирует operation/session state и не является универсальным event journal. Он отвечает только за atomic cycle reservation, запрет шестого запуска и adaptive provider policy.

## 4. Finalization policy

### 4.1. Циклы

Один cycle — один terminal ReviewAttempt exact HEAD. Same-HEAD callback-submit recovery не создаёт новый cycle. После `changes-requested` выполняется максимум один fix batch, полный gate и следующий cycle нового HEAD.

- новый commit/task/worktree/provider не сбрасывает lineage;
- preflight без attempt/session/effect не расходует cycle;
- materialized attempt расходует зарезервированный cycle;
- пятый неуспешный result заканчивает `finalization-budget-exhausted`;
- security, permission, unknown effect или scope stop может закончить раньше.

### 4.2. Вторая модель на циклах 4–5

| Условие | Решение |
|---|---|
| explicit user single-model | не расширять; typed reason `explicit-single-model` |
| provider запрещён frozen policy | не расширять; `provider-policy` |
| недельный лимит подтверждённо недоступен | single-model fallback; сохранить источник и время проверки |
| availability unknown/stale | не делать пробный вызов; `availability-unknown` |
| независимый provider разрешён и доступен | добавить его lanes в тот же cycle |

Проверка availability принадлежит provider adapter/capability registry, не statusline text и не LLM. Общий cycle и verification/token ceilings не увеличиваются.

## 5. Что берём из CLIProxyAPI

CLIProxyAPI используется как design reference, не как обязательная зависимость:

- provider executor отделён от routing, transport и protocol translation;
- event/stream lifecycle отделяет transport liveness от завершения запроса;
- retry policy ограничена явной irreversible boundary;
- один владелец transport, cancellable lifecycle и идемпотентный close;
- capability/routing registry не становится workflow truth.

Официальные источники:

- [README и scope proxy](https://github.com/router-for-me/CLIProxyAPI/blob/main/README.md)
- [SDK provider/executor boundaries](https://github.com/router-for-me/CLIProxyAPI/blob/main/docs/sdk-advanced.md)
- [WebSocket session lifecycle](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/wsrelay/session.go)
- [retry, routing и session-affinity configuration](https://github.com/router-for-me/CLIProxyAPI/blob/main/config.example.yaml)
- [MIT License](https://github.com/router-for-me/CLIProxyAPI/blob/main/LICENSE)

Не переносим account affinity, automatic credential failover, usage TTL и сам proxy daemon как source of truth. Optional CLIProxy HTTP/WebSocket provider adapter допускается только отдельным prototype после Stability Gate S: он не входит в release denominator и не заменяет интерактивные adapters без отдельного approval.

## 6. Pipeline DSL сохраняется

Упрощение происходит ниже DSL. Existing PipelineSpec продолжает задавать:

- последовательные steps;
- bounded loops и max iterations;
- implementation/test/review stages;
- model/provider policy;
- verification and finalization budgets;
- stop/attention conditions;
- после Stability Gate — Split, bounded parallel waves и join.

Пример целевого выражения:

```yaml
pipeline:
  steps:
    - implementation
    - test
    - finalization
    - join
  finalization_policy:
    max_cycles: 5
    add_independent_model_after: 3
    execution: ephemeral
    primary_route_alias: finalization-primary
    independent_route_alias: finalization-independent
```

`finalization-primary` — отдельный зарегистрированный route alias и единственный active route на циклах 1–3. Он не переиспользует и не сужает публичный standalone preset `review --deep`: тот сохраняет существующую dual-provider topology. `finalization-independent` — frozen candidate, который Harness может материализовать только на циклах 4–5 после availability и explicit-single-model checks. Inline model override и заранее активный массив providers запрещены.

Совместимость PipelineSpec v1 обязательна: `finalization_policy` добавляется только как optional additive property, existing required set не меняется, а parser различает required и allowed fields. Старые specs, включая `examples/pipelines/document-project-v1.json` и persisted schema-v1 bytes, продолжают парситься без миграции и получают прежнее поведение. Если это невозможно без несовместимого изменения, нужен versioned schema и отдельный ADR/approval. DSL может запросить меньше code-owned ceiling, но не больше. LLM предлагает spec и typed artifacts; Harness компилирует, резервирует cycles, запускает attempts и применяет terminal transitions.

## 7. TDD implementation slices

Каждый slice: failing evidence → minimal green → refactor while green → focused verification. Shared integration files принадлежат только integration slice.

### Slice 0 — causal baseline и удаляемый contour

**Files/responsibility:** `docs/acceptance/v2.6.5-causal-ledger.md`, machine-readable D-264-73 fixture, отдельный stale-`exiting` interleaving fixture, новый `tests/harness/test_exact_head_review_attempt.py`.

**Consumes:** exact 2.6.4 retained failure и D-264 ledger.

**Failing evidence:** attempt A может подготовить child B после terminal/timeout parent; HEAD chain меняется внутри gate; физически исчезнувший process/workspace оставляет operation `exiting` без durable close event.

**Produces/minimal green:** только RED, root mapping и measured inventory writers/markers. Production unchanged. Covers E1.

### Slice 1 — immutable exact-HEAD ReviewAttempt

**Files/responsibility:** новый глубокий module `scripts/harness/review_attempt.py`; bounded wiring в `review_program*` и `workflows/review_gate*`; `tests/harness/test_review_attempt.py`.

**Consumes:** existing ReviewProgram compilation, lane operations, CallbackEnvelope.

**Failing evidence:** terminal attempt rearm, changed HEAD rebind и verification_iteration > 0 проходят current code.

**Minimal green:** freeze exact identity, terminal `changes-requested`, reject changed HEAD before session start, no verification child.

**Refactor seam:** existing initial-lane compilation/receipt validation переиспользуется, не дублируется. Covers E2.

### Slice 2 — вывести cross-HEAD machinery из critical path

**Files/responsibility:** `task_review_resolution_flow.py`, verification/recovery/resubmit/rebind modules, review gate resolution/continuation/recovery; compatibility tests.

**Consumes:** terminal ReviewAttempt contract.

**Failing evidence:** new attempt всё ещё записывает awaiting_resolution, continuation_effects, rebind journal либо создаёт child нового HEAD.

**Minimal green:** new flow не вызывает эти paths; historical records read-only inspect/archive/cleanup, resume returns typed disabled result. Удалить unreachable branches только после call-graph and regression proof.

**Refactor seam:** не создавать compatibility adapter, способный снова запускать provider. Covers E3.

### Slice 3 — ProviderEvent core contract

**Files/responsibility:** новый `scripts/harness/provider_events.py`; расширение существующих Claude/Codex/cmux adapters без второго session manager; `tests/harness/test_provider_events.py`.

**Consumes:** exact SessionIdentity/OwnedResources и cmux event cursor.

**Failing evidence:** duplicate, stale, wrong surface, boot gap или repaint выглядит как progress.

**Minimal green:** bounded event values, dedup, cursor/gap and identity validation; adapters только переводят provider facts. Covers E4.

### Slice 3b — равноправные ephemeral provider adapters

**Files/responsibility:** provider-neutral `EphemeralRunSpec/Result`, существующий routing/compiler, узкие Claude print и Codex exec adapters, transport registry, auth/capability/billing-profile preflight и focused contract/process tests. Этот slice не меняет skills; он produces executable evidence для Slice 7.

**Consumes:** frozen ReviewAttempt policy, ProviderEvent contract, minimal ContextPacket и output schema.

**Produces:** один provider-neutral ephemeral contract; `anthropic → claude-print` и `openai → codex-exec` bindings, которые можно заменить конфигурацией без DSL/evidence migration; typed auth/billing-profile/failure receipts и D-265-EPH-01 acceptance evidence.

**Failing evidence:** bounded review требует cmux/checkpoint; Claude и Codex имеют разные core state paths; ambient credential или unverified account mode меняет billing premise; transport name попадает в public PipelineSpec; provider emits value вне vocabulary; malformed/truncated JSON принимается; process exit без durable close оставляет operation `exiting`.

**Minimal green:** одинаковый lifecycle и closed event subset для обеих logical routes; subscription/ChatGPT fail-closed preflight; bounded Claude native-account acceptance probe; fixed provider-specific argv compilers; schema-valid result плюс exit/resource-close reconciliation; no paid/provider/interactive fallback; interactive path остаётся совместимым.

**Refactor seam:** общими являются только spec/result/events/identity/policy; argv, auth probing и stream parsing остаются provider-owned. Не создавать universal proxy abstraction или второй process manager.

**Focused verification:** deterministic fake-process matrices для Claude/Codex, vocabulary-subset conformance, exact argv snapshots, environment sanitization, bounded native-account probe, partial stream/nonzero exit, duplicate close, disappeared PID/surface и adapter substitution. Covers E4b.

### Slice 4 — delivery boundary и same-HEAD callback submit

**Files/responsibility:** existing runtime session delivery, callback-submit classifier/liveness; новый focused reducer лишь для `send|wait|submit-callback|attention|close`, без universal lifecycle abstraction.

**Consumes:** ProviderEvent and OperationStore effect receipt.

**Failing evidence:** time/screen разрешает Enter; accepted input replayed; Stop без callback зависает; process exit вызывает restart.

**Minimal green:** pre-accept idempotent send, post-accept zero replay, один Stop-driven callback-submit, второй Stop/exit/gap → attention, zero review restart.

**Refactor seam:** удалить superseded positive polling branches и callback-submit state representations, которые полностью выводятся из OperationStore effect + event cursor. Covers E5, E6.

### Slice 5 — FinalizationLedger и пять циклов

**Files/responsibility:** новый `scripts/harness/finalization_ledger.py`; additive task metadata/schema; dispatch/review-finalization wiring; `tests/harness/test_finalization_ledger.py`.

**Consumes:** terminal ReviewAttempt and full gate receipt.

**Failing evidence:** HEAD/worktree/provider reset; concurrent duplicate cycle; sixth session materializes.

**Minimal green:** atomic reservation 1–5, immutable terminal entry, exhausted disposition, zero sixth effect. Covers E7.

### Slice 6 — provider availability и adaptive cycles

**Files/responsibility:** existing model routing/provider capability modules; finalization policy compiler; tests.

**Consumes:** frozen provider policy and typed availability source.

**Failing evidence:** explicit single-model overridden; stale statusline trusted; availability tested дорогим model call; finalization ошибочно компилируется через dual-provider standalone `review --deep` уже на цикле 1.

**Minimal green:** отдельные registered `finalization-primary` и `finalization-independent` routes, precedence matrix and fallback reasons; standalone Deep остаётся dual-provider, а finalization добавляет independent provider только на циклах 4–5, когда он permitted and available. Covers E8.

### Slice 7 — DSL и skills parity

**Files/responsibility:** additive optional PipelineSpec `finalization_policy` validation и registered finalization route aliases; `.claude-memory/feedback_no_claude_p_headless.md`, AGENTS/CLAUDE/runtime docs; implementation-plan, dispatch, review skills; improve-skills/skill-creator verdicts, audits and baselines.

**Consumes:** executable finalization contract, GREEN Slice 3b receipts и explicit D-265-EPH-01 superseding user decision.

**Failing evidence:** skill предлагает cross-HEAD retained verification, unlimited cycle или model-owned lifecycle; finalization незаметно меняет dual-provider standalone Deep; existing schema-v1 specs отвергаются после добавления policy; review/dispatch всё ещё требует cmux при compiled `execution: ephemeral`; memory/AGENTS/CLAUDE запрещает разрешённый code-owned Claude adapter либо допускает arbitrary print mode.

**Minimal green:** DSL compiler owns route precedence/limits through an optional additive field with unchanged schema-v1 required set; old persisted specs and `examples/pipelines/document-project-v1.json` parse byte-for-byte unchanged; standalone Deep topology remains dual-provider. Skills distinguish ephemeral review from cmux-required continuable dispatch and describe exact terminal attempt/five-cycle rule. `improve-skills` first records protected cmux/review behavior and `defer`; approved D-265-EPH-01 product change then runs through `skill-creator`, atomically reconciles memory/AGENTS/CLAUDE/skills, preserves arbitrary-print prohibition, and records verdicts, quick validation, budgets and lint. Covers E9.

### Stability Gate S

Split implementation запрещён, пока:

- exact D-264-73 mixed-state regression green;
- no new flow reaches cross-HEAD machinery;
- event/retry matrix proves maximum one provider-facing effect;
- deadline never yields positive transition;
- full suite leaves no helper threads, reviewer owners or eventual files;
- complexity audit proves fewer writable authorities/markers and material negative diff in active recovery paths;
- full tests, honest coverage, acceptance and clean exact HEAD green.

Если S не достигнут в bounded implementation budget, release заканчивается lifecycle-only partial outcome; `split --dispatch` не включается.

### Slice 8 — Split skill и manifest preview

**Files/responsibility:** новый `skills/split/SKILL.md`, `schemas/split-manifest-v1.schema.json`, `scripts/harness/split_contracts.py`, preview facade, raw baseline cases, improve-skills verdict/audit evidence, tests/plugin generation.

**Consumes:** stable PipelineSpec and exact parent Outcome.

**Failing evidence:** fresh-context no-skill baseline не умеет стабильно вывести bounded manifest и stop conditions; hard-coded four children, preview effects, incomplete evidence/non-goal inheritance; router false-positive или skill без verifiable completion проходит quality gates.

**Minimal green:** до authoring сохранить raw baseline cases и fresh-context failure; инициализировать skill через system `skill-creator`; затем провести five-pass `improve-skills` audit с schema-v1 verdict, strong-intent/false-positive/completion/authority checks. Variable-count proposal, exact digests, bounded schema, one-child fallback, zero effects. Focused verification: `audit_skills.py --strict` plain и verdict-scoped, `make test-instruction-lint test-skill-budget test-codex-adapter`, `release-acceptance.py check`. Covers E10 и non-goal 11.

### Slice 9 — validation и bounded waves

**Files/responsibility:** `split_validation.py`, `split_execution.py`, bounded dispatch/workspace wiring; tests.

**Consumes:** valid manifest.

**Failing evidence:** все восемь классов достигают dispatch: stale parent/Outcome digest, uncovered evidence, overlapping ownership, dependency cycle, missing declared join, weakened inherited non-goal set, unregistered child pipeline и exceeded frozen budgets.

**Minimal green:** zero-effect rejection каждого из восьми классов в одной validation matrix, deterministic ready queue/waves, frozen child policy и transport-neutral provider route. Covers E11, E12.

### Slice 10 — workspace locality

**Files/responsibility:** existing task-session/cmux layout and cleanup modules; locality tests.

**Consumes:** child wave assignment.

**Failing evidence:** review opens in coordinator workspace or survives terminal attempt.

**Minimal green:** child-local executor/review, aggregate coordinator status, idempotent close/reap. Covers E12.

### Slice 11 — deterministic join

**Files/responsibility:** `split_join.py`, exact receipts, integration/acceptance wiring.

**Consumes:** terminal approved child attempts and branches.

**Failing evidence:** attention child, stale HEAD или changed order accepted.

**Minimal green:** manifest-order integration, typed conflict attention, parent evidence proof. Covers E13.

### Slice 12 — dogfood и release evidence

**Files/responsibility:** release readiness/notes/version metadata only after exact candidate.

**Consumes:** S plus Split/join integrated HEAD.

**Minimal green:** mandatory provider-neutral conformance gates для Claude print и Codex exec; один live single-model dogfood на разрешённом доступном route; второй live provider smoke только при typed availability. `usage-exhausted|availability-unknown` сохраняет E8 fallback receipt и single-provider resilience, но не позволяет пропустить conformance. Затем full ladder, bounded final review и resource-free close. Covers E14.

## 7.1. Parallel execution topology

Component work идёт четырьмя независимыми планами от exact base `3e391fc9e6aa48e1344520dbffdebba704312540`; ни один component task не активирует release path самостоятельно:

1. [[2026-08-05-145351-llm-obsidian-2-6-5-subplan-a-exact-head-review-attempt|LLM Obsidian 2.6.5 Subplan A — exact-HEAD review attempt]] — Slices 0–2, ReviewAttempt и legacy critical-path removal.
2. [[2026-08-05-145351-llm-obsidian-2-6-5-subplan-b-provider-events-and-delivery|LLM Obsidian 2.6.5 Subplan B — provider events and delivery]] — Slices 3–4, provider events/adapters/delivery boundary.
3. [[2026-08-05-145351-llm-obsidian-2-6-5-subplan-c-bounded-finalization-and-dsl|LLM Obsidian 2.6.5 Subplan C — bounded finalization and DSL]] — Slices 5–7, ledger, adaptive routes, backward-compatible DSL и existing-skill governance.
4. [[2026-08-05-145351-llm-obsidian-2-6-5-subplan-d-split-components|LLM Obsidian 2.6.5 Subplan D — Split components]] — Slices 8–11 как disabled components и tests; никакого dispatch activation до Stability Gate.
5. [[2026-08-05-145351-llm-obsidian-2-6-5-join-stability-activation-release|LLM Obsidian 2.6.5 Join — stability, activation and release]] — не запускается параллельно; принимает четыре terminal handoff, интегрирует в manifest order, выполняет Stability Gate S, включает Split и собирает E14.

У каждого A–D собственные production/test paths. Изменение чужих owned paths требует typed scope escalation. Общие integration files, version metadata, release notes и существующие broad matrix tests принадлежат только Join.

## 8. Test matrix

Обязательные свойства:

1. ReviewAttempt никогда не меняет exact HEAD.
2. `changes-requested` terminal и не создаёт child; новый HEAD создаёт новый cycle/attempt.
3. Duplicate/out-of-order/wrong-identity/gap events fail closed.
4. Claude print и Codex exec эмитят только закрытый ProviderEvent vocabulary; их documented subsets проходят одну provider-neutral conformance matrix и производят ровно один schema-valid result либо typed terminal failure без session resurrection.
5. Замена `anthropic → claude-print` на fake/alternate adapter не меняет PipelineSpec, attempt identity, ledger или result schema.
6. Auth preflight cannot silently switch subscription/ChatGPT login to API key or paid credits.
7. Physical process/surface disappearance converges through one durable idempotent close; stale `exiting` is reproduced and fixed.
8. Input до irreversible boundary может быть reconciled; после boundary не повторяется.
9. Interactive Stop без result вызывает максимум один same-HEAD submit-only effect; ephemeral exit без result вызывает zero submit/restart/fallback effects.
10. Timer/screen alone никогда не разрешает progress или restart.
11. Cycle reservation линейризуема, шестая попытка zero-effect.
12. Adaptive model matrix сохраняет explicit single-model, standalone Deep dual-provider topology и не материализует `finalization-independent` на циклах 1–3.
13. Split DAG/evidence/ownership/wave properties generated deterministically.
14. Join принимает только exact terminal receipts.
15. Focused и full suite используют один state path без real sleep как доказательства.

## 9. Rollout и rollback

1. Provider-neutral contract и Codex exec adapter входят capability gate после conformance/auth tests; Claude print входит в тот же gate только после D-265-EPH-01 native-account probe и атомарного Slice 7 rule reconciliation. После этого bounded reviews по умолчанию используют ephemeral profile, а continuable work сохраняет cmux.
2. New exact-HEAD attempt path включается для current review/finalization под internal capability gate.
3. Historical cross-HEAD state остаётся read-only; provider resume для него запрещён.
4. После parity и Stability Gate старый active continuation call graph удаляется/отключается, а не поддерживается вторым живым путем.
5. Rollback возвращается к последнему clean compatibility commit до активации нового flow; durable attempts/effects не удаляются и не replay'ятся.
6. CLIProxy adapter, новый store, public migration или universal scheduler требуют отдельного ADR/approval.

## 10. Stop conditions

- Пятый cycle не approved → finalization-budget-exhausted, без шестого review.
- Changed HEAD пытается использовать старую session → fail closed и новый attempt только через ledger.
- Ambiguous provider effect, event gap, unknown ownership или unverified auth/billing profile → attention/explicit interactive disposition без replay или hidden fallback.
- Stability Gate S не зелёный → Split dispatch не включается.
- Новый marker/authority/checkpoint нужен для happy path → design review; частный patch не принимается.
- Full-suite flake, helper leak или focused/full divergence → release blocked.
- Public migration/rewrite либо обязательный CLIProxy daemon нужен для продолжения → stop/ADR.
- Push, tag, publish и установка — отдельное пользовательское решение.

## 11. Definition of done

2.6.5 достигнут только когда все evidence items E1–E14, включая E4b, доказаны на одном exact clean HEAD. Lifecycle-only результат без Split честно частичный. Split без Stability Gate недопустим. RC-attention 2.6.4 остаётся историческим evidence и не переписывается. Финальный handoff показывает exact HEAD, lineage/cycles, provider availability decisions, event/retry receipts, удалённые active recovery paths, Split/join proof, полный gate и resource-free lifecycle.
