<p align="center">
  <img src="docs/assets/llm-obsidian-banner.png" alt="Claude Code и Codex обмениваются проверенной работой через Obsidian-вольт" width="100%">
</p>

# LLM Obsidian

[![CI](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml/badge.svg)](https://github.com/zerg-su/llm-obsidian/actions/workflows/ci.yml)

**Локальная проектная среда, в которой Claude Code и Codex CLI делят долговременную память, одни и те же скиллы, видимую cmux-оркестрацию и независимое кросс-модельное ревью.**

Язык: [English](README.md) · **Русский**
История релизов: [English](CHANGELOG.md) · [Русский](CHANGELOG.ru.md)

LLM Obsidian — одновременно Obsidian-вольт и набор инструментов для LLM-агентов. Он превращает разговоры, планы, документы, решения, команды, исследования и завершённые задачи в связанный Markdown, который не исчезает вместе с окном модели. Этот же репозиторий даёт обоим поддерживаемым CLI версионируемые скиллы, детерминированные скрипты, retrieval, защитные проверки и полный жизненный цикл dispatch → review → reap.

Проект рассчитан на продолжительную работу с ноутбука: разработку ПО, инфраструктуру, исследования, личную базу знаний и большие операционные задачи. Это не облачная agent-платформа, не proxy провайдера и не обход подписок или лимитов. Вы продолжаете пользоваться официальными Claude Code и Codex CLI со своим доступом; репозиторий добавляет общий рабочий слой для знаний и задач.

## В чём главная идея

Большинство agent-setup оптимизируют один промпт или одну coding-сессию. LLM Obsidian оптимизирует весь цикл:

- **Результат переживает чат.** Планы, решения, источники, ревью и итоги становятся связанными, diffable-файлами, а не скрытой памятью диалога.
- **Claude и Codex работают как одна система.** У них общий вольт и общая механика, но независимый контекст и разные типичные ошибки.
- **Ревью — это lifecycle, а не “спросить вторую модель”.** Reviewer не пишет product-файлы, callback типизирован и привязан к операции, исправления возвращаются executor'у, а каждый раунд сохраняется вместе с результатом.
- **Долгая работа остаётся видимой.** cmux открывает task/reviewer рядом с координатором, сохраняет интерактивный контекст, наблюдает активность и закрывает только точную surface после выхода процесса.
- **Повторяемой механикой владеет код.** Роутинг, candidate discovery, fingerprints, validation, retries, cleanup, telemetry, индексы и транзакции выполняют скрипты. Токены модели остаются на понимание и решения.
- **Вики — data layer, а не побочный эффект агента.** `wiki/` и производные `.vault-meta/` отделены от поведенческого кода, поэтому обычное изменение заметки не инвалидирует pipeline.
- **Качество измеряется.** Механика покрыта герметичными тестами, retrieval — RU/EN goldset'ом, релиз — live-матрицей 29 скиллов × 2 runtime с семантическим переиспользованием evidence.

Главное правило: если детерминированная программа может выполнить шаг без потери качества результата, этим шагом должна владеть программа. Модель должна решать, **что означает** действие, а не каждый раз заново вспоминать, **как технически его выполнить**.

## Для кого это сделано

| Пользователь | Что получает |
|---|---|
| **Разработчик** | Clarify требований, планы реализации, изолированные worktree, ревью другой моделью, долговременные решения, runbooks и поиск по истории проекта. |
| **DevOps / platform engineer** | Видимые параллельные операции, capture команд, incident/runbook memory, строгие границы внешних эффектов, supervision задач и локальную MCP-инфраструктуру. |
| **Исследователь / аналитик** | Защищённый web-fetch, networkless synthesis, нормализацию документов, provenance источников, мультиязычный retrieval и связанные заметки. |
| **Knowledge worker** | Journal, agenda, backlog, daily summary, сохранение разговоров, ingest документов, помощь с ответами и Obsidian UI над обычными файлами. |
| **Пользователь нескольких проектов** | В cmux можно держать project/task вкладки; task/model lanes связаны стабильными ID и не угадываются по заголовку окна или давности. |

Лучший сценарий — macOS-ноутбук с cmux. Вольт, retrieval, документы и productivity-скиллы полезны и без cmux; именно видимая unattended-оркестрация остаётся macOS-first частью.

## Что именно заточено под русский язык

Русский здесь не ограничен переводом README или trigger-фразами:

- **Sparse retrieval понимает кириллицу.** Unicode-tokenizer, русские стоп-слова, заголовки, теги и mixed RU/EN техническая лексика входят в основной BM25-путь.
- **Dense retrieval мультиязычный.** Локальный `bge-m3` поддерживает русский и используется для semantic search и проверки близких дубликатов.
- **Retrieval оценивается на RU/EN goldset.** Качество русских запросов входит в release-метрики, а не проверяется “на глаз”.
- **OCR устанавливается сразу для `ru,en`.** Docling + EasyOCR заранее получают русские и английские модели для сканов и PDF.
- **Router знает русские формулировки.** “Сохрани”, “найди сессию”, “не забудь”, “давай clarify”, “grill me”, journal/agenda/daily и другие намерения имеют RU/EN hints и false-positive тесты.
- **Вольт нормально смешивает языки.** Wikilinks, теги, имена файлов, заголовки, поиск и provenance не требуют латиницы.
- **Документация двуязычна.** README и полная release history поддерживаются на русском и английском; канонический поведенческий контракт при этом остаётся один и тестируется одинаково.

## Архитектура

```text
клавиатура / опциональный VoiceInk
             │
             ▼
┌──────────────────── сессия-координатор ─────────────────────┐
│ Claude Code или Codex CLI                                   │
│   │                                                         │
│   ├── repo skills ──► детерминированные Python/Bash runners │
│   ├── retrieval ────► Obsidian Markdown + локальные индексы │
│   └── cmux broker ──► task/reviewer справа от координатора  │
└──────────────────────────┬───────────────────────────────────┘
                           │ typed ID, hashes, callbacks
                 ┌─────────┴─────────┐
                 ▼                   ▼
        изолированный worktree   reviewer другой модели
        Claude или Codex         Codex или Claude
                 │                   │
                 └── исправления ◄───┘
                           │
                           ▼
                 валидированный reap
                           │
                           ▼
          связанный Obsidian-результат + история ревью
```

Каноническое состояние — обычный Markdown, JSON/TOML-контракты, Git history и воспроизводимые скрипты. Obsidian — человеческий интерфейс и граф ссылок; приложению не обязательно работать в фоне, чтобы работали скрипты.

### Путь задачи от вопроса до результата

1. **Понять.** `clarify` — встроенный аналог “grill me”: сначала смотрит факты репозитория, затем задаёт по одному действительно важному вопросу до кода или плана.
2. **Спланировать.** Решения становятся сохранённым планом с provenance и стабильным DragonScale-адресом.
3. **Dispatch.** Code-owned runner фиксирует project/task/session ID, точный model route, hash утверждённого плана, permission domain, worktree и cmux surface вызывающего координатора. Окно открывается справа от правильной сессии, а не рядом со случайно выбранной позже вкладкой.
4. **Выполнить.** Claude или Codex работает в отдельном Git worktree. Ограниченную работу той же моделью по умолчанию берёт внутренний агент; явный запрос отдельного окна создаёт долговременную видимую lane.
5. **Review.** Противоположный runtime получает read-only запрос, привязанный к точному baseline. Findings возвращаются в typed-формате; verify возобновляет ту же task/model/domain lane, не оплачивая повторную передачу всего контекста.
6. **Reap.** Проверяются typed summary, approved review archive, hash плана, result path и session provenance. Одна vault-транзакция пишет результат и закрывает plan.
7. **Безопасный exit.** `/exit` разрешается только после завершения lifecycle. Supervisor закрывает точную surface после выхода процесса и никогда не угадывает соседнюю вкладку.

Успешный review заканчивает **раунд**, а не всю задачу. Task остаётся возобновляемой до final reap. Архивная сессия никогда автоматически не привязывается к другому task ID.

## Где код экономит токены без потери качества

| Механика | Code-owned реализация | Зачем |
|---|---|---|
| Готовность сессии | `session-preflight.py`, config/dependency checks | Один быстрый локальный проход заменяет повторный осмотр моделью. Для optional-компонентов печатаются точные repair commands. |
| Выбор модели | `config/model-routing.toml` + `model_routing.py` | Конкретные defaults лежат в одном месте; resolved route сохраняется в task metadata. Нет хардкода имени модели в десятках файлов. |
| Repo/plan/context candidates | dispatch resolver + task registry | Проверенные ID и пути заменяют токены на угадывание проекта, плана, окна и прошлой сессии. |
| Изменение вольта | `vault-write.py` | Одна optimistic journaled-транзакция вместо серии хрупких edits страницы, log, hot, plan и manifest. |
| Поиск | section BM25 + optional local embeddings | Модель получает лучшие ограниченные секции, а не целые папки и повторяющиеся страницы. |
| Очистка web | `defuddle` до synthesis | Навигация, реклама и boilerplate не занимают контекст. |
| Документы | cached stdlib/Docling pipeline | OCR и parsing переиспользуются по hash источника; неизменный PDF не читается заново моделью. |
| Review transport | operation-scoped JSON outbox + deterministic `drive` | Не нужно копировать длинные пути и свободный текст между терминалами. |
| Acceptance | semantic per-cell fingerprints | Docs/data-only изменение переиспользует evidence; модель вызывается только для поведенчески затронутых ячеек. |
| Наблюдение | content-free heartbeat + numeric telemetry | Можно отличить активную работу от зависания, не сохраняя prompt, response или текст экрана. |
| Финализация | `reap-runner.py` | Review archive, result routing, plan close, reindex, validation и exact exit проходят по одному fail-closed контракту. |

Требования, интерпретация, synthesis, code review и оценка рисков остаются моделям и людям. Hashing, routing, schemas, filesystem bookkeeping и retry policy остаются коду.

## Кросс-модельное ревью

Claude может реализовать, а Codex — ревьюить; Codex может реализовать, а Claude — ревьюить. По умолчанию выбирается противоположный runtime. Явный override фиксируется, а не превращается в silent fallback.

Review operation хранит:

- opaque project, task, lane, operation, runtime, model и permission-domain ID;
- ветку и стабильный baseline;
- product-read-only mandate и единственный isolated outbox;
- typed severity, evidence, recommendation, verification gaps и residual risks;
- безопасные переходы через `review-dispatch drive --apply-action`;
- same-session verification после исправлений executor'а;
- долговременный review archive, связанный с итогом задачи.

Reviewer не может push/publish, менять product-файлы или расширять scope. Warning можно исправить и подтвердить автоматически внутри утверждённой задачи. Blocking-решение о scope, security, permissions, migration, destructive action или external effect возвращается пользователю.

Это ограниченная проверка, а не формальная верификация: две модели могут разделить одну ошибочную предпосылку. Надёжность даёт комбинация независимого контекста, тестов, typed evidence и явной человеческой границы.

## Obsidian как долговременная память

У каждой долговременной страницы есть typed frontmatter, даты, tags, provenance сессий и детерминированный адрес `c-NNNNNN`. Pathless `[[wikilinks]]` сохраняют portability в Obsidian, GitHub, текстовом редакторе и другом агенте.

```text
модель предлагает структурированный контент
  -> duplicate/title/link checks
  -> одна JSON-транзакция
  -> optimistic SHA-256 validation
  -> crash-safe journal
  -> reindex
  -> whole-vault validation
  -> scoped commit в Stop pipeline
```

Одинаково хорошо можно вести личный дневник, командную архитектурную базу, решения codebase или коллекцию DevOps-runbooks. Credentials и machine-local runtime state в вольт попадать не должны.

DragonScale Memory добавляет детерминированные адреса, fold по content hash, boundary-first research и semantic tiling для близких дубликатов. `wiki/log.md` — append-only operational history, `wiki/hot.md` — ограниченный актуальный срез, `.vault-meta/` — регенерируемые индексы.

## Retrieval: hybrid по умолчанию, полезный без embeddings

Основная единица поиска — H2/H3-секция до 800 слов с overlap 100 слов. Sparse ranking индексирует title, tags, headings и body Unicode-токенизатором с русскими стоп-словами. На страницу возвращается лучший heading/snippet.

При наличии локальных Ollama и `bge-m3` dense multilingual channel объединяется со sparse через rank fusion. Если их нет, полноценный sparse-путь продолжает работать, а session preflight один раз объясняет, как включить улучшение. Система не делает вид, будто hybrid search уже запущен.

Изменения retrieval проходят gate на committed RU/EN goldset: hit@1, hit@5, MRR@10, recall и section NDCG.

## Документы: сначала локальная нормализация, потом модель

`wiki-ingest` принимает Markdown, text, JSON, YAML, CSV, local HTML, PDF, DOCX, PPTX, XLSX, OpenDocument, EPUB и scans. Source остаётся read-only.

```text
локальный источник
  -> format/size/page checks
  -> stdlib fast path для text-like файлов
     ИЛИ isolated pinned Docling + EasyOCR для binary/scans
  -> content-addressed artifact
  -> quality gate
  -> synthesis в связанные страницы
  -> одна vault transaction
```

Docling работает **до** LLM. Bootstrap заранее получает layout/table и `ru,en` OCR artifacts и запрещает remote services, external plugins и runtime downloads во время conversion. Неизменный файл берётся из cache. Missing dependency или плохой extraction возвращает typed action, а не молча отправляет binary модели.

Подробности: [локальный ingest документов](docs/document-ingestion.md).

## Защищённый research

Networked research разделён на два контекста:

1. web-enabled fetcher без доступа к вольту;
2. networkless synthesizer, который получает только validated artifact и пишет через vault contract.

Persistent task lanes сохраняют provider context только внутри точной task/isolation domain, поэтому follow-up research не стартует с нуля. У каждой операции свежий scratch. `unsafe-research` — отдельный явно разрешаемый single-context escape hatch; он не является fallback защищённого режима.

## 29 скиллов из коробки

Claude вызывает их через plugin UI (`/skill`), Codex — через repo-local marketplace (`$llm-obsidian:skill`). Механика лежит в `skills/<name>/SKILL.md`, поэтому другой coding agent может следовать ей вручную.

| Область | Скиллы |
|---|---|
| **Ориентация и согласование** | `wiki` знакомит с вольтом; `clarify` проводит одно-вопросное интервью по требованиям и дизайну до реализации. |
| **Capture и запись** | `save`, `save-plan`, `journal`, `backlog`, `daily`, `agenda` превращают разговоры и датированные дела в канонические данные. |
| **Доступ к знаниям** | `wiki-query`, `find-session`, `wiki-lint`, `wiki-fold` ищут, проверяют и компактно сворачивают историю. |
| **Документы и web** | `wiki-ingest`, `defuddle`, `autoresearch`, `unsafe-research` нормализуют источники и явно разделяют trust domains. |
| **Мышление и коммуникация** | `draft` предлагает redacted-ответы; `learn` учит по заметкам; `distill-runbook` превращает sanitized shell history в исполняемую человеком процедуру. |
| **Obsidian-native output** | `obsidian-markdown`, `obsidian-bases`, `canvas` создают корректные links/properties, database views и визуальные canvases. |
| **Task orchestration** | `dispatch`, `dispatch-workspace`, `review-dispatch`, `review-send`, `reap-send`, `reap`, `close` реализуют видимый multi-session lifecycle. |

Router даёт soft hints для “clarify before code”, “grill me” и русских формулировок, но ничего не навязывает. Session-start nudge один раз сообщает о missing optional dependencies, stale indexes и due folds, а не повторяет предупреждение на каждом запросе.

## Внешние инструменты и зачем они нужны

| Инструмент | Обязателен? | Роль |
|---|---:|---|
| **macOS + Xcode Command Line Tools** | Поддерживаемая цель | Git/toolchain и проверенный host для cmux, launchd и unattended lifecycle. |
| **Python 3.9+** | Да | Portable deterministic core: writer, retrieval, schemas, runners, telemetry, validation, tests. |
| **Git** | Да | История, evidence, isolated worktrees, review baselines и release provenance. |
| **Obsidian** | Core UX | Просмотр/редактирование Markdown, backlinks, Bases, Canvas, Tasks и Excalidraw. Скриптам не нужен запущенный GUI. |
| **Claude Code** | Один из first-class agents | Coordinator, executor, bounded subagent host или reviewer через официальный CLI. |
| **Codex CLI** | Один из first-class agents | Те же роли через официальный CLI, repo marketplace, profiles и общий Stop pipeline. |
| **cmux** | Для multi-session orchestration | Видимые splits/workspaces, exact surface IDs, interactive resume, notifications и cleanup lifecycle. |
| **Homebrew** | Bootstrap helper | Ставит недостающие macOS-зависимости вроде `uv`. |
| **uv** | Для default document setup | Изолированная pinned Docling-среда без загрязнения agent Python. |
| **Docling + EasyOCR** | Default; optional для text-only | Локальные PDF/Office/EPUB, tables и русский/английский OCR до LLM. |
| **Ollama + `bge-m3`** | Optional, рекомендуется | Локальные multilingual embeddings и semantic duplicate checks; fallback — sparse. |
| **mcp-proxy** | Optional gateway core | Один pinned HTTP gateway вместо копии каждого MCP server в каждом terminal. |
| **Context7 MCP** | Optional example | Актуальная документация библиотек через gateway с пользовательским API key. |
| **DCG** | Optional, рекомендуется | Preflight опасных команд для обоих CLI; defense in depth, не sandbox. |
| **Obsidian Tasks** | Optional UI | Отображение планов/reminders и agenda views; Python contract остаётся источником истины. |
| **Excalidraw** | Optional UI | Диаграммы в вольте; bootstrap умеет проверить/починить pinned asset. |
| **VoiceInk** | Optional | Голосовой ввод на macOS в любой CLI без отдельного agent protocol. |
| **launchd** | Системный сервис macOS | Держит MCP gateway доступным между terminal sessions. |

Облачная модель или платный сервис не поставляется вместе с репозиторием. Credentials optional-сервисов лежат вне Git в user-owned config.

## MCP без process zoo

Локальный HTTP gateway запускает один pinned [mcp-proxy](https://github.com/TBXark/mcp-proxy) на машину. Claude и Codex подключаются к стабильным `127.0.0.1` routes вместо запуска копии каждого stdio-server в каждом terminal.

```bash
cp scripts/mcp-gateway/config.json.example scripts/mcp-gateway/config.json
cp scripts/mcp-gateway/secrets.env.example ~/.config/mcp-gateway/secrets.env
chmod 600 ~/.config/mcp-gateway/secrets.env

# Опциональный Context7:
# CONTEXT7_API_KEY=...

scripts/mcp-gateway/mcp-gateway.sh doctor
scripts/mcp-gateway/mcp-gateway.sh install
scripts/mcp-gateway/mcp-gateway.sh health
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
```

Gateway экономит процессы, RAM и cold starts. Он не уменьшает tool schemas, уже загруженные в model context, поэтому тяжёлые серверы должны жить в opt-in `.mcp-profiles/`. Полный гайд: [MCP gateway](docs/mcp-gateway.md).

## Быстрый старт

Нужны macOS, Xcode Command Line Tools, Git, рабочий Python 3.9+, Obsidian и хотя бы один из Claude Code / Codex CLI.

```bash
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-clean-machine.sh
```

Bootstrap:

- сохраняет существующие Obsidian settings и secrets;
- инициализирует vault и managed plugin assets;
- генерирует Claude/Codex plugin metadata;
- проверяет или ставит pinned MCP proxy;
- устанавливает isolated Docling + RU/EN OCR, если не указан `--skip-docling`;
- печатает конкретные repair steps и не угадывает credentials.

Откройте каталог как Obsidian vault и запустите агент в нём же:

```bash
claude
# или
codex
```

Для Claude добавьте local marketplace/plugin через plugin UI. Для Codex:

```bash
python3 scripts/codex-adapter.py --apply
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
codex plugin marketplace add "$(pwd)"
codex plugin add llm-obsidian@llm-obsidian-codex
```

После установки/обновления начните новый Codex thread, чтобы host перечитал registry скиллов.

### Опциональные local embeddings

```bash
brew install ollama
brew services start ollama
ollama pull bge-m3
```

Session preflight сообщает о degraded hybrid retrieval один раз за сессию и даёт точную install-команду.

### Первые полезные запросы

```text
познакомь меня с вольтом
сделай clarify перед кодом
сохрани это
ingest ~/Downloads/design.pdf
найди предыдущую сессию про инцидент
dispatch утверждённого плана
покажи результат другой модели на review
```

В Codex используйте явные имена: `$llm-obsidian:wiki-query`, `$llm-obsidian:clarify`, `$llm-obsidian:review-dispatch` и т.д.

## Тестирование и release evidence

```bash
make test                 # полный hermetic suite; без сети и Ollama
make bench-retrieval      # measured ranking gate
make acceptance-check     # model-free matrix/dependency contract
make acceptance-live      # только поведенчески затронутые live cells
```

Release-матрица содержит 29 skills × 2 runtimes = 58 cells. В v2.1.2 используются минимальный committed seed vault, deterministic synthetic commits, точные runtime registrations, semantic fingerprints, atomic checkpoints и integrity-protected reuse. Изменение README или release notes не запускает 58 платных model sessions; необъявленная behavioral dependency остановит check до открытия модели.

Acceptance heartbeat хранит только stage/status/counters/timestamps. Telemetry schema отвергает prompts, responses, commands, snippets, page bodies, queries и error text. См. [acceptance architecture](docs/acceptance-architecture.md) и [pipeline observability](docs/pipeline-observability.md).

## Security и trust boundaries

- Официальные auth, subscriptions, limits и safety controls Claude/Codex сохраняются.
- Reviewers product-read-only; isolated fetcher не читает vault; synthesizer не имеет сети.
- Vault writes используют optimistic hashes и durable recovery journal.
- Credentials лежат в user-owned файлах вроде `~/.config/mcp-gateway/secrets.env`, не в репозитории.
- Task metadata и callbacks проверяются строгими schemas и exact IDs.
- DCG, host sandbox, review и tests — разные слои; ни один не объявляется доказательством безопасности.
- Push, deployment, publication, destructive history, credentials и material scope требуют явной authority.
- Repo-owned mechanism failure чинится автоматически только в узкой reversible границе; Stop hooks fail-closed.

Точные правила: [unattended pipeline](docs/unattended-pipeline-operations.md), [task sessions](docs/task-sessions.md), [failure-to-repair contract](docs/skill-references/failure-repair-contract.md).

## Платформы и ограничения

- **macOS — maintained и release-gated платформа.** cmux, launchd, status integration, document setup и полный unattended lifecycle тестируются там.
- **Linux имеет базовую script-level portability, но не full product support.** Core Python/Bash может работать, однако cmux/launchd-centered UX не обещан и не входит в release gate.
- **Windows не поддерживается.**
- **First-class runtimes — только Claude Code и Codex CLI.** Новый adapter имеет смысл лишь при сохранении тех же contracts/tests.
- **cmux нужен для видимых dispatch/review/research lanes.** Wiki, retrieval, writer и большинство productivity skills работают без него.
- **Cross-model review не является формальной верификацией.**
- **Mobile не основная рабочая поверхность.** Obsidian-файлы можно синхронизировать, но pipeline рассчитан на большие проекты с ноутбука.

В README нет спекулятивного roadmap. Здесь описано то, что реализовано и проверено сейчас; будущая platform/runtime работа должна входить только с теми же typed lifecycle, permission boundaries и acceptance evidence.

## Дополнительная документация

| Тема | Документ |
|---|---|
| Model inheritance и overrides | [Model routing](docs/model-routing.md) |
| Различия Claude/Codex | [Runtime capability matrix](docs/runtime-capabilities.md) |
| Dispatch, review, watchdog, close | [Unattended pipeline](docs/unattended-pipeline-operations.md) |
| Persistent task/model/domain lanes | [Task sessions](docs/task-sessions.md) |
| Acceptance fingerprints и reuse | [Acceptance architecture](docs/acceptance-architecture.md) |
| Numeric content-free metrics | [Pipeline observability](docs/pipeline-observability.md) |
| PDF/Office/OCR | [Document ingestion](docs/document-ingestion.md) |
| MCP service operations | [MCP gateway](docs/mcp-gateway.md) |
| Addresses, folds, memory model | [DragonScale guide](docs/dragonscale-guide.md) |

## Благодарности и лицензия

MIT; см. [LICENSE](LICENSE). История исходного проекта и сохранённый copyright описаны в [ATTRIBUTION.md](ATTRIBUTION.md). Система была обкатана в приватном DevOps-вольте и затем обобщена для этого репозитория.
