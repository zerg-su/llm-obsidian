---
type: meta
title: "daily-pipeline-guide"
created: 2026-07-05
updated: 2026-08-07
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
  - 019f72c4-816e-7200-a399-505adaa350e0
  - 019f6ddd-d07e-7a30-b018-f6358753fb91
  - 019fab00-3160-7380-8920-4b20183afb76
---

# Daily Pipeline Guide

Повседневный гайд по скиллам вольта: что дёргать в типовых ситуациях. Валидатор (`validate-vault.py`, чек guide) следит, чтобы каждый установленный скилл был здесь упомянут — страница не разъезжается с реальностью.

## Skill catalog

### Wiki-ядро

- **wiki** — бутстрап вольта под себя: режимы, scaffold, персонализация. Первая команда в новом вольте.
- **wiki-ingest** — `ingest <путь|URL>`: источник → 8-15 связанных типизированных страниц.
- **wiki-query** — «что ты знаешь про X?»: поиск с цитатами (режимы quick/standard/deep).
- **wiki-lint** — health-check: орфаны, мёртвые ссылки, frontmatter-гэпы, dupes (tiling).
- **vault-repair** — один bounded recovery/validation/Stop pass после `COMMIT_BLOCKED`; Claude: `/vault-repair`, Codex: `$llm-obsidian:vault-repair`.
- **wiki-fold** — роллап разросшегося log.md в фолд-страницы (DragonScale M1).
- **save** — зафиксировать вывод текущего разговора страницей + bookkeeping.
- **save-plan** — зафайлить план из разговора в `wiki/plans/` без исполнения.
- **close** — save + аккуратно выйти из сессии.
- **unsafe-research** — только по явному запросу: один vault-aware web-контекст с предупреждением о риске.
- **canvas** — визуальные канвасы: изображения, страницы, PDF.
- **defuddle** — очистить веб-страницу от мусора перед ингестом.

### Согласование

- **clarify** — один вопрос за раз: требования, ограничения, edge cases и acceptance criteria до плана или реализации. Claude: `/clarify`; Codex: `$llm-obsidian:clarify`.

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

- **dispatch** — approved plan → видимый task split/workspace + git worktree через code-owned harness.
- **review** — единый simple/deep review для dispatched task или текущего checkout; поддерживает cross-model и model aliases.
- **research** — защищённый research: vaultless network fetch → networkless synthesis → coordinator-owned запись.
- **reap** — собрать typed Wiki Summary завершённого task и закрыть его точный lifecycle.

### Инженерная работа

- **design** — уточнить домен и выбрать минимальный дизайн до реализации.
- **codebase-design** — спроектировать или углубить границы модулей, durable interfaces и test seams без pass-through дробления.
- **implementation-plan** — разложить утверждённый outcome/design на owned `consumes`/`produces` TDD-слайсы и evidence.
- **tdd** — вести изменение коротким red → green → regression циклом.
- **debug** — локализовать root cause и проверить исходный failing loop.
- **prototype** — проверить риск в disposable worktree, не смешивая прототип с production.
- **resolve-conflict** — собрать BASE/ours/theirs evidence и разрешать только явно авторизованные пути.
- **improve-skills** — структурный аудит скиллов по закреплённым snapshot Superpowers и Matt Pocock Skills.

### Reference (не вызываются, подгружаются по контексту)

- **obsidian-markdown** — корректный Obsidian-синтаксис: wikilinks, callouts, embeds.
- **obsidian-bases** — .base файлы: динамические таблицы и представления.

## Auto-triggers vs manual

Роутер (`.claude/skill-rules.json`) сам подсказывает скилл по фразе: «сохрани это» → save, «что ты знаешь про» → wiki-query, «не забыть» → backlog, «напомни в пятницу» → journal, «собери незавершённое» → agenda, «сделай ранбук из сессии» → distill-runbook. Подсказки мягкие: игнорируй, если не в тему. Полный список паттернов — в самом rules-файле.

## Типовые цепочки

- Новый материал: `ingest <источник>` → `lint the wiki` (раз в неделю) → `/wiki-fold` (по нуджу).
- Рабочий день: `/journal` утром → `agenda scan` и при необходимости `agenda collect` → работа с `/save` → `/daily` вечером.
- Большая задача: `/clarify` → `/design`/`codebase-design` → `implementation-plan` → `/dispatch` → автоматический `/review` → `/reap` результата.
