# Architecture Workflow v1: от идеи до проверенной реализации

Этот гайд показывает полный проектный путь LLM Obsidian на одном небольшом, но
не искусственном примере: браузерной игре **Neon Snake**. Мы начнём с размытой
идеи, превратим её в Vision, архитектуру, ограниченные Design-решения,
проверяемые спецификации, контракты и ADR, построим Work Item DAG, подготовим
implementation plan и только после этого передадим работу в Harness.

Документ рассчитан на технического лидера, архитектора, продакт-инженера или
разработчика, который умеет читать Markdown и Git diff, но не обязан заранее
знать терминологию LLM Obsidian. Результат чтения — способность провести свой
проект через тот же workflow, понимать владельца каждого решения и отличать
принятую проектную истину от плана, runtime-состояния и текста, сгенерированного
моделью.

Гайд применим к Architecture Workflow v1, выпущенному в LLM Obsidian 2.8.0 и
присутствующему в стабильной линии 2.8.5. Он объясняет contracts, но не выдаёт
разрешений на запись в wiki, запуск моделей, network, dispatch, push, publish
или release. Такие эффекты всегда требуют полномочия от пользователя и
подчиняются [`AGENTS.md`](../AGENTS.md), [`CLAUDE.md`](../CLAUDE.md), skill и
Harness contracts.

## Перед началом и граница эффектов

Для чтения нужны только Markdown и базовое понимание Git. Для повторения полного
пути понадобятся установленный LLM Obsidian 2.8.5, Git, Python 3 и современный
браузер. Obsidian удобен для навигации, но scripts могут работать без открытого
GUI. cmux нужен для видимых dispatch, review и protected research lanes. npm,
package manager, browser CDN и game engine не нужны.

Примеры артефактов ниже — содержательные fragments, а не готовые payload для
`vault-write.py`. Не копируйте их в wiki в обход skill/writer contracts. Команды
models, dispatch, network, Git publish и release выполняются только после
соответствующего разрешения.

После реализации static application следует отдавать browser через локальный
HTTP, а не полагаться на `file://`, где native ES Modules ограничены browser
security policy. Полный локальный smoke выглядит так:

```text
python3 -m http.server 8000 --bind 127.0.0.1 --directory neon-snake
```

- **Предварительные условия:** текущая директория содержит готовую папку
  `neon-snake/`; Python 3 установлен; loopback port 8000 свободен.
- **Вход:** команда выше без подстановок и внешних URL.
- **Ожидаемый результат:** stdlib server слушает только IPv4 loopback
  `127.0.0.1` на port 8000; browser открывает
  `http://127.0.0.1:8000/`. Явный `--bind` не публикует каталог на LAN.
- **Независимая проверка:** `curl -I http://127.0.0.1:8000/` возвращает успешный
  HTTP status, а browser walkthrough подтверждает input/render/audio behavior.
- **Вероятная ошибка:** `Address already in use` означает конфликт loopback
  port, а не дефект игры.
- **Восстановление:** остановить точный server через Ctrl-C или выбрать свободный
  port и повторить URL; никаких процессов не убивать по догадке.
- **Источник поведения:** `python3 -m http.server --help`, browser console и
  принятые Spec/Contract проекта.

## Короткая версия: зачем нужен этот workflow

Самая дорогая ошибка AI-assisted разработки обычно рождается не в строке
кода. Она появляется раньше, когда неявное предположение превращается в
архитектурное решение, архитектурное решение — в задачу, а задача — в хороший,
но решающий не ту проблему diff.

Прямой путь «идея → implementation plan → код» смешивает вопросы разных
масштабов:

- зачем продукт существует и что сознательно не строим;
- из каких частей состоит система и кто за что отвечает;
- как устроен один рискованный механизм;
- какое поведение наблюдает пользователь или соседний компонент;
- какое решение долговечно и почему отвергнуты альтернативы;
- какие delivery outcomes можно выполнять независимо;
- какие конкретные файлы и тесты меняет один outcome;
- что уже выполняется прямо сейчас.

Architecture Workflow разделяет эти вопросы на артефакты с разными владельцами
и соединяет их явными зависимостями. Его главная гарантия:

> Нижележащий артефакт может обнаружить проблему выше по потоку, но не может
> молча решить её за владельца верхнего уровня.

Если implementation plan обнаруживает два независимых продукта вместо одного
Work Item, он возвращает **Upstream Gap** в `decompose`. Если декомпозиция
выясняет, что граница компонента не определена, вопрос возвращается в
`architecture` или `design`. Исполнитель не «додумывает архитектуру по месту».

Полный путь выглядит так:

```text
идея
  → clarify / Outcome Contract
  → Vision
  → Architecture
  → Design / research / prototype / codebase-design
  → Spec + Contract + ADR
  → accepted project knowledge
  → decompose: MAP → ACCEPT → MATERIALIZE
  → Work Item DAG
  → implementation-plan → save-plan
  → dispatch / Harness
  → review → reap
```

Это не обязательный waterfall. Маленький локальный фикс может сразу перейти к
`implementation-plan`. Research или prototype вызываются только когда есть
конкретный неизвестный факт. Полный маршрут нужен там, где цена неявного
решения выше стоимости нескольких страниц проектного знания.

## Ментальная модель: три frontier, а не один большой план

Workflow удерживает три независимые границы готовности.

| Frontier | Владелец | Главный вопрос | Выход |
|---|---|---|---|
| **Design Frontier** | `architecture` | Какой самый ценный проектный вопрос ещё не решён? | Принятое проектное знание или точный handoff в `design`/`research`/`prototype` |
| **Planning Frontier** | `decompose` | Какие outcomes уже можно планировать с учётом реальных зависимостей? | Валидный Work Item DAG и список готовых Work Items |
| **Execution Frontier** | Harness | Какой утверждённый план разрешено исполнять сейчас? | Typed lifecycle, exact-HEAD evidence, review и reap |

Wiki хранит смысл проекта, а не состояние планировщика задач. У Work Item нет
полей «процент выполнения» или «worker занят». `depends_on` описывает структуру
delivery, но не запускает scheduler. Runtime-состояние принадлежит Harness.

Разделение важно и для свежести. Изменение Vision не переписывает автоматически
все нижележащие страницы. Оно делает зависимые артефакты потенциально
устаревшими; владелец следующего этапа читает pins и revisions, показывает gap и
останавливает зависимую материализацию до явного решения.

## Откуда взялись идеи

Architecture Workflow v1 — локальная композиция проверенных идей, а не порт
одного upstream-проекта. Во время проектирования полные upstream-репозитории
читались как недоверенные reference trees: без установки их CLI, hooks,
зависимостей или plugin runtime. Точные upstream-адреса и закреплённые revisions
исследованного материала приведены в таблице ниже.

| Источник | Что взяли или адаптировали | Что не переносили |
|---|---|---|
| [arc42](https://github.com/arc42/arc42-template/tree/8dff0d9b1f96) | Целостное описание системы: контекст, ограничения, building blocks, runtime, deployment, cross-cutting concerns, risks | Обязательное заполнение большой универсальной формы |
| [C4 model](https://github.com/simonbrowndotje/c4model/tree/69d1eb704585) | Явные уровни приближения: system context → container → component; архитектура остаётся картой, а не свалкой деталей | Требование рисовать каждый уровень и превращать диаграммы в источник runtime-истины |
| [MADR](https://github.com/adr/madr/tree/835fc94baa37), [Rust RFCs](https://github.com/rust-lang/rfcs/tree/354518a8c902), [Kubernetes Enhancements](https://github.com/kubernetes/enhancements/tree/552c48ba8adf) | Контекст решения, alternatives, rationale, drawbacks, risks, readiness и test plan | Чужие governance-процессы, номера и обязательные шаблоны целиком |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec/tree/2826b8889e52) | Артефакты как dependency graph; следующий документ готов только после зависимостей; delta thinking для изменений | OpenSpec CLI, каталоги и schema как runtime-зависимость |
| [GitHub Spec Kit](https://github.com/github/spec-kit/tree/bf88c9f9a82f) | Разделение constitution/spec/plan/tasks, requirement coverage и independently valuable slices | Генераторы, команды и конкретная файловая раскладка |
| [BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD/tree/ea668933c805) | Диалоговое уточнение, перенос контекста между product/architecture/delivery и глубина процесса по размеру работы | Персоны, полный набор workflows и автоматический выбор решений за пользователя |
| [Ponytail](https://github.com/DietrichGebert/ponytail/tree/2ed6c52c9d7e), [shadcn/improve](https://github.com/shadcn/improve/tree/03369ee6d7ca) | YAGNI/stdlib-first, audit перед планом, самодостаточный план для слабейшего разумного исполнителя | Глобальная «персона» и один универсальный audit workflow |
| [Superpowers](https://github.com/obra/superpowers/tree/44c9b2d6e889982ac18c27d05a19fefe335194e1) и [Matt Pocock Skills](https://github.com/mattpocock/skills/tree/2ab958093e83e0ec752e6c1c5932da465bf23e0c) | TDD, evidence before completion, composable skills, чёткие completion criteria и проверка поведения skill | Непроверенное слепое копирование prompts; используются digest-pinned snapshots |

Точные pinned revisions и контроль целостности двух skill-наборов записаны в
[`references/upstream-skills/manifest.json`](../references/upstream-skills/manifest.json).
Commit здесь — идентичность источника; номер версии сам по себе не считается
достаточной provenance.

Локальная компиляция этих идей дала несколько собственных решений:

1. **Artifact roles не взаимозаменяемы.** Vision, Architecture, Design, Spec и
   Contract отвечают на разные вопросы.
2. **Semantic acceptance отделён от persistence.** Согласовать текст в
   разговоре не значит разрешить запись в vault.
3. **MAP, ACCEPT и MATERIALIZE разделены.** Декомпозицию можно исследовать и
   исправлять без преждевременного создания страниц.
4. **Один Work Item — один implementation plan.** File/TDD детализация не
   просачивается в проектную декомпозицию.
5. **Execution остаётся в Harness.** Wiki не становится второй базой runtime
   state.
6. **Freshness report-only.** Никакая модель не меняет lifecycle автоматически
   только потому, что заметила новую revision.

## Словарь артефактов и их физическое место

Проектное знание хранится под `wiki/projects/<project>/`. `<project>` —
безопасный lowercase key, например `neon-snake`. Названия страниц начинаются с
display name проекта, чтобы path-free wikilinks оставались глобально
однозначными.

```text
wiki/projects/neon-snake/
├── Neon Snake.md
├── Neon Snake Vision.md
├── Neon Snake Architecture.md
├── design/
│   └── Neon Snake Design — Deterministic Tick.md
├── specs/
│   └── Neon Snake Spec — Core Game Loop.md
├── contracts/
│   └── Neon Snake Contract — Simulation Snapshot.md
└── work/
    ├── Neon Snake Work Graph.md
    ├── Neon Snake WI-001 — Deterministic Core.md
    ├── Neon Snake WI-002 — Canvas Presentation.md
    └── Neon Snake WI-003 — Shell and Persistence.md
```

| Role | Отвечает на вопрос | Не должен содержать |
|---|---|---|
| **HUB** | Где вход в проект и какие артефакты каноничны? | Дубли полных документов |
| **VISION** | Что и зачем строим, для кого, с какими quality goals и non-goals? | Выбор классов, файлов и функций |
| **ARCHITECTURE** | Как система разделена целиком и где главные boundaries? | Детальный дизайн каждого алгоритма |
| **DESIGN** | Как решить один ограниченный технический вопрос? | Несколько несвязанных frontiers |
| **SPEC** | Какое наблюдаемое поведение обязательно? | Случайную реализацию как требование |
| **CONTRACT** | Что обязаны стороны конкретной границы? | Общие пожелания без owner и failure semantics |
| **ADR/DECISION** | Какой долговечный выбор принят и почему? | Незавершённый brainstorm без disposition |
| **WORK GRAPH** | Как выглядит decomposition целиком? | Runtime-статусы workers |
| **WORK ITEM** | Какой один bounded delivery outcome нужен? | Точный file-by-file implementation plan |
| **IMPLEMENTATION PLAN** | Какие файлы, interfaces, TDD slices и evidence реализуют один Work Item? | Новую продуктовую или архитектурную власть |

Авторитетный schema, lifecycle, pin и path-safety contract описан в
[`architecture-artifacts.md`](skill-references/architecture-artifacts.md).
Этот гайд объясняет его применение, но не заменяет.

## Сквозной пример: Neon Snake

Исходная идея пользователя:

> Хочу современную змейку в браузере: несложную, красивую, без установки
> движка. Можно с AI-generated картинками и звуком.

Фраза содержит как минимум восемь неоднозначностей. Что значит «современная»?
Нужен ли сервер? Как управлять с клавиатуры и телефона? Допустимы ли внешние
CDN? Должен ли replay быть воспроизводимым? Что происходит с generated assets
и их лицензиями? Поэтому мы не открываем редактор кода, а начинаем с outcome.

### 1. Clarify: получить Outcome Contract

`clarify` задаёт по одному вопросу, объясняет, почему ответ влияет на решение,
и не начинает реализацию. Для Neon Snake разговор может дать такие ответы:

- игра работает как static application из локального каталога через обычный
  HTTP server в современном desktop/mobile browser;
- первый релиз — score attack на одном экране, без аккаунтов и backend;
- управление: arrows/WASD и swipe;
- игровое поле и правила детерминированы seed и последовательностью input;
- пауза, restart, best score и reduced motion обязательны;
- звук генерируется Web Audio и может быть выключен;
- core не зависит от DOM, Canvas, clock или random globals;
- generated visual assets допустимы только как необязательная тема с
  provenance, лицензией/правами и человеческим review;
- никаких npm packages, bundler, CDN, Godot или большого engine;
- публикация в web и генерация ассетов не входят в текущую реализацию без
  отдельного разрешения.

Получившийся Outcome Contract может выглядеть так:

```json
{
  "schema_version": 1,
  "purpose": "Проверить полный Architecture Workflow на компактной браузерной игре.",
  "desired_outcome": "Neon Snake работает как static browser application, имеет детерминированное ядро, Canvas UI, клавиатурный и touch input, паузу, restart, best score, reduced motion и отключаемый звук.",
  "success_evidence": [
    "Одинаковые seed и inputs дают одинаковые snapshots.",
    "Игра проходит keyboard и touch walkthrough без network.",
    "Core tests выполняются без DOM и Canvas.",
    "Все shipped assets имеют manifest provenance и review disposition."
  ],
  "non_goals": [
    "Нет backend, аккаунтов, multiplayer и глобального leaderboard.",
    "Нет npm, bundler, CDN или game engine.",
    "Нет автоматической публикации или генерации внешних ассетов."
  ]
}
```

Это ещё не Vision: contract фиксирует смысл конкретной работы и критерии её
успеха. Перед переходом дальше человек подтверждает, что именно такой outcome
ему нужен.

### 2. Vision: зафиксировать «что» и «почему» проекта

Vision живёт дольше одного изменения. Он описывает продуктовую рамку, а не
текущую сессию.

**Neon Snake Vision — пример содержания**

- **Аудитория:** человек с телефоном или ноутбуком, желающий начать короткую
  сессию за несколько секунд.
- **Проблема:** классическая Snake понятна без обучения, но многие реализации
  либо визуально безликие, либо перегружены framework/toolchain.
- **Ценность:** мгновенный offline-capable arcade loop, который одновременно
  служит образцом testable browser game architecture.
- **Принципы:** instant start; deterministic rules; input parity; accessible by
  default; platform primitives first; assets are replaceable presentation.
- **Quality goals:** первая интеракция менее чем за секунду после загрузки
  локальных файлов; стабильные 60 render frames при отдельном fixed simulation
  tick; воспроизводимый replay; отсутствие network dependency.
- **Non-goals:** контентная платформа, live ops, monetization, user-generated
  levels и server authority.
- **Operating context:** evergreen browser с ES Modules, Canvas 2D, Web Audio,
  Pointer Events и localStorage.

Vision не утверждает, что нужен `GameEngine` class или файл `state.js`. Если
такое имя появляется здесь, слой уже слишком низкий.

### 3. Architecture: нарисовать целое и выбрать Design Frontier

`architecture` сначала читает существующий artifact graph, pins и revisions.
Он либо предлагает следующий high-value frontier, либо возвращает точный
handoff другому skill. Он не заменяет `design`.

Для Neon Snake целое разумно разделить так:

```text
Browser shell
├── Input adapters ───────┐
│   keyboard / pointer    │ commands
│                         ▼
├── Fixed-step scheduler → Deterministic simulation core
│                              │ immutable snapshot
│                              ├──────────────┐
├── Canvas renderer ◀──────────┘              │
├── Audio presenter ◀─────────────────────────┤ events
├── UI/accessibility presenter ◀──────────────┤
└── Persistence adapter ◀─────────────────────┘ score/settings

Optional asset theme → reviewed manifest → renderer
```

Границы компонентов:

- **Simulation core** принимает value-like state, normalized command и seedable
  random source; возвращает новый state и domain events. Никаких DOM, Canvas,
  audio, storage или wall clock.
- **Scheduler** переводит реальное время в ограниченное число fixed ticks. Он
  не меняет правила игры.
- **Input adapters** нормализуют keyboard и swipe в одинаковые commands.
- **Presenters** потребляют snapshot/events, но не являются источником game
  truth.
- **Persistence** хранит только settings и best score с versioned parsing;
  повреждённые данные дают безопасные defaults.
- **Asset theme** не может изменить collision, scoring или timing.

Контекст системы очень мал: один пользователь и browser platform. Container
level — static web application. Component level — перечисленные границы. Ни
сервер, ни database не рисуются «на будущее».

Первый Design Frontier: **как совместить fixed simulation tick, быстрый
пользовательский input и render cadence так, чтобы игра была детерминированной и
не делала неожиданный разворот на 180 градусов?**

### 4. Design: решить один bounded concern

`design` рассматривает alternatives и invariants только для выбранного
frontier.

**Альтернативы**

1. Двигать змейку прямо из `requestAnimationFrame`. Просто, но результат
   зависит от frame cadence; плохая основа для replay и тестов.
2. `setInterval` одновременно для simulation и render. Лучше отделяет cadence,
   но background throttling и drift смешивают real time с количеством ходов.
3. Fixed-step accumulator: `requestAnimationFrame` обслуживает render, scheduler
   накапливает elapsed time и вызывает bounded число pure `tick` операций.

**Решение:** вариант 3. Scheduler ограничивает catch-up, например четырьмя
ticks за frame, чтобы возвращение из background не создавало минуту мгновенной
симуляции.

**Invariants**

- `tick(state, command, random)` детерминирован для одинаковых входов;
- simulation direction меняется не чаще одного раза на tick;
- input queue хранит максимум два допустимых поворота;
- противоположное текущему или уже queued направлению отбрасывается;
- render interpolation не меняет authoritative grid state;
- pause очищает accumulator, но не state;
- food placement выбирается только из свободных cells;
- заполненное поле завершает run предсказуемым `board-complete` outcome.

**Failure model**

- большой elapsed interval clamp'ится;
- invalid persisted value не попадает в core;
- неизвестный command игнорируется adapter'ом;
- невозможность создать AudioContext отключает audio, но не game loop.

**Empirical gap:** если не ясно, достаточно ли Canvas 2D для выбранного glow на
старом мобильном устройстве, `architecture` выдаёт bounded handoff в
`prototype`: измерить только frame budget на disposable prototype. Результат
возвращается артефактом; prototype не становится production code.

### 5. Spec: описать наблюдаемое поведение

Spec не говорит «вызвать метод `advanceSnake()`». Он описывает, что обязано
наблюдаться и как это проверить.

**Core Game Loop — фрагмент спецификации**

| ID | Требование | Наблюдаемая проверка |
|---|---|---|
| NS-SPEC-001 | Новая игра начинается с заданных board size и seed | Snapshot содержит ожидаемую snake, direction, food и score |
| NS-SPEC-002 | Один tick без food перемещает head на одну cell и удаляет tail | Сравнить before/after coordinates |
| NS-SPEC-003 | Поедание food увеличивает snake и score, затем размещает food в свободной cell | Length +1, score + rule value, food не пересекает snake |
| NS-SPEC-004 | Столкновение с границей или собой завершает run | Status становится `game-over`, дальнейший tick не двигает state |
| NS-SPEC-005 | Обратный поворот отбрасывается | East + West input продолжает движение East |
| NS-SPEC-006 | Pause не меняет simulation state | Любое число animation frames без resume сохраняет snapshot |
| NS-SPEC-007 | Одинаковые seed и normalized inputs воспроизводят run | Полные snapshot sequences совпадают |
| NS-SPEC-008 | Reduced motion отключает необязательные trails/shake | Rules и input latency не меняются |

Один сценарий в Given/When/Then форме:

```text
Given: board 8×8, snake [(3, 4), (2, 4)], direction East,
       food (4, 4), score 0 and deterministic random source R
When:  one normalized tick is applied
Then:  snake is [(4, 4), (3, 4), (2, 4)], score is 10,
       food is placed on a cell not occupied by the snake,
       and event FoodEaten{score: 10} is emitted
```

Таблица создаёт requirement coverage. Каждый Work Item позже укажет, какие IDs
он удовлетворяет; тесты и review evidence смогут показать пропуск, а не
полагаться на ощущение полноты.

### 6. Contract: закрепить обязательства на границе

Spec и Contract похожи только тем, что оба проверяемы. Spec владеет поведением
продукта; Contract — взаимодействием конкретных сторон.

**Simulation Snapshot Contract**

- **Producer:** simulation core.
- **Consumers:** Canvas renderer, UI/accessibility presenter, persistence
  adapter и replay verifier.
- **Input:** предыдущий valid state, zero-or-one normalized command, explicit
  random source.
- **Output:** новый immutable snapshot и ordered domain events.
- **Snapshot fields:** schema version, tick number, board dimensions, ordered
  snake cells, direction, food cell or terminal null, score, status.
- **Ordering:** snake[0] всегда head; events расположены в причинном порядке.
- **Ownership:** consumers не мутируют snapshot; renderer не возвращает
  gameplay decisions.
- **Failure:** programmer-invalid state отклоняется на development/test seam;
  user-controlled persisted bytes валидируются adapter'ом до core.
- **Compatibility:** добавление optional presentation metadata допустимо;
  переименование или изменение смысла обязательного field требует новой schema
  version и migration/disposition.

Отдельный **Asset Manifest Contract** определяет, что shipped asset имеет
stable logical ID, local relative path, media type, source/provenance,
license-or-rights disposition, content digest, reviewer disposition и fallback.
Если provenance отсутствует, asset не проходит materialization. Это не делает
текст модели или картинку источником требований: авторитет остаётся у Vision,
Spec и человека, принявшего artifact.

### 7. ADR: записать долговечный выбор

Для Neon Snake существенен выбор platform stack.

**ADR: Canvas 2D + native ES Modules без build step**

- **Status:** accepted.
- **Context:** нужен мгновенно запускаемый учебный и production-like пример без
  установки тяжёлого engine или package toolchain.
- **Decision:** один `index.html`, CSS, native ES Modules, Canvas 2D, Web Audio,
  Pointer Events и localStorage. Core остаётся platform-neutral.
- **Alternatives:** Godot/Web export; Phaser; React + Canvas wrapper; DOM grid.
- **Rationale:** browser platform уже даёт нужные primitives; Canvas отделяет
  presentation от grid core; отсутствие dependencies уменьшает setup и supply
  chain surface.
- **Consequences:** команда сама владеет небольшим scheduler/input layer;
  browser compatibility проверяется напрямую; нет asset pipeline из коробки.
- **Revisit when:** нужны сложная physics, authoring tools, большие animated
  scenes или измеримо не хватает Canvas 2D.

ADR хранится в `wiki/decisions/`, а не внутри папки Work Items. Architecture
ссылается на решение, но не дублирует rationale.

### 8. Принять проектное знание и отдельно разрешить запись

На этом этапе важно не перепутать два действия:

1. **Semantic ACCEPT:** человек подтверждает, что содержание соответствует его
   намерению.
2. **Persistence authorization:** человек отдельно разрешает materialize или
   обновить страницы в vault.

До второго шага модель может показывать полный candidate artifact graph, gap
report и suggested changes, но не писать страницы. Запись выполняется только
через `scripts/vault-write.py`: с текущим hash при update, выделенным address,
provenance session и атомарным bookkeeping. Прямое редактирование `log`/`hot`
запрещено.

После записи `architecture` возвращает carrier с точными artifact identities,
revisions, pins, unresolved gaps и рекомендованным следующим frontier. Carrier
нужен следующему skill; произвольный prose-summary не заменяет его.

### 9. Decompose: MAP → ACCEPT → MATERIALIZE

`decompose` читает только принятое проектное знание и превращает его не в список
файлов, а в outcomes, ценность каждого из которых можно проверить отдельно.

#### MAP

Первый candidate graph Neon Snake:

| Work Item | Outcome | Covers | Depends on |
|---|---|---|---|
| WI-001 Deterministic Core | Pure simulation воспроизводит rules без browser APIs | NS-SPEC-001..005, 007 | — |
| WI-002 Browser Input and Scheduler | Keyboard/swipe дают normalized commands, fixed-step loop безопасно управляет ticks | NS-SPEC-005..007 | WI-001 |
| WI-003 Canvas Presentation | Snapshot виден как responsive Canvas game с reduced motion | NS-SPEC-006, 008 | WI-001 |
| WI-004 Shell, Audio and Persistence | Доступный UI, pause/restart, sound setting и best score работают с safe fallback | NS-SPEC-006, 008 | WI-001 |
| WI-005 Integrated Offline Game | Все части соединены в static application и проходят walkthrough | все | WI-002, WI-003, WI-004 |

`depends_on` — единственное авторитетное edge relation. Нельзя одновременно
объявить обратный `unblocks` как второй источник истины. Перед ACCEPT validator
проверяет same-project targets, отсутствие self/duplicate edges и cycles.

#### ACCEPT

Человек проверяет не только названия, но и delivery semantics:

- WI-001 даёт самостоятельное evidence — deterministic unit tests;
- WI-002 не владеет Canvas;
- WI-003 не меняет scoring;
- WI-004 может разрабатываться параллельно WI-002/WI-003 после WI-001;
- WI-005 — integration outcome, а не свалка незавершённых features;
- каждый spec ID покрыт, и лишнего продукта в graph нет.

Если выясняется, что generated asset pipeline должен стать отдельным продуктом
с внешним provider API, декомпозиция не придумывает provider contract. Она
возвращает Upstream Gap: уточнить Vision/non-goal и Architecture boundary.

#### MATERIALIZE

Только после отдельного разрешения `decompose` создаёт Work Graph и Work Item
pages через owned vault write path. Planning Frontier после materialization:
WI-001. После его принятой delivery следующие структурно готовые outcomes —
WI-002, WI-003 и WI-004. Это не означает, что они автоматически dispatched.

### 10. Implementation plan: один Work Item в file/TDD slices

Берём только WI-001. `implementation-plan` проверяет identity, accepted status,
revision pins, freshness и boundedness. Если carrier содержит WI-001 и WI-003
одновременно, planning останавливается: это снова работа `decompose`.

Язык следующего примера — сознательное исключение: пользователь этого
русскоязычного walkthrough явно просит показать normative implementation plan
по-русски. По умолчанию нормативный implementation plan пишется на английском,
если пользователь явно не запросил другой язык; user-facing разговор при этом
может оставаться русским. Если accepted Outcome Contract уже написан не на
английском, но явного language request нет, `implementation-plan` возвращает
его владельцу для amendment вместо молчаливого перевода evidence identity.

Пример самодостаточного implementation plan для WI-001:

**Outcome:** pure deterministic core реализует NS-SPEC-001..005 и 007, не
импортирует browser APIs.

**Files in scope**

```text
src/core/directions.js
src/core/random.js
src/core/game-state.js
src/core/tick.js
test/core/tick.test.js
test/core/replay.test.js
```

**Explicitly out of scope:** `index.html`, Canvas, DOM input, AudioContext,
localStorage, generated assets и deploy.

**Slice 1 — movement and collision**

- Consumes: NS-SPEC-001, 002, 004, Simulation Snapshot Contract.
- Failing evidence: `tick.test.js` создаёт exact state и проверяет move, wall
  collision, self collision и terminal no-op.
- Minimal green: pure `createInitialState` и `tick`, без renderer hooks.
- Refactor seam: direction/cell equality helpers только после green.
- Verification: Node built-in test runner выполняет файл без packages.
- Produces: core snapshot semantics, нужные Slice 2.

**Slice 2 — turn queue and food**

- Consumes: NS-SPEC-003, 005 и output Slice 1.
- Failing evidence: reverse turn rejected; food grows snake; next food never
  overlaps occupied cells; full board returns terminal outcome.
- Minimal green: injected deterministic random index, максимум два commands.
- Verification: focused core tests.
- Produces: complete single-tick rules.

**Slice 3 — replay evidence**

- Consumes: NS-SPEC-007 и outputs Slice 1/2.
- Failing evidence: две runs с одинаковыми seed/commands сравниваются snapshot
  by snapshot; изменённый command показывает чувствительность теста.
- Minimal green: tiny test helper, не production replay framework.
- Verification: full core suite и dependency scan proving no DOM imports.
- Produces: evidence для acceptance WI-001.

**Completion:** все listed spec IDs имеют green evidence; diff не содержит
browser integration; review не обнаруживает скрытого global randomness или
mutation входного state.

Такой plan достаточно точен для исполнителя без контекста текущего разговора,
но не диктует каждую строку. Он владеет файлами, interfaces, red/green order,
verification и stop boundaries.

### 11. Save-plan: сделать план каноническим

Разговорный план ещё не является исполняемым carrier. После подтверждения
`save-plan` сохраняет его как `type: plan` в `wiki/plans/`, с provenance и
ссылками на WI-001, конкретные revisions Spec/Contract/ADR и Outcome Contract.

Перед dispatch проверяют:

- plan описывает ровно один accepted Work Item;
- upstream pins не устарели;
- ownership файлов не пересекается с параллельной задачей;
- команды проверки известны и не требуют неразрешённых dependency/network
  effects;
- success evidence наблюдаем, а non-goals перечислены;
- пользователь явно разрешил исполнение, а не только сохранение.

### 12. Dispatch и Harness: исполнить, не переопределяя intent

`dispatch` передаёт approved plan в code-owned Harness. Harness создаёт
изолированный worktree и typed operation. Конкретная pipeline может быть
built-in или custom, но planning authority от этого не меняется.

Для WI-001 достаточно bounded engineering flow:

```text
preflight → reproduce/red → implement minimal green → focused verification
→ exact-HEAD summary → review → resolution if needed → reap
```

Исполнитель может выбрать имена локальных helper functions, но не добавить
multiplayer, framework или DOM dependency. Если тест требует изменить
Simulation Snapshot Contract, executor публикует finding и останавливается на
границе; он не правит contract молча.

Harness evidence отвечает на другие вопросы, чем проектные страницы:

- какой exact Git HEAD проверялся;
- какой verification profile выполнен;
- какой callback принят и сколько раз;
- какие review findings открыты и как они dispositioned;
- завершены ли resource cleanup и reap.

Эти данные не записываются как поля runtime status в Work Item. После успешного
reap в wiki возвращается компактный durable outcome с provenance и ссылкой на
реализацию; project knowledge обновляется отдельно, если delivery действительно
изменила принятую архитектуру.

## Generated assets без потери доверия

AI-generated asset — вход в обычный supply chain, а не особый источник истины.
Для Neon Snake можно сгенерировать background texture или optional icon set, но
без ассетов игра обязана иметь CSS/Canvas fallback.

Минимальный безопасный путь:

1. Vision разрешает optional themed assets и запрещает зависимость gameplay от
   них.
2. Spec описывает observable fallback, размеры, contrast и reduced-motion
   поведение, но не конкретную модель.
3. Contract требует manifest provenance, digest и review disposition.
4. Генерация — отдельный авторизованный external effect. Prompt и output не
   становятся проектным решением.
5. Человек проверяет права использования, нежелательный контент, читаемость,
   alpha edges и размер.
6. Оптимизированный локальный файл получает stable logical ID и digest.
7. Tests проверяют наличие manifest entry и fallback, а visual review —
   rendered result.

Если правовой статус output нельзя установить, честный outcome — rejected или
deferred asset, а не «вероятно можно». Workflow помогает сохранить этот разрыв
видимым.

## Как работать практически

Ниже — рекомендуемая последовательность для реального проекта. Формулировки —
примеры запросов агенту, а не shell-команды.

1. **Сформулируйте идею:** «Хочу Neon Snake в браузере без dependencies».
2. **Уточните outcome:** «Сделай clarify перед проектированием; один вопрос за
   раз». Примите Outcome Contract.
3. **Откройте проектный frontier:** «Используй architecture для проекта Neon
   Snake; сначала read-only artifact map».
4. **Разрешайте только нужные handoffs:** отдельный design для fixed tick,
   prototype для измерения Canvas, research для version-sensitive browser
   факта.
5. **Примите Vision/Architecture/Spec/Contract/ADR семантически.** Проверьте
   goals, non-goals, alternatives и evidence.
6. **Отдельно разрешите persistence.** Убедитесь, что агент использует
   `vault-write.py`, а не прямой edit wiki.
7. **Попросите decompose MAP.** Исправьте granularity и зависимости до записи.
8. **Примите DAG, затем отдельно MATERIALIZE.** Проверьте acyclicity и coverage.
9. **Выберите один Work Item из Planning Frontier.** Постройте для него
   implementation plan с owned files и TDD slices.
10. **Подтвердите и сохраните plan.** Сохранение не разрешает dispatch.
11. **Разрешите dispatch.** Следите за typed lifecycle, не дублируйте model или
    callback effects при неоднозначной ошибке.
12. **Проведите review и reap.** При material finding исправьте causal layer;
    после успешного reap закройте task resources.

## Проверка качества каждого перехода

Большинство дорогих сбоев происходит на переходах, поэтому проверять нужно не
только содержимое страниц.

| Переход | Доказательство, которое следует потребовать |
|---|---|
| Idea → Outcome Contract | Все неоднозначности с высоким blast radius решены; non-goals явны |
| Outcome → Vision | Долгоживущие product goals отделены от scope текущей работы |
| Vision → Architecture | Каждый quality goal имеет system boundary или честный gap |
| Architecture → Design | Handoff содержит один bounded concern, context и expected return |
| Design → Spec | Решение переведено в observable behavior, без утечки случайной реализации |
| Spec → Contract | Owner boundary, inputs/outputs/failures/compatibility названы |
| Knowledge → Decompose | Используются accepted и fresh revisions; ни одного file-level task |
| MAP → ACCEPT | DAG валиден, outcomes независимо ценны, requirements покрыты |
| ACCEPT → MATERIALIZE | Есть отдельное write authorization и атомарный writer path |
| Work Item → Plan | Ровно один outcome, exact files, TDD order и evidence |
| Plan → Dispatch | Plan сохранён, authority явна, worktree/route/profile определены |
| Verification → Review | Review видит exact tested HEAD и неизменный contract |
| Review → Reap | Findings dispositioned; summary accepted once; resources закрыты |

Для изменения самого workflow дешёвый deterministic prototype должен сначала
симулировать transition matrix много раз: доставка initial input, callback
acceptance exactly once, terminal summary, review continuation и cleanup. Реальный
dogfood дорогой и нужен после prototype/regression gates, а не вместо них.

## Ошибки и правильный уровень восстановления

### Implementation plan внезапно содержит два outcome

Не делить plan «на глаз». Вернуть Upstream Gap в `decompose`, уточнить Work Item
boundaries, снова пройти ACCEPT и только затем планировать один item.

### Spec требует поведения, которого нет в Vision

Не объявлять его «очевидным». Вернуть вопрос в Vision/Outcome Contract. После
принятого изменения обновить revision и проверить dependent freshness.

### Architecture и ADR противоречат друг другу

ADR — запись принятого durable choice. Сначала установить, какой source актуален.
Исправить owning artifact и pins, а не оставить две «частично правильные» версии.

### Materialization отклонена validator'ом

Это нормальный fail-closed result, не mechanism failure. Прочитать точную
причину: collision, unsafe path, malformed pin, cycle или stale upstream.
Исправить candidate в read-only phase и повторно получить ACCEPT/authorization,
если смысл изменился. Не править derived indexes вручную.

### Harness застыл между model step и callback

Не нажимать Enter и не перезапускать provider effect вслепую. Сначала read-only
inspect exact operation, callback outbox/receipt и accepted identity. Если
repo-owned transition нарушил documented behavior, действует failure-to-repair
contract: contain, diagnose, узкий regression fix, затем model-free continuation
с последней безопасной границы. Внешний эффект нельзя повторять без доказанного
zero acceptance.

### Review просит изменить Outcome Contract, но task не владеет им

Это authority gap, а не обычный code finding. Task останавливается; coordinator
либо materialize'ит amendment и создаёт свежую review boundary, либо даёт
bounded disposition. Executor не расширяет себе scope.

### Тест зелёный локально, но receipt относится к другому HEAD

Локальный console output полезен для диагноза, но не доказывает exact-HEAD
acceptance. Нужен новый зарегистрированный receipt для текущего чистого HEAD или
явно определённая parent-owned evidence boundary.

## Anti-patterns

- **Один мегадокумент вместо ролей.** Его невозможно независимо принять,
  обновить и проверить на freshness.
- **Architecture как список технологий.** Названия Canvas и localStorage не
  объясняют boundaries, ownership или failure model.
- **Spec как псевдокод.** Он цементирует первую реализацию вместо observable
  requirement.
- **Contract без владельцев.** Фраза «данные должны быть валидны» не определяет,
  кто валидирует и что происходит при ошибке.
- **ADR без alternatives.** Это журнал выбора, а не обоснование.
- **Work Item как файл.** `Создать renderer.js` не является delivery outcome.
- **DAG как scheduler.** Structural dependency не даёт права запускать worker.
- **ACCEPT равен write.** Согласие с идеей не разрешает mutation.
- **Generated prose равен authority.** Модель синтезирует; source и человек
  определяют принятую истину.
- **Review исправляет intent.** Reviewer находит gap, но не переписывает цель
  задачи сам.
- **Dogfood вместо transition tests.** Дорогой end-to-end run не локализует
  exactly-once и handoff defects.
- **«Все тесты зелёные» без exact subject.** Evidence без HEAD/profile/receipt
  не закрывает release claim.

## Минимальные шаблоны

Эти формы — prompts для мышления, не новые schema. При записи используйте
авторитетный artifact contract и обязательный frontmatter.

### Vision

```text
Audience / problem:
Desired value:
Goals:
Non-goals:
Principles:
Constraints:
Quality goals with measurable observations:
Operating context:
Open project questions:
```

### Architecture

```text
System context and external actors:
Containers / deployable units:
Components and owners:
Authoritative data and state:
Interfaces and trust boundaries:
Runtime and failure paths:
Cross-cutting concerns:
Accepted decisions and pins:
Risks / gaps / next Design Frontier:
```

### Design

```text
Bounded concern:
Context and constraints:
Alternatives:
Decision and rationale:
Invariants:
State / sequence / failure model:
Security and compatibility:
Evidence or prototype needed:
Consequences and return artifact:
```

### Spec

```text
Observable actor and trigger:
Preconditions:
Required behavior:
Failure / edge behavior:
Acceptance examples:
Requirement IDs:
Verification seam:
```

### Contract

```text
Boundary, producer and consumers:
Inputs / outputs:
Ordering / idempotency:
Validation ownership:
Failure semantics:
Compatibility and versioning:
Security / privacy:
Minimal valid example:
```

### Work Item

```text
Bounded delivery outcome:
User/system value:
Consumes accepted artifacts and revisions:
Covers requirement IDs:
Produces:
depends_on:
Acceptance evidence:
Explicit non-goals:
Upstream gaps:
```

### Implementation plan slice

```text
Owned files:
Consumes:
Failing evidence (red):
Minimal behavior (green):
Refactor seam:
Focused verification and expected observation:
Produces for the next slice:
Stop / escalation boundary:
```

## Чек-лист перед автономным исполнением

- [ ] Outcome Contract подтверждён человеком и не противоречит Vision.
- [ ] Architecture показывает целое, owners, state и failure paths.
- [ ] Каждый unresolved concern либо явно out of scope, либо имеет handoff.
- [ ] Spec содержит observable IDs; Contract содержит стороны и failure
  semantics; значимые alternatives записаны ADR.
- [ ] Все потребляемые artifacts accepted, pinned и fresh для текущего stage.
- [ ] Work Item graph same-project, без cycles/self/duplicate edges.
- [ ] Выбран ровно один bounded Work Item.
- [ ] Implementation plan самодостаточен, называет файлы, TDD slices, evidence
  и запрещённые эффекты.
- [ ] Persistence и dispatch получили разные явные разрешения.
- [ ] Harness route, model/profile, worktree и exact success evidence известны.
- [ ] Review проверяет тот же HEAD и Outcome Contract.
- [ ] Reap закрывает lifecycle и возвращает durable summary без превращения wiki
  в runtime store.

## Источники истины и дальнейшее чтение

Локальные contracts и executable evidence имеют приоритет над этим
объяснением:

- [`Architecture artifact contract`](skill-references/architecture-artifacts.md)
  — роли, identity, paths, lifecycle, pins, freshness, DAG и handoff;
- [`architecture` skill](../skills/architecture/SKILL.md) — Design Frontier и
  orchestration;
- [`decompose` skill](../skills/decompose/SKILL.md) — MAP, ACCEPT, MATERIALIZE и
  Planning Frontier;
- [`implementation-plan` skill](../skills/implementation-plan/SKILL.md) — один
  outcome в owned TDD slices;
- [`Architecture Workflow v1 pressure evidence`](acceptance/architecture-workflow-v1-pressure.md)
  — поведенческая проверка workflow;
- [`v2.8.0 release notes`](releases/v2.8.0.md) — состав исходного релиза;
- [`Russian handbook`](ru/index.md) — установка, operations, pipelines, review и
  troubleshooting;
- [`Unattended pipeline operations`](unattended-pipeline-operations.md) —
  эксплуатация Harness после dispatch.

Upstream-источники полезны для происхождения идей, но не переопределяют локальный
contract. Если upstream и текущий repository behavior расходятся, сначала
проверяют schema, tests, skills и release evidence LLM Obsidian, затем явно
фиксируют disposition: adopt, adapt, reject или defer.

## Итог

Хороший Architecture Workflow не делает документации больше ради документации.
Он переносит дорогие решения туда, где их дешевле увидеть, обсудить и
перепроверить. Vision защищает смысл продукта, Architecture — целостность
системы, Design — качество одного трудного решения, Spec и Contract —
проверяемость, ADR — память о trade-off, Work Item DAG — delivery boundaries,
implementation plan — исполнимость, а Harness — честное evidence.

На примере Neon Snake это особенно видно: маленькая игра не нуждается в
framework или сервере, но всё равно выигрывает от детерминированного core,
явных presenter boundaries, observable requirements и provenance ассетов.
Именно способность использовать ровно необходимую глубину — от короткого fix
до полного проектного графа — делает workflow практичным, а не ритуальным.
