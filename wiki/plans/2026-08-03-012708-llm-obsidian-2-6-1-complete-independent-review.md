---
type: plan
title: "LLM Obsidian 2.6.1 — complete independent review"
address: c-000096
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-03
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
review_risk_profile: release
created: 2026-08-03
updated: 2026-08-03
tags:
  - plan
  - manual-save
---

# LLM Obsidian 2.6.1 — complete independent review

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Устранить слепые зоны split-axis review и сохранить работоспособность инженерного pipeline при доступности только одной разрешённой модели.",
  "desired_outcome": "LLM Obsidian предоставляет simple, deep и явно запрашиваемый full review: каждая независимая reviewer-сессия проверяет полный outcome и инженерный контракт, а code-owned routing вызывает только модели, разрешённые пользователем в начале задачи, без скрытого fallback или расширения pipeline authority.",
  "success_evidence": [
    {
      "evidence_id": "deep-complete-independent",
      "observable": "Deep review создаёт две независимые identity-bound сессии; каждая получает полный review contract, а не только spec или standards половину."
    },
    {
      "evidence_id": "full-explicit-cross-product",
      "observable": "Full review запускается только по явному запросу и создаёт точный cross-product разрешённых моделей и intent-first/engineering-first perspectives."
    },
    {
      "evidence_id": "single-model-safe-routing",
      "observable": "Sol-only и Fable-only policies завершают simple/deep/full без обращения к запрещённому runtime; недоступность единственной модели создаёт typed attention, а не скрытый fallback."
    },
    {
      "evidence_id": "review-contract-integrity",
      "observable": "Каждая lane проверяет Outcome Contract, specification, scope/non-goals, correctness, architecture, maintainability, test quality, security и применимые release risks; material finding любой lane блокирует approval и остаётся независимо атрибутированным."
    },
    {
      "evidence_id": "lifecycle-no-regression",
      "observable": "Exact-HEAD binding, callbacks, bounded verification, typed resolution, cleanup, telemetry, expert aliases и purpose-bound intent/implementation/release review остаются зелёными."
    },
    {
      "evidence_id": "bounded-live-proof",
      "observable": "После полной hermetic matrix один live Deep и один явно запрошенный live Full подтверждают provider wiring без большой дорогой live-матрицы."
    }
  ],
  "non_goals": [
    "General parallel/join в model-authored pipeline DSL.",
    "Новый scheduler, store, FSM, supervisor или lifecycle engine.",
    "Автоматический выбор Full review по risk profile без явного запроса пользователя.",
    "Скрытый вызов модели вне разрешённой task model-policy.",
    "Broad refactor или механическое дробление cohesive-файлов по универсальному line-count limit.",
    "Переписывание engineering skills без нового доказанного semantic gap.",
    "Импорт technology-specific tracker, GitHub/GitLab или upstream orchestration mechanics."
  ]
}
```

## 1. Результат релиза

Выпустить небольшой `2.6.1` из `/Users/zak/Projects/llm-obsidian`. Релиз меняет только полноту и маршрутизацию review; существующие harness, pipeline execution, engineering skills и task decomposition остаются основой.

Пользовательские режимы:

| Режим | Сессии | Выбор |
|---|---:|---|
| `simple` | одна holistic reviewer-сессия | существующий default |
| `deep` | две независимые complete-contract сессии | существующий `--deep` |
| `full` | `allowed models × 2 perspectives` | только явный `--full` или эквивалентный явный запрос |

`full` никогда не включается автоматически по risk profile. Architecture, migration или release risk могут рекомендовать его пользователю до запуска, но не расширяют review topology без явного согласия.

## 2. Review topology

### 2.1 Общий полный контракт каждой lane

Каждая initial и verification lane обязана независимо проверить:

1. исходный Outcome Contract и его `success_evidence`;
2. specification, plan/design и declared scope;
3. каждый `non_goal` на scope creep;
4. correctness и failure behavior;
5. architecture, ownership, dependency direction и maintainability;
6. test quality, честность coverage denominator и verification gaps;
7. security и permission boundaries;
8. применимые reliability, recovery, operability, compatibility, migration и release risks.

Lane не может пропустить категорию как «чужую ось». Perspective определяет порядок и основной adversarial угол, но не урезает denominator.

### 2.2 Deep

Deep создаёт две независимые сессии:

- `intent-first`: начинает с outcome/spec/scope, затем проходит весь engineering contract;
- `engineering-first`: начинает с code/test/architecture/security, затем независимо доказывает достижение outcome и отсутствие scope drift.

Default routing:

- `intent-first` → Fable `xhigh`;
- `engineering-first` → Sol `xhigh`.

Findings остаются раздельно атрибутированными. Для отображения допустима evidence-preserving группировка дубликатов, но запрещены голосование, усреднение severity и удаление material finding одной модели решением другой.

### 2.3 Full

Full строит cross-product:

```text
allowed review models × {intent-first, engineering-first}
```

При default `{Fable, Sol}` создаются четыре независимые сессии. При `Sol-only` или `Fable-only` создаются две. В single-model policy Deep и Full могут иметь одинаковое число сессий; runner обязан честно показать это до запуска, а не создавать дубли ради числа четыре.

## 3. Task model-policy

Model-policy фиксируется до первого provider effect и входит в immutable task/review identity.

Она содержит:

- разрешённые model aliases из `config/model-routing.toml`;
- назначение alias на reviewer roles/perspectives;
- ordered fallback pool, если пользователь его явно разрешил;
- `fallback=none` для строгого single-model режима.

Правила:

1. `Sol-only` никогда не вызывает Claude/Fable/Opus.
2. `Fable-only` никогда не вызывает Codex/Sol/Terra.
3. Отсутствующий или exhausted единственный route создаёт typed attention.
4. Автоматическая замена допустима только внутри заранее разрешённого ordered pool.
5. Expert override использует только зарегистрированные aliases; mismatch fail-closed.
6. Preview до запуска показывает режим, точные aliases, perspectives, число сессий, verification budget и fallback policy.
7. Same-session verification сохраняет исходную model-policy и не расширяет её.

## 4. Pipeline boundary

Не добавлять `parallel` или `join` в custom pipeline DSL.

Параллелизм достигается до исполнения pipeline:

```text
approved plan
  ├─ independent task A → complete sequential pipeline A
  ├─ independent task B → complete sequential pipeline B
  └─ independent task C → complete sequential pipeline C
```

Каждая задача получает отдельные worktree, identity, session и полный последовательный pipeline. Существующих registered model steps, typed decisions, bounded loops, verification, review, attention/stop и reap достаточно для утверждённой задачи. Review fan-out/fan-in остаётся code-owned `ReviewProgram`, а не model-authored общей capability.

## 5. Contract и migration boundary

Новые review profiles и axis identities внедрить атомарно:

- `scripts/review_contract.py`: modes, ordered lane identities и budgets;
- `scripts/model_routing_config.py` и `config/model-routing.toml`: `full` profile и role routes;
- task/review metadata schemas и parsers: captured model-policy и explicit-full intent;
- review request, prompt, callback, resolution, archive, telemetry и finalization contracts;
- CLI/facades и `skills/review/SKILL.md`;
- exhaustive axis-keyed tests.

Нельзя допустить промежуточный HEAD, где schema принимает `full`, но routing validator или lifecycle его отвергает.

Смена axis identities намеренно делает старые open review receipts непригодными для нового profile. Перед upgrade активные 2.6 review operations завершаются или останавливаются штатно. Historical archives остаются читаемыми; in-place migration и compatibility branches для продолжения старого review новым topology не добавляются.

## 6. Implementation slices

### Slice A — topology contract

**Responsibility:** versioned modes, lane roles, perspectives, budgets и aggregation invariants.

**Red evidence:** contract tests отклоняют `full`, complete-contract lane identities и single-model topology.

**Green:** pure topology compiler выдаёт точные ordered lanes для simple/deep/full и не выполняет provider effects.

### Slice B — routing policy

**Responsibility:** immutable allowed aliases, explicit fallback и role routing.

**Red evidence:** default resolver вызывает запрещённый runtime или silently expands single-model policy.

**Green:** route selection остаётся внутри captured policy; exhaustion создаёт typed attention.

### Slice C — complete reviewer packet

**Responsibility:** одинаковый полный review denominator и разные perspective ordering.

**Red evidence:** mutation test удаляет outcome или engineering category из одной lane, и deterministic assertion падает.

**Green:** каждый prompt/ContextPacket содержит все категории, exact purpose question и evidence pointers.

### Slice D — lifecycle integration

**Responsibility:** independent sessions, callbacks, finding attribution, common resolved HEAD verification, cleanup и archive.

**Red evidence:** duplicate identities, hidden rerouting, lost material finding, stale callback или orphaned surface.

**Green:** existing review gate владеет всеми lanes без второго scheduler/store/FSM.

### Slice E — user surface и skills

**Responsibility:** explicit `full`, model-policy preview, expert aliases и concise review instructions.

**Red evidence:** router/CLI принимает implicit Full либо documentation обещает topology, не совпадающий с compiled plan.

**Green:** one authoritative preset table; `improve-skills` scoped audit для `review` подтверждает invocation, hierarchy, steering, pruning и goal preservation.

### Slice F — release evidence

**Responsibility:** deterministic matrix, bounded provider smokes, version/docs и exact-HEAD release gate.

**Green:** все evidence IDs Outcome Contract имеют точные durable pointers.

## 7. Дешёвая verification matrix

До любого live review исчерпать hermetic combinations:

- mode: simple / deep / full;
- model-policy: default dual / Sol-only / Fable-only / explicit ordered fallback;
- perspective: holistic / intent-first / engineering-first;
- purpose: intent / implementation / release;
- stage: initial / shared resolved-HEAD verification / terminal;
- verdict: approve / minor-only / material finding / blocked;
- route state: available / unavailable / exhausted / forbidden fallback;
- callback state: valid / stale identity / duplicate / missing;
- cleanup: every lane complete / partial exit / exact owned recovery.

Обязательные свойства:

- каждый supported topology combination перечислен и проверен;
- provider/transport замокан, policy/state/resolution остаются реальными;
- любой material finding любой lane блокирует aggregate approval;
- forbidden runtime имеет ноль launch effects;
- `full` без explicit intent отклоняется до provider effect;
- exact HEAD, model-policy и profile digest входят в receipt identity;
- verification не создаёт новую модель или perspective вне исходного topology.

## 8. Live acceptance

После полной hermetic matrix:

1. Один видимый Deep на Fable + Sol подтверждает две полные независимые lanes и cleanup.
2. Один явно запрошенный Full подтверждает четыре default lanes и finding attribution.
3. Single-model paths доказываются deterministic adapter tests; дополнительный live smoke нужен только при реальном routing gap.

Не запускать большую provider matrix и не повторять green effects ради количества.

## 9. Release gate

- focused topology/routing/prompt tests;
- exhaustive review transition matrix;
- существующие harness, review-program, task contract, telemetry, archive, cleanup и model-routing suites;
- scoped `improve-skills` verdict и behavioral pressure для `review`;
- `make test` и `make test-harness-coverage`;
- `make acceptance-check`;
- vault validation, Codex adapter/MCP sync, upstream pin verification и `git diff --check`;
- один Opus intent-review этого плана;
- после implementation — independent implementation review и zero-fix release review на exact integration HEAD.

## 10. Stop conditions

- Не расширять релиз общим parallel DSL, новым scheduler или broad refactor.
- Если topology нельзя выразить через существующий review gate без второго lifecycle owner, остановиться и пересмотреть design.
- После трёх failed behavior-preserving fixes одного seam — architecture stop.
- Недоступная разрешённая модель не разрешает скрытый provider switch.
- Full без явного user intent не запускается.
- Green unit test, callback или отдельная approved lane не закрывают Outcome Contract без aggregate evidence.
