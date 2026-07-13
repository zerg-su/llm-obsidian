# llm-obsidian — твой второй мозг

Этот репозиторий — одновременно Claude Code/Codex plugin и Obsidian-вольт. Открой каталог как вольт в Obsidian, работай с ним из Claude Code или Codex CLI, и вики будет расти по мере того как ты с чем-то сталкиваешься, что-то узнаёшь или планируешь.

**Plugin name:** `llm-obsidian`
**Skills:** `/wiki`, `/wiki-ingest`, `/wiki-query`, `/wiki-lint`, `/wiki-fold`, `/save`, `/save-plan`, `/close`, `/autoresearch`, `/canvas`, `/daily`, `/journal`, `/agenda`, `/backlog`, `/find-session`, `/draft`, `/distill-runbook`, `/learn`, `/defuddle`, `/dispatch`, `/review-dispatch`, `/review-send`, `/reap`, `/reap-send` (+ reference: obsidian-markdown, obsidian-bases)
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

- Агентские мутации `wiki/` и `.raw/.manifest.json` — ТОЛЬКО через `scripts/vault-write.py` (page create/update с expected SHA-256, log/hot caps, recovery journal; прямые Edit'ы запрещены).
- Поиск: `scripts/retrieve.py "<запрос>" --top 5 --json` — H2/H3 section chunks, sparse-first, optional bge-m3 fusion; dense failure даёт `degraded=true`, но exit 0. `semantic-search.py --hybrid` — compatibility wrapper. Свежесть держит Stop-хук.
- Качество измеримо: `make bench-retrieval` по goldset `.vault-meta/retrieval-goldset.jsonl` (наполняй своими запросами; правки ranking-а только со сдвигом метрик; новые запросы добавляй ПОСЛЕ тюнинга — held-out).

## Хуки

- **Hooks** (`hooks/hooks.json`, `.claude/hooks/`) — prompt/session/tool hooks остаются Claude-specific и в Codex выходят по `CODEX_THREAD_ID` guard. `Stop` — исключение: Codex plugin hooks запускают тот же `stop.sh` с `LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1`, чтобы turn-end reindex/auto-commit работал и в Codex.
- **`SessionStart`** (`startup|resume`): грузит `wiki/hot.md`; на `startup` дополнительно `session-nudge.sh` (maintenance-подсказки).
- **`PostCompact`**: повторно читает `wiki/hot.md`.
- **`UserPromptSubmit`**: skill-router — мягкие подсказки скиллов по `.claude/skill-rules.json`.
- **`PostToolUse[Bash]`**: `command-capture.sh` — санитизированный лог команд в `.vault-meta/command-log.jsonl` (сырьё для /distill-runbook).
- **`PostToolUse[ExitPlanMode]`**: `plan-capture.sh` — каждый approved план → `wiki/plans/`.
- **`Stop`**: shell-wrapper → `stop-hook.py`: transaction recovery → reindex → section sparse ensure → fingerprinted deferred-dense marker/worker → opt-in memory backup → strict validate → scoped commit (legacy BM25 индекс пока тоже self-heal'ится для compatibility). Stop не ждёт embeddings; dense worker сериализован отдельным `fcntl` lock, а hybrid до его завершения деградирует в sparse. Validation failure или небезопасный memory snapshot блокирует commit. Required budget по умолчанию 60s; override: `LLM_OBSIDIAN_STOP_REQUIRED_TIMEOUT_SEC` (1–600s). Memory backup не угадывает пути: только `CLAUDE_MEMORY_DIR` или локальный `.vault-meta/memory-backup.json` из `config/memory-backup.example.json`.
- **Cross-runtime telemetry**: shared scripts пишут только content-free events (`op`, runtime/session, относительные paths, числовые counters) в gitignored `.vault-meta/pipeline-events.jsonl`. `pipeline-stats.py` показывает их отдельно от Claude-only history/transcript/hook статистики. Матрица: `docs/runtime-capabilities.md`.

## DragonScale механизмы (опциональные)

1. **Fold operator** (M1): rollup лога в фолд-страницы (`wiki/folds/`), нудж каждые 64 записи.
2. **Deterministic addresses** (M2): `c-NNNNNN` на каждой странице, счётчик в `.vault-meta/address-counter.txt`.
3. **Semantic tiling lint** (M3): через bge-m3 находит candidate-дубли. Требует `ollama serve`. Пороги: `.vault-meta/tiling-thresholds.json` (перекалибруй на своём вольте).
4. **Boundary-first autoresearch** (M4): `/autoresearch` без темы предлагает frontier-кандидатов из самой вики.

Подробности: `[[DragonScale Memory]]` и `docs/dragonscale-guide.md`.

## MCP

MCP-серверы ходят через локальный HTTP-гейтвей (`scripts/mcp-gateway/`; порт задаётся только в локальном `runtime.env`): один набор процессов на машину вместо набора на каждый терминал. После смены порта: `mcp-gateway.sh sync-config --apply`. Секреты в `~/.config/mcp-gateway/secrets.env` (вне репо). Дефолтный пример — context7 (документация библиотек): одна строка `CONTEXT7_API_KEY=` и работает. Тяжёлые/редкие серверы — в `.mcp-profiles/` (см. README там). Гайд: `docs/mcp-gateway.md`. Для Codex MCP TOML генерируется через `scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply`: repo `.codex/config.toml`, global cleanup `~/.codex/config.toml`, профили `~/.codex/llm-obsidian-*.config.toml`.

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

## Portable shell timeouts

Локальные operational-команды часто запускаются с macOS. Не использовать голый GNU `timeout`: на macOS он отсутствует без Homebrew coreutils и ломает triage/verify-style shell fallback. Для shell-probe с wall-clock лимитом из корня репо использовать:

```bash
./scripts/with-timeout 8 kubectl --context my-cluster -n default get pod
```

Если MCP/API tool имеет собственный timeout-параметр (`timeout`, `since_seconds`, `limit`, `step`) - предпочитать его. Для долгих fan-out запросов сначала сужать selector/window, а не ставить большой shell timeout.

## Claude reviewer sessions

Для Claude-review, `/review-dispatch`, `/dispatch` и любых task-split'ов не использовать `claude -p` / `claude --print`. Нужна интерактивная Claude Code-сессия в cmux split. Task executor запускается с выбранным рабочим permission mode; read-only reviewer — с locked-down `dontAsk` и точным tool allow-list. После утверждённого unattended-плана routine review/reap не спрашивают повторных подтверждений; blocking/scope drift эскалируются. Observer-only watchdog уведомляет после 15/20 минут без видимого прогресса, но не отправляет input и не останавливает агента. Завершённые фоновые процессы закрывают только собственный armed cmux surface; coordinator/workspace не закрываются.

Не прерывать интерактивный Claude-review, если видно, что он работает: spinner, меняются token counters, идут tool calls или обновляется экран. Ждать минимум 15 минут без видимого прогресса перед вмешательством. Через 20 минут без прогресса можно диагностировать состояние и попросить краткий статус/итог, но не делать ранний interrupt только потому, что review думает несколько минут.

## Дисциплина

- **Pre-flight**: write-skill перед действием задаёт до 3 уточняющих вопросов (только по пунктам, которые не резолвятся из prompt'а). Read-only скиллы — auto-skip.
- **Failure-to-repair**: если repository-owned script/hook/skill/contract не выполняет документированное поведение, сначала contain + read-only diagnosis. Координатор чинит без дополнительного вопроса только repo-owned, локальный, воспроизводимый, обратимый дефект в уже согласованном scope, без пересечения с чужим dirty work и без новых permissions/dependencies/security/public API/migration/destructive/external effects; иначе один раз спрашивает пользователя. Затем: минимальный fix → regression test → relevant suite → повтор failed stage → продолжение с последней safe boundary без повтора внешнего эффекта. Фоновая задача сама решение не принимает: поднимает `mechanism-failure` и ждёт coordinator resolution. Stop hook остаётся fail-closed. Полный контракт: `docs/skill-references/failure-repair-contract.md`.
- **Код-дисциплина**: think before coding; simplicity first; surgical changes; goal-driven (без «полезного попутно»).
- **Sub-agent задачи** — контрактом: objective + границы (in/out) + формат результата + источники.

## Как пользоваться

Ядро: `ingest <путь|URL>` (источник → страницы в wiki/), `что ты знаешь про X?` (поиск с цитатами), `/save` (зафиксировать разговор), `/journal` (план на дату), `/agenda` (собрать незавершённые планы и напоминания), `/backlog add` (не забыть), `lint the wiki`, `update hot cache`. Параллельные задачи: `/dispatch` + `/review-dispatch` + `/reap-send` (требует cmux). Полный каталог скиллов: [[getting-started]].
