# История изменений

Язык: [English](CHANGELOG.md) · **Русский**

Все значимые изменения llm-obsidian. Формат основан на
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии следуют
[SemVer](https://semver.org/lang/ru/).

> llm-obsidian происходит от
> [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian)
> (см. [ATTRIBUTION.md](ATTRIBUTION.md)). Его механика создавалась и проверялась
> в частном DevOps-вольте до публичного универсального выпуска 2026 года.
> Поэтому эта история начинается заново с версии 1.0.0.

Ниже перечислены только публичные релизы. Версии 2.0.5 и 2.1.1 были
внутренними контрольными точками и вошли в следующие публичные релизы; тегов и
пакетов с этими номерами не выпускалось.

## [2.1.2] — 2026-07-21

### Добавлено

- Сгенерирован fail-closed lock-файл зависимостей acceptance-матрицы. Он без
  выполнения продуктового или исторического кода учитывает статические Python-
  импорты, постоянные пути к коду и данным, runtime-регистрации и явно
  объявленные динамические пути репозитория.
- Добавлены минимальный seed-вольт и детерминированный синтетический seed-
  коммит: live-фикстуры больше не зависят от рабочей `wiki/` и данных
  `.vault-meta/`.
- В ограниченный поток pipeline events добавлены обезличенные тайминги каждого
  модельного хода и стадий runner-а, учёт незавершённых ходов и отчёты p50/p95.
  Промпты, ответы, команды и тексты ошибок туда не попадают.
- Добавлены content-addressed acceptance-доказательства: точные зависимости
  каждой ячейки, поколение production-модели, хеши целостности строк, возраст
  доказательств, атомарные checkpoints и fail-closed выборочное переиспользование.

### Изменено

- Монолитный live-acceptance runner разделён на contracts, sandbox, launchers,
  prompting, scenario adapters и skill adapters при сохранении прежнего CLI.
  До исправлений фикстур все 58 промптов v2.1.1 были побайтово сверены на
  закреплённых входах.
- Шесть live-фикстур — backlog, daily, distill-runbook, learn, reap и
  wiki-query — получили более точную операционную подготовку без изменения
  ожидаемого поведения. Остальные 46 сгенерированных промптов остались
  побайтово идентичны v2.1.1.
- Вместо глобальной инвалидации по неизвестному пути и переноса старых
  доказательств введены evidence epoch 3 и семантические fingerprints каждой
  ячейки. Изменения только данных, упаковки, orchestration и alias-а того же
  поколения модели переиспользуют доказательства; незарегистрированное runtime-
  ребро останавливает model-free проверку.
- Acceptance записывает точную реально запущенную модель, а fingerprint строит
  по её зарегистрированному major generation. По умолчанию матрица работает в
  двух собственных cmux workspace по пять ячеек, сохраняет checkpoint после
  каждой ячейки и возобновляет только незавершённые fingerprints.
- Обычные пути dispatch, review, reap, reap-send и close уплотнены вокруг
  детерминированных repo-owned runner-ов и условных compatibility references.
  Контекст нормального orchestration-пути уменьшен примерно на 30%, при этом
  семантические решения и safety gates остались за моделью.
- Live acceptance умеет запускать ограниченный набор скиллов, хеширует только
  относящиеся к ячейке части fixture/scenario registry и отдельно записывает
  фактическую дешёвую тестовую модель Sonnet/Terra и production generation.
- Точно проверенные release-packaging пути классифицируются как
  non-behavioral. Неизвестные non-runtime пути не инвалидируют evidence и не
  обходят runtime graph; незарегистрированные runtime-рёбра по-прежнему
  останавливают dependency-lock check.
- Временные отчёты разделяют завершённые и незавершённые ходы по runtime и роли
  coordinator/task/reviewer; незавершённым ходам не приписывается выдуманная
  задержка.

### Исправлено

- Operation-scoped review callbacks больше не зависят от текущей директории
  executor-а: handoff хранит абсолютные пути скрипта, worktree и action-файла.
- Возобновлённый scratch reviewer сохраняет свой owner-only рабочий каталог и
  не реконструирует устаревший вложенный путь.
- Перед выделением acceptance workspace один code-owned preflight проверяет
  подписку Claude; модель больше не тратит ход на проверку credentials, а
  обычная сессия сохраняет fail-closed поведение.
- Codex daily subagent закреплён за обнаруженной legacy-формой summary. Один
  невалидный ответ корректируется в том же agent thread точной ошибкой
  валидатора и схемой, без fallback на другую модель.
- Автоматические retries ограничены тремя попытками и только явными cmux-
  allocation и agent-capacity transients. Ошибки продукта, прав, контракта и
  неизвестные ошибки не повторяются автоматически.
- Wall-clock завершение ячеек заменено heartbeat-ами экрана и lifecycle:
  status probe через 15 минут и граница бездействия. После каждого запуска
  сверяются точные owned surfaces/workspaces; пустые shells и orphan tabs
  блокируют релиз.
- Точный native-диалог Claude о фоновой работе подтверждается автоматически
  только после lifecycle-авторизации закрытия unattended task/reviewer.
- Переходы review v3 доставляются через operation-bound task-local handoff:
  executor запускает короткую детерминированную команду вместо копирования
  длинных registry paths.
- Final reap блокируется, пока ожидается operation-bound review transition;
  drive-команда должна выполняться отдельно, а composer Claude очищается перед
  `/exit`, чтобы подсказанный текст не поглотил команду.
- Повторный reap-send остаётся идемпотентным после точного закрытия плана;
  Codex daily summarizer привязан к полной object schema.
- Daily acceptance учитывает canonical writer-owned обновление `wiki/log.md` и
  одно выделение адреса; backlog восстанавливает inbox побайтово через
  canonical writer вместо ослабления residue-проверок.
- Валидным live-review evidence считается как прямой approve, так и проверенный
  warning/fix/verification round. Матрица больше не выдумывает finding ради
  недетерминированной optional verification ветки.
- Task-local review drive использует стандартный `python3` и нейтральный
  authoritative-result contract, избегая classifier denial на Homebrew-пути.
- Завершение reviewer-а сначала сохраняется в точной broker lane и только потом
  закрывает surface. Успешный unattended reap-send больше не печатает уже
  доставленную coordinator-команду повторно.
- Disposable acceptance clones получают стандартный запрет auto-commit, чтобы
  Codex Stop hook не двигал coordinator HEAD.
- Cleanup cmux определяет window/workspace anchors, проверяет исчезновение в
  cmux tree и повторяет операцию один раз. Если последний surface заменился
  пустым shell, удаляется именно этот layout delta, и orphan tab не остаётся.
- Task/reviewer hooks пишут в origin vault только обезличенную turn telemetry;
  context injection, command/plan capture и полный Stop pipeline на read-only
  границе отключены.
- Canonical evidence отвергает dirty behavioral worktree — staged, unstaged и
  untracked пути — сохраняя лишь точное исключение для release metadata.
  Review startup удаляет stale outboxes до выдачи однопутевого write surface.
- Claude acceptance загружает точный repo-local plugin, отключает interactive
  question UI, заранее содержит варианты promotion и обновляет address indexes
  перед close validation.
- Прерванный acceptance принудительно закрывает только свой coordinator и
  зарегистрированные дочерние surfaces, не оставляя orphan tabs.
- Persistent protected-research callbacks находят точный run-to-operation
  locator внутри текущего vault; выход за его пределы отклоняется fail-closed.
- Protected-research workspace получает точный notifier: он пишет typed Codex
  checkpoint sidecar до callback, а synthesis переиндексирует vault до
  валидации; модель не копирует длинные пути и не вызывает cmux resume API.
- Autoresearch cleanup выполняет runner: находит одну связанную operation,
  удаляет новые страницы и восстанавливает deduplicated pages/indexes одной
  optimistic vault transaction, после чего доказывает чистоту clone.
- Approved-plan dispatch переведён на typed idempotent post-approval runner для
  route capture, worktree/task identity, prompt/meta rendering, anchored spawn,
  supervisor launch и log filing. Повторный preparing/failed запрос не создаёт
  второй surface.
- Phase 1 dispatch получил read-only candidate resolver; первый final reap v3 —
  contract-bound runner. Обе цепочки пишут обезличенные stage timings.
- Unattended reap-send валидирует и отправляет точный callback `reap-runner.py`.
  Log/hot используют адрес result page, а structured writer failures сохраняют
  понятную причину.
- Codex task session может писать только в свой точный registry subtree v3,
  чего достаточно для operation-scoped callback без доступа ко всему registry.
- Архивация review определяет coordinator по reviewed worktree, поэтому linked
  task корректно откладывает запись независимо от cwd вызывающего процесса.
- Live acceptance удерживает вложенные worktrees, ждёт медленное завершение
  interactive agent, применяет scenario-specific timeouts и отличает disposable
  append-only bookkeeping от product residue.
- Defuddle fallback без CLI теперь действительно удаляет ограниченный
  boilerplate и проверяет результат, а не принимает raw Markdown за очищенный.
- Launch Codex task v3 валидирует оба writable root, не меняет защищённого
  parent без необходимости и распознаёт переносимый trust dialog Claude.
- Dispatch привязывает callbacks к явно переданному surface вызывающего, а
  semantic-tiling reports содержат обязательную session provenance.
- Dispatch хранит только проверенные vault context links, соблюдает skill-size
  budget; schema validation игнорирует illustrative links в lint/archive/index
  шаблонах, чтобы отчёты не размножали собственные findings.
- Caller identity разбирается детерминированно; свежий clone получает MCP JSON
  до Codex sync; task Stop hooks не коммитят coordinator-owned derived indexes.
  Vault writer поддерживает optimistic journaled deletion.
- Dispatch fixture полностью готовится runner-ом и доказывает lifecycle
  one-commit/review/reap по durable artifacts. Claude reviewer использует
  пустой MCP config; точный native project-MCP prompt выбирает «continue
  without», не повышая доверие. Task launch v3 закрепляет canonical DCG profile.

## [2.1.0] — 2026-07-18

### Добавлено

- Owner-only registry задач и сессий по opaque project/task/permission-domain/
  runtime/pinned-model identity, поддерживающий несколько coordinator-ов без
  угадывания по имени или давности.
- Постоянные product-read-only lanes для review, protected fetch и synthesis с
  typed cmux checkpoints, видимым fallback в свежую сессию, FIFO на lane и
  task-scoped cleanup.
- Task-meta v3, namespaced review operations, ссылки на несколько архивов
  review, блокировка upgrade при активном broker и macOS cmux preflight.
- Интерактивный live-acceptance runner: точный registry skill/runtime, реальная
  fixture для каждого скилла, disposable clones committed HEAD, typed evidence,
  exact-surface cleanup и единый gate `make acceptance-live`.

### Изменено

- Все cmux workflow открываются справа от захваченного caller surface. Initial
  и verify review используют один surface; следующий раунд той же task/model/
  domain возобновляет checkpoint.
- Небольшая same-model работа по умолчанию идёт во внутреннего subagent-а;
  видимое same-model review требует явного запроса отдельного окна.
- Protected research сохраняет контекст только внутри точных task и isolation
  domain. Scratch каждой operation свежий; runtime homes удаляются после final
  reap и архивации задачи.
- Trusted review submission принимает и рендерит callback до уведомления
  executor-а. `review-dispatch drive --apply-action` владеет безопасными
  approve/verify переходами; semantic fixes/escalations остаются решением агента.

### Исправлено

- Параллельные review одного проекта больше не перезаписывают singleton-
  metadata, baselines, callbacks, results, watchdog state и close sentinels.
- Reviewer exit закрывает только вооружённый surface после возврата процесса;
  потеря checkpoint видима и освобождает lane.
- Resume не зависит от новой operation-scoped callback permission; сбой UI-
  уведомления не повторяет уже сохранённый переход.
- Acceptance ждёт стабильный bounded regular-file outbox, не принимает symlink
  или oversized output и терпит короткую non-atomic запись.
- Repo-spawned Codex task/review/research/acceptance используют default service;
  Fast/priority остаётся только явным выбором пользователя.
- Checkpoint атомарен после каждой ячейки; resume возможен только для того же
  source commit и matrix fingerprint.
- Временные файлы ограничены operation, product residue отклоняется, а
  прерванной ячейке даётся время закрыть точный surface.
- Protected fetch/synthesis передают cmux короткий operation-owned launcher,
  исключая обрезку длинной команды в composer.

## [2.0.9] — 2026-07-18

### Добавлено

- Динамический cross-runtime release acceptance contract для всех скиллов,
  sanitized evidence ledger, видимых сбоев и baseline/final фаз.
- Once-per-session readiness preflight для routing, generated-config drift, CLI-
  зависимостей и hybrid retrieval. При отсутствии Ollama/`bge-m3` выдаются
  точные команды установки, а sparse retrieval продолжает работать.
- Явный `unsafe-research` как отдельный single-context escape hatch: только по
  прямому разрешению, с предупреждением, наследованием текущей сессии и без
  ослабления protected research как fallback.

### Изменено

- Конкретные Claude/Codex defaults живут только в `config/model-routing.toml`.
  Dispatch наследует текущий route, daily — модель с medium effort, review —
  противоположный runtime, protected research сохраняет Codex isolation.
- Task/review metadata записывают model, effort, source и config fingerprint.
  Same-model review явное и может менять effort без смены модели.
- Overlay upgrade отклоняется при активных task/reviewer/research sessions,
  игнорирует stock v2.0.8 defaults и переносит только изменённые legacy routes
  после проверки и подтверждения в gitignored override.

### Исправлено

- Неизвестные модели, provider mismatch, invalid effort, config drift и
  неполный routing завершаются явно, без silent fallback.
- Daily defaults больше не дублируются в runtime agent definitions; lint
  запрещает новые model literals в активном коде.

## [2.0.8] — 2026-07-17

### Добавлено

- `/clarify`: по одному вопросу за раз до написания кода, с предварительной
  проверкой локальных фактов, сохранением существенных решений за пользователем
  и без повторного подтверждения уже разрешённого шага.
- RU/EN router hints и regression-тесты против ложных срабатываний на явные
  запросы clarification и `grill me`.

### Изменено

- Явный reasoning effort Codex reviewer сохраняется после `--model`.
- Defaults dispatch/review: Claude `fable` high и Codex `gpt-5.6-sol` high.
  Явные overrides приоритетны; deep Codex остаётся `max`, daily — ограниченный
  Terra/low или Claude Sonnet/low.
- Версии plugin и обоих marketplaces обновлены до v2.0.8.

### Исправлено и безопасность

- Read-only Codex reviewer не наследует full-MCP executor profile: используется
  readonly profile или отсутствие profile, что предотвращает schema overflow.
- Coordinator review canonical vault допускает только owner-only empty
  gitignored scratch hierarchy; остальные in-worktree runtimes отклоняются.
- Task metadata reviewer-а приоритетнее repository defaults; supervisor
  проверяет model и effort.
- DCG smoke очищает inherited `DCG_CONFIG`. Base profile блокирует rebase и
  destructive history/lifecycle, но допускает amend; task worktree сохраняет
  прежние разрешения rebase/amend.

## [2.0.7] — 2026-07-14

### Добавлено

- Cross-model review хранит стабильную идемпотентную историю в
  `wiki/meta/reviews/`: исходную задачу, каждый round, resolution, verification
  gap, residual risk, reviewer/model/mode и verdict. Finalization проверяет хеш,
  наличие архива и ссылку из task result.
- Coordinator review использует тот же durable contract; task worktree
  откладывает запись в canonical coordinator vault.

### Изменено и исправлено

- Unattended task split использует workspace-write только для task worktree,
  точного cmux socket и supervisor command; callbacks передаются атомарным relay
  file. Monthly agenda явно помечает незавершённые планы и напоминания.
- `log.md`/`hot.md` не накапливают runtime sessions в frontmatter; content pages,
  plans и review archives сохраняют provenance.
- Dense retrieval догоняет sparse self-heal даже на чистом tree, соблюдает
  backoff одного corpus fingerprint и сразу принимает новый fingerprint.
- Escalation delivery восстанавливаем, task executor может коммитить в своём
  worktree, archives привязаны к coordinator vault, collision result name
  маршрутизируется детерминированно.

### Безопасность

- Review archive создаётся coordinator-owned транзакцией `vault-write.py` и
  хранит только ограниченное описание задачи. Raw prompts, callbacks, commands,
  sockets и cmux IDs не попадают в durable page.
- Read-only Codex reviewer допускает loopback tests, но не внешний network/web.
- Auto-repair ограничен локальным обратимым repo-owned механизмом; права,
  зависимости, public API, migrations, destructive и external effects требуют
  разрешения.

## [2.0.6] — 2026-07-13

### Добавлено

- Обезличенная telemetry unattended lifecycle: latency task/reviewer,
  callback validity/findings, escalations, watchdog, reap и surface outcomes.
- `pipeline-stats.py` с p50/p95, counters, privacy boundary и предупреждением о
  малой выборке.
- macOS GitHub Actions CI для hermetic suite и Codex marketplace drift.

### Исправлено и безопасность

- Close guard сохраняет tracked `.vault-meta/`, а gitignored events не влияют
  на Git status; сохранён agenda spacing fix внутренней v2.0.5.
- Lifecycle events принимают только безопасные identifiers и неотрицательные
  числа — без task text, review prose, commands, queries, errors и page bodies.

## [2.0.4] — 2026-07-13

### Добавлено

- Runtime-neutral `/agenda`: read-only preview незавершённых планов и reminders,
  атомарный carry-over в одну дату и декларативные monthly Obsidian Tasks reports.
- Опциональный pinned Obsidian Tasks 8.2.2 UI с SHA-256 verification, сохранением
  settings, backup/repair и status snippet.

### Изменено, исправлено и безопасность

- Journal использует Tasks-compatible checkboxes, стабильные block IDs,
  completion dates и exact-text deduplication, читая и legacy reminders.
- Agenda пропускает ambiguous legacy chains/nested subtrees, защищает terminal,
  duplicate/conflicting targets и восстанавливает headings по canonical order.
- Partial install восстанавливает только недостающие проверенные assets; reruns
  идемпотентны. Все затронутые страницы пишутся одной optimistic
  `vault-write.py` транзакцией; downloads pinned и проверены SHA-256.

## [2.0.3] — 2026-07-13

### Добавлено

- Локальная нормализация документов: Markdown/text через stdlib fast path;
  PDF, Office, EPUB и scans — через pinned isolated Docling с OCR `ru,en`,
  content-addressed cache, confidence и fail-closed лимитами размера/страниц/
  времени.
- Cross-runtime failure-to-repair contract: read-only диагностика repo-owned
  дефекта, затем узкий fix, regression test, retry стадии и resume задачи в
  рамках разрешённой границы.

### Изменено и безопасность

- Claude reviewer в `dontAsk` может запускать чистые cwd-relative
  `python3 tests/test_*.py` и `bash tests/test_*.sh`; pipes, redirects и wrappers
  не входят в allowlist.
- Fresh setup ставит isolated Docling и OCR/layout/table artifacts; есть явный
  `--skip-docling`.
- Docling отключает remote services/plugins, работает offline и не меняет
  source. Невозможность конвертации возвращает typed escalation, а не скрытый
  fallback на native model parsing.

## [2.0.2] — 2026-07-12

### Исправлено

- Восстановлены macOS bootstrap, pinned Python, protected-research callbacks и
  restart fixes из 2.0.1, случайно не попавшие в её tag.
- Завершённые fetch/synthesis splits закрываются только по durable marker и,
  для synthesis, валидному output; `--keep-surfaces` оставляет debug opt-in.
- Claude reviewer получает cwd-relative read-only Git commands, Codex reviewer —
  worktree-qualified команды из isolated scratch.

### Безопасность

- Codex protected research работает deny-by-default: без external domains,
  upstream proxy, broad bind, non-loopback listeners, arbitrary Unix sockets,
  SOCKS5/UDP. Единственное исключение — точный cmux callback socket.
- Cleanup marker-gated, exact-UUID, idempotent, coordinator-safe и retryable.

## [2.0.1] — 2026-07-12

### Исправлено и безопасность

- Clean-machine macOS bootstrap проверяет Xcode Command Line Tools до мутаций,
  отклоняет inert system Python placeholder и выбирает рабочий Python 3.9+.
- Protected research закрепляет этот interpreter и даёт sandbox-у read-only
  доступ только к нужным Homebrew/CLT roots.
- Fetch/synthesis callbacks получают единственное явное исключение для cmux
  Unix socket, durable markers и могут возобновить networkless synthesis из
  валидированного artifact после сбоя доставки.
- Network остаётся limited без external allowlist; сбой callback восстанавливаем
  без выдачи общего доступа к vault или сети.

## [2.0.0] — 2026-07-11

### Добавлено

- First-class packaging Claude Code и Codex, generated Codex marketplace,
  общий безопасный Stop processing, runtime docs и portable setup helpers.
- Contract-bound unattended orchestration cmux worktrees: supervision,
  observer-only watchdog, typed escalation, cross-model review, bounded verify,
  reap gating и auto-close после проверенного handoff.
- Evidence-grounded daily, journal/backlog, research isolation, instruction
  lint, schema validation, telemetry и crash-safe transactional writes.
- Section-level sparse retrieval, optional local `bge-m3`, quality gates, dense
  refresh, experiment tooling и расширенные hermetic regressions.

### Изменено и безопасность

- Cross-model review defaults: subscription-backed Claude `opus` (Opus 4.8)
  для Codex и Codex `gpt-5.6-sol` для Claude; Fable — explicit opt-in.
- Hooks, MCP generation, memory backup, sanitization и bootstrap укреплены для
  repeatable multi-agent use без коммита machine-local state.
- Commands, metadata, callbacks, lifecycle и external-effect escalation
  проверяются строгими schemas и permission boundaries.
- Личные wiki pages, sessions, workspace state, credentials, runtime metadata и
  private memory исключены; template indexes построены только по public seed.

## [1.0.0] — 2026-07-05

Первый публичный релиз.

### Retrieval

- Локальный dense retrieval на Ollama `bge-m3`: RU-capable embeddings без cloud
  calls. На calibration vault hit@1 достиг 0.85, MRR@10 — 0.904 против 0.27 и
  0.405 у прежней English-centric модели.
- Scope-aware hybrid fusion: dense ранжирует покрытые страницы, BM25 с Unicode-
  tokenizer и RU stopwords добавляет только страницы вне dense tiling scope.
  Дизайн проверен на goldset с held-out половиной.
- Tag prefilter и постоянный benchmark `scripts/retrieval-bench.py` с hit@1,
  hit@5, MRR@10 и автоматической деградацией: Ollama down → BM25-only, индекс
  BM25 отсутствует → dense-only.

### Запись и hooks

- `scripts/vault-write.py` атомарно ведёт `wiki/log.md` и `wiki/hot.md` с
  детерминированными лимитами и plan lifecycle; validator проверяет frontmatter
  и caps.
- Stop hook выполняет reindex, sanitized memory backup, BM25, incremental dense
  refresh и scoped auto-commit под lock, с atomic indexes и latency telemetry.
- Data-driven skill router, session maintenance nudge, sanitized command capture
  для `/distill-runbook` и автоматический plan capture.

### MCP HTTP gateway

- Один launchd-managed pinned `mcp-proxy` обслуживает MCP children по HTTP;
  secrets остаются во внешнем env-файле. `doctor`, `smoke`, `health`, `update`
  и `sync-tools` проверяют и обслуживают конфигурацию.
- Context7 служит готовым примером; `.mcp-profiles/` даёт escape hatch от
  schema-budget overflow.

### Скиллы, память и шаблон vault

- Поставлялось 23 скилла: wiki/search/ingest/lint/fold/save/research и Obsidian-
  форматы; journal/daily/backlog/session/draft/runbook/learn/plans; optional cmux
  dispatch/reap/reap-send.
- DragonScale Memory: deterministic адреса `c-NNNNNN`, fold, duplicate tiling и
  boundary-first research с bge-m3 thresholds и процедурой recalibration.
- Публичный seed `wiki/` содержит полный набор папок, generated indexes,
  совместимые `hot.md`/`log.md`, русскоязычный `CLAUDE.md` и agent-neutral
  `AGENTS.md`.

### Тестирование и ограничения первого релиза

- Девять hermetic suites без network/Ollama: allocator, tiling, boundary, vault,
  Stop hook, BM25/fusion, benchmark, router и MCP management.
- В 1.0.0 hooks были подключены только к Claude Code, Codex adapter ещё
  планировался; тела скиллов были английскими при уже работающих RU triggers;
  launchd autostart был macOS-only. Эти ограничения устранены или уточнены в
  последующих релизах выше.
