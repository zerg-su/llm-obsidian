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

Ниже перечислены только публичные релизы. Версии 2.0.5, 2.1.1 и 2.4.0 были
внутренними контрольными точками и вошли в следующие публичные релизы; тегов и
пакетов с этими номерами не выпускалось.

## [2.6.6-rc4] — 2026-08-08

Этот control-plane candidate делает review routing и callback continuation
детерминированными без нового оркестратора или provider route.

### Добавлено

- Добавлен единый digest effective review topology, общий для validation,
  finalization и runtime launch.
- Добавлены machine-checked lifecycle transition certificate и six-part
  engineering review denominator: quality, implementation, testing,
  simplification, documentation и security.
- Добавлены bounded skill-quality evidence и exact release-boundary fixtures.
- Добавлены committed exact-HEAD gate bundle и отдельный RC4
  accepted-deviations artifact.
- Добавлен `harness-cli.py dashboard`: read-only английский терминальный вид,
  который проецирует реальный compiled pipeline, параллельные lanes, loop
  visits, один bounded cmux surface probe и ограниченный список свежих issues.
  Он не имеет lifecycle-авторитета и всё, что не резолвится точно, помечает как
  `request-coordinator-classification`, а не как прогресс.

### Исправлено

- Harness dashboard теперь рисует один dispatch как одно дерево. Verification-
  дети, review parents и review rounds вкладываются в тот compiled step,
  который их исполняет, а не всплывают отдельными top-level программами; на
  каждом шаге видны frozen runtime/model/effort и preset той записи, которая
  его исполняет (`unknown`, если метаданных нет); активная верификация больше
  не оставляет завершённую реализацию подсвеченной; а нетерминальная операция
  без собственного runtime-ресурса помечается как unresolved, а не как живая.
- Восстановлен живой scope ratchet для `scripts/`: он снова измеряет рабочее
  дерево, а не замороженный исторический коммит, с явными RC4-потолками в
  `tests/rc4_scope_ratchet.py` (265 файлов, 90 553 строк). В dashboard теперь
  входит отдельный read-only валидатор receipts; review-коррекции привязывают
  fix visits к принятым callbacks, сериализуют recovery marker через atomic
  writes и отличают failed/cancelled roots от успешного завершения. Числа RC2
  остаются в
  `tests/test_v266_rc2_scope.py` только как историческое свидетельство и больше
  не подменяют собой ratchet.
- Accepted review callbacks возобновляются инкрементально и ровно один раз
  после crash prefixes, stale surfaces, changed-HEAD verification и zero-lane
  preflight.
- Release evidence привязано к exact plan, Outcome Contract, artifact root,
  reviewed bytes и candidate HEAD.
- Initial input считается принятым только после semantic provider activity,
  включая текущий Claude UI с пробелом перед activity timer.
- Отмена закрывает точное владеемое поддерево lifecycle. `harness-cli
  cancel|close` теперь обходит цепочку `parent_operation_id` в пределах того же
  durable owner и терминализует каждого точного потомка child-first до самого
  root, поэтому отмена root больше не оставляет review parent и review-round в
  `awaiting-callback`. Каскад, заблокированный нетерминальным потомком,
  сообщает `"status": "partial"` с запрошенным root и блокирующим потомком и
  завершается с кодом `3`, а не как успех.

## [2.6.6-rc3] — 2026-08-07

Этот финальный evidence-polish candidate закрывает оставшиеся замечания RC2,
не расширяя runtime capability или lifecycle authority.

### Добавлено

- Добавлены внешние post-commit exact-tree inventory sidecar, prospective
  receipts по каждому slice, append-only attempt ledger и typed compiler над
  реальными gate/review/finding bytes.
- Добавлены воспроизводимые coverage observations и переносимый allocator
  scratch-каталогов shell-тестов для ограниченной macOS-среды.
- Добавлены исполняемая карта RC3 evidence и сфокусированные контракты
  документации и релиза.

### Изменено

- Нормативные implementation plans по умолчанию пишутся на английском, если
  пользователь явно не запросил другой язык; user-facing conversation
  сохраняет язык пользователя.
- Root, runtime, task, testing, review и русская release-документация приведены
  в соответствие с Harness/runtime-worker lifecycle, оставшимся после RC2.

### Исправлено

- Каждое выполнение full profile, включая unpublished и test-only попытки,
  механически расходует оператором разрешённый лимит из восьми candidate
  attempts; авторизация расширения входит в итоговый digest.
- Machine-readable RC3 evidence отклоняет drift candidate, gate, review,
  finding, waiver, output и profile.

## [2.6.6-rc2] — 2026-08-07

Этот polishing candidate исправляет exact ownership callback/evidence и
удаляет доказанно недостижимый classic cmux contour без новой orchestration.

### Добавлено

- Добавлен один bounded skill `vault-repair` и точный blocked-Stop handoff для
  Codex и Claude Code поверх существующих recovery, validation и scoped commit.
- В immutable release profile добавлена exact-candidate проверка добавленного
  содержимого и новых путей secret-container.

### Исправлено

- Release receipts привязаны к exact subject HEAD и profile; drift между
  parent и descendant отвергается.
- Callback acceptance стал одной публичной атомарной Store transaction, а уже
  принятый resource-free review dispatch завершается ровно один раз.
- Durable terminal approval после crash возобновляется без повторения provider,
  callback, verification, process или cmux effects.
- Implementation review authority до запуска reviewer проверяет полный
  immutable evidence bundle, включая каждый output sidecar.

### Удалено

- Удалены семь zero-caller classic supervisor/watchdog/trust-prompt и
  review-archive compatibility файлов: 2 217 физических строк frozen tree.

## [2.6.6-rc1-fix2] — 2026-08-07

Этот промежуточный патч RC1 исправляет reconciliation точного cmux cleanup
после уже успешного завершения provider.

### Исправлено

- Если закрытый surface больше не имеет `caller` в ответе `cmux identify`,
  adapter подтверждает его отсутствие по exact cmux tree, а не объявляет ответ
  ошибкой adapter. Завершённый `request-exit` теперь может очистить exact owned
  resources и продолжить protected research на synthesis без повторного fetch
  или provider effect.

## [2.6.6-rc1-fix1] — 2026-08-07

Этот промежуточный патч RC1 исправляет четыре сбоя dispatch, обнаруженные при
dogfood packaged-релиза. Scope lifecycle и orchestration RC1 не расширяется.

### Исправлено

- Codex resume жёстко привязан к task worktree, поэтому native-диалог выбора
  текущей папки не может перенаправить unattended continuation.
- При сбое до доставки input сохраняются bounded provider-start diagnostics и
  receipts этапов transport.
- Harness ждёт готовности свежего cmux terminal перед отправкой команды и
  переносит временные пустые или malformed чтения surface.
- Exact-HEAD `review.mode=skip` завершается через code-owned review path без
  создания reviewer/provider effects.

## [2.6.6-rc1] — 2026-08-07

Этот release candidate упрощает lifecycle по принципу deletion-first. Он
удаляет legacy cross-HEAD recovery authority, оставляет reviewer liveness
наблюдаемым сигналом и привязывает Split dispatch, replay и Join к одному
неизменяемому base commit.

### Изменено

- Удалён incident-bound compatibility lifecycle и сокращён активный authority
  contour с сохранением exact-attempt evidence.
- Callback recovery стал attention-only, пока runtime не публикует
  authenticated turn-complete event: время и стабильность экрана не могут
  отправлять input или перезапускать provider.
- Каждый Split child request, task contract, launch/terminal receipt, replay и
  Join result привязан к sealed base SHA манифеста.
- Добавлена exact replay-проверка durable child metadata и fail-closed обработка
  отсутствующей, конфликтующей, дублирующейся или неродственной ancestry.

### Граница релиза

- Публикация требует immutable release gate из 15 команд и финального Fable
  release-review packaged exact HEAD. Единственное minor замечание к
  конфигурации implementation-кандидата перенесено в RC2.

## [2.6.5] — 2026-08-05

Это минимальный стабилизационный релиз. Полный технический gate зелёный;
terminal exact-HEAD review явно отменён решением пользователя, а все принятые
остаточные findings сохранены в плане lifecycle debt для 2.6.6.

### Добавлено

- Immutable exact-HEAD записи `ReviewAttempt` и bounded
  `VerificationAttempt`. Одно coordinator-authorized решение о mechanism-flake
  может создать same-HEAD attempt 1 без model call; attempt 0 сохраняется, а
  второй retry заканчивается typed attention.
- Закрытый cursor-bound поток `ProviderEvent` для delivery, progress, exit и
  resource closure с одинаковыми контрактами Claude print и Codex exec
  ephemeral adapters.
- Atomic five-cycle finalization, freshness-bounded availability независимого
  provider и additive finalization metadata в PipelineSpec.
- Governed Split manifests. `$split` остаётся zero-effect preview; явный
  `$split --dispatch` запускает bounded workspace-local waves через
  существующий dispatch adapter и соединяет exact approved resource-free
  receipts в порядке manifest.

### Изменено

- Интерактивные Codex-задачи и continuation получают один content-addressed
  указатель на полный prompt artifact вместо многострочной вставки в editor;
  доставка Claude не меняется. Native update dialog выбирает `Skip` только для
  текущего запуска, а установку и будущие напоминания оставляет пользователю.
- Skill `tdd` требует сначала доказать неизвестный adapter/runtime-механизм
  одним disposable live-`prototype`, затем перенести наблюдаемое поведение в
  real-seam RED regression и focused GREEN до широкого gate.
- Ручной transport-прототип получил model-free CMUX layout smoke: один
  изолированный workspace, дополнительный tab, splits справа/слева, exact tree
  checks и точное закрытие без хвостов workspace/surface.
- Добавлен новый exact-HEAD attempt path. Legacy V3/pre-activation continuation
  и bounded review-drive rearm остаются принятой compatibility-задолженностью
  для 2.6.6.
- Добавлены typed provider-event и delivery paths. Legacy screen/time recovery
  authority остаётся принятой задолженностью; на новом пути Stop владеет
  callback submit.
- Task metadata v4 опционально несёт immutable Split child policy, сохраняя
  read compatibility v1-v3 и обычное поведение dispatch.

### Исправлено

- Current-checkout callback notification больше не добавляет synthetic
  review-scope как legacy `current --plan`; точная команда повторяет исходную
  purpose/boundary identity и может принять готовый callback.
- Synthetic task-summary publisher в runtime-тестах переведён на atomic
  publication; regression доказывает, что stable-read watcher не видит partial
  JSON.
- Immutable escalation records остаются durable, а их runtime-каталог
  игнорируется Git: raise/resolve evidence сохраняется при чистом status.
- Определение Codex subscription теперь требует нулевой exit code и точный
  поддерживаемый logged-in marker в нормализованных stdout/stderr; одного
  warning недостаточно.
- Dispatch context alias привязан к точному stem wiki-пути; strict typed
  exact-binding repair закрывает единственный случай, который нельзя безопасно
  вывести автоматически.
- Уже durable завершение reap согласуется только с точным pending effect и
  идентификаторами receipts, без повтора vault или provider effect.
- Split activation разрешается только после зелёного Stability Gate; любая
  ошибка manifest, budget, receipt, dependency, HEAD или resource state
  останавливается до повторного child/provider effect.
- Зафиксировано, что ephemeral adapters готовы на уровне conformance, но ещё не
  выбраны production review; late-cycle independent availability не подключена,
  а Split ownership требует exact sealed base commit.

## [2.6.4] — 2026-08-04

### Добавлено

- Harness-owned восстановление пропущенного reviewer submit. Оно связано с
  точными operation/run/lane/generation, callback target, deadline и владением
  process/surface и использует существующий общий лимит в один nudge; готовые
  typed input, callback или receipt принимаются без вызова модели.
- Append-only записи escalation, resolution и amendment плана. Последний
  attention marker теперь является ограниченным указателем на неизменяемую
  историю решений; старый полный marker мигрирует детерминированно.
- `task-review-runner.py plan` — безопасный фасад ревью планов с
  `purpose=intent`, защищёнными Outcome/disposition/evidence областями, точными
  base/head OID и готовыми ограниченными командами инспекции.
- Одноразовое самоисправление Wiki-ссылок, если цель однозначно определяется
  по title/H1. Изменение идёт через канонический transactional writer, после
  чего перестраиваются индексы и выполняется строгая валидация.

### Изменено

- Harness публикует принятые resource-free callback, безопасно rearm'ит только
  точного timed-out parent и закрывает только exact-owned superseded reviewer
  ресурсы. Активное или неизвестное владение по-прежнему останавливается.
- Same-session continuation различает transport acceptance и подтверждение
  provider generation. Нулевые exit-коды paste/Enter больше не означают успех;
  один identity-bound повтор Enter может потратить общий liveness budget,
  иначе сохраняется typed attention.
- Доставка callback wake сериализована для точной operation и использует
  write-ahead фазы paste/submit. Частичный или неоднозначный effect fail closed,
  а конкурентный reconcile не может отправить второй provider-facing wake.
- Перед continuation Enter теперь сохраняется точная generation reservation.
  Crash replay не отправляет второй Enter, а retirement принятого поколения
  идемпотентен при конкурентных worker'ах.
- Исчезновение точного screen после первого continuation Enter сразу даёт
  typed unconfirmed result и не расходует retry budget на второй Enter.
- Dispatch сохраняет точный путь wiki context, а не принимает отображаемый
  title за имя файла. Review inspect принимает только полный канонический OID.
- Standing Makefile и coverage denominator теперь проверяют полный набор
  harness-тестов и production entrypoints.

### Исправлено

- Exact-фаза `sent` state плюс `reserved` callback-submit receipt теперь
  согласуется до accepted-callback fast return, поэтому callback, принятый до
  restart, не может заблокировать следующее reviewer generation.
- Release-доказательство E6/E14 переведено на production review-gate и
  task-summary entrypoints, а iteration barrier проверяется через публичный
  Deep facade; прямые вызовы lifecycle helper больше не считаются unattended.
- E14 связан с реально исполненным двухпроходным `engineering/fix`: семь typed
  step receipts, verification fail→pass, review, checkpoint, accepted summary
  callback, освобождённые ресурсы и `reap-ready`.
- Stale callback receipt предыдущего поколения больше не блокирует recovery
  текущего, а typed artifact после reservation побеждает гонку без повторного
  provider prompt или Enter.
- Публикация liveness state и callback receipts стала directory-durable до
  любого provider-facing effect.
- Устранена точная crash-фаза callback-submit, где durable state уже был
  `sent`, а отдельный receipt оставался `reserved`: restart продвигает только
  совпадающий receipt, не повторяет provider input и fail closed на повреждённых
  байтах.
- Устранены молчаливые остановки после готового reviewer output, callback races,
  сбоев callback rearm и случая, когда prompt остался в editor и не был запущен.
- Unattended Codex review больше не зависает на нативном предложении сменить
  модель при приближении к лимиту. Точный prompt сохраняет route-bound модель и
  будущие напоминания; неизвестные варианты по-прежнему не получают ввода.
- Восстановлены точные UTF-8-байты frozen Outcome в плане после того, как
  промежуточная документационная транзакция внесла replacement characters и
  корректно вызвала отказ до provider effect.
- Пользовательские backlog-записи про 2.7 TaskGraph/project tasks теперь точно
  перечислены в planning-only accepted-deviation ledger.
- Закрыт пробел release evidence между callback acceptance и lifecycle
  completion: dogfood trace доходит до terminal resource-free parent/child и
  фактической границы `reap-ready`; отдельный финальный trace фиксирует
  harness-only lifecycle authority.
- Fixture-owned terminal transitions и фиксированные manual-effect counters в
  E6/E14 dogfood заменены production review acceptance, exit/cleanup и pipeline
  advancement с effect-derived доказательством отсутствия ручных действий.
- Accepted и duplicate callback receipts теперь связаны с точной broker
  callback/payload identity; continuation paste использует write-ahead
  fail-closed replay, поэтому crash не вставит один prompt повторно.
- Plan review больше не запускается как implementation по умолчанию и не
  переиспользует старую lane после изменения защищённого контракта плана.
- Новые coordinator decisions больше не перезаписывают старые escalation и
  amendment evidence.
- Устранена проявлявшаяся только в полном suite гонка verification recovery:
  тест ограниченно ждёт точный packet и join'ит responder до cleanup.
- Устранены ещё две гонки task-summary fixtures: helper readiness/completion
  синхронизированы, а atomic recovery receipts ограниченно ожидаются до cleanup.
- Устранена проявлявшаяся под trace гонка summary refresh: оба responder-потока
  используют документированное ограниченное окно и join до проверок.
- Устранена проявлявшаяся только в полном suite гонка pipeline-fix retry:
  retry-intent receipt ожидается в том же ограниченном окне атомарной публикации.
- Устранена гонка fake provider под полным trace-прогоном: увеличен только
  ограниченный fixture polling, production provider/callback deadlines не менялись.
- Sent binding поколения callback recovery теперь снимается только после
  точного durable broker acceptance. Следующее поколение в той же retained
  session остаётся наблюдаемым в active-состоянии, но не получает второй
  prompt, Enter, nudge или restart budget.
- Retirement теперь безопасно повторяется после crash между записью accepted
  receipt и очисткой liveness state: снимается только точный совпавший sent
  binding, а общие recovery budgets остаются израсходованными.
- Continuation Enter retry теперь использует тот же digest callback target и
  полную generation identity, что и worker liveness. Replay pending effect
  подтверждает activity и переводит только точную reservation в `sent`, не
  расходуя ещё одну попытку и не повторяя prompt или key.
- Публикация append-only escalation стала directory-durable: создание каталога
  records при первом использовании и каждая immutable directory entry получают
  `fsync` раньше, чем заменяется latest pointer.
- Добавлена прямая fail-closed проверка uncertain callback submit без точной
  reservation; установленный порог покрытия не снижался.
- Во время resolution исходный план и Outcome теперь читаются из точного
  reviewed Git object: append-only amendment передаётся как fix delta, а
  обычная stale/dirty граница по-прежнему блокируется.
- Игнорируемый release receipt без Git blob сохраняется при resolution только
  из path-confined regular file с точным frozen digest; tracked evidence
  остаётся привязанным к reviewed commit, а fallback разрешён только для
  доказанно отсутствующей entry reviewed tree.
- Публикация continuation retry теперь crash-safe: exact generation binding
  становится durable `sent` раньше receipt `submit-retried`, поэтому replay не
  получает продвинутый receipt рядом с всё ещё reserved liveness effect.
- Callback новой verification iteration той же axis больше не скрывается
  старым записанным результатом на Deep/Full lane barrier; уже записанная
  точная iteration по-прежнему фильтруется идемпотентно.
- После декомпозиции lifecycle-модулей восстановлена атомарная pointer-
  материализация resolution review inputs, превышающих inline-лимит пакета.
- Повтор равного attention marker теперь заново выполняет ранее неудавшийся
  authoritative state transition, а не оставляет operation молча зависшей.
- Канонический одноразовый wikilink repair может изменить writer-owned log/hot
  только когда весь optimistic payload точно совпадает с текущим результатом
  planner. Подделанные и обычные прямые обновления остаются запрещены.

## [2.6.3] — 2026-08-04

### Добавлено

- Добавлен версионированный русский технический handbook из 24 страниц с
  маршрутами для новичка, оператора, автора PipelineSpec и maintainer'а;
  полным каталогом 34 skills и обоих invocation branches; runnable examples с
  expected result, verification, failure, recovery и authority; а также
  документацией release candidate.
- Добавлено практическое руководство по ручному fan-out/join: одна большая цель
  делится на независимо владеемые планы, одновременно исполняется в нескольких
  task worktree и собирается из принятых exact HEAD без заявления об
  автоматическом task graph.
- Добавлены `make test-docs`, mutation-sensitive проверки handbook,
  source/coverage matrix, три walkthrough в disposable checkout и строгий
  documentation PipelineSpec, компилируемый только из существующих primitives,
  skills `tdd`/`review` и named checks.
- Добавлены claim-level rulings для восьми protected-fetch primary sources;
  ошибочный synthesis с девятью citations сохранён как negative evidence.

### Исправлено

- Любой exact accepted callback сохраняется как terminal completion при owned
  cleanup; protected research служит release-регрессией. Exact-identity
  cancelled fetch receipt может восстановиться
  только при nonterminal research parent; mismatch digest, run, request или
  artifact не создаёт synthesis child и не запускает provider. Terminal
  composition никогда не возобновляется, cancelled-synthesis recovery не
  поддерживается. Cleanup или явный cancel после принятия exact callback
  завершается как `complete`; cancel без него остаётся `cancelled`.
- Исправлен bootstrap MCP config в fresh worktree: только default
  `sync-config --apply` может атомарно создать отсутствующий `runtime.env` из
  строго валидированного committed sibling example с owner-only mode.
  Check/print, custom paths, direct calls, invalid examples и symlinks ничего не
  пишут и fail closed.
- Исправлен coordinator-authorized review recovery для mixed-HEAD gate в
  `awaiting-resolution`: existing fresh boundary разрешён только когда все
  retained parents и current rounds terminal, resource-free и без pending
  effects; verification budget зажимается до нуля.
- Добавлена recovery-only совместимость для terminal pre-schema review rounds,
  где единственное отличие specification — исторически отсутствующий
  `parent_operation_id`. Durable records не переписываются; live ownership и
  любое другое identity drift остаются fail-closed.

### Изменено

- Кандидат `document-project` проверен, но не поставляется: fresh no-skill
  control уже выполнил все четыре обязательных поведения. Stop condition удалил
  candidate, router/compiler registrations и временный cap реестра 8 000 bytes.
  Reusable quality contracts и typed rejection evidence сохранены в docs.
- Новый review-recovery facade разделён на связные модули authorization,
  legacy-round и resolution-evidence; durable payload/identity research
  callback централизован между producer и recovery. Focused tests фиксируют
  refactor и результат explicit `cancel` после accepted callback.

### Совместимость

- Это documentation-first patch release. Runtime, permissions, providers,
  fallbacks, built-in pipeline descriptors и custom PipelineSpec ceilings не
  меняются за пределами exact regression-covered repairs research callback,
  default MCP runtime-config self-bootstrap и review recovery.
- Bootstrap — отдельно разрешённое compatibility exception: default
  `sync-config --apply` получает только узкое filesystem authority создать
  missing canonical `runtime.env` из validated committed example.

## [2.6.2] — 2026-08-03

### Исправлено

- cmux workspace progress теперь показывает только exact live программы
  dispatch/review/research из coordinator origin workspace и сразу очищается в
  idle. Terminal controller подавляет stale descendants, а controller с
  заведомо отсутствующим exact surface больше не выглядит активной работой,
  включая stale attention после failed launch.
- Каждый publish использует один bounded live-tree snapshot. Неизвестный probe
  сохраняет текущий UI и не меняет lifecycle; Claude и Codex используют одну
  content-free строку и одинаковый cleanup.
- Coordinator SessionStart обновляет stale progress, не передавая task
  worktree полномочия coordinator status.
- Сохранён pre-integrated turn-end save-and-close fix; ordinary provider launch
  закрепляет trusted env-shebang interpreter и exact executor product root.
- Сохранён bounded hot-thread cache eviction: заполненный Active Threads cache
  вытесняет самую старую запись вместо отказа добавить новый актуальный thread.
- Возвращена reviewer-local видимость Claude usage: code-owned стандартная
  status line показывает model, effort, context, 5H и 7D limits через
  subscription-compatible профиль. User/project/local setting sources и
  обычная Claude memory исключены; skills, marketplace autoinstall, MCP, сеть,
  product writes и произвольная status-line command остаются отключены.
- Зафиксировано, что неопределённость чужого stale controller сохраняет
  текущий bar и устраняется только exact harness diagnose/reconcile/cancel.

### Изменено

- Единственный исторический план v2.1.1 перенесён из `docs/plans` в
  канонический `wiki/plans` с executed provenance, DragonScale address,
  сохранённым body и ссылкой на validated final result.

## [2.6.1] — 2026-08-03

### Добавлено

- Добавлен только явный `--full`: четыре lane по провайдерам и зонам
  ответственности; автоматически Full не выбирается. В Deep каждый провайдер
  получает независимый holistic обзор, а explicit single-model Deep разделяет
  intent и engineering.
- Публичные lane identities стали стабильными по провайдерам
  (`anthropic-*`, `openai-*`); имена моделей остаются routing aliases.

### Исправлено

- Исправлены выбор свежего current review, exact same-session continuation,
  bounded delta transport, callback/checkpoint races и exact reviewer cleanup,
  найденные Full-topology dogfood.
- Принятый terminal callback завершается и без cmux resume checkpoint;
  отсутствие checkpoint типизировано и не разрешает новый provider effect.
- Независимые review-lane больше не конфликтуют из-за одинакового локального
  finding ID без переписывания trusted callback bytes; outcome-review
  выполняется и без implementer summary.

### Безопасность

- Claude и Codex reviewer могут писать только в callback/test scratch своей
  lane и не могут подменить соседний callback или изменить product worktree.
- Codex reviewer не получает ambient-запись в системные временные каталоги,
  работает без сети и фильтрует credential-like переменные shell-процессов;
  persisted reviewer command повторно проверяется fail-closed перед запуском.

### Совместимость

- Lane IDs и review-v1 axis vocabulary намеренно изменены. Перед обновлением
  завершите или отмените активные review 2.6.0: они не мигрируются молча.

## [2.6.0] — 2026-08-02

### Добавлено

- Добавлен канонический Outcome Contract v1 внутри утверждённого плана:
  стабильный digest в task metadata v4, reserved ContextPacket delivery и
  Wiki Summary v2 с typed outcome disposition, bounded evidence и residual-gap
  pointers.
- Добавлены bounded `review-inspect`, identity-bound решения по каждому review
  finding, content-free review telemetry, детерминированные paired evaluations
  и пятипроходный `improve-skills` с обязательной проверкой goal preservation.
- Добавлен provenance-aware command evidence для RT10 runbook distillation:
  учитываются agent-executed команды и строго типизированные user-attested
  результаты.

### Изменено

- `clarify`, `design`, `prototype`, `save-plan`, `debug`, `tdd`, `review` и
  `reap` получили применимые общие практики из запиненных Superpowers и Matt
  Pocock Skills без замены harness-first lifecycle.
- Review считает implementer summary непроверенным claim, сначала проверяет
  утверждённый outcome, классифицирует каждый declared evidence item и в deep
  режиме сохраняет независимые Fable/spec и Sol/engineering axes.
- Новые задачи используют чистые metadata v4 и единый severity vocabulary;
  исторические v1-v3 operations и summaries остаются читаемыми, но не
  переписываются и не мигрируются молча.

### Исправлено

- Исправлены accepted-callback cleanup, зависшие review verification и
  finalization recovery, stale-callback liveness, fresh-review resolution
  identity, protected-research cleanup/error normalization и загрязнение Git
  status служебным dispatch binding.
- В компактный debug skill возвращены явные invocation, diagnosis-only,
  feedback-loop и residual-uncertainty маркеры без изменения безусловного
  architecture stop после трёх неудачных исправлений.

### Безопасность

- Outcome fields не могут разрешать effects, расширять permissions, продолжать
  typed stop или создавать второй scheduler, pipeline engine, review lane либо
  model call в детерминированных переходах.
- Reviewer inspection остаётся bounded и read-only; callback, resolution,
  verification, cleanup и reap evidence привязаны к точным operation, receipt,
  callback, plan, contract и Git identities.

## [2.5.1] — 2026-08-01

### Добавлено

- Добавлены точная диагностика stale operations с identity-bound recovery,
  прототип same-session review delta и сравнение запиненных библиотек
  Superpowers и Matt Pocock Skills.
- Зафиксированы результаты девяти независимых real-task dogfood задач для
  change, fix, prototype, research и vault-health workflows.

### Изменено

- Усилены lifecycle-контракты task/review: exact surface ownership, canonical
  summaries, prior-phase evidence, bounded telemetry verdicts и явная проверка
  валидности review callbacks.
- Shared plans сохраняются при reap; документация скиллов удерживается в
  бюджете; исправлена навигация вольта, найденная real-task аудитом.

### Исправлено

- Исправлены abnormal acceptance cleanup, доказательство release stale
  operation, неоднозначные reap modes, callback races и порядок review
  finalization.
- Protected research сразу классифицирует умерший provider, сохраняет
  разрешённый runtime interpreter и обходит cmux wrapper только для
  изолированных fetch/synthesis. Dispatch и review сохраняют exact-surface
  wrappers.
- Исправлены task summary rendering и stale identity diagnostics: repair и
  resume остаются привязаны к точной operation и принятому phase evidence.

## [2.5.0] — 2026-07-31

### Добавлено

- Добавлены ограниченные model-authored данные `PipelineSpec`, строгая
  компиляция, immutable approval snapshots, typed branches/loops и
  зарегистрированные checks поверх существующего harness lifecycle.
- Добавлены code-owned liveness recovery и content-free promotion reporting
  для повторяющихся успешных custom definitions.

### Безопасность

- Provider routes, команды, permissions, effects, dependencies и approval
  остались вне власти модели; custom definition не может расширить выбранный
  built-in baseline.

## [2.4.1] — 2026-07-31

### Добавлено

- Завершён исполняемый профиль `engineering/fix` с persistent-фазами
  reproduce, root-cause, regression-test, minimal-fix, verification, review и
  reap-ready.
- Добавлены immutable phase receipts, bounded retry/restart policy, typed
  решения `cannot-reproduce` и restart-safe resume по принятому evidence.

### Изменено

- Compiled pipelines используют единственные 2.3 operation store, supervisor,
  provider session, callback seam и coordinator-owned finalization path.

## [2.3.0] — 2026-07-30

### Добавлено

- Добавлено restartable owner-scoped harness-ядро с typed operation specs,
  atomic state, write-ahead effects, exact callbacks, reconciliation и
  командами `status`/`inspect`/`resume`/`cancel`/`close`/`doctor`.
- Добавлены публичные workflows `review` и защищённый `research`, а также
  инженерные скиллы `debug`, `tdd`, `design`, `prototype` и
  `resolve-conflict`.
- Добавлены unified simple/deep review contracts и четырёхъячеечный live
  acceptance driver, привязанный к точному SHA.
- Добавлен единый state-driven review facade для dispatched tasks и текущего
  checkout; ad-hoc review больше не требует ручной сборки harness pointers или
  `.task-meta.json`.

### Изменено

- Механика cmux, provider process, worktree, context, callback, verification,
  retry и cleanup перенесена в code-owned harness modules.
- Aliases Sol, Terra, Opus и Fable и review-профили централизованы в
  `config/model-routing.toml`; alias Opus закреплён за `claude-opus-5`, а не
  за меняющимся host-default.
- Защищённый research теперь использует vaultless fetch, networkless synthesis,
  hashed pointer artifacts и запись в vault только координатором.
- Версия 2.3.0 является чистым runtime baseline; pre-harness operation state не
  мигрируется.

### Удалено

- Без compatibility aliases удалены `dispatch-workspace`, `review-dispatch`,
  `review-send`, `reap-send` и `autoresearch`. Используются `dispatch`,
  `review`, `reap` и `research`.
- Legacy skill-by-runtime acceptance и prompt baselines заменены hermetic replay
  покрытием и четырьмя ограниченными live-cell.

### Безопасность

- Wrong-run, late, terminal, mutated и duplicate callbacks теперь fail closed
  либо становятся idempotent no-op в соответствии с точным operation state.
- Review approval публикуется только после выхода provider и точного cleanup
  принадлежащих операции ресурсов.

## [2.1.3] — 2026-07-29

### Исправлено

- Исправлена очистка primary coordinator-review: корневой review-state больше
  не считается v3 broker operation только потому, что `--state-dir` указывает
  на канонический checkout. После подтверждённого ревью процесс теперь закрывает
  свой точный cmux surface без отсутствующих `project_id`, `task_id` и
  `lane_id`.
- Для настоящих v3 operation directories сохранено fail-closed поведение:
  отсутствующая или повреждённая broker identity по-прежнему оставляет точный
  reviewer surface видимым и доступным для безопасного retry.
- Добавлена hermetic-регрессия полного root-state пути
  `request-exit` → `after-exit` с проверкой закрытия точного surface.
- Test harness защищённого research теперь удаляет ambient routing текущей
  coordinator-сессии перед запуском subprocess-фикстур. Благодаря этому
  `make test` остаётся hermetic внутри активных Claude/Codex-сессий без
  изменения продуктового routing.
- Синтетическая identity acceptance-координатора теперь сохраняется локально в
  disposable clone и не теряется, когда Codex фильтрует у tool-команд
  унаследованные env-переменные. Приоритет identity обычных Claude/Codex-
  сессий не меняется.
- Восстановлен callback opposite-model Claude review: оба нативных редактора
  разрешены для одного и того же точного `.review-outbox.json`. Reviewer
  остаётся в `dontAsk`, product-read-only и не получает записи в другие пути
  checkout-а.
- Обработчик точного native workspace-trust prompt теперь ждёт ограниченные
  30 минут вместо прежних 120 секунд; сниженная частота polling не создаёт
  пожизненный поток subprocess-ов у длинных задач.
- Tree-запрос к актуальному cmux теперь явно просит UUID и short refs, сохраняя
  совместимость со старыми CLI. Поэтому review/dispatch снова разрешает точный
  workspace вызывающего координатора после изменения формата cmux по умолчанию.
- Live primary coordinator review теперь якорится к текущему точному
  `CMUX_SURFACE_ID`, а не переиспользует устаревший корневой task-handoff от
  предыдущего review cycle.
- Root-scoped coordinator review больше не захватывает и не валидирует
  неиспользуемый resume checkpoint; broker-scoped review rounds сохраняют
  прежнее checkpoint-поведение.
- Явный review primary checkout теперь использует собственную ограниченную
  политику approve/resolve/escalate вместо устаревшей legacy task metadata.
  Подтверждённый approve механически применим, а finish ставит exact-surface
  cleanup даже без unattended dispatch contract.
- Model-authored setup для acceptance-ячеек `reap`/`reap-send` заменён на
  подготовленные runner-ом v3 task, canonical summary, точные coordinator/task
  surfaces и readiness handshake. Ячейки по-прежнему выполняют настоящий
  opposite-model review, duplicate-safe reap-send, final reap, graceful task
  exit и независимый durable proof, но больше не падают из-за выдуманных
  моделью адресов плана.
- Unsafe-research acceptance закреплён на одной стабильной официальной
  PEP-странице с ограниченным same-URL GET fallback. Теперь ячейка проверяет
  single-context route, а не случайный выбор ненадёжного documentation endpoint.
  Outbound proxy access к `peps.python.org` получает только эта точная Codex
  acceptance-ячейка; политики остальных acceptance и product task не меняются.

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
