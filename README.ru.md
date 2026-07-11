# llm-obsidian

**Самоорганизующийся второй мозг для Obsidian под управлением LLM-агента.** Ваш ассистент строит и поддерживает структурированную, насквозь перелинкованную вики из разговоров, источников и решений, а затем действительно *находит* в ней нужное: на русском или английском, полностью локально.

🇬🇧 **Read in English: [README.md](README.md)**

Основано на [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) от AgriciDaniel (реализации паттерна *LLM Wiki* Андрея Карпатого) и серьёзно переработано: retrieval-стек, write path, хуки и MCP-интеграция были перепроектированы и месяцами обкатывались в бою как ежедневная рабочая память DevOps-инженера, прежде чем превратиться в этот generic-релиз. Поддерживаемые агенты: Claude Code и Codex CLI. Отсюда и имя: *llm*-obsidian, а не *claude*-obsidian.

---

## Что вы получаете

- **Вики, которая растёт сама.** `ingest <path|URL>` превращает сырой материал в 8-15 перелинкованных типизированных страниц; `/save` файлит инсайты из любого разговора; `/autoresearch` запускает автономные research-циклы; каждый одобренный план автоматически захватывается в `wiki/plans/`.
- **Retrieval, который измеряют, а не оценивают на глазок.** H2/H3-секции режутся до 800 слов с overlap 100, ранжируются sparse-каналом и дедуплицируются до лучшего heading/snippet на страницу; optional `bge-m3` добавляется через RRF. Goldset содержит 48 RU/EN-запросов, половина held-out; `make bench-retrieval` блокирует регресс hit@5/MRR больше 0,02.
- **RU-first, local-first.** Обязательный sparse-канал понимает кириллицу и смешанный технический словарь без отдельного сервиса. Опциональные `bge-m3` embeddings остаются локально; cloud API не нужен.
- **Транзакционный write path.** Агентские create/update страниц, merge манифеста, `wiki/log.md` и `wiki/hot.md` идут через `scripts/vault-write.py`: строгий frontmatter, optimistic SHA-256, капы и crash-safe roll-forward из журнала.
- **Индустриальный Stop-хук.** На каждом ходе: recovery незавершённых writes, reindex, self-heal section retrieval, опциональный dense refresh, строгая валидация и commit только vault-owned путей. Сессии сериализует stdlib `fcntl`; validation failure блокирует commit и оставляет dirty state видимым.
- **MCP без зоопарка процессов.** Локальный HTTP-гейтвей (один launchd-сервис) стоит перед всеми вашими MCP-серверами: один набор долгоживущих процессов на машину вместо дублей на каждый терминал. Из коробки преднастроен [context7](https://context7.com): добавьте одну строку с API-ключом, и документация библиотек доступна в каждой сессии.
- **Параллельная работа, опционально.** `/dispatch` порождает задачу в отдельном git worktree + сплите [cmux](https://github.com/wandb/cmux) (с передачей плана более дешёвой модели), `/reap` файлит её результаты обратно в вики. Требуется cmux; всё остальное работает без него.

## Почему этот форк

| | upstream claude-obsidian | llm-obsidian |
|---|---|---|
| Единица retrieval | contextual-prefix chunk-каскад | H2/H3-секции, cap 800 слов, overlap 100, лучший heading/snippet на уникальную страницу |
| Fusion | chunk-каскад | обязательный sparse title/tag/heading/body + optional local `bge-m3` RRF; явный degraded metadata |
| Retrieval QA | разовые бенчмарк-скрипты | 48 RU/EN-запросов, 50% held-out, baseline и gate регресса hit@5/MRR ≤0,02 |
| write path вольта | свободные правки моделью | транзакционный `vault-write.py`: pages + manifest + log/hot, optimistic hashes, recovery journal |
| Turn-end hook | inline-команды в hooks.json | `stop-hook.py`: `fcntl`, recovery, self-healing индексы, validation gate, scoped commit |
| Skill routing | только descriptions | data-driven хук `skill-router` (regex-правила → мягкие подсказки) + maintenance-подсказки `session-nudge` |
| MCP | stdio-серверы на каждую сессию | HTTP-гейтвей: 1 набор процессов на машину, секреты вне репо, тулинг `doctor`/`smoke`/`update`, паттерн профилей для тяжёлых серверов |
| Оркестрация | нет | workflow `/dispatch` / `/reap`: worktree + сплит с авто-передачей плана |
| Productivity-слой | нет | `/journal`, `/daily`, `/backlog`, `/find-session`, `/draft`, `/distill-runbook`, `/save-plan` |
| Язык документации | EN | EN + RU, аудитория RU-first |

Сознательно **не** перенесено из upstream: methodology modes, `/think`, транспортный слой `wiki-cli` и contextual-prefix chunk-каскад (его лучший тир требует облачных API-вызовов; наш стек дружит с flat-подпиской и эмбеддит локально).

DragonScale Memory (fold-роллапы, детерминированные адреса страниц `c-NNNNNN`, semantic tiling линт дублей, boundary-first autoresearch) унаследована из upstream и сохранена, но пороги тайлинга перекалиброваны под `bge-m3`, а процедура перекалибровки под свой вольт задокументирована.

## Быстрый старт

Требования: macOS (Linux в основном работает, launchd-части только для macOS), [Obsidian](https://obsidian.md), [Claude Code](https://claude.com/claude-code) или Codex CLI, Python 3.9+, git. Опционально: [ollama](https://ollama.com) для семантического retrieval (рекомендуется), cmux для параллельных задач.

```bash
# 1. Забираем вольт
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-clean-machine.sh  # вольт + MCP gateway config + Codex metadata

# 2. Открываем папку как вольт в Obsidian, затем запускаем в ней Claude Code
claude
# > /wiki                        # bootstrap: агент проведёт вас через персонализацию
```

`setup-clean-machine.sh` сохраняет существующие настройки Obsidian, MCP-конфиги
и секреты. Флаг `--reset-obsidian` нужен только для намеренного сброса трёх
управляемых файлов; перед сбросом скрипт делает полную резервную копию
`.obsidian`. Добавьте `--install-service` после заполнения
`~/.config/mcp-gateway/secrets.env`, а `--install-codex-plugin` — когда Codex CLI
уже установлен.

Установка как плагина (skills + hooks) вместо клонирования или в дополнение к нему:

```bash
claude plugin marketplace add zerg-su/llm-obsidian
# затем: /plugin install llm-obsidian
```

### Локальные эмбеддинги (рекомендуется)

Опциональный dense retrieval секций и tiling-линт дублей работают на локальной ollama с `bge-m3` (1024-dim, 8k контекста, 100+ языков, ~1.2 GB):

```bash
brew install ollama
brew services start ollama       # или: ollama serve
ollama pull bge-m3
curl -s http://127.0.0.1:11434 && echo " ollama is up"
```

Без ollama ничего не ломается: retrieval возвращает полный sparse-результат с `degraded=true`, а Stop пишет bounded retry-marker для dense.

### Опциональный backup памяти агента

Backup отдельной auto-memory Claude по умолчанию выключен и никогда не ищет
соседние vault. Для явного opt-in задайте `CLAUDE_MEMORY_DIR` или выполните:

```bash
cp config/memory-backup.example.json .vault-meta/memory-backup.json
# укажите source и поставьте enabled=true
python3 scripts/memory-backup.py --status
```

До изменения `.claude-memory/` helper санитизирует весь кандидатный snapshot и
сканирует и его, и существующий backup. Остаточный credential-паттерн блокирует
все записи и turn-end commit.

### Первое знакомство

```text
ingest ~/Downloads/some-article.pdf     # источник -> структурированные вики-страницы
что ты знаешь про <тему>?               # ответы с цитатами из ВАШИХ заметок (работает и на EN)
/save                                   # зафайлить текущий разговор
/journal план на завтра: ...            # журнал по датам
/backlog add не забыть продлить домен   # однострочный capture-inbox
lint the wiki                           # health check: сироты, битые ссылки, дубли
```

## MCP HTTP гейтвей

Если оставить за скобками tool-схемы, главный практический налог MCP: процессы. Каждая терминальная сессия поднимает собственную копию каждого stdio-сервера. Гейтвей держит **один** набор дочерних процессов на машину за [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy); сессии подключаются по HTTP и переживают рестарты гейтвея. Bootstrap устанавливает только артефакт и SHA-256 из `scripts/mcp-gateway/mcp-proxy.lock.json`, без скачивания непроверенного `latest`.

```bash
# одноразово
bin/setup-clean-machine.sh --skip-vault

# context7 (MCP с документацией библиотек): возьмите бесплатный ключ на https://context7.com
# и добавьте ОДНУ строку
#   CONTEXT7_API_KEY=ctx7sk-...
# в ~/.config/mcp-gateway/secrets.env: это вся настройка.

scripts/mcp-gateway/mcp-gateway.sh doctor    # read-only pre-flight: покажет, чего не хватает
scripts/mcp-gateway/mcp-gateway.sh install   # launchd-сервис, автостарт при логине
scripts/mcp-gateway/mcp-gateway.sh health    # настоящий MCP handshake с первым дочерним сервером
```

Единственный источник локального порта — `scripts/mcp-gateway/runtime.env`.
После его изменения выполните `mcp-gateway.sh sync-config --apply`: gateway и
дефолтный клиентский JSON обновятся вместе.

Добавить свои серверы: одна правка конфига (stdio-дети с `command`/`args`/`env`, remote-дети с `url`; плейсхолдеры `${VAR}` резолвятся из `secrets.env`, а `doctor` сам выводит список требуемых ключей). Полный гайд, чеклист добавления сервера, disaster recovery и выстраданные гочи: **[docs/mcp-gateway.md](docs/mcp-gateway.md)**.

Одна оговорка, которую стоит знать заранее: гейтвей экономит процессы, RAM и холодные старты, но **не** уменьшает tool-схемы в вашем контексте. Тяжёлые и редко используемые серверы держите в `.mcp-profiles/` (см. README там) и подгружайте per-session через `claude --mcp-config`.

Для Codex синхронизируйте HTTP pointers в TOML:

```bash
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
```

Команда пишет repo-local `.codex/config.toml`, убирает дубли llm-obsidian MCP из
`~/.codex/config.toml` и создаёт optional profile overlays вроде
`~/.codex/llm-obsidian-mcp.config.toml`.

## Скиллы

| Группа | Скиллы |
|---|---|
| Ядро вики | `/wiki` (bootstrap), `/wiki-ingest`, `/wiki-query`, `/wiki-lint`, `/wiki-fold`, `/save`, `/close`, `/autoresearch`, `/canvas`, `/defuddle` |
| Продуктивность | `/journal` (планировщик по датам), `/daily` (статус в конце дня), `/backlog` (capture-inbox), `/find-session`, `/draft` (советник по ответам), `/distill-runbook` (команды сессии → copy-paste runbook), `/learn` (тьютор по вашим заметкам), `/save-plan` |
| Оркестрация (нужен cmux) | `/dispatch`, `/review-dispatch`, `/review-send`, `/reap`, `/reap-send` (Codex: `$llm-obsidian:*`) |
| Справочные | `obsidian-markdown`, `obsidian-bases` |

Роутер на `UserPromptSubmit` подсказывает подходящий скилл по regex-правилам из `.claude/skill-rules.json` (мягкие подсказки, никогда не mandatory); `session-nudge` поднимает просроченный maintenance (возраст линта, назревший fold, устаревшие бэкапы, совет skill-of-the-day).

## Тестирование

Вся механика покрыта герметичными тест-сьютами: без сети, ollama не нужна:

```bash
make test          # аллокатор адресов, tiling, boundary, vault-write/validate,
                   # безопасный stop-хук + latency, bm25 + fusion, bench-харнес, router
make test-gateway  # management-слой MCP-гейтвея (офлайн, фейковый MCP-сервер)
```

## Codex CLI

Codex использует сгенерированный local plugin marketplace, а не legacy symlink:

```bash
python3 scripts/codex-adapter.py --apply
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
codex plugin marketplace add "$(pwd)"
codex plugin add llm-obsidian@llm-obsidian-codex
```

После установки/обновления начните новый Codex thread. Явный вызов скиллов:
`$llm-obsidian:save`, `$llm-obsidian:wiki-query`,
`$llm-obsidian:review-dispatch`,
`$llm-obsidian:close` и т.д.
Prompt/session/tool hooks остаются Claude-specific. Установленный Codex plugin
запускает общий безопасный `Stop` pipeline, но не заявляет автоматический parity
для `SessionStart`, `UserPromptSubmit` или `PostToolUse`. См.
[матрицу runtime-возможностей](docs/runtime-capabilities.md).

Хелперы для Codex limits тоже лежат в репо:

```bash
python3 scripts/codex-limit-monitor.py --install
codex-limit-status --scope recent --once
.codex/codex-limits-status.py --with-pct --compact
```

Для cmux status integration зарегистрируйте `.codex/update-cmux-limits.sh` в
Codex hooks `SessionStart`, `UserPromptSubmit` и `Stop`.

## DCG Guard

В репо есть portable policy и installer для Destructive Command Guard:

```bash
bin/setup-dcg.sh          # ставит dcg если его нет, конфиг, Codex + Claude hooks
bin/setup-dcg.sh --check  # показывает drift без записи файлов
bash scripts/dcg-test-suite.sh
```

Policy лежит в `config/dcg/config.toml`, hook-template — в
`.github/hooks/dcg.json`. Installer делает backup существующих hook/config
файлов с суффиксом `.bak-dcg-*` и merge'ит только `PreToolUse`/`Bash` запись dcg.
Smoke-suite по умолчанию проверяет repo policy в изолированном временном HOME;
для проверки live allowlist/config используйте `DCG_TEST_USE_USER_CONFIG=1`.

## Roadmap

- **Codex prompt/tool hook parity**: skills, MCP sync, общие scripts и безопасный `Stop` pipeline поддержаны; `SessionStart`, `UserPromptSubmit` и `PostToolUse` остаются host-specific.
- RU-локализация тел скиллов (сейчас EN: инструкции выполняются измеримо лучше; русские trigger-слова уже работают).
- Больше примеров MCP-серверов в конфиге гейтвея.
- Generic-порты оставшихся инкубаторных скиллов (утренний брифинг, дайджест коммитов, verification gates, debug-дисциплина).

## Благодарности и лицензия

MIT с сохранением копирайта upstream: см. [LICENSE](LICENSE) и [ATTRIBUTION.md](ATTRIBUTION.md). Родословная: паттерн LLM Wiki Андрея Карпатого → [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) → приватный DevOps-вольт, где эти механики инкубировались → этот репозиторий.
