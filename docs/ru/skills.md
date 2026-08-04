# Skills: полный inventory версии 2.6.3

Skill — versioned reasoning contract. Claude использует plugin skill `/name`;
Codex — `$llm-obsidian:name`. Другой agent может прочитать
`skills/<name>/SKILL.md` вручную. Router даёт hint, но не расширяет разрешения.

## Каталог 34 skills

| Skill и вызов | Когда применять | Результат и граница |
|---|---|---|
| `agenda` · `/agenda` · `$llm-obsidian:agenda` | Собрать незавершённые планы/reminders | Carry-forward и monthly report; не future planning |
| `backlog` · `/backlog` · `$llm-obsidian:backlog` | Быстро добавить/list/promote inbox item | Append-only capture; полноценная заметка идёт через save |
| `canvas` · `/canvas` · `$llm-obsidian:canvas` | Явно создать/изменить Obsidian Canvas | Визуальные cards/embeds; manual-only |
| `clarify` · `/clarify` · `$llm-obsidian:clarify` | Неясный outcome перед кодом | По одному существенному вопросу, без реализации |
| `close` · `/close` · `$llm-obsidian:close` | Сохранить и выйти из текущего agent process | Не закрывает произвольную cmux surface |
| `codebase-design` · `/codebase-design` · `$llm-obsidian:codebase-design` | Запутанные границы модулей | Read-only deep module design и test seams |
| `daily` · `/daily` · `$llm-obsidian:daily` | Итог дня по evidence | EOD status; не план на будущее |
| `debug` · `/debug` · `$llm-obsidian:debug` | Воспроизводимый defect | Root cause; fix только если разрешён |
| `defuddle` · `/defuddle` · `$llm-obsidian:defuddle` | Очистить web page перед ingest | Readable Markdown; не vault write сам по себе |
| `design` · `/design` · `$llm-obsidian:design` | Архитектурные варианты до реализации | Boundaries, alternatives, ADR candidate |
| `dispatch` · `/dispatch` · `$llm-obsidian:dispatch` | Передать утверждённый plan в worktree | Harness task; требует cmux |
| `distill-runbook` · `/distill-runbook` · `$llm-obsidian:distill-runbook` | Превратить captured commands в human runbook | Sanitized procedure; не выполняет её |
| `draft` · `/draft` · `$llm-obsidian:draft` | Подготовить внешний ответ | 2–3 redacted варианта; не отправляет |
| `find-session` · `/find-session` · `$llm-obsidian:find-session` | Найти похожую прошлую сессию | Read-only top matches |
| `implementation-plan` · `/implementation-plan` · `$llm-obsidian:implementation-plan` | Спланировать multi-file change | Owned TDD slices, interfaces, evidence |
| `improve-skills` · `/improve-skills` · `$llm-obsidian:improve-skills` | Проверить/улучшить существующие skills | Five-pass audit без semantic drift |
| `journal` · `/journal` · `$llm-obsidian:journal` | Датированная заметка/reminder/plan | Journal update и session map |
| `learn` · `/learn` · `$llm-obsidian:learn` | Учиться по wiki curriculum | Study/quiz/practice/progress |
| `obsidian-bases` · `/obsidian-bases` · `$llm-obsidian:obsidian-bases` | Создать `.base` view | Native table/cards/filter/formula |
| `obsidian-markdown` · `/obsidian-markdown` · `$llm-obsidian:obsidian-markdown` | Писать Obsidian Flavored Markdown | Wikilinks, embeds, callouts, properties |
| `prototype` · `/prototype` · `$llm-obsidian:prototype` | Ответить на один технический вопрос spike'ом | Disposable worktree, не production change |
| `reap` · `/reap` · `$llm-obsidian:reap` | Закрыть approved dispatch task | Typed Wiki Summary, review archive, lifecycle close |
| `research` · `/research` · `$llm-obsidian:research` | Текущий/нишевый вопрос с источниками | Isolated fetch + networkless synthesis |
| `resolve-conflict` · `/resolve-conflict` · `$llm-obsidian:resolve-conflict` | Уже существующий Git conflict | Один exact worktree по intent evidence |
| `review` · `/review` · `$llm-obsidian:review` | Проверить outcome/code/spec/release | Harness-owned read-only reviewer lanes |
| `save-plan` · `/save-plan` · `$llm-obsidian:save-plan` | Сохранить утверждённый план | Canonical wiki plan с provenance |
| `save` · `/save` · `$llm-obsidian:save` | Сохранить решение/вопрос/знание | Vault transaction; сначала поиск дубликатов |
| `tdd` · `/tdd` · `$llm-obsidian:tdd` | Реализовать ясное поведение | RED→GREEN slices; не diagnosis-only |
| `unsafe-research` · `/unsafe-research` · `$llm-obsidian:unsafe-research` | Явно принять single-context web risk | Никогда не fallback защищённого research |
| `wiki-fold` · `/wiki-fold` · `$llm-obsidian:wiki-fold` | Свернуть старые operation-log entries | Dry-run по умолчанию, commit явно |
| `wiki-ingest` · `/wiki-ingest` · `$llm-obsidian:wiki-ingest` | Импортировать local document/protected URL | Normalize, dedup, provenance, one transaction |
| `wiki-lint` · `/wiki-lint` · `$llm-obsidian:wiki-lint` | Проверить здоровье vault | Orphans, links, claims, frontmatter, dashboards |
| `wiki-query` · `/wiki-query` · `$llm-obsidian:wiki-query` | Ответить из wiki | Quick/standard/deep retrieval; web gap isolated |
| `wiki` · `/wiki` · `$llm-obsidian:wiki` | Познакомиться с vault и выбрать маршрут | Read-only orientation, не mutation |

## Выбор и комбинации

- Неясная feature: `clarify` → `design` → `implementation-plan` → `tdd`.
- Project documentation: `implementation-plan` → `tdd` → deterministic docs
  gate → `review`; отдельный `document-project` skill в 2.6.3 не поставляется,
  потому что fresh control уже покрывал требуемое поведение.
- Новый документ: `defuddle` для web cleanup или `wiki-ingest` для файла →
  `save`/transaction → `wiki-query` для проверки findability.
- Долгая task: `save-plan` → `dispatch` → `review` → `reap`.
- Defect: `debug` до root cause, затем разрешённый `tdd`; не называйте
  отсутствие expected new behavior mechanism failure.

Источники: каталоги [`skills/`](../../skills/),
[`AGENTS.md`](../../AGENTS.md) и generated Codex marketplace.
