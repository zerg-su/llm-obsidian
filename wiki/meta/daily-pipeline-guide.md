---
type: meta
title: "daily-pipeline-guide"
created: 2026-07-05
updated: 2026-07-13
tags:
  - meta
  - guide
  - skills
status: evergreen
related:
  - "[[index]]"
  - "[[getting-started]]"
sessions:
  - public-template-v2
---

# Daily Pipeline Guide

Повседневный гайд по скиллам вольта: что дёргать в типовых ситуациях. Валидатор (`validate-vault.py`, чек guide) следит, чтобы каждый установленный скилл был здесь упомянут — страница не разъезжается с реальностью.

## Skill catalog

### Wiki-ядро

- **wiki** — бутстрап вольта под себя: режимы, scaffold, персонализация. Первая команда в новом вольте.
- **wiki-ingest** — `ingest <путь|URL>`: источник → 8-15 связанных типизированных страниц.
- **wiki-query** — «что ты знаешь про X?»: поиск с цитатами (режимы quick/standard/deep).
- **wiki-lint** — health-check: орфаны, мёртвые ссылки, frontmatter-гэпы, dupes (tiling).
- **wiki-fold** — роллап разросшегося log.md в фолд-страницы (DragonScale M1).
- **save** — зафиксировать вывод текущего разговора страницей + bookkeeping.
- **save-plan** — зафайлить план из разговора в `wiki/plans/` без исполнения.
- **close** — save + аккуратно выйти из сессии.
- **autoresearch** — автономный research-цикл по теме (или frontier-кандидаты без темы).
- **canvas** — визуальные канвасы: изображения, страницы, PDF.
- **defuddle** — очистить веб-страницу от мусора перед ингестом.

### Продуктивность

- **journal** — дневник по датам: планы и напоминания как Tasks-checkboxes, завершение с датой.
- **agenda** — read-only scan незавершённого → атомарный перенос планов/напоминаний → месячный live-report.
- **daily** — статус за день (3-7 буллетов) в Daily Status Log.
- **backlog** — «не забыть»: одна строка в capture-инбокс; promote → /save.
- **find-session** — найти прошлую сессию по похожей задаче.
- **draft** — 2-3 варианта ответа для внешней коммуникации, с redaction-проходом.
- **distill-runbook** — команды сессии → copy-paste ранбук (работает без ИИ).
- **learn** — интерактивный тьютор по материалам вольта: study/quiz/practice.

### Оркестрация (требует cmux)

- **dispatch** — вынести задачу в параллельный split + git worktree, с передачей approved-плана.
- **reap** — собрать результат task-split'а в вики (interim/final).
- **reap-send** — вызывается ИЗ task-split'а: handoff summary в вики одной командой.
- **review-dispatch** — запускает независимое cross-model review и bounded verify.
- **review-send** — возвращает типизированный reviewer verdict исполнителю.

### Reference (не вызываются, подгружаются по контексту)

- **obsidian-markdown** — корректный Obsidian-синтаксис: wikilinks, callouts, embeds.
- **obsidian-bases** — .base файлы: динамические таблицы и представления.

## Auto-triggers vs manual

Роутер (`.claude/skill-rules.json`) сам подсказывает скилл по фразе: «сохрани это» → save, «что ты знаешь про» → wiki-query, «не забыть» → backlog, «напомни в пятницу» → journal, «собери незавершённое» → agenda, «сделай ранбук из сессии» → distill-runbook. Подсказки мягкие: игнорируй, если не в тему. Полный список паттернов — в самом rules-файле.

## Типовые цепочки

- Новый материал: `ingest <источник>` → `lint the wiki` (раз в неделю) → `/wiki-fold` (по нуджу).
- Рабочий день: `/journal` утром → `agenda scan` и при необходимости `agenda collect` → работа с `/save` → `/daily` вечером.
- Большая задача: план в plan mode → auto-capture в `wiki/plans/` → `/dispatch` на исполнение → `/reap` результата.
