---
type: backlog
title: "Personal Capture Inbox"
address: c-000011
created: 2026-07-19
updated: 2026-08-04
status: living
tags: [backlog, todo, capture]
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
  - id: 019f6ddd-d07e-7a30-b018-f6358753fb91
    date: 2026-07-19
---

# Backlog

Lightweight capture inbox. Append-only via /backlog skill. One line per item.
Format: `- [YYYY-MM-DD] <slug> — <summary> — context: <link or ->`

## Entries

- [2026-07-19] cmux-acceptance-surface-cleanup — исправить автоматическое закрытие точной coordinator cmux surface после штатного выхода, timeout или interrupted acceptance-запуска и добавить regression-тест — context: [[Unattended Pipeline]]
- [2026-07-20] review-verify-delta-context — проверить, можно ли сократить verify-передачу в повторно используемую review-сессию до машинно сформированной дельты (commit range, resolution, тесты и изменённые файлы), не теряя исходный контекст и качество — context: [[Unattended Pipeline]]
- [2026-08-02] llm-obsidian-2-7-project-memory-layout — спроектировать project-aware структуру LLM Obsidian: открывать разные внешние проекты из одного основного vault, сохранять историю работы и документацию раздельно по проектам; сравнить хранение в самом проекте и централизованное хранение в Obsidian, с предпочтением project spaces внутри vault — context: [[DragonScale Memory]]
- [2026-08-03] llm-obsidian-2-7-code-owned-task-graph — добавить program-level TaskGraph над существующими последовательными task pipelines: модель предлагает независимые slices и зависимости, пользователь один раз утверждает программу, harness валидирует ownership, запускает ready-задачи с bounded concurrency, собирает typed evidence и выполняет integration/final-review node без parallel/join внутри PipelineSpec — context: [[2026-08-03-012708-llm-obsidian-2-6-1-complete-independent-review|LLM Obsidian 2.6.1 complete independent review]]
- [2026-08-03] llm-obsidian-2-7-project-task-system — превратить lightweight backlog в project-scoped task system: capture, triage, promote, dependencies, status и durable history; большую цель модель декомпозирует в независимые owned tasks, пользователь один раз утверждает TaskGraph, harness запускает ready-задачи с bounded concurrency и собирает их через integration/final-review node; wiki остаётся human-facing представлением, а active runtime state — code-owned — context: [[2026-08-03-012708-llm-obsidian-2-6-1-complete-independent-review|LLM Obsidian 2.6.1 complete independent review]]
- [2026-08-03] llm-obsidian-2-7-project-scoped-task-namespaces — сделать project_id обязательной частью task identity и хранить inbox, ready tasks, TaskGraphs, результаты и историю внутри соответствующего Project Space; глобальный экран может только агрегировать project task indexes, но не владеть общим backlog; active project наследуется автоматически, cross-project work требует отдельной явной program identity — context: [[DragonScale Memory]]
