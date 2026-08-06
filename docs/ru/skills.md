# Skills: полный inventory версии 2.6.5

Skill — versioned reasoning contract. Claude использует plugin skill `/name`;
Codex — `$llm-obsidian:name`. Другой agent может прочитать
`skills/<name>/SKILL.md` вручную. Router даёт hint, но не расширяет разрешения.

## Каталог 35 skills

| Skill и вызов | Когда применять | Вход | Результат и граница | Permission/effect · минимальный пример |
|---|---|---|---|---|
| `agenda` · `/agenda` · `$llm-obsidian:agenda` | Собрать незавершённые планы/reminders | Период или текущий месяц | Carry-forward и monthly report; не future planning | Vault write · `/agenda collect` |
| `backlog` · `/backlog` · `$llm-obsidian:backlog` | Быстро добавить/list/promote inbox item | Item или режим list/promote | Append-only capture; полноценная заметка идёт через save | Vault append · `/backlog add проверить индекс` |
| `canvas` · `/canvas` · `$llm-obsidian:canvas` | Явно создать/изменить Obsidian Canvas | Имя canvas и cards/links | Визуальные cards/embeds; manual-only | Явный `.canvas` write · `/canvas create Architecture` |
| `clarify` · `/clarify` · `$llm-obsidian:clarify` | Неясный outcome перед кодом | Черновое требование | По одному существенному вопросу, без реализации | Conversation-only · `/clarify новый import flow` |
| `close` · `/close` · `$llm-obsidian:close` | Сохранить и выйти из текущего agent process | Текущая session | Сохранённый контекст и выход; не закрывает произвольную cmux surface | Vault write + process exit · `/close` |
| `codebase-design` · `/codebase-design` · `$llm-obsidian:codebase-design` | Запутанные границы модулей | Модули и design question | Deep module design и test seams | Read-only · `/codebase-design harness callbacks` |
| `daily` · `/daily` · `$llm-obsidian:daily` | Итог дня по evidence | Дата и доступные session/git facts | EOD status; не план на будущее | Vault write · `/daily` |
| `debug` · `/debug` · `$llm-obsidian:debug` | Воспроизводимый defect | Symptom, repro, expected behavior | Root cause; fix только если разрешён | Read-only diagnosis · `/debug validation exits 2` |
| `defuddle` · `/defuddle` · `$llm-obsidian:defuddle` | Очистить web page перед ingest | URL страницы | Readable Markdown; не vault write сам по себе | Network read · `/defuddle https://example.com/guide` |
| `design` · `/design` · `$llm-obsidian:design` | Архитектурные варианты до реализации | Outcome и constraints | Boundaries, alternatives, ADR candidate | Read-only · `/design callback ownership` |
| `dispatch` · `/dispatch` · `$llm-obsidian:dispatch` | Передать утверждённый plan в worktree | Путь approved plan | Harness task; не выполняет неутверждённый plan | Local worktree/cmux effect · `/dispatch wiki/plans/example.md` |
| `distill-runbook` · `/distill-runbook` · `$llm-obsidian:distill-runbook` | Превратить captured commands в human runbook | Current session command log | Sanitized procedure; не выполняет её | Vault write · `/distill-runbook` |
| `draft` · `/draft` · `$llm-obsidian:draft` | Подготовить внешний ответ | Получатель, context, intent | 2–3 redacted варианта; не отправляет | No-send output · `/draft ответ на issue` |
| `find-session` · `/find-session` · `$llm-obsidian:find-session` | Найти похожую прошлую сессию | Поисковая фраза | Read-only top matches | Read-only · `/find-session callback repair` |
| `implementation-plan` · `/implementation-plan` · `$llm-obsidian:implementation-plan` | Спланировать multi-file change | Approved behavior и constraints | Owned TDD slices, interfaces, evidence | Plan-only · `/implementation-plan docs gate` |
| `improve-skills` · `/improve-skills` · `$llm-obsidian:improve-skills` | Проверить/улучшить существующие skills | Exact skill set и eval evidence | Five-pass audit без semantic drift | Skill writes только в approved scope · `/improve-skills skills/save` |
| `journal` · `/journal` · `$llm-obsidian:journal` | Датированная заметка/reminder/plan | Дата, item и режим | Journal update и session map | Vault transaction · `/journal на завтра проверить RC` |
| `learn` · `/learn` · `$llm-obsidian:learn` | Учиться по wiki curriculum | Module или quiz request | Study/quiz/practice/progress | Vault read; progress write по запросу · `/learn quiz module-2` |
| `obsidian-bases` · `/obsidian-bases` · `$llm-obsidian:obsidian-bases` | Создать `.base` view | Source notes, fields, filters, view | Native table/cards/filter/formula | Vault `.base` write · `/obsidian-bases table projects` |
| `obsidian-markdown` · `/obsidian-markdown` · `$llm-obsidian:obsidian-markdown` | Писать Obsidian Flavored Markdown | Note intent и нужные OMF elements | Wikilinks, embeds, callouts, properties | Write только по task authority · `/obsidian-markdown callout summary` |
| `prototype` · `/prototype` · `$llm-obsidian:prototype` | Ответить на один технический вопрос spike'ом | Один bounded question | Disposable evidence; не production change | Disposable-worktree write · `/prototype можно ли compile spec` |
| `reap` · `/reap` · `$llm-obsidian:reap` | Закрыть approved dispatch task | Terminal task/review evidence | Typed Wiki Summary, review archive, lifecycle close | Coordinator-owned vault/lifecycle effect · `/reap` |
| `research` · `/research` · `$llm-obsidian:research` | Текущий/нишевый вопрос с источниками | Bounded query и minimal context | Isolated fetch + networkless synthesis | Protected network fetch · `/research PipelineSpec guidance` |
| `resolve-conflict` · `/resolve-conflict` · `$llm-obsidian:resolve-conflict` | Уже существующий Git conflict | Exact conflicted worktree и intent evidence | Один resolved conflict set | Exact-worktree write · `/resolve-conflict` |
| `review` · `/review` · `$llm-obsidian:review` | Проверить outcome/code/spec/release | Purpose, exact HEAD и evidence boundary | Harness-owned read-only reviewer lanes | Provider lifecycle через harness · `/review --deep` |
| `save-plan` · `/save-plan` · `$llm-obsidian:save-plan` | Сохранить утверждённый план | Plan body и provenance | Canonical wiki plan | Vault transaction · `/save-plan` |
| `save` · `/save` · `$llm-obsidian:save` | Сохранить решение/вопрос/знание | Content, type и provenance | Deduplicated vault page | Vault transaction · `/save решение по callback` |
| `split` · `/split` · `$llm-obsidian:split` | Preview или явная bounded activation approved plan | Frozen plan/Outcome Contract, bounded candidates; для activation — `--dispatch` | Exact SplitManifest; preview имеет zero effects, activation запускает dependency-ready workspace children через harness и принимает только exact approved resource-free receipts | Preview: read-only · `/split wiki/plans/example.md`; activation: `/split --dispatch wiki/plans/example.md` |
| `tdd` · `/tdd` · `$llm-obsidian:tdd` | Реализовать ясное поведение | Authorized behavior и observable seam | RED→GREEN slices; не diagnosis-only | Product/test writes · `/tdd fail-closed parent gate` |
| `unsafe-research` · `/unsafe-research` · `$llm-obsidian:unsafe-research` | Явно принять single-context web risk | Query, full context, explicit authorization | Cited answer; никогда не fallback protected research | Network с принятым context risk · `/unsafe-research current vendor status` |
| `wiki-fold` · `/wiki-fold` · `$llm-obsidian:wiki-fold` | Свернуть старые operation-log entries | Oldest unprocessed entries | Deterministic rollup, dry-run по умолчанию | Vault transaction только explicit commit · `/wiki-fold --dry-run` |
| `wiki-ingest` · `/wiki-ingest` · `$llm-obsidian:wiki-ingest` | Импортировать local document/protected URL | File path или protected URL | Normalize, dedup, provenance, one transaction | Local read/isolated fetch + vault write · `/wiki-ingest report.pdf` |
| `wiki-lint` · `/wiki-lint` · `$llm-obsidian:wiki-lint` | Проверить здоровье vault | Vault scope и нужный report | Orphans, links, claims, frontmatter, dashboards | Read audit; dashboard write по contract · `/wiki-lint` |
| `wiki-query` · `/wiki-query` · `$llm-obsidian:wiki-query` | Ответить из wiki | Query и quick/standard/deep mode | Grounded vault answer; web gap isolated | Vault read; optional isolated fetch · `/wiki-query deep callback` |
| `wiki` · `/wiki` · `$llm-obsidian:wiki` | Познакомиться с vault и выбрать маршрут | Goal или orientation question | Read-only orientation, не mutation | Read-only · `/wiki как сохранить решение` |

## Выбор и комбинации

- Неясная feature: `clarify` → `design` → `implementation-plan` → `tdd`.
- Project documentation: `implementation-plan` → `tdd` → deterministic docs
  gate → `review`; отдельный `document-project` skill в 2.6.3 не поставляется,
  потому что fresh control уже покрывал требуемое поведение.
- Новый документ: `defuddle` для web cleanup или `wiki-ingest` для файла →
  `save`/transaction → `wiki-query` для проверки findability.
- Долгая task: `save-plan` → `dispatch` → `review` → `reap`.
- Approved параллельная task: `save-plan` → `split` preview → явный
  `split --dispatch` → manifest-order join exact receipts; merge/release остаются
  отдельными действиями.
- Defect: `debug` до root cause, затем разрешённый `tdd`; не называйте
  отсутствие expected new behavior mechanism failure.

Источники: каталоги [`skills/`](../../skills/),
[`AGENTS.md`](../../AGENTS.md) и generated Codex marketplace.
