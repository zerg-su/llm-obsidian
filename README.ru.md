<p align="center">
  <img src="docs/assets/llm-obsidian-banner.png" alt="Два терминальных агента обмениваются работой через Obsidian-хранилище знаний" width="100%">
</p>

# LLM Obsidian

**Единая долговечная рабочая среда для Claude Code и Codex CLI: общая память, общие скиллы, ограниченная контрактом оркестрация и кросс‑ревью разными моделями.** Она превращает разговоры, источники, планы и решения в структурированную Obsidian‑вики, а затем передаёт эти знания следующим агентским сессиям вместо очередного старта с нуля.

🇬🇧 **Read in English: [README.md](README.md)**

Это сознательно не универсальный роутер для любых агентов. Два first-class агента — Claude Code и Codex CLI; Obsidian хранит долговечную память; общую механику реализуют обычные Python- и shell-скрипты. Проект не заменяет эти CLI, не эмулирует провайдера и не обходит ограничения аккаунтов.

Workflow вырос из нескольких месяцев ежедневного использования как рабочая память DevOps‑инженера, а затем был извлечён из приватного вольта и обобщён.

---

## Зачем LLM Obsidian?

Даже сильный кодинг‑агент обычно остаётся временным процессом. Его локальный контекст заканчивается, личная память отличается от памяти другого CLI, а полезный план или ревью легко остаётся в истории терминала. Просто открыть два агента рядом недостаточно: им нужны общее место для памяти, контракт передачи работы и надёжное завершение без копирования свободного текста между окнами.

LLM Obsidian добавляет именно этот слой:

- **Память:** локальный Markdown‑вольт с provenance, ссылками, retrieval, lifecycle и транзакционными записями.
- **Скиллы:** одинаковые версионируемые workflow для capture, research, planning, review, ежедневной работы и обслуживания.
- **Оркестрация:** видимые task‑сплиты cmux, изолированные git worktree, типизированные контракты, watchdog и детерминированный reap.
- **Независимое ревью:** Claude может реализовать, а Codex проверить — или наоборот.
- **Защитные слои:** валидация, read-only роль ревьюера, ограниченные verify‑циклы, DCG и явная эскалация человеку при изменении scope или trust boundary.

На выходе получается одна рабочая система с несколькими модельными перспективами, а не набор несвязанных чатов.

## Почему не просто Claude Code? Почему не просто Codex?

Их по‑прежнему стоит использовать. LLM Obsidian — соединительный слой вокруг обоих.

| Что хорошо делает один CLI | Какой пробел закрывает LLM Obsidian |
|---|---|
| Решает задачу в текущем контексте | Переносит решения, источники и историю задачи в будущие сессии |
| Использует собственные команды и расширения | Даёт обоим агентам один версионируемый набор скиллов и один вольт |
| Проверяет собственную реализацию | Отправляет результат другой модельной семье по read-only review‑контракту |
| Выполняет долгую задачу | Наблюдает за ней в видимом сплите, замечает stall и закрывает окно только после валидированной передачи результата |
| Запрашивает разрешения по ходу работы | Переносит предсказуемые решения в одобренный план и поднимает только существенные неожиданности |

Разные модели ошибаются по‑разному. Кросс‑ревью полезно именно потому, что ревьюер не писал реализацию и не разделяет автоматически все предположения исполнителя. Это повышает уверенность, но не изображает формальное доказательство корректности.

## Архитектура в нескольких строках

```text
VoiceInk (опциональный голос)            ввод с клавиатуры
                 \                            /
                  v                          v
        ┌────────────── рабочая среда LLM Obsidian ───────────┐
        │                                                     │
        │   Claude Code  ⇄  typed handoffs  ⇄  Codex CLI     │
        │         \          cmux + worktrees          /      │
        │          └── общие скиллы + Obsidian-вольт ──┘      │
        │                    │                                │
        │       sparse retrieval + optional Ollama/bge-m3     │
        │       DCG + schemas + validation + Stop pipeline    │
        └────────────────────┬────────────────────────────────┘
                             │
              Claude реализует → Codex проверяет
              Codex реализует  → Claude проверяет
```

Obsidian хранит долговечное состояние, но автоматика не заперта внутри процесса Obsidian: канонические данные — Markdown и репозиторные скрипты. cmux нужен только для видимой многосессионной оркестрации. Ollama нужен только для опционального dense retrieval.

## Как проходит кросс‑ревью

Пример: Claude Code реализует, Codex проверяет.

1. Координатор превращает запрос в одобренный план и задаёт существенные вопросы до начала выполнения.
2. `/dispatch` записывает ограниченный task‑контракт, создаёт изолированный worktree и открывает интерактивную Claude Code‑сессию в cmux‑сплите.
3. Claude реализует и запускает проверки, не переспрашивая о действиях, которые уже входят в контракт.
4. `/review-dispatch` открывает read-only ревьюера Codex. Callback типизирован и привязан к конкретным task, review и baseline — это не вставка произвольного текста из терминала.
5. Принятые замечания возвращаются тому же исполнителю. Verify ограничен числом проходов; изменение scope, безопасности, доверия или необходимость разрушительного действия возвращается пользователю.
6. `/reap-send` валидирует итоговую сводку и provenance результата до записи координатором. cmux‑окно закрывается только после выхода агентского процесса.

Обратное направление использует тот же протокол: Codex реализует, Claude проверяет. Подробности: [маршрутизация моделей](docs/model-routing.md) и [runbook unattended‑пайплайна](docs/unattended-pipeline-operations.md).

## Автоматические проверки перед завершением

| Этап | Что проверяется |
|---|---|
| Dispatch | идентичность одобренного плана, task‑контракт, metadata worktree, сгенерированная команда, ролевая permission policy |
| Выполнение | состояние supervised‑процесса, heartbeat/progress evidence, observer-only stall‑уведомления, явный exit state |
| Review | read-only мандат, task/review ID, fingerprint baseline, схема callback, ограниченное число verify‑проходов |
| Reap | типизированная wiki summary, путь и hash результата, provenance задачи, terminal outcome, готовность close-on-exit |
| Vault Stop | recovery транзакций, sparse reindex/self-heal, строгая валидация вольта, scoped commit, fingerprinted optional dense refresh |

Watchdog не убивает вслепую агента, который явно продолжает работать. Сначала он сообщает о возможном stall; действительно блокирующие решения возвращаются координатору, а не угадываются в фоновом окне.

## Поддерживаемые агенты и соседние компоненты

| Runtime или компонент | Статус | Роль и границы |
|---|---|---|
| **Claude Code** | First-class | Координатор, исполнитель или opposite-model ревьюер через официальный CLI и собственный аккаунт/подписку пользователя. Лимиты провайдера сохраняются. |
| **Codex CLI** | First-class | Координатор, исполнитель или opposite-model ревьюер через официальный CLI и собственный доступ пользователя. Лимиты провайдера сохраняются. |
| **Obsidian** | Ядро данных и UX | Локальный Markdown‑вольт, ссылки, навигация и ручное редактирование. Файлы остаются полезными без агента. |
| **cmux** | Опционален; обязателен для оркестрации | Видимые task/reviewer‑сплиты, сокеты, lifecycle tracking и продолжаемые интерактивные сессии. Базовые wiki‑скиллы работают без него. |
| **Ollama + `bge-m3`** | Опциональная локальная модель | Dense multilingual embeddings и поиск дублей без hosted API и оплаты за вызов модели. Использует собственные disk/RAM; без Ollama полный sparse retrieval продолжает работать. |
| **DCG** | Опционален, рекомендуется | Preflight разрушительных команд для обоих CLI. Defense in depth, а не sandbox или доказательство безопасности. |
| **VoiceInk** | Опциональный слой ввода | Нативная macOS‑диктовка в любой CLI. Не входит в поставку и не является агентской интеграцией. |
| **Gemini CLI и другие агенты** | Сейчас не поддерживаются | Будущий адаптер возможен только с теми же гарантиями скиллов, контрактов, ревью и тестов. Совместимость сейчас не заявляется. |

Имена моделей меняются быстрее workflow. Дефолты, subscription-only маршруты и явные overrides описаны в [docs/model-routing.md](docs/model-routing.md), а не выдаются за вечную поддержку конкретного провайдера.

## Принципы дизайна

- **Долговечные файлы важнее скрытой памяти.** Существенный контекст можно проверить, сравнить diff и перенести.
- **Планируем один раз, выполняем в границах.** Предсказуемые решения принимаются до dispatch; unattended‑сессия наследует только этот мандат.
- **Другая модель — независимое ревью.** Ревьюер advisory и read-only; исправления и коммиты принадлежат исполнителю.
- **Типизированные контракты вместо terminal paste.** ID, hashes, schemas и явные terminal states делают handoff аудируемым.
- **Local-first retrieval.** Обязательный путь не требует embedding API; dense retrieval — локальное опциональное улучшение.
- **Fail closed на границах доверия.** Невалидный callback, изменившийся baseline, отсутствующий provenance или drift auth‑маршрута не продолжаются молча.
- **Внешние эффекты принадлежат пользователю.** Push, deploy, credentials, разрушительные действия и существенный scope не угадываются автоматически.

## Безопасность и доверие

LLM Obsidian **не обходит подписки, rate limits, authentication, safety controls или условия Claude, Codex и любых провайдеров**. Официальные CLI устанавливает и авторизует сам пользователь. Subscription-only Claude‑маршруты ревью отвергают API/provider overrides, а не начинают незаметно тратить API‑ключ; смена модели остаётся явной.

Ревьюеры запускаются с read-only мандатом. Секреты MCP‑сервисов хранятся вне репозитория. Записи вольта используют optimistic hashes и recovery journal. DCG блокирует известные разрушительные shell‑паттерны до выполнения, а sandbox/approval policy CLI и одобренный человеком task‑контракт остаются отдельными слоями. Ни один guardrail не устраняет ошибки модели: кросс‑ревью и тесты повышают надёжность, но не превращают сгенерированную работу в гарантию.

## Реальные сценарии

- **Отправить задачу и отойти от терминала:** одобрить план, запустить одну ограниченную реализацию, получить ревью другой моделью и вернуться к валидированному результату вместо цепочки permission‑диалогов.
- **Создать долговечную техническую память:** ingest документации и решений, поиск по RU/EN‑понятиям и сохранение provenance для следующих агентов.
- **Исследовать, не загрязняя vault-aware контекст:** получить источники в изолированной сессии, синтезировать без сети и записать всё одной vault‑транзакцией.
- **Работать голосом:** диктовать запросы в Claude Code или Codex через [VoiceInk](https://github.com/beingpax/VoiceInk), сохраняя те же скиллы и safety gates.
- **Вести личный операционный цикл:** journal‑планы, backlog, daily‑сводки, поиск прошлых сессий и превращение shell‑истории в выполняемые человеком runbook.

## Основные возможности

- **Вики, которая растёт сама.** `ingest <path|URL>` превращает сырой материал в 8-15 перелинкованных типизированных страниц; `/save` файлит инсайты из любого разговора; `/autoresearch` запускает автономные research-циклы; каждый одобренный план автоматически захватывается в `wiki/plans/`.
- **Retrieval, который измеряют, а не оценивают на глазок.** H2/H3-секции режутся до 800 слов с overlap 100, ранжируются sparse-каналом и дедуплицируются до лучшего heading/snippet на страницу; optional `bge-m3` добавляется через RRF. Goldset содержит 48 RU/EN-запросов, половина held-out; `make bench-retrieval` блокирует регресс hit@5/MRR больше 0,02.
- **RU-first, local-first.** Обязательный sparse-канал понимает кириллицу и смешанный технический словарь без отдельного сервиса. Опциональные `bge-m3` embeddings остаются локально; cloud API не нужен.
- **Транзакционный write path.** Агентские create/update страниц, merge манифеста, `wiki/log.md` и `wiki/hot.md` идут через `scripts/vault-write.py`: строгий frontmatter, optimistic SHA-256, капы и crash-safe roll-forward из журнала.
- **Индустриальный Stop-хук.** На каждом ходе: recovery незавершённых writes, reindex, self-heal section retrieval, опциональный dense refresh, строгая валидация и commit только vault-owned путей. Сессии сериализует stdlib `fcntl`; validation failure блокирует commit и оставляет dirty state видимым.
- **MCP без зоопарка процессов.** Локальный HTTP-гейтвей (один launchd-сервис) стоит перед всеми вашими MCP-серверами: один набор долгоживущих процессов на машину вместо дублей на каждый терминал. Из коробки преднастроен [context7](https://context7.com): добавьте одну строку с API-ключом, и документация библиотек доступна в каждой сессии.
- **Параллельная работа, опционально.** `/dispatch` передаёт одобренному плану один ограниченный unattended‑мандат в отдельном git worktree + сплите [cmux](https://github.com/wandb/cmux). Кросс‑ревью, финальный `/reap` по контракту, валидация, observer-only stall‑уведомления через 15/20 минут и armed close-on-process-exit проходят без повторных запросов; blocking/scope changes возвращаются координатору. Требуется cmux; всё остальное работает без него.

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

Требования: macOS (единственная поддерживаемая и регулярно тестируемая платформа), [Obsidian](https://obsidian.md), [Claude Code](https://claude.com/claude-code) или Codex CLI, Python 3.9+ и git. Опционально: [Ollama](https://ollama.com) для семантического retrieval, [cmux](https://github.com/wandb/cmux) для параллельных задач, DCG для проверки разрушительных команд и VoiceInk для голосового ввода. Портирование на Linux и Windows возможно, но сейчас проект их не тестирует и не поддерживает.

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

### Опциональный голосовой ввод через VoiceInk

[VoiceInk](https://github.com/beingpax/VoiceInk) — нативное macOS‑приложение voice-to-text, которое умеет диктовать прямо в терминал и поэтому одинаково работает перед Claude Code и Codex. Это соседний инструмент ввода, а не зависимость или привилегированная интеграция LLM Obsidian.

Исходный код VoiceInk опубликован под GPL-3.0 и может быть собран локально без подписки. Готовая сборка автора имеет собственные trial/license и дополнительные возможности поддержки. VoiceInk сейчас требует macOS 14.4 или новее; актуальные условия установки и лицензирования смотрите в его репозитории.

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

## Ограничения

- **macOS — единственная поддерживаемая и обкатанная платформа.** cmux, launchd‑сервисы, status line и unattended lifecycle тестируются там. Другие платформы — возможность для портирования, а не заявленная поддержка.
- **First-class агенты только Claude Code и Codex CLI.** Возможности host hooks различаются; см. [матрицу runtime‑возможностей](docs/runtime-capabilities.md).
- **Для dispatch/review‑оркестрации нужен cmux.** Вольт, retrieval, запись и большинство productivity‑скиллов работают без него.
- **Ollama опционален, но не невидим.** Dense retrieval потребляет локальные RAM/disk и требует скачанной модели; sparse retrieval — поддерживаемый fallback.
- **Кросс‑ревью — ограниченная проверка, а не формальная верификация.** Вторая модель тоже может пропустить дефект или разделить неверную предпосылку.
- **Облачная LLM не входит в поставку и не становится бесплатной.** Пользователь приносит официальный CLI‑доступ и подписки, нужные выбранным маршрутам.

## Roadmap

- Закрывать безопасные gaps в prompt/tool hook parity Codex там, где host предоставляет эквивалентные lifecycle events.
- Публиковать больше end-to-end macOS acceptance‑примеров и эксплуатационных сценариев.
- Расширять RU‑документацию, сохраняя один канонический тестируемый поведенческий контракт.
- Добавлять примеры MCP‑профилей, не загружая схемы редко используемых tools в каждую сессию.
- Исследовать Linux и адаптеры других агентов только при сохранении typed handoffs, permission boundaries, lifecycle supervision и герметичных тестов.

## Благодарности и лицензия

MIT с сохранением копирайта upstream: см. [LICENSE](LICENSE) и [ATTRIBUTION.md](ATTRIBUTION.md). Родословная: паттерн LLM Wiki Андрея Карпатого → [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) → приватный DevOps-вольт, где эти механики инкубировались → этот репозиторий.
