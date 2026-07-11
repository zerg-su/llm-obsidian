---
type: concept
title: "Cherry-Picks: Feature Backlog from Ecosystem Research"
created: 2026-04-08
updated: 2026-07-11
tags:
  - backlog
  - cherry-picks
  - product-roadmap
  - llm-obsidian
status: current
related:
  - "[[LLM Wiki Pattern]]"
sources: []
sessions:
  - public-template-v2
---

# Cherry-Picks: feature-backlog

> Из экосистемного research'а 2026-04-08; проанализировано 16+ проектов
> Приоритет: impact × ease of implementation × uniqueness
> На момент v1.6.0 многие из Tier 1/2 уже зашипены. Backlog оставлен как референс эволюции плагина.

---

## Tier 1 — Quick Wins (high impact, low effort)

### 1. URL Ingestion в /wiki-ingest
**Source**: ekadetov/llm-wiki, Ar9av/obsidian-wiki
**Что**: передавать URL напрямую в ingest вместо file-path. Агент забирает страницу, чистит, сохраняет в `.raw/`, ингестит.
**Текущее состояние**: пользователи должны вручную копи-пасть веб-контент.
**Как добавить**: detect `https://` префикс в ingest-скилле → WebFetch → save в `.raw/articles/` → стандартный ingest.
**Бонус**: парится с **defuddle** (kepano's web-cleaner) для clean-token-efficient extraction.

### 2. Auto-commit PostToolUse-хук
**Source**: ballred/obsidian-claude-pkm, ekadetov/llm-wiki
**Что**: каждый Write/Edit tool-call в вольте триггерит `git add -A && git commit -m "auto: [filename] [timestamp]"`.
**Текущее состояние**: нет auto-commit; пользователи пушат вручную.
**Как добавить**: PostToolUse-хук в hooks.json с matcher Write+Edit, scoped к wiki/-директории.
**Заметка**: автоматически делает вольт version-controlled базой знаний.

### 3. defuddle Web Cleaning Skill
**Source**: kepano/obsidian-skills
**Что**: скилл-обёртка `defuddle-cli` — чистит ads, nav, clutter из веб-страниц до ингеста. Уменьшает token-расход ~40-60% на типичных веб-статьях.
**Как добавить**: новый sub-скилл `defuddle` или reference в wiki-ingest. Требует `defuddle-cli` npm-пакет.

---

## Tier 2 — средний effort, высокая ценность

### 4. Delta Tracking Manifest
**Source**: Ar9av/obsidian-wiki
**Что**: `.raw/.manifest.json` трекает каждый ингестнутый source — path, hash, timestamp, какие wiki-страницы породил. Re-ingest обрабатывает только новые/изменённые файлы.
**Текущее состояние**: каждый `/wiki-ingest` re-processes всё.
**Как добавить**:
  - На ingest: вычислить MD5-хэш source → проверить manifest → skip если unchanged
  - На ingest: записать `{path, hash, ingested_at, pages_created}` в manifest
  - На update: re-process если hash изменился, merge changes в существующие страницы

### 5. Multi-Depth Query Modes
**Source**: rvk7895/llm-knowledge-bases
**Что**: 3 query-tier'а в `/wiki-query`:
  - **Quick** — только hot.md + index.md (~3 страницы)
  - **Standard** — full wiki cross-reference + опциональный web-search supplement
  - **Deep** — параллельные sub-агенты, каждый исследует свой угол
**Текущее состояние**: один уровень глубины.
**Как добавить**: флаги `/wiki-query quick <вопрос>`, `/wiki-query deep <вопрос>` в SKILL.md.

### 6. /wiki-ingest Vision Support
**Source**: Ar9av/obsidian-wiki
**Что**: ингестить картинки, скриншоты, фотографии whiteboard через vision-capable модель.
**Как добавить**: detect image-extension → read как base64 → передать Claude с vision-промптом запрашивая транскрипцию/описание → relate как text-source → стандартный ingest pipeline.
**Полезно для**: фотографий whiteboard со встреч, скриншотов веб-контента, диаграмм.

---

## Tier 3 — крупные фичи планирования

### 7. /adopt — импорт существующего вольта
**Source**: heyitsnoah/claudesidian, ballred/obsidian-claude-pkm
**Что**: `/adopt` анализирует существующий Obsidian-вольт, детектирует organization method (PARA, Zettelkasten, LYT, plain), оборачивает LLM Wiki pattern вокруг него не разрушая существующую структуру.
**Зачем**: сейчас пользователи должны стартовать с нуля. Это разблокирует adoption людьми с существующими вольтами.
**Implementation**: scan folder-структуру → classify patterns → генерация CLAUDE.md мапящего existing-folders в wiki-roles → non-destructive.

### 8. Productivity Wrapper (daily/weekly reviews)
**Source**: ballred/obsidian-claude-pkm
**Что**: опциональные `/daily` и `/weekly` скиллы, связывающие goal-tracking с базой знаний.
**Может быть отдельным плагином** вместо bundle в llm-obsidian.
**Goal-cascade**: 3-Year Vision → Yearly Goals → Projects → Weekly → Daily.

### 9. Multi-Agent Compatibility (Cursor, Windsurf, Codex)
**Source**: Ar9av/obsidian-wiki, kepano/obsidian-skills
**Что**: `setup.sh` или `/wiki-convert` команда, генерирующая `.cursor/rules/`, `AGENTS.md`, `GEMINI.md` эквиваленты чтобы wiki-скиллы работали в других coding-агентах.
**Заметка**: kepano уже опубликовал скиллы в Agent Skills формате — llm-obsidian уже в этом формате. Нужны просто adapter-файлы.

### 10. Marp Presentation Output
**Source**: rvk7895/llm-knowledge-bases, ekadetov/llm-wiki
**Что**: `/wiki-query --slides <тема>` генерирует Marp-презентацию из wiki-контента, сохраняет в `output/`.
**Требует**: `marp-cli` npm-пакет.

---

## Tier 4 — research / ecosystem-плеи

### 11. obsidian-memory-mcp интеграция
**Source**: YuNaga224/obsidian-memory-mcp
**Что**: подключить MCP-сервер, хранящий Claude-memories как Markdown-сущности с `[[wikilinks]]` — они автоматически появляются в Obsidian graph view.
**Как добавить**: указать `MEMORY_DIR` на `wiki/entities/` директорию — entity memory-страницы становятся proper wiki-страницами.

### 12. obsidian-bases Skill (от kepano)
**Source**: kepano/obsidian-skills
**Что**: научить Claude создавать и редактировать Obsidian Bases (.base-файлы) для динамических таблиц, views, filters.
**Зачем**: Obsidian Bases это новая core-фича — никакой другой LLM Wiki проект пока не учит Claude о ней.

### 13. Schema-Emergent Vault Mode
**Source**: Ar9av/obsidian-wiki
**Что**: альтернативный `/wiki` режим где структура вольта не скаффолдится upfront, а emerge из ингестнутого контента. Хорошо для exploratory knowledge building vs. structured domains.
**Как**: skip scaffold-шага; пусть wiki-ingest создаёт папки/категории organically на основе source-контента.

---

## Competitive positioning

После research'а уникальные преимущества llm-obsidian остаются:
- **Hot cache** — ни у кого другого нет этого session-context механизма
- **Canvas visual layer** — уникально в LLM Wiki категории
- **/save conversation** — filing chat → wiki это distinct workflow
- **Marketplace polish** — лучший install-experience в категории

Экосистема созревает быстро. Tier 1 items (URL ingest, auto-commit, defuddle) должны зашипиться в v1.3.0 чтобы оставаться впереди.

---

## Implementation priority

```
v1.3.0 (quick wins):
  - URL ingestion (#1)
  - Auto-commit hook (#2)
  - defuddle integration (#3)

v1.4.0 (quality):
  - Delta tracking (#4)
  - Multi-depth query (#5)

v1.5.0 (expansion):
  - Vision ingest (#6)
  - /adopt command (#7)
  - Multi-agent compat (#9)

Future:
  - Productivity wrapper (#8)
  - Marp output (#10)
  - Memory MCP integration (#11)
```
