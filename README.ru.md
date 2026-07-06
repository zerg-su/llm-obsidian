# llm-obsidian

**Самоорганизующийся второй мозг для Obsidian под управлением LLM-агента.** Ваш ассистент строит и поддерживает структурированную, насквозь перелинкованную вики из разговоров, источников и решений, а затем действительно *находит* в ней нужное: на русском или английском, полностью локально.

🇬🇧 **Read in English: [README.md](README.md)**

Основано на [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) от AgriciDaniel (реализации паттерна *LLM Wiki* Андрея Карпатого) и серьёзно переработано: retrieval-стек, write path, хуки и MCP-интеграция были перепроектированы и месяцами обкатывались в бою как ежедневная рабочая память DevOps-инженера, прежде чем превратиться в этот generic-релиз. Поддерживаемые агенты: Claude Code и Codex CLI. Отсюда и имя: *llm*-obsidian, а не *claude*-obsidian.

---

## Что вы получаете

- **Вики, которая растёт сама.** `ingest <path|URL>` превращает сырой материал в 8-15 перелинкованных типизированных страниц; `/save` файлит инсайты из любого разговора; `/autoresearch` запускает автономные research-циклы; каждый одобренный план автоматически захватывается в `wiki/plans/`.
- **Retrieval, который измеряют, а не оценивают на глазок.** Гибридный поиск: dense-эмбеддинги (локальная [ollama](https://ollama.com) + `bge-m3`) в fusion с BM25 и tag-префильтром. Бенчмарк-харнес (`make bench-retrieval`) считает hit@1 / hit@5 / MRR@10 на вашем собственном goldset'е: ни одно изменение ранжирования не уходит в релиз, не сдвинув цифры.
- **RU-first, local-first.** `bge-m3` справляется с русской прозой и транслитерированными техническими терминами, на которых сыплются англоцентричные embedding-модели (на нашем калибровочном вольте dense-канал после замены модели вырос с hit@1 0.27 до **0.85**). Ноль облачных API-вызовов: всё эмбеддится на вашей машине. Если ваши заметки написаны по-русски или на привычной смеси русского с английским жаргоном, это ровно тот сценарий, под который стек и калибровался.
- **Детерминированный write path.** `wiki/log.md` (append-only журнал) и `wiki/hot.md` (кэш свежего контекста на ~500 слов, загружается при старте сессии) пишутся только через `scripts/vault-write.py`, который навязывает жёсткие капы: hot-кэш остаётся кэшем и не гниёт во второй журнал.
- **Индустриальный Stop-хук.** На каждом завершении хода: reindex, пересборка BM25, инкрементальный рефреш dense-эмбеддингов, sanitized-бэкап памяти, автокоммит. Всё сериализовано под `flock`, чтобы параллельные сессии не портили друг друга, с latency-телеметрией по фазам и предупреждением о медленном ходе.
- **MCP без зоопарка процессов.** Локальный HTTP-гейтвей (один launchd-сервис) стоит перед всеми вашими MCP-серверами: один набор долгоживущих процессов на машину вместо дублей на каждый терминал. Из коробки преднастроен [context7](https://context7.com): добавьте одну строку с API-ключом, и документация библиотек доступна в каждой сессии.
- **Параллельная работа, опционально.** `/dispatch` порождает задачу в отдельном git worktree + сплите [cmux](https://github.com/wandb/cmux) (с передачей плана более дешёвой модели), `/reap` файлит её результаты обратно в вики. Требуется cmux; всё остальное работает без него.

## Почему этот форк

| | upstream claude-obsidian | llm-obsidian |
|---|---|---|
| Dense retrieval | contextual-prefix каскад, облачный API-тир для лучших результатов | локальная ollama `bge-m3`, без облачных вызовов, умеет русский (dense hit@1 0.27 → 0.85 на RU/EN goldset'е) |
| Fusion | chunk-каскад | scope-aware fusion: dense ранжирует то, что сам эмбеддит, BM25 *только инжектит* страницы вне dense-скоупа; настроено на goldset'е с held-out половиной после того, как обычный weighted RRF измеримо провалился |
| Retrieval QA | разовые бенчмарк-скрипты | постоянный харнес: goldset + `make bench-retrieval`, hit@1/hit@5/MRR@10 по каждому каналу, тесты деградаций |
| hot/log write path | свободные правки моделью | payload-API `vault-write.py` с детерминированными капами + `validate-vault.py` |
| Turn-end hook | inline-команды в hooks.json | `stop.sh`: сериализация flock'ом, атомарные записи, инкрементальный рефреш индексов, latency-телеметрия (`STOP_HOOK_SLOW`) |
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
bash bin/setup-vault.sh          # одноразовый провижининг (скачивает бинарь плагина Excalidraw)

# 2. Открываем папку как вольт в Obsidian, затем запускаем в ней Claude Code
claude
# > /wiki                        # bootstrap: агент проведёт вас через персонализацию
```

Установка как плагина (skills + hooks) вместо клонирования или в дополнение к нему:

```bash
claude plugin marketplace add zerg-su/llm-obsidian
# затем: /plugin install llm-obsidian
```

### Локальные эмбеддинги (рекомендуется)

Dense retrieval и tiling-линт дублей работают на локальной ollama с `bge-m3` (1024-dim, 8k контекста, 100+ языков, ~1.2 GB):

```bash
brew install ollama
brew services start ollama       # или: ollama serve
ollama pull bge-m3
curl -s http://127.0.0.1:11434 && echo " ollama is up"
```

Без ollama ничего не ломается: гибридный поиск автоматически деградирует до чистого BM25, а Stop-хук пропускает рефреш эмбеддингов.

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

Если оставить за скобками tool-схемы, главный практический налог MCP: процессы. Каждая терминальная сессия поднимает собственную копию каждого stdio-сервера. Гейтвей держит **один** набор дочерних процессов на машину за [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy); сессии подключаются по HTTP и переживают рестарты гейтвея.

```bash
# одноразово
mkdir -p ~/.local/bin ~/.config/mcp-gateway
# скачайте release-бинарь mcp-proxy под свою платформу -> ~/.local/bin/mcp-proxy && chmod +x
cp scripts/mcp-gateway/config.json.example scripts/mcp-gateway/config.json
cp scripts/mcp-gateway/secrets.env.example ~/.config/mcp-gateway/secrets.env
chmod 600 ~/.config/mcp-gateway/secrets.env
cp .mcp.json.example .mcp.json

# context7 (MCP с документацией библиотек): возьмите бесплатный ключ на https://context7.com
# и добавьте ОДНУ строку
#   CONTEXT7_API_KEY=ctx7sk-...
# в ~/.config/mcp-gateway/secrets.env: это вся настройка.

scripts/mcp-gateway/mcp-gateway.sh doctor    # read-only pre-flight: покажет, чего не хватает
scripts/mcp-gateway/mcp-gateway.sh install   # launchd-сервис, автостарт при логине
scripts/mcp-gateway/mcp-gateway.sh health    # настоящий MCP handshake с первым дочерним сервером
```

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
                   # stop-хук (flock + latency), bm25 + fusion, bench-харнес, router
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
`$llm-obsidian:review-dispatch`, `$llm-obsidian:close` и т.д.
Claude Code hooks остаются Claude-specific; Codex использует те же skills и
scripts, но не исполняет `.claude/hooks/`.

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

- **Codex hook parity**: skills и MCP sync поддержаны; автоматический Claude hook layer (`SessionStart`, `PostToolUse`, `Stop`) остаётся Claude Code specific.
- RU-локализация тел скиллов (сейчас EN: инструкции выполняются измеримо лучше; русские trigger-слова уже работают).
- Больше примеров MCP-серверов в конфиге гейтвея.
- Generic-порты оставшихся инкубаторных скиллов (утренний брифинг, дайджест коммитов, verification gates, debug-дисциплина).

## Благодарности и лицензия

MIT с сохранением копирайта upstream: см. [LICENSE](LICENSE) и [ATTRIBUTION.md](ATTRIBUTION.md). Родословная: паттерн LLM Wiki Андрея Карпатого → [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) → приватный DevOps-вольт, где эти механики инкубировались → этот репозиторий.
