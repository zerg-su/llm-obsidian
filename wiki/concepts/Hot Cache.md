---
type: concept
title: "Hot Cache"
complexity: basic
domain: knowledge-management
aliases:
  - "hot.md"
  - "Session Cache"
  - "Context Cache"
created: 2026-04-07
updated: 2026-04-26
tags:
  - concept
  - knowledge-management
  - context
status: mature
related:
  - "[[LLM Wiki Pattern]]"
  - "[[Compounding Knowledge]]"
  - "[[index]]"
  - "[[hot]]"
  - "[[concepts/_index]]"
sources:
---

# Hot Cache

~500-словное summary самого свежего контекста в вики-вольте. Хранится в `wiki/hot.md`. Обновляется в конце каждой сессии и после каждого значимого ингеста или запроса.

Hot cache существует чтобы ответить на один вопрос: "где мы остановились?" Новая сессия читает `hot.md` первым. Если ответ там — пропускает crawl остальной вики.

---

## Что хранит

- Что было ингестнуто или обсуждалось недавно
- Ключевые свежие факты и takeaway'и
- Страницы недавно созданные или обновлённые
- Активные threads и открытые вопросы
- На чём пользователь сейчас сфокусирован

---

## Формат

```markdown
---
type: meta
title: "Hot Cache"
updated: YYYY-MM-DDTHH:MM:SS
---

# Recent Context

## Last Updated
YYYY-MM-DD — [что происходило]

## Key Recent Facts
- [Самый важный свежий takeaway]
- [Второй]

## Recent Changes
- Created: новые wiki-страницы из этого ингеста
- Updated: существующие страницы с новыми связями
- Flagged: противоречия между источниками если найдены

## Active Threads
- Пользователь исследует [тему]
- Открытый вопрос: [над чем работает]
```

---

## Правила

- Держать под 500 слов. Это кэш, не журнал.
- Перезаписывать целиком каждый раз. Не append-only.
- Один файл. Не разбивать по датам.
- Обновляется после каждого ингеста, значимого запроса, и в конце каждой сессии.

---

## Почему это важно

Без hot cache каждая сессия стартует холодной: читать index (1000 токенов), читать несколько domain sub-index'ов, читать несколько отдельных страниц. С hot cache первые 500 токенов часто покрывают всё что нужно.

На практике добавление `hot.md` в executive assistant-вольт радикально снижает token-стоимость старта сессии по сравнению с crawl-ом нескольких wiki-страниц.

Hot cache особенно ценен в cross-project setup'ах: другой Claude Code проект может указывать на этот вольт и читать `hot.md` первым чтобы получить свежий контекст за минимальную стоимость.

---

## Связи

Hot cache часть token-discipline стратегии [[LLM Wiki Pattern]]. См. [[index]] как работает broader-навигация.
