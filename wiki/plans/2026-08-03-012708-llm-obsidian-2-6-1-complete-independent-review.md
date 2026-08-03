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
  "purpose": "Минимально скорректировать существующую review topology без создания нового lifecycle или policy subsystem.",
  "desired_outcome": "LLM Obsidian сохраняет Simple review, использует в default Deep две независимые holistic sessions Fable и Sol, при single-model Deep разделяет review одной модели на intent и engineering sessions, а явно запрашиваемый Full объединяет обе модели и обе специализации в четыре sessions; Full single-model отсутствует и fail-fast предлагает Deep.",
  "success_evidence": [
    {
      "evidence_id": "deep-adaptive-topology",
      "observable": "Default Deep создаёт две независимые holistic sessions Fable и Sol, каждая проверяет всё; single-model Deep создаёт intent и engineering specialist sessions выбранной модели и не вызывает другой runtime."
    },
    {
      "evidence_id": "full-explicit-dual-model-grid",
      "observable": "Full запускается только по явному запросу и создаёт ровно четыре lanes: intent и engineering отдельно для Fable и Sol; single-model Full не существует."
    },
    {
      "evidence_id": "single-model-safe-topology",
      "observable": "При доступности одной модели Simple создаёт одну holistic session, Deep — две specialist sessions intent и engineering; Full отклоняется до provider effect с предложением использовать Deep."
    },
    {
      "evidence_id": "aggregate-findings-preserved",
      "observable": "Material finding любой Deep или Full lane блокирует aggregate approval; findings остаются независимо атрибутированными без голосования и усреднения."
    },
    {
      "evidence_id": "existing-lifecycle-reused",
      "observable": "Exact-HEAD binding, same-session verification, callbacks, resolution, archive и cleanup проходят через существующий review gate без нового scheduler, store или FSM."
    }
  ],
  "non_goals": [
    "Новая task model-policy, fallback pools или automatic provider substitution.",
    "Автоматический выбор Full по risk profile или любому другому сигналу.",
    "Новый review lifecycle, scheduler, store, FSM, migration framework или telemetry vocabulary.",
    "General parallel/join в PipelineSpec или custom pipeline DSL.",
    "Broad refactor, file decomposition или переработка engineering skills.",
    "Большая combinatorial live matrix или повторение уже зелёных provider effects."
  ]
}
```

## 1. Существующий baseline

Релиз делается в `/Users/zak/Projects/llm-obsidian` как небольшой patch поверх 2.6.0.

Уже существуют и не перепроектируются:

- `simple` и `deep` presets;
- alias-backed `--runtime`, `--model`, `--effort` override;
- alias-backed выбор одного review route и существующие default Fable/Sol routes;
- независимые reviewer sessions, aggregate verdict, exact-HEAD verification, callbacks, resolution и cleanup;
- purpose-bound intent, implementation и release review.

Следовательно, 2.6.1 меняет только compiled topology, reviewer responsibility и явный пользовательский флаг `--full`. Default Deep использует model diversity, single-model Deep — specialist depth, Full объединяет оба свойства. В этом плане single-model означает только существующий явный alias-backed override `--runtime`/`--model`, который ограничивает review одной зарегистрированной моделью; это не новый persisted policy object.

Публичные lane identities provider-stable: `anthropic-*` и `openai-*`. Имена Fable, Opus и Sol остаются только внутренними model-routing aliases и могут меняться без изменения callback/archive schema. Эта поправка согласована после plan review; она не меняет число sessions или ответственность lanes.

## 2. Public review modes

| Режим | Default Fable + Sol | Single-model policy | Ответственность |
|---|---:|---:|---|
| `simple` | 1 session | 1 session | одна holistic-проверка |
| `deep` | 2 holistic sessions | 2 specialist sessions | diversity двух моделей либо intent/engineering depth одной |
| `full` | 4 specialist sessions | недоступен | diversity двух моделей × intent/engineering depth |

`--full` запускается только явно и никогда не выводится из risk profile. Если разрешена только одна модель, Full отклоняется до provider effect с понятным предложением использовать Deep single-model. Автоматически превращать Full в Deep запрещено.

### 2.1 Deep

Default Deep запускает две независимые holistic sessions:

- Fable holistic;
- Sol holistic.

Каждая модель самостоятельно проверяет полный Outcome Contract и engineering contract. Модели не делят ответственность, не голосуют и не усредняют findings.

При single-model policy Deep вместо дублирования одной holistic prompt разделяет review на две specialist sessions выбранной модели:

- `intent`: Outcome Contract, success evidence, specification, scope и non-goals;
- `engineering`: correctness, failure behavior, architecture, ownership, maintainability, tests, security и применимые recovery/compatibility/release risks.

Обе specialist sessions получают общий ContextPacket и вместе покрывают полный denominator. Другой runtime не вызывается.

### 2.2 Full

Full объединяет model diversity и specialist depth:

```text
{Fable, Sol} × {intent, engineering}
```

Точная topology:

1. Fable — intent;
2. Fable — engineering;
3. Sol — intent;
4. Sol — engineering.

Каждая session отвечает только за назначенную ось. Все четыре результата обязательны; material finding любой lane блокирует approval. Findings разных моделей не голосуются и не усредняются.

Full single-model отсутствует: его полезная topology уже представлена Deep single-model. При недостатке второй модели preflight fail-fast, не создавая provider effects.

## 3. Минимальный implementation delta

### Slice A — topology contract

Добавить `full` в существующий review preset и compile точных ordered lane identities:

- Simple: `<selected-alias>-holistic`;
- Deep default: `anthropic-holistic`, `openai-holistic`;
- Deep single-model: `<alias>-intent`, `<alias>-engineering`;
- Full default: `anthropic-intent`, `anthropic-engineering`, `openai-intent`, `openai-engineering`.

Не добавлять отдельный model-policy object. Использовать существующие aliases и explicit override.

### Slice B — prompts и responsibility

**Default Deep:**

- Fable получает holistic prompt и проверяет весь review contract: outcome, specification, scope, correctness, architecture, maintainability, tests и security.
- Sol получает такой же полный holistic scope и независимо проверяет те же категории.

**Deep single-model:**

- `<alias>-intent` проверяет только Outcome Contract, success evidence, specification, scope и non-goals.
- `<alias>-engineering` проверяет только correctness, failure behavior, architecture, ownership, maintainability, tests, security и применимые recovery/release risks.

**Full:**

- Fable получает отдельные `intent` и `engineering` prompts из Deep single-model.
- Sol получает те же две отдельные prompts.
- В результате работают четыре specialist sessions с одинаковыми границами ответственности у обеих моделей.

Каждый prompt с engineering responsibility — Simple holistic, обе default Deep holistic prompts, Deep single-model engineering и обе Full engineering prompts — содержит authoritative pointer на `docs/skill-references/engineering-quality-contract.md` и правило repository-specific overrides. Intent-only prompts этот checklist не дублируют. Все sessions получают один общий ContextPacket; prompt определяет, какую часть этого контекста reviewer обязан оценивать.

### Slice C — routing и user surface

- добавить явный `--full` в current review и dispatch review preset;
- запретить комбинацию `--deep --full`;
- Default Deep запускает `fable-holistic` и `sol-holistic`;
- single-model Deep запускает `<alias>-intent` и `<alias>-engineering`;
- Full компилирует intent и engineering lanes для Fable и Sol;
- Full при существующем explicit alias-backed `--runtime`/`--model` override отклоняется до provider effect и предлагает Deep;
- preview показывает mode, exact aliases, responsibilities и точное число sessions.

Никакого implicit Full, Full single-model, нового routing profile, fallback или скрытой дополнительной модели.

### Slice D — focused tests и документация

Обновить только review skill, preset table и тесты изменённых seams. Не проводить общий skill rewrite.

## 4. Acceptance matrix

Обязательные дешёвые проверки:

1. Simple остаётся одной holistic lane выбранной модели.
2. Default Deep компилирует ровно `anthropic-holistic` и `openai-holistic`.
3. Simple holistic и обе default Deep holistic prompts содержат полный outcome checklist, engineering checklist, authoritative pointer на `docs/skill-references/engineering-quality-contract.md` и правило repository-specific overrides.
4. Single-model Deep компилирует `<alias>-intent` и `<alias>-engineering`; другой runtime имеет ноль launch effects.
5. Intent и engineering prompts имеют непересекающуюся ответственность и вместе покрывают полный denominator.
6. Full без explicit flag не компилируется и не запускается.
7. Full компилирует ровно четыре specialist lanes: intent и engineering для Fable и Sol.
8. Full при существующем explicit alias-backed `--runtime`/`--model` override отклоняется до provider effect с typed рекомендацией Deep и не создаёт сокращённую Full topology.
9. Material finding любой Deep или Full lane блокирует aggregate approval.
10. Duplicate/stale callback, verification budget, exact HEAD и cleanup сохраняют существующее поведение.
11. Existing Simple lifecycle regressions остаются зелёными; изменённые Deep/Full topology покрыты focused contract tests без compatibility branches.
12. Single-model branch активируется только существующим explicit alias-backed override; новый persisted model-policy object, fallback pool или routing profile не создаётся.

Provider runtime замокан во всех topology tests. После зелёных unit/harness suites достаточно одного видимого explicit Full smoke; повторять большую live matrix не нужно.

## 5. Вероятный ограниченный набор файлов

Точный список подтверждается red tests, но ожидаемый seam ограничен:

- `scripts/review_contract.py`;
- `scripts/harness/workflows/review_gate_contracts.py` и review request contracts;
- `scripts/task_review_request.py`;
- current/dispatch flag parsing и review-mode validation;
- `skills/review/SKILL.md`;
- focused review contract, routing, prompt и lifecycle regression tests.

Если реализация требует нового scheduler/store/FSM, отдельной migration subsystem или широкого изменения task lifecycle, остановиться: это означает, что выбран не минимальный seam.

## 6. Release gate

- focused topology, prompt, routing и aggregation tests;
- существующие review gate, callback, resolution, cleanup и model-routing suites;
- `make test` и `make test-harness-coverage`;
- `make acceptance-check`;
- vault validation и `git diff --check`;
- один explicit Full live smoke;
- implementation review на exact integration HEAD;
- zero-fix release review.

## 7. Stop conditions

- Full без явного user intent не запускается.
- Single-model Simple/Deep не обращаются к другой модели.
- Default Deep остаётся двумя независимыми holistic reviews; single-model Deep остаётся intent/engineering split.
- Full существует только как явный dual-model specialist grid.
- Ни одна specialist session не расширяет responsibility до holistic verdict.
- Нельзя создавать второй lifecycle owner ради новой topology.
- После трёх неудачных behavior-preserving fixes одного seam — architecture stop.
