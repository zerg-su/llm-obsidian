---
type: plan
title: "LLM Obsidian 2.7 — project-scoped task programs"
address: c-000106
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-03
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-08-03
updated: 2026-08-03
tags:
  - plan
  - manual-save
---

# LLM Obsidian 2.7 — project-scoped task programs

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Добавить project-scoped память, задачи и автоматическую декомпозицию поверх хорошего 2.6 substrate с минимальными изменениями существующего harness и pipeline lifecycle.",
  "desired_outcome": "Один LLM Obsidian работает с несколькими внешними проектами, хранит документацию и задачи раздельно по Project Spaces и превращает большую цель в однократно утверждённую программу обычных dispatch-задач; существующие OperationStore, dispatch, последовательные pipelines, callbacks, verification, review и reap переиспользуются без второго orchestration engine.",
  "success_evidence": [
    {
      "evidence_id": "project-scoped-memory-and-tasks",
      "observable": "Каждая документация, task, plan, TaskGraph и result принадлежат одному Project Space; глобальный экран только агрегирует project indexes и не владеет общим authoritative backlog."
    },
    {
      "evidence_id": "existing-task-pipelines-reused",
      "observable": "Каждая program node исполняется существующим dispatch и полным последовательным pipeline 2.6; callbacks, watchdog, recovery, verification, review и reap не получают альтернативного lifecycle owner."
    },
    {
      "evidence_id": "thin-program-coordination",
      "observable": "Program coordination переиспользует существующие operation/request UUID, plan identity и harness reconciliation; новый публичный project_id, graph_id, store, scheduler или FSM не вводится без доказанной неоднозначности."
    },
    {
      "evidence_id": "bounded-approved-decomposition",
      "observable": "Модель предлагает независимые owned tasks и зависимости, пользователь один раз утверждает программу, а код до provider effects проверяет DAG, ownership overlap, budgets и bounded concurrency."
    },
    {
      "evidence_id": "integration-closes-root-outcome",
      "observable": "После child tasks обычная integration task собирает результаты, запускает существующие verification и multi-stage review и доказывает Outcome Contract исходной цели по typed evidence."
    }
  ],
  "non_goals": [
    "Переписывание или замена хорошего 2.6 harness, review lifecycle или built-in pipelines.",
    "Добавление parallel/join, dynamic graph mutation или произвольного кода внутрь PipelineSpec.",
    "Новый общий scheduler, OperationStore, supervisor, FSM или второй lifecycle owner.",
    "Обязательные новые публичные project_id или graph_id, если достаточно project page address, repo registration, plan identity и существующих operation UUID.",
    "Глобальный authoritative task backlog, смешивающий задачи разных проектов.",
    "Technology-specific GitHub, GitLab, issue tracker или marketplace integrations."
  ]
}
```

## 1. Главный принцип релиза

2.6 и 2.6.1 считаются качественным substrate, а не legacy для переделки. 2.7 добавляет только отсутствующий project/program layer и максимально переиспользует существующие seams.

Порядок предпочтений:

1. Скомпилировать новое поведение из существующих dispatch, harness operations и sequential pipelines.
2. Расширить существующий контракт минимальным полем или derived view.
3. Добавлять новую durable abstraction только после доказанной неоднозначности, которую нельзя выразить существующими identities.
4. Остановиться, если реализация требует второго scheduler/store/FSM или широкого изменения 2.6 lifecycle.

Связанные основания: [[2026-08-03-012708-llm-obsidian-2-6-1-complete-independent-review|LLM Obsidian 2.6.1 complete independent review]] и [[2026-07-30-224926-llm-obsidian-2-5-model-authored-custom-pipelines|LLM Obsidian 2.5 Model-Authored Custom Pipelines]].

## 2. Project Spaces

Каждый зарегистрированный внешний проект получает собственный Project Space внутри vault. Физическая структура уточняется в design, но должна различать:

- overview и repository registration;
- tasks и их историю;
- plans и Outcome Contracts;
- decisions;
- sessions и evidence;
- runbooks и project-specific documentation.

Project identity по умолчанию переиспользует существующую project page с DragonScale address и canonical repository registration. Отдельный публичный `project_id` вводится только если эти identities оказываются недостаточны.

Новая task наследует активный проект из подтверждённого repository/worktree context. Если проект нельзя определить однозначно, persistence и decomposition останавливаются до выбора. Глобального task inbox как источника состояния нет.

## 3. Project-scoped task system

Внутри каждого Project Space существуют человеческие представления:

- inbox/captured;
- ready/approved;
- active;
- blocked/attention;
- completed/archived.

Wiki хранит durable task knowledge и derived views. Во время активного исполнения authoritative state остаётся в существующем code-owned harness store. Глобальная страница может собирать задачи всех проектов только как read-only dashboard.

Task переиспользует существующий dispatch request/operation UUID. Дополнительный task identifier не нужен, пока не доказан случай, который существующая identity не различает.

## 4. Декомпозиция большой цели

Перед запуском модель предлагает bounded program:

- исходный Outcome Contract;
- независимые vertical tasks;
- ownership файлов, интерфейсов или артефактов;
- зависимости;
- expected evidence каждой task;
- integration task;
- общий concurrency и provider budget.

Code-owned validator проверяет DAG, отсутствие циклов, ownership conflicts, доступность pipelines, budgets и соответствие каждой node исходной цели. Пользователь утверждает всю программу один раз до первого provider effect.

## 5. Исполнение без нового scheduler

Program layer является тонкой координацией существующих операций:

```text
approved plan
  → compile ready tasks
  → existing dispatch × N
  → existing harness observes callbacks and attention
  → existing dispatch for integration
  → existing verification and multi-stage review
  → existing reap
```

Зависимости хранятся в immutable compiled program manifest, привязанном к plan digest и корневой operation identity. Existing harness reconciliation вычисляет ready set и запускает не больше утверждённого concurrency limit. Внутри каждой node pipeline остаётся последовательным.

## 6. Логические workstreams

### A. Project registration и scoped retrieval

- связать repository/worktree context с одним Project Space;
- маршрутизировать retrieval и wiki writes в project scope;
- дать явный global/project/both query scope без смешивания authoritative state.

### B. Project task views

- project-local capture, triage, status и history;
- derived global dashboard;
- transactional vault writes и существующая provenance/address model.

### C. Program compiler

- bounded model proposal;
- deterministic DAG/ownership/budget validation;
- one-shot approval и immutable compiled manifest.

### D. Existing-harness orchestration

- запуск ready nodes через обычный dispatch;
- bounded concurrency и provider quotas;
- callback/attention propagation через существующие operations;
- resume с первой отсутствующей child receipt без повторения accepted effects.

### E. Integration и root outcome

- отдельная integration task;
- typed child evidence packet;
- existing verification, multi-stage review и reap для исходного Outcome Contract.

### F. Dogfood

- один реальный Project Space;
- одна большая цель, разложенная на 3–5 tasks;
- минимум две действительно независимые параллельные nodes;
- одна blocked/recovery ветка;
- integration и финальный review без ручного управления каждой session.

## 7. Acceptance

1. Задачи двух проектов не смешиваются в storage, retrieval или execution context.
2. Неопределённый проект fail-closed до task persistence.
3. Global dashboard производен от Project Spaces и не становится вторым task store.
4. Program compiler отклоняет cycle, ownership conflict, unsupported pipeline и превышение concurrency budget до provider effects.
5. Каждая node использует существующий dispatch и последовательный pipeline без `parallel`/`join` в PipelineSpec.
6. Existing operation IDs и receipts достаточно для resume, dedup и attribution; новые публичные IDs отсутствуют без отдельного доказательства необходимости.
7. Child material failure блокирует integration либо создаёт typed attention согласно утверждённой программе.
8. Integration task получает полный typed evidence packet и закрывает исходный Outcome Contract.
9. Existing Simple/Deep/Full review, callback, cleanup и reap regressions остаются зелёными.
10. Реальный dogfood завершается при минимальном участии пользователя после one-shot approval.

## 8. Stop conditions

- Не переписывать 2.6 mechanisms ради удобства новой модели данных.
- Не создавать второй lifecycle owner, scheduler, store или FSM.
- Не добавлять обязательный ID, если существующая durable identity уже решает задачу.
- Не превращать wiki в mutable runtime control plane.
- Не добавлять parallel execution внутрь task PipelineSpec.
- Не сохранять task вне однозначно определённого Project Space.
- После трёх неудачных behavior-preserving попыток одного seam — architecture stop.
