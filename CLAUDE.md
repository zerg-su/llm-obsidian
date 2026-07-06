# llm-obsidian — твой второй мозг

Этот репозиторий — одновременно Claude Code/Codex plugin и Obsidian-вольт. Открой каталог как вольт в Obsidian, работай с ним из Claude Code или Codex CLI, и вики будет расти по мере того как ты с чем-то сталкиваешься, что-то узнаёшь или планируешь.

**Plugin name:** `llm-obsidian`
**Skills:** `/wiki`, `/wiki-ingest`, `/wiki-query`, `/wiki-lint`, `/wiki-fold`, `/save`, `/save-plan`, `/close`, `/autoresearch`, `/canvas`, `/daily`, `/journal`, `/backlog`, `/find-session`, `/draft`, `/distill-runbook`, `/learn`, `/defuddle`, `/dispatch`, `/reap` (+ reference: obsidian-markdown, obsidian-bases)
**Vault path:** этот каталог (открывать в Obsidian напрямую)

> Этот файл — шаблон. После установки перепиши разделы «Назначение» и «Источники» под себя: чем конкретнее описан твой use case, тем лучше ассистент работает с вольтом.

## Назначение вольта

Персональная база знаний по паттерну LLM Wiki (Karpathy): ассистент строит и поддерживает структурированную вики из твоих разговоров, источников и решений. Опиши здесь СВОЙ фокус: работа, учёба, домашняя инфраструктура, исследования.

## Структура вольта

```
.raw/                # источники: дампы, статьи, скриншоты. Claude читает, никогда не пишет
wiki/                # сама вики
├── index.md         # каталог
├── log.md           # лог операций (append-only, новые записи сверху)
├── hot.md           # ~500-словный кэш свежего контекста, грузится SessionStart-хуком
├── overview.md      # executive summary вольта
├── getting-started.md
│
├── concepts/        # технические и прочие концепты, которые исследуешь
├── entities/        # люди, команды, инструменты, вендоры
├── sources/         # суммари ингестнутых документов
├── comparisons/     # side-by-side анализы
├── questions/       # открытые вопросы и ответы на них
│
├── runbooks/        # пошаговые процедуры (как сделать X)
├── decisions/       # решения с обоснованием (ADR-стиль)
├── goals/           # что планируешь, миграции, улучшения
├── routines/        # чеклисты и повторяющиеся практики
├── daily/           # журнал по датам (skill /journal)
├── plans/           # авто-захваченные approved-планы (hook plan-capture)
│
├── folds/           # авто-роллапы лога (DragonScale Mechanism 1)
└── meta/            # дашборды, отчёты, sessions
.vault-meta/         # internal: счётчики, retrieval-индексы, кэши
.obsidian/           # настройки Obsidian
skills/, scripts/, bin/, tests/, hooks/   # код плагина (вендорнут в этот репо)
```

**Любой внешний источник read-only. Записываем только в этот вольт.**

## Конвенции записи

- **Язык**: проза на твоём языке (шаблон настроен под русский). На английском остаются: имена файлов, wikilink-таргеты, ключи frontmatter, код, команды, устоявшиеся технические термины.
- **Frontmatter**: обязательны `type`, `status`, `created`, `updated`, `tags`. Дополнительные поля по типу страницы (см. `_index.md` в каждой папке).
- **Wikilinks**: `[[Page Name]]`, имена файлов уникальны во всём вольте, путей не пишем.
- **Имена файлов**: Title Case или slug-style по смыслу. Папки lowercase.
- **Session provenance**: каждая страница несёт `sessions:` со списком сессий, которые её трогали (log.md исключение).
- **DragonScale-адреса**: `address: c-NNNNNN` на контентных страницах, аллоцируется через `./scripts/allocate-address.sh`.

## Retrieval

- Запись log/hot — ТОЛЬКО через `scripts/vault-write.py` (single-pass payload, детерминированные капы; прямые Edit'ы этих файлов запрещены).
- Поиск: `scripts/semantic-search.py "<запрос>" --hybrid` — dense (ollama bge-m3 по tiling-кэшу) + BM25 (scope-aware fusion: BM25 инжектит только страницы вне dense-скоупа: meta/plans/folds). Префильтр: `scripts/tag-search.py`. Свежесть кэшей держит Stop-хук.
- Качество измеримо: `make bench-retrieval` по goldset `.vault-meta/retrieval-goldset.jsonl` (наполняй своими запросами; правки ranking-а только со сдвигом метрик; новые запросы добавляй ПОСЛЕ тюнинга — held-out).

## Хуки

- **Hooks** (`hooks/hooks.json`, `.claude/hooks/`) — prompt/session/tool hooks остаются Claude-specific и в Codex выходят по `CODEX_THREAD_ID` guard. `Stop` — исключение: Codex plugin hooks запускают тот же `stop.sh` с `LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1`, чтобы turn-end reindex/auto-commit работал и в Codex.
- **`SessionStart`** (`startup|resume`): грузит `wiki/hot.md`; на `startup` дополнительно `session-nudge.sh` (maintenance-подсказки).
- **`PostCompact`**: повторно читает `wiki/hot.md`.
- **`UserPromptSubmit`**: skill-router — мягкие подсказки скиллов по `.claude/skill-rules.json`.
- **`PostToolUse[Bash]`**: `command-capture.sh` — санитизированный лог команд в `.vault-meta/command-log.jsonl` (сырьё для /distill-runbook).
- **`PostToolUse[ExitPlanMode]`**: `plan-capture.sh` — каждый approved план → `wiki/plans/`.
- **`Stop`**: `stop.sh` — reindex → memory-backup → BM25 rebuild → dense refresh → автокоммит (flock-сериализация параллельных сессий; opt-out: `touch .vault-meta/auto-commit.disabled`; latency-телеметрия в `.vault-meta/stop-hook-latency.jsonl`, WARN при turn-wrap ≥ 30с).

## DragonScale механизмы (опциональные)

1. **Fold operator** (M1): rollup лога в фолд-страницы (`wiki/folds/`), нудж каждые 64 записи.
2. **Deterministic addresses** (M2): `c-NNNNNN` на каждой странице, счётчик в `.vault-meta/address-counter.txt`.
3. **Semantic tiling lint** (M3): через bge-m3 находит candidate-дубли. Требует `ollama serve`. Пороги: `.vault-meta/tiling-thresholds.json` (перекалибруй на своём вольте).
4. **Boundary-first autoresearch** (M4): `/autoresearch` без темы предлагает frontier-кандидатов из самой вики.

Подробности: `[[DragonScale Memory]]` и `docs/dragonscale-guide.md`.

## MCP

MCP-серверы ходят через локальный HTTP-гейтвей (`scripts/mcp-gateway/`, порт 9090): один набор процессов на машину вместо набора на каждый терминал. Секреты в `~/.config/mcp-gateway/secrets.env` (вне репо). Дефолтный пример — context7 (документация библиотек): одна строка `CONTEXT7_API_KEY=` и работает. Тяжёлые/редкие серверы — в `.mcp-profiles/` (см. README там). Гайд: `docs/mcp-gateway.md`. Для Codex MCP TOML генерируется через `scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply`: repo `.codex/config.toml`, global cleanup `~/.codex/config.toml`, профили `~/.codex/llm-obsidian-*.config.toml`.

## Codex

- `scripts/codex-adapter.py --apply` генерирует `.codex-plugin/plugin.json` и `.agents/plugins/marketplace.json`.
- Установка: `codex plugin marketplace add "$(pwd)"`, затем `codex plugin add llm-obsidian@llm-obsidian-codex`.
- Явный вызов скиллов в Codex: `$llm-obsidian:save`, `$llm-obsidian:wiki-query`, `$llm-obsidian:close`; Claude-style `/save` остаётся триггером в описании, но не является Codex slash command.
- Provenance писать через `./scripts/current-session-id.sh` (`CLAUDE_CODE_SESSION_ID` → `CODEX_THREAD_ID` → `unknown`).
- Limit helpers: `.codex/codex-limits-status.py` для compact/cmux status, `.codex/update-cmux-limits.sh` для Codex hooks, `scripts/codex-limit-monitor.py` для CLI/TUI monitor (`--install` ставит `codex-limit-status`).

## DCG

Destructive Command Guard assets are portable: policy `config/dcg/config.toml`,
hook template `.github/hooks/dcg.json`, installer `bin/setup-dcg.sh`, smoke
`scripts/dcg-test-suite.sh`. Installer merge'ит только `PreToolUse`/`Bash` dcg
entry в `~/.codex/hooks.json` и `~/.claude/settings.json`, делая backups
`.bak-dcg-*`. Smoke по умолчанию изолирует user allowlist; live config:
`DCG_TEST_USE_USER_CONFIG=1 bash scripts/dcg-test-suite.sh`.

## Дисциплина

- **Pre-flight**: write-skill перед действием задаёт до 3 уточняющих вопросов (только по пунктам, которые не резолвятся из prompt'а). Read-only скиллы — auto-skip.
- **Код-дисциплина**: think before coding; simplicity first; surgical changes; goal-driven (без «полезного попутно»).
- **Sub-agent задачи** — контрактом: objective + границы (in/out) + формат результата + источники.

## Как пользоваться

Ядро: `ingest <путь|URL>` (источник → страницы в wiki/), `что ты знаешь про X?` (поиск с цитатами), `/save` (зафиксировать разговор), `/journal` (план на дату), `/backlog add` (не забыть), `lint the wiki`, `update hot cache`. Параллельные задачи: `/dispatch` (требует cmux). Полный каталог скиллов: [[getting-started]].
