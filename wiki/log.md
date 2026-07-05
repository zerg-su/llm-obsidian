---
type: meta
title: "Operation Log"
created: 2026-07-05
updated: 2026-07-05
tags:
  - meta
  - log
status: evergreen
related:
  - "[[index]]"
  - "[[hot]]"
  - "[[overview]]"
---

# Operation Log

Navigation: [[index]] | [[hot]] | [[overview]]

Append-only. Новые записи добавляются СВЕРХУ. Прошлые записи не редактируются.

Формат записи: `## [YYYY-MM-DD] operation | Title`

Парсинг недавних записей: `grep "^## \[" wiki/log.md | head -10`

---

## [2026-07-05] init | Vault initialized from the llm-obsidian template

Вольт создан из шаблона llm-obsidian v1.0.0. Демо-страницы concepts/entities/sources можно оставить как справочник по механикам или снести после онбординга. Дальше: [[getting-started]].
