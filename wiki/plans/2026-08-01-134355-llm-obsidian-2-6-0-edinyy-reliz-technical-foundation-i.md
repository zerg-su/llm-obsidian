---
type: plan
title: "LLM Obsidian 2.6.0 — единый релиз technical foundation и skill intelligence"
address: c-000059
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-01
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-08-01
updated: 2026-08-01
tags:
  - plan
  - manual-save
---

# LLM Obsidian 2.6.0 — единый релиз technical foundation и skill intelligence

## 1. Результат и границы

Выпустить один крупный `2.6.0` из обычного `/Users/zak/Projects/llm-obsidian`, без публичного `2.5.2`.

Внутри релиза сохранить рекомендацию Fable как внутреннюю последовательность:

1. Technical foundation.
2. Foundation dogfood и baseline на точном SHA.
3. Параллельная разработка трёх skill-workstream.
4. Последовательная интеграция.
5. Paired comparison и реальные задачи.
6. Один общий fix batch.
7. Один финальный Fable + Sol deep review.

Swarm integration repair в релиз не входит. Namespace, branding и повторный Swarm merge-gate остаются необязательным housekeeping после импорта готового 2.6.

Не добавлять новый program-level scheduler, `parallel/join` или второй pipeline engine. Release plan служит coordinator ledger, а каждая его вершина исполняется существующим `engineering/change` или `engineering/fix` pipeline в отдельном worktree.

## 2. Technical foundation

### 2.1 Bounded `review-inspect`

Реализовать утверждённый code-owned CLI для Claude reviewer под `dontAsk`.

Разрешённые операции:

- clean status и exact HEAD;
- bounded recent log;
- diff stat, name list, `diff --check` и bounded patch;
- metadata или bounded content одного commit;
- branch/tag containment для валидированного SHA.

CLI обязан:

- принимать exact worktree, refs и repository-relative paths;
- иметь фиксированные лимиты вывода и timeout;
- запрещать Git mutation, remote operations, shell chaining, glob expansion, pipes, redirects и environment injection;
- не писать в product tree, Git metadata, telemetry или vault;
- оставить review callback outbox единственной writable review-точкой.

Провести один видимый Claude simple-review smoke. Финальный Fable review одновременно подтвердит работоспособность CLI для Claude reviewer.

### 2.2 Единый review contract и task metadata v4

Создать чистый `task-meta v4` для новых операций:

- удалить неиспользуемые `auto_resolve_severities` и `escalate_severities`;
- единственным severity vocabulary сделать `critical`, `important`, `minor`;
- `critical` и `important` считать material findings;
- `minor` сохранять в результате, но не открывать обязательный fix loop;
- не конвертировать активные v3 operations: upgrade preflight требует сначала завершить их;
- старые v1–v3 архивы остаются исторически читаемыми, но новые операции их не создают.

Добавить harness-owned resolution evidence по каждому material finding:

- `applied` — исправление присутствует на новом HEAD;
- `rejected` — обязательна bounded rationale;
- `out-of-scope` — обязательны rationale и durable follow-up pointer;
- `attempted` не является допустимым terminal disposition.

Verification получает previous finding IDs и fix delta. Независимые deep-review axes остаются раздельными и не rerank’ятся.

### 2.3 Review telemetry

Восстановить code-owned producers для:

- начала review round;
- принятия или отклонения callback;
- завершения round;
- количества `critical`, `important`, `minor`;
- iteration, axis, runtime, duration и terminal status.

Telemetry остаётся content-free: никаких findings, prompts, commands, snippets или page bodies.

Producer и `pipeline-stats` используют одну severity-константу. Ошибка telemetry никогда не меняет результат review pipeline.

### 2.4 Upstream refresh

Через protected research проверить текущий drift Superpowers и Matt Pocock Skills.

Порядок:

1. Проверить live upstream commit и релизное состояние.
2. Сравнить только релевантные general practices.
3. При изменении pins обновить snapshot bytes и manifest отдельным механическим commit.
4. Обновить adopt/adapt/reject judgement отдельным commit.
5. Не импортировать GitHub, installer, issue tracker, worktree или чужую orchestration-механику.
6. Новые идеи вне шести утверждённых skills записать как будущие кандидаты, не расширяя 2.6.

### 2.5 RT10 и foundation checkpoint

После установки локального adapter с foundation-ветки выполнить RT10 `distill-runbook`.

Все найденные mechanism defects исправить до baseline через `engineering/fix`. После зелёного RT10:

- прогнать foundation test gate;
- зафиксировать exact foundation SHA;
- больше не менять telemetry definitions и paired-task fixtures;
- этот SHA становится внутренним baseline, но не тегом и не публичным релизом.

## 3. Skill intelligence workstreams

### Обязательный meta-gate `improve-skills`

Перед созданием веток A/B/C прогнать все шесть изменяемых skills — `clarify`, `design`, `prototype`, `debug`, `tdd`, `review` — через локальный `improve-skills`:

Pre-branch запуск является строго audit-only: он фиксирует verdicts, baseline findings и behavioural expectations, но не изменяет skills. Каждый finding заранее назначается своей ветке-владельцу — A для `clarify`/`design`/`prototype`, B для `debug`/`tdd`, C для `review`; любые правки выполняются только внутри этой workstream. Frozen foundation SHA остаётся единым branch point и baseline для paired comparison.

- выполнить строгий структурный аудит `python3 skills/improve-skills/scripts/audit_skills.py --strict`;
- зафиксировать baseline findings и behavioural expectations до редактирования;
- использовать pinned `writing-skills` из Superpowers и `writing-great-skills` Matt Pocock только как reference evidence;
- исправлять только доказанные проблемы, не переносить чужую orchestration и не менять workflow semantics без отдельного решения.

В каждой workstream findings `improve-skills` становятся входом для focused изменений и тестов. После интеграции выполнить единый post-audit тех же шести skills и принять изменение только если:

- строгий аудит, instruction lint и skill-budget checks зелёные;
- paired behavioural comparison не ухудшил completion, число вмешательств, rounds или lifecycle stability;
- каждое изменение связано с исходным finding либо подтверждённым behavioural improvement;
- недоказанные улучшения удалены или вынесены за пределы 2.6.

`improve-skills` остаётся manual engineering meta-skill и не запускается автоматически в пользовательских workflow.

Все три ветки создаются от одного foundation SHA и разрабатываются параллельно. На ветках выполняются self-review и focused deterministic tests; отдельных Fable/Sol review не запускать.

### Workstream A — clarify, design, prototype

`clarify`:

- сначала исследует доступные repo facts;
- задаёт строго один material question за раз;
- включает brainstorming/domain modeling только при реальной неоднозначности;
- фиксирует в контексте термины, инварианты, противоречия, edge cases и ADR candidates;
- ничего не пишет до отдельной разрешённой vault transaction.

`design` и plan capture:

- начинают с ownership boundaries и test seams;
- содержат problem, non-goals, invariants, alternatives, data/control flow, recovery, rollout и rollback;
- запрещают placeholders и неопределённые интерфейсы;
- используют vertical slices по умолчанию;
- используют expand-contract только для wide migrations;
- различают unresolved fog и явно закрытый out-of-scope.

`prototype`:

- отвечает на один точный технический вопрос;
- production code остаётся неизменным;
- durable result содержит вопрос, evidence, decision, limitations и provenance;
- disposable code не превращается автоматически в production implementation.

### Workstream B — debug и TDD

`debug`:

- если дефект прямо наблюдается одной детерминированной командой, эта команда является допустимым red evidence;
- иначе до гипотез обязателен red-capable feedback loop;
- отсутствие воспроизводимого evidence приводит к явному evidence gap, а не к speculative fix;
- гипотезы ранжируются и формулируются фальсифицируемо;
- root cause устанавливается до product mutation;
- failed fix attempt считается только после изменения продукта и повторного запуска исходного repro;
- после трёх неудачных fix attempts срабатывает безусловный architecture stop.

`tdd`:

- до написания теста называет production change, который должен его сломать;
- проверяет observable behavior, а не наличие текста в source;
- regression test демонстрирует red и green на правильном seam;
- доказательство red на pre-fix состоянии выполняется в disposable worktree или на сохранённом base, без destructive reset рабочего checkout;
- docs, mechanical config и disposable prototypes используют эквивалентную пропорциональную проверку.

### Workstream C — review semantics

Обновить skill и harness вместе:

- implementer report считается непроверенным claim;
- reviewer не получает заранее заданного ограничения severity;
- rejection material finding требует typed ruling;
- verification классифицирует finding как addressed или not-addressed;
- новый material regression в fix delta присоединяется к открытому набору;
- наблюдение вне scope получает durable follow-up, но не расширяет текущий loop;
- simple review остаётся одной holistic session;
- deep review сохраняет независимые Fable/spec и Sol/standards-correctness-architecture-security lanes;
- axes не сливаются и не rerank’ятся.

Обобщённый nuance/exemption detector в 2.6 не входит. Hard safety, permission, lifecycle и external-effect prohibitions сохраняются.

## 4. Интеграция и циклы исправлений

### Merge strategy

1. Все workstreams обязаны иметь focused green tests и чистый diff.
2. Ветки мержатся в release branch последовательно, без cherry-pick:
   - Workstream A;
   - Workstream B;
   - Workstream C.
3. Общие router tests, version files, changelog и release docs меняются только на integration branch.
4. Конфликты решает coordinator по утверждённому плану; branch executors не переписывают чужие изменения.
5. После каждого merge запускается только затронутый suite; полный gate запускается после всех merge.

### Defect loop

После интеграции и dogfood findings собираются один раз в общий ledger:

- независимые дефекты разрешено исправлять параллельно в отдельных worktrees;
- дефекты общих contracts или review lifecycle исправляются последовательно;
- каждый fix использует `engineering/fix`;
- narrow repo-owned mechanism failure автоматически чинится по failure-repair contract;
- после трёх неудачных исправлений одного симптома дальнейшие патчи запрещены до пересмотра архитектуры;
- все fixes объединяются до финального cross-model review.

## 5. Evidence window и release gate

### Paired baseline

На foundation SHA выполнить две замороженные задачи:

1. Fix: `clarify → debug → tdd → review`.
2. Design: `clarify → design → prototype → review`.

После интеграции skill changes повторить те же задачи:

- на том же исходном fixture/base;
- с теми же runtime/model aliases;
- с одинаковыми verification profiles и budgets.

Сравнить:

- число пользовательских вмешательств;
- completion/attention outcome;
- число model/review rounds;
- wall-clock duration;
- findings по severity;
- callback/lifecycle failures;
- duplicate effects;
- соблюдение новых skill invariants.

### Real-task dogfood

После paired comparison выполнить четыре настоящие задачи:

- неоднозначная архитектурная задача через clarify/design;
- воспроизводимый product defect через debug/TDD;
- review с применённым и аргументированно отклонённым finding;
- disposable prototype с durable evidence result.

Задачи можно запускать параллельно только в независимых worktrees. Они не считаются доказательством универсального улучшения — вывод ограничивается проверенными классами задач.

### Final gate

На точном release-candidate HEAD должны пройти:

- полный hermetic test suite;
- acceptance check;
- vault validation;
- Codex adapter check;
- MCP sync check;
- upstream snapshot verification;
- `git diff --check`;
- bounded review-inspect live smoke;
- отсутствие unresolved callbacks и принадлежащих релизу orphan surfaces;
- paired comparison и четыре real tasks;
- version, changelog и release docs для `2.6.0`.

После этого запустить один deep review:

- Fable — spec;
- Sol xhigh — standards, correctness, architecture, security.

Все material findings исправляются одним общим batch. Затем выполняется максимум один scoped same-session verification round. Если после него остаётся material finding, релиз останавливается для отдельного решения, а не входит в новый автоматический цикл.

Push, tag и публикация релиза остаются ручными действиями пользователя.

## 6. Зафиксированные допущения

- Разработка ведётся в `llm-obsidian`, не в Swarm.
- Публичного `2.5.2` не будет; foundation SHA — внутренний checkpoint.
- Swarm namespace/branding repair не блокирует 2.6.
- Текущая custom pipeline grammar остаётся последовательной с bounded backward loops.
- Release-level параллелизм обеспечивают coordinator, isolated worktrees и существующие typed pipelines.
- Нового scheduler, `parallel/join`, nested pipeline или runtime DSL в 2.6 нет.
- Перед реализацией сохранённая версия этого плана проходит один Fable xhigh plan review; material замечания вносятся в план одним batch до dispatch.
