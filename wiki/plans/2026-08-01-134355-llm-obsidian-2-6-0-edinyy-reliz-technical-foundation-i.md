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
updated: 2026-08-02
tags:
  - plan
  - manual-save
---

# LLM Obsidian 2.6.0 — единый релиз technical foundation и skill intelligence

## 1. Результат и границы

Выпустить один крупный `2.6.0` из обычного `/Users/zak/Projects/llm-obsidian`, без публичного `2.5.2`.

### North Star

После 2.6 утверждённый пользователем outcome проходит от clarify через plan, dispatch, pipeline execution, review и reap без смыслового дрейфа. Harness детерминированно владеет identity, bounds, переходами и evidence flow; модели принимают только семантические решения и не могут подменить исходный outcome локальным proxy вроде зелёных тестов или формального task completion.

### Outcome Contract этого релиза

План остаётся единственным source of truth. Канонический bounded JSON-блок ниже имеет собственный digest, который не зависит от остальных редакционных изменений плана:

```json
{
  "schema_version": 1,
  "purpose": "Предотвратить потерю пользовательской цели и инженерного качества при декомпозиции больших планов, генерации кода и автономном выполнении pipeline.",
  "desired_outcome": "Каждая новая v4-задача сохраняет утверждённый пользователем outcome до независимо проверенного результата и typed reap disposition, а engineering skills и code-owned harness направляют реализацию к поддерживаемому коду, честным тестам и глубоким модулям без добавления второго orchestration или stop authority.",
  "success_evidence": [
    {"evidence_id": "paired-contract-stability", "observable": "Baseline и post-change paired fixtures используют один и тот же outcome-contract digest, route и verification budget."},
    {"evidence_id": "semantic-drift-detection", "observable": "Review отклоняет реализацию, которая локально зелёная, но не достигает desired_outcome или выходит за non_goals."},
    {"evidence_id": "typed-reap-disposition", "observable": "Wiki Summary v2 сохраняет achieved, partially-achieved или not-achieved вместе с bounded evidence IDs и residual-gap pointers."},
    {"evidence_id": "engineering-skill-completeness", "observable": "Exhaustive Matt/Superpowers capability matrix классифицирована; codebase-design, implementation-plan, TDD test quality, debug и review standards имеют router, semantic и pressure-scenario evidence."},
    {"evidence_id": "maintainable-harness-core", "observable": "Крупные mixed-responsibility harness-модули разделены по глубоким seams без изменения public contracts; code-quality audit не имеет release blockers."},
    {"evidence_id": "honest-cheap-verification", "observable": "Declared deterministic policy/state/validation core и поддерживаемые transition/decision matrices покрыты на 100%; whole-harness denominator остаётся честным, а provider/OS проверки ограничены минимальным live набором."},
    {"evidence_id": "legacy-isolation", "observable": "Активные v1-v3 операции не конвертируются и завершаются по прежнему frozen contract."}
  ],
  "non_goals": [
    "Отдельный пользовательский goal-скилл.",
    "Новый scheduler, pipeline engine или model-owned lifecycle authority.",
    "Импорт tracker, GitHub, subagent или orchestration механики upstream-репозиториев.",
    "Автоматическая миграция активных v1-v3 операций.",
    "Механическое дробление cohesive-кода, pass-through модули или coverage exclusions ради красивой метрики."
  ]
}
```

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
- разрешать reviewer’у bounded read-only navigation по exact ContextPacket root и exact worktree: предпочтительно через явные `--packet-root`/`--worktree`, а если нужен `cd` — только как code-owned переход в один из этих validated roots без shell chaining;
- материализовать обязательные packet inputs в формате, читаемом штатными `Read`/`Grep`, чтобы `.bin`-расширение не вынуждало reviewer использовать shell для обычного чтения;
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
- старые v1–v3 архивы остаются исторически читаемыми, но новые операции их не создают;
- для plan-mode v4 добавить обязательный `outcome_contract_sha256`, вычисленный из канонического Outcome Contract внутри утверждённой plan page;
- любое отсутствие, неоднозначность или drift contract digest до effect приводит к fail-closed, а после запуска — к typed attention, без переписывания цели моделью.

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
6. Новые идеи вне явно расширенного 2.6 inventory записать как будущие кандидаты; Outcome Contract является отдельным одобренным scope amendment, а не скрытым расширением.

### 2.5 Outcome Contract foundation

До foundation checkpoint реализовать только детерминированный transport и typed contracts:

- определить bounded schema с обязательными `desired_outcome`, `success_evidence`, `non_goals` и опциональным `purpose`;
- не добавлять `invariants` и `stop_conditions`: safety, permissions, forbidden actions, budgets, watchdog и terminal outcomes остаются существующей code-owned authority;
- хранить контракт как один канонический JSON-блок внутри plan page; отдельный свободно дрейфующий artifact запрещён;
- канонически сериализовать контракт и вычислять независимый `outcome_contract_sha256`;
- bind digest рядом с `approved_plan_sha256` в новых `task-meta v4` operations;
- доставлять контракт built-in semantic steps через ContextPacket, а custom pipelines — через существующий reserved `context_pointer` с `pointer_id=outcome-contract`; grammar v1, переходы и loop semantics не менять;
- code-owned проверки подтверждают только schema, bounds, digest, identity и evidence references; deterministic transitions не получают дополнительных model calls;
- выпустить `wiki-summary v2` с `outcome_disposition=achieved|partially-achieved|not-achieved`, bounded `outcome_evidence_ids` и `residual_gap_pointers`;
- обновить callback broker, task-summary validation, reap и архивное чтение так, чтобы новые v4 operations создавали v2 summary, а старые v1 summaries оставались читаемыми;
- fixture-команды не хранить сырьём в Outcome Contract: `success_evidence` описывает observable behavior и ссылается на зарегистрированный verification check/evidence ID.

Обязательные deterministic tests:

- canonical serialization и стабильный digest;
- rejection отсутствующего, дублированного, oversized или изменённого contract block;
- независимое обнаружение plan drift и outcome drift;
- v3 operation и frozen custom definition остаются byte-compatible и не получают новый pointer задним числом;
- reserved context pointer нельзя переопределить model-authored spec;
- Wiki Summary v2 принимает только declared disposition/evidence IDs и отклоняет свободные новые authority fields;
- ни один contract field не может разрешить запрещённый effect, продолжить typed stop или расширить permissions.

### 2.6 RT10 и foundation checkpoint

После установки локального adapter с foundation-ветки выполнить RT10 `distill-runbook`.

Все найденные mechanism defects исправить до baseline через `engineering/fix`. После зелёного RT10:

- прогнать foundation test gate;
- зафиксировать exact foundation SHA;
- больше не менять telemetry definitions, Outcome Contract schema/serialization и paired-task fixtures;
- заморозить contract-bearing paired fixtures до baseline: оба прогона используют идентичные contract bytes и digest;
- этот SHA становится внутренним baseline, но не тегом и не публичным релизом.

## 3. Skill intelligence workstreams

### Обязательный meta-gate `improve-skills`

Перед созданием веток A/B/C прогнать расширенный inventory — `clarify`, `design`, `prototype`, `save-plan`, `debug`, `tdd`, `review`, `reap` и `improve-skills` — через локальный `improve-skills`.

- выполнить строгий структурный аудит `python3 skills/improve-skills/scripts/audit_skills.py --strict`;
- зафиксировать baseline findings, behavioural expectations и goal-preservation expectations до редактирования;
- использовать pinned `writing-skills` из Superpowers и `writing-great-skills` Matt Pocock только как reference evidence;
- исправлять только доказанные проблемы, не переносить чужую orchestration и не менять workflow semantics вне явно одобренного Outcome Contract amendment.

Pre-branch запуск является строго audit-only: он фиксирует verdicts, но не изменяет skills. Каждый finding заранее назначается владельцу — A для `clarify`/`design`/`prototype`/`save-plan`, B для `debug`/`tdd`, C для `review`/`reap`, integration branch для `improve-skills`. `dispatch` и schema/harness wiring принадлежат technical foundation и проверяются как product change, а не как quality-only edit. Frozen foundation SHA остаётся единым branch point и baseline для paired comparison.

В каждой workstream findings `improve-skills` становятся входом для focused изменений и тестов. После интеграции выполнить единый post-audit того же inventory и принять изменение только если:

- строгий аудит, instruction lint и skill-budget checks зелёные;
- paired behavioural comparison не ухудшил completion, число вмешательств, rounds или lifecycle stability;
- каждое изменение связано с исходным finding либо подтверждённым behavioural improvement;
- недоказанные улучшения удалены или вынесены за пределы 2.6;
- goal-preservation pass подтверждает, что скилл получает общий outcome, не заменяет его локальным proxy и связывает completion claim с объявленным evidence.

`improve-skills` остаётся manual engineering meta-skill и не запускается автоматически в пользовательских workflow.

Все три ветки создаются от одного foundation SHA и разрабатываются параллельно. На ветках выполняются self-review и focused deterministic tests; отдельных Fable/Sol review не запускать.

### Engineering-quality closure — явное расширение 2.6

После behavior-preserving workstreams выполнить capability-gap аудит относительно pinned Matt Pocock Skills и Superpowers. Классифицировать каждую релевантную general-engineering capability как adopted, equivalent, missing, rejected или deferred; отдельно зафиксировать transfer decision adopt/adapt/reject. Technology-specific tracker, GitHub/GitLab, installer и upstream orchestration mechanics остаются non-goals.

Добавить model-invoked `codebase-design` и `implementation-plan`, общий technology-agnostic engineering-quality contract и progressive-disclosure test-quality reference. Обязательные semantics: YAGNI, domain modeling по необходимости, deep modules, locality, dependency direction, один change reason, consumes/produces slices, independent expectations, mutation sensitivity, mock only external adapters, ranked falsifiable debug hypotheses и независимый standards review. Structural text checks подтверждают wiring, а pressure scenarios подтверждают поведение.

Кодовый harness проходит cohesion audit. Примерно 200 строк является review signal, а не универсальным лимитом; multi-thousand-line functions и mixed policy/transport/orchestration являются release blockers. Extraction должна скрывать решения за маленькими durable interfaces, сохранять identity/effects и не создавать pass-through modules. После трёх неудачных behavior-preserving extraction attempts на одном seam обязателен architecture stop.

Verification имеет два честных denominator: 100% declared deterministic policy/state/validation core и всех поддерживаемых transition/decision combinations; отдельно whole-harness statement coverage с never-executed lines, явными adapter gaps и без exclusions ради метрики. Сначала выполняются дешёвые unit/matrix/mocked-adapter checks, затем один минимальный integration/live набор.

### Workstream A — clarify, design, prototype, save-plan

`clarify`:

- сначала исследует доступные repo facts;
- задаёт строго один material question за раз;
- включает brainstorming/domain modeling только при реальной неоднозначности;
- фиксирует в контексте термины, инварианты, противоречия, edge cases и ADR candidates;
- ничего не пишет до отдельной разрешённой vault transaction;
- на alignment gate формирует один Outcome Contract из согласованных решений, не придумывая неоговорённую цель;
- при материальной неоднозначности `desired_outcome`, evidence или non-goals продолжает интервью, а не создаёт placeholder.

`design` и plan capture:

- начинают с ownership boundaries и test seams;
- содержат problem, non-goals, invariants, alternatives, data/control flow, recovery, rollout и rollback;
- запрещают placeholders и неопределённые интерфейсы;
- используют vertical slices по умолчанию;
- используют expand-contract только для wide migrations;
- различают unresolved fog и явно закрытый out-of-scope;
- сохраняют `desired_outcome`, `success_evidence`, `non_goals` и опциональный `purpose` без семантического дрейфа;
- `save-plan` пишет канонический Outcome Contract в ту же vault transaction, что и plan page, и не создаёт второй goal artifact.

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
- docs, mechanical config и disposable prototypes используют эквивалентную пропорциональную проверку;
- red/green и локальная acceptance не считаются завершением, если они не подтверждают соответствующий `success_evidence` общего Outcome Contract.

### Workstream C — review и reap semantics

Обновить skill и harness вместе:

- implementer report считается непроверенным claim;
- reviewer не получает заранее заданного ограничения severity;
- rejection material finding требует typed ruling;
- verification классифицирует finding как addressed или not-addressed;
- новый material regression в fix delta присоединяется к открытому набору;
- наблюдение вне scope получает durable follow-up, но не расширяет текущий loop;
- каждый review boundary имеет явный purpose `intent`, `implementation` или `release`; внутри одного boundary simple review остаётся одной holistic session, а deep review сохраняет независимые Fable/spec и Sol/standards-correctness-architecture-security lanes;
- `intent` до дорогой реализации проверяет exact Outcome Contract, design/plan digests, capability dispositions и карту success evidence;
- `implementation` после implementation batch проверяет exact product HEAD, verification evidence и соответствие outcome через существующие simple/deep axes;
- `release` после интеграции проверяет exact merge HEAD, полный outcome-evidence map, принятые deviations и merge/refactor drift; этот boundary только approve или stop;
- внутри одного boundary не создаются дополнительные axes, surfaces или model calls сверх выбранного simple/deep preset; разные purpose-boundaries являются разными digest-bound lifecycle checkpoints, а не повторением одной проверки;
- axes не сливаются и не rerank’ятся;
- reviewer классифицирует каждый `success_evidence` как established, missing или contradicted и отдельно проверяет `non_goals` на scope creep;
- `reap` принимает только approved review evidence, пишет Wiki Summary v2 disposition и residual gaps и никогда молча не переписывает desired outcome.

Обобщённый nuance/exemption detector в 2.6 не входит. Hard safety, permission, lifecycle и external-effect prohibitions сохраняются.

### Сквозной goal-preservation contract

`improve-skills` получает пятый обязательный pass `goal preservation` для engineering skills:

- назвать общий outcome/input, который скилл обязан сохранить;
- назвать допустимый локальный subgoal и доказать, что он служит общему outcome;
- найти completion proxies — зелёный тест, чистый diff, закрытый ticket, callback или task summary — которые ошибочно могут быть приняты за пользовательский результат;
- требовать evidence именно для approved outcome, не добавляя новый model call в code-owned переход;
- выдавать `fix`, `no-change` или `defer` по существующей verdict discipline.

Полный semantic flow 2.6: `clarify` создаёт контракт → `design`/`save-plan` сохраняют → `intent` review подтверждает направление для дорогой задачи → `dispatch` валидирует и bind’ит digest → semantic steps получают общий outcome и локальный subgoal → `implementation` review проверяет exact product HEAD → `release` review проверяет exact integration HEAD → `reap` фиксирует typed disposition. Ни один этап не имеет права менять контракт; изменение цели требует нового пользовательского решения и нового approved digest.

### Outcome-preserving multi-stage review — approved amendment

Fable xhigh plan-only review одобрил amendment без findings на exact HEAD `4696e33`.

- Три purpose являются минимальным полезным набором: intent, implementation, release.
- Малые обратимые задачи могут схлопнуть intent в implementation. Architecture, migration, release и skill-integration обязаны пройти все три boundary.
- Harness выбирает обязательный профиль по approved task risk; модель может рекомендовать escalation, но не пропускать required boundary.
- Каждый boundary имеет отдельные ContextPacket, operation/receipt identity, input digest, вопрос и budget. Intent bind’ит Outcome Contract и plan/design digests; implementation — product HEAD и verification evidence; release — integration HEAD и полный outcome-evidence map.
- Результаты additive: прежний boundary не заменяет следующий и становится stale при изменении своего bound digest.
- Material finding на release не открывает поздний скрытый fix loop: релиз останавливается, а исправление начинает новый bounded implementation cycle.
- Low-risk migration сохраняет текущий terminal review как implementation boundary для существующих compiled tasks; новые purpose checkpoints добавляются schema/compiler/state-machine контрактами.
- Дешёвые policy, compiler, transition-matrix, mocked-adapter и crash-window checks предшествуют единственному bounded live release review.

## 4. Интеграция и циклы исправлений

### Merge strategy

1. Все workstreams обязаны иметь focused green tests и чистый diff.
2. Ветки мержатся в release branch последовательно, без cherry-pick:
   - Workstream A;
   - Workstream B;
   - Workstream C.
3. Общие router tests, `improve-skills` goal-preservation pass, version files, changelog и release docs меняются только на integration branch.
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

На foundation SHA выполнить две замороженные contract-bearing задачи:

1. Fix: `clarify → debug → tdd → review`.
2. Design: `clarify → design → prototype → review`.

До первого запуска сохранить canonical contract bytes, digest, fixture/base, expected evidence IDs и намеренно включить один локально зелёный, но goal-misaligned вариант для проверки semantic-drift detection. Foundation и post-change прогоны обязаны использовать те же bytes; изменение контракта аннулирует пару вместо тихого продолжения.

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
- соблюдение новых skill invariants;
- established/missing/contradicted outcome evidence;
- scope creep относительно `non_goals`;
- совпадение contract digest и корректность Wiki Summary v2 disposition.

### Real-task dogfood

После paired comparison выполнить четыре настоящие задачи:

- неоднозначная архитектурная задача через clarify/design;
- воспроизводимый product defect через debug/TDD;
- review с применённым и аргументированно отклонённым finding;
- disposable prototype с durable evidence result;
- хотя бы одна из задач должна иметь несколько локально успешных подшагов, но незавершённый desired outcome, чтобы проверить `partially-achieved` и отсутствие ложного completion claim.

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
- bounded review-inspect live smoke, включая чтение exact ContextPacket без shell workaround и validated read-only navigation по packet/worktree roots;
- Outcome Contract parser/digest/task-meta v4/context delivery/Wiki Summary v2 compatibility gate;
- негативный smoke, где зелёные локальные проверки не достигают desired outcome и review блокирует completion;
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
- Outcome Contract не является вторым plan, отдельным Goal-скиллом или lifecycle authority; он является bounded semantic input с отдельным digest внутри plan page.
- `purpose` опционален; `desired_outcome`, `success_evidence` и `non_goals` обязательны; stop/safety/permission authority остаётся в существующих typed harness contracts.
- Новые task-meta v4 operations требуют Outcome Contract и Wiki Summary v2; активные v1-v3 operations и исторические summaries не мигрируются.
- Перед реализацией сохранённая версия этого плана проходит один Fable xhigh plan review; material замечания вносятся в план одним batch до dispatch.
