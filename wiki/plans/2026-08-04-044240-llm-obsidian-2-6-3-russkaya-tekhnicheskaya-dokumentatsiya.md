---
type: plan
title: "LLM Obsidian 2.6.3 — русская техническая документация"
address: c-000107
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: executed
created: 2026-08-04
updated: 2026-08-04
tags:
  - plan
  - manual-save
  - release
  - documentation
---

# LLM Obsidian 2.6.3 — русская техническая документация

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Сделать LLM Obsidian самостоятельно изучаемым инженерным продуктом: пользователь должен уметь установить его, безопасно работать с wiki и скиллами, строить и запускать pipeline, а разработчик — расширять и проверять проект без чтения реализации по классам.",
  "desired_outcome": "Выпустить лёгкий документационный релиз 2.6.3 с подробным русскоязычным handbook, практическими сценариями и справочниками, а также с проверенным skill для проектной документации и воспроизводимым documentation pipeline на существующем PipelineSpec DSL. Документация должна учить глубокому использованию и сопровождению проекта, а не ограничиваться обзором функций.",
  "success_evidence": [
    {
      "evidence_id": "E1-install-to-first-result",
      "observable": "Читатель с чистым checkout проходит документированный путь установки Claude Code и/или Codex, настраивает компоненты, выполняет preflight и получает первый сохранённый результат; команды, ожидаемые признаки успеха, варианты без optional-компонентов и rollback описаны явно."
    },
    {
      "evidence_id": "E2-complete-user-handbook",
      "observable": "Русский handbook имеет явные маршруты для пользователя, оператора, автора pipeline и разработчика; объясняет mental model, все поставляемые skills, планы, Outcome Contract, сессии, dispatch, review, reap, wiki/retrieval, ingest/research, recovery и типовые комбинации skills на конкретных примерах."
    },
    {
      "evidence_id": "E3-pipeline-dsl-mastery",
      "observable": "Отдельный учебник по PipelineSpec v1 ведёт от built-in выбора к созданию собственного bounded pipeline: разбирает каждое поле, primitives, schemas, transitions, budgets, permissions, side effects, context pointers, approval, verification и recovery; поставляемый documentation pipeline парсится и компилируется существующим code-owned compiler."
    },
    {
      "evidence_id": "E4-maintainer-guide",
      "observable": "Разработчик по документации может создать изолированный worktree, найти authoritative source, изменить skill/config/schema/component, выбрать дешёвые unit/static проверки, запустить coverage и полный release gate, обновить адаптеры/плагины, выполнить upgrade/rollback и диагностировать распространённые сбои."
    },
    {
      "evidence_id": "E5-document-project-skill",
      "observable": "Новый document-project skill создан через skill-creator и improve-skills: capability matrix классифицирует релевантные практики Matt, Superpowers и LLM Obsidian; baseline-сценарии сначала демонстрируют реальные пробелы, затем RED/GREEN/REFACTOR и независимый forward-test доказывают полноту, правильный порядок понятий, практичность и сохранение Outcome Contract."
    },
    {
      "evidence_id": "E6-docs-verification",
      "observable": "Детерминированный docs gate подтверждает навигацию без битых относительных ссылок, полное покрытие skill inventory, синтаксически валидные JSON/TOML примеры, компиляцию documentation PipelineSpec, отсутствие placeholders и наличие у учебных страниц prerequisites, runnable example, expected result, failure/recovery и source-of-truth ссылок."
    },
    {
      "evidence_id": "E7-release-no-runtime-regression",
      "observable": "На точном release HEAD проходят focused docs/skill/DSL проверки, make test, acceptance-check, validate-vault, Codex adapter/MCP checks и diff-check; независимые intent и implementation/release review подтверждают соответствие цели, а diff не меняет runtime state machine, provider permissions или существующие pipeline semantics, кроме одного узкого regression-covered исправления: принятый research callback при exact cleanup завершается как complete, а ранее ошибочно cancelled receipt восстанавливается только при совпадении callback digest, run, request и artifact."
    }
  ],
  "non_goals": [
    "Перевод полного handbook на английский в 2.6.3; английская навигация может только сослаться на русскую документацию.",
    "Документирование каждого класса, функции или внутреннего поля реализации.",
    "Новые runtime primitives, второй orchestration engine, parallel/join, Project Spaces или task graph из плана 2.7.",
    "Широкое изменение callback, lifecycle, cleanup, provider sandbox, model routing или permission semantics; единственное исключение — узкий fail-closed repair принятого research callback, обнаруженный при выполнении этого плана и доказанный regression-тестами.",
    "Генератор сайта, внешний documentation hosting, CMS или новая обязательная зависимость.",
    "Общий rewrite существующих skills; меняются только новый document-project и минимальное регистрационное подключение, необходимое его DSL-пайплайну.",
    "Обещание полной платформенной поддержки Windows или Linux поверх фактической capability matrix 2.6.2."
  ]
}
```

## 1. Исходная точка и зафиксированные решения

- Реализация начинается от опубликованного `v2.6.2`, commit `613b93b2271c386a62c5e44fb82ca4f70e972a7a`, в отдельном worktree и ветке `release/2.6.3`; текущий dirty checkout с планом 2.7 не трогается.
- Release docs-first: основная масса diff — `docs/ru/`, новый skill, один валидный пример PipelineSpec, тесты документационных контрактов и release metadata.
- Каноническая пользовательская документация живёт в `docs/ru/`. `README.ru.md` остаётся landing page и ведёт в handbook; wiki остаётся памятью конкретного vault, а не вторым набором продуктовых руководств.
- Документы организуются по задачам читателя, а не по структуре Python-модулей. Один факт имеет один authoritative source; другие страницы дают краткий контекст и ссылку.
- Примеры считаются частью интерфейса: у каждого есть prerequisites, команда/ввод, ожидаемый результат, способ проверить успех и recovery/rollback.
- Новый skill называется `document-project`. Он не пишет обзор по одному prompt; он строит coverage matrix, dependency order понятий, учебные маршруты, reference и runnable examples, а затем доказывает полноту.
- Documentation pipeline использует существующий PipelineSpec v1, `model_step → model_step → verify → review`, существующий store/FSM/supervisor и явный initial approval. Новых primitives и нового controller нет.
- Обнаруженный dogfood-дефект protected research включается как узкое исключение: generic reconcile не должен превращать exact exiting operation с durable accepted callback в cancelled; recovery уже затронутого receipt разрешён только при полном совпадении callback payload digest и валидного artifact. Это отдельный regression-covered repair без нового interface, permission или provider effect.
- Единственные допустимые runtime-adjacent изменения: зарегистрировать `document-project` как разрешённую semantic skill и зарегистрировать точную проверку `docs-tests → make test-docs`. Если это требует более широкого изменения, pipeline остаётся release-blocked, а runtime не расширяется обходным путём.

## 2. Почему выбран многостраничный handbook

### Вариант A — один большой README

Плюсы: легко найти. Минусы: невозможно одновременно быть quick start, учебником, reference и maintainer guide; команды и ограничения быстро теряются; обновление одного компонента создаёт конфликт по всему файлу. Отклонён.

### Вариант B — handbook с маршрутами, cookbook и reference

Плюсы: прогрессивное раскрытие, понятная навигация, один источник на правило, отдельные страницы можно проверять и обновлять независимо. `README.ru.md` остаётся коротким входом. Выбран.

### Вариант C — отдельный сайт/генератор документации

Плюсы: поиск и красивый UI. Минусы: новая зависимость, build/deploy surface и дублирование Markdown/Obsidian. Для 2.6.3 это лишняя система. Отклонён.

## 3. Информационная архитектура

### Вход и учебные маршруты

- `docs/ru/index.md` — карта handbook, версии, четыре маршрута и definition of done для каждого.
- `docs/ru/getting-started.md` — установка от чистого checkout до первого результата для Claude Code, Codex и agent-agnostic режима; optional components отделены от обязательных.
- `docs/ru/mental-model.md` — vault, wiki, skills, Outcome Contract, plan, task/worktree, harness operation, pipeline, review и reap как одна модель.
- `docs/ru/first-project.md` — сквозной tutorial: уточнить идею, сохранить план, исполнить, проверить, review, reap, найти результат.

### Ежедневная работа

- `docs/ru/skills.md` — полный inventory с trigger, когда применять/не применять, Claude/Codex invocation, входом, выходом, разрешениями и одним примером; затем рецепты комбинаций skills.
- `docs/ru/planning.md` — clarify/grill me, Outcome Contract, design, codebase-design, implementation-plan, save-plan, качественные и плохие примеры планов, evidence mapping и non-goals.
- `docs/ru/sessions-and-tasks.md` — foreground session, dispatch, worktree, cmux surface, status, callback, resume, close, cancel, reap и безопасное восстановление.
- `docs/ru/review.md` — Simple/Deep/Full, single-model fallback, intent/engineering responsibilities, resolution, verification iteration, terminal attention и примеры выбора режима.

### Pipeline и DSL

- `docs/ru/pipelines.md` — built-ins `lifecycle/default`, `engineering/change`, `engineering/fix`, selection rules, lifecycle и observability.
- `docs/ru/pipeline-dsl.md` — полный PipelineSpec v1 reference и tutorial создания custom pipeline с разбором security/approval boundary.
- `examples/pipelines/document-project-v1.json` — валидный reusable documentation pipeline без placeholders.
- `docs/ru/documentation-pipeline.md` — как подготовить Outcome Contract, запустить пример, интерпретировать approval card, получить handbook, обработать finding и завершить reap.

### Wiki, источники и память

- `docs/ru/wiki-memory.md` — структура vault, frontmatter, DragonScale, save/backlog/journal/daily, log/hot, retrieval, provenance и single-writer.
- `docs/ru/documents-and-research.md` — ingest, normalization, OCR, defuddle, protected research, unsafe research boundary, citations и типовые failure modes.
- `docs/ru/operations.md` — model routing, MCP gateway, optional embeddings, status/progress, telemetry, upgrade-preflight, backups и troubleshooting decision tree.

### Разработка и сопровождение

- `docs/ru/development.md` — repository map по ответственностям, authoritative contracts, ветка/worktree, TDD slices, code quality и безопасное изменение компонентов.
- `docs/ru/testing.md` — пирамида static/unit/contract/integration/live, Makefile targets, coverage denominators, mock boundaries, матрицы переходов, acceptance evidence и экономный порядок запуска.
- `docs/ru/extending.md` — создание/изменение skills через skill-creator + improve-skills, добавление schema/config/pipeline/MCP, Codex adapter sync и backward-compatibility boundary.
- `docs/ru/upgrading-and-releasing.md` — preflight, version metadata, changelog/release notes, adapter/plugin install, full gate, tag/release и rollback.
- `docs/ru/troubleshooting.md` — symptom → read-only diagnosis → supported recovery для retrieval locks, stale operations, callbacks, cleanup, permissions, provider limits и plugin drift.

### Практика и reference

- `docs/ru/cookbook.md` — минимум 12 сквозных use cases: новая feature, bugfix, документация, research, ingest PDF, knowledge save, runbook, single-model review, custom pipeline, failed callback recovery, upgrade, release.
- `docs/ru/reference/commands.md` — команды по задачам с authority и expected exit/result, без копирования внутренних API.
- `docs/ru/reference/configuration.md` — поддерживаемые config files, назначение, безопасное изменение и проверка.
- `docs/ru/reference/glossary.md` — термины в dependency order с единым русским переводом и английским canonical name.

## 4. Матрица источников для нового skill

| Capability | Источник | Локальная адаптация | Вердикт |
|---|---|---|---|
| Начинать обучение от цели человека | Matt `teach` mission | Outcome Contract и четыре reader journeys | adopt |
| Не использовать понятие до его введения | Matt `writing-shape`, `writing-beats`, `edit-article` | concept dependency map и glossary gate | adopt |
| Давать знания вместе с практикой и feedback loop | Matt `teach` | tutorial + exercise + verification + recovery | adopt |
| Сначала увидеть failure, затем написать skill | Superpowers `writing-skills`; system `skill-creator` | RED/GREEN/REFACTOR pressure cases | adopt |
| Подбирать форму инструкции под тип failure | Superpowers `writing-skills` | page contract вместо списка запретов | adopt |
| План должен исполняться человеком без контекста | Superpowers `writing-plans` | exact files, commands, expected results, no placeholders | adopt |
| Evidence перед completion | Superpowers `verification-before-completion` | docs gate + release evidence IDs | adopt |
| Русский textbook: проблема → определение → плохо/хорошо → нюансы → итог → вопросы | local `learn` Authoring | шаблон tutorial/guide pages | adopted-local |
| Пять quality passes и сохранение общей цели | local `improve-skills` | invocation/hierarchy/steering/pruning/goal-preservation verdict | adopted-local |
| Один durable Outcome Contract проходит через план, реализацию и review | local 2.6 foundation | source/coverage/evidence identities в skill и pipeline | adopted-local |
| HTML lessons, внешняя CMS, article-only interactive co-writing | Matt экспериментальные writing skills | не нужно для repository handbook | rejected |
| Upstream worktree/subagent/publishing mechanics | Superpowers | harness и vault contracts остаются authoritative | rejected |

Полный capability-gap verdict сохраняется в `docs/acceptance/v2.6.3-document-project-skill-audit.md`; строки без verdict запрещены.

## 4.1 Внешняя матрица практик документации и AI

Protected fetch зафиксировал восемь источников без ошибок. Synthesis был намеренно отклонён strict contract: он перечислил девятую citation, которой не было в fetch artifact. Поэтому ниже фиксируются только fetched source identities и проверяемые направления; ни один неподтверждённый synthesis-вывод не считается evidence. В Slice 0.5 исполнитель обязан перечитать точные источники в изолированном research-контексте и вынести для каждой практики adopt/adapt/reject с цитатой.

| Проверяемое направление | Fetched authoritative source | Требуемая локальная проверка |
|---|---|---|
| Разделение tutorial/how-to/reference/explanation | Diátaxis — https://diataxis.fr/ | Сопоставить четыре reader journeys и тип каждой страницы; не смешивать формы в одном тексте без причины. |
| Явная аудитория и prerequisites | Google Technical Writing, Audience — https://developers.google.com/tech-writing/one/audience | Проверить audience/result в начале каждой учебной страницы. |
| Безопасное применение LLM при авторинге | Google Technical Writing, Using LLMs — https://developers.google.com/tech-writing/two/llms | Зафиксировать source grounding, human/independent review и запрет принимать непроверенную AI-генерацию как факт. |
| Docs-as-code и executable checks | GitLab Docs, Documentation testing — https://docs.gitlab.com/development/documentation/testing/ | Вывести дешёвые link/schema/example/inventory gates и порядок до дорогого dogfood. |
| Версионность документации | GitHub Docs, Versioning documentation — https://docs.github.com/en/contributing/writing-for-github-docs/versioning-documentation | Для version-sensitive страниц маркировать exact supported release, source и update trigger. |
| Качество runnable code samples | Google developer documentation style guide, Code samples — https://developers.google.com/style/code-samples | Проверять copy-paste команды, prerequisites, expected result, failure/recovery и отсутствие секретов/placeholders. |
| Доступность | W3C WCAG 2.2 — https://www.w3.org/TR/WCAG22/ | Применить релевантные Markdown-критерии: содержательные headings/link text, текстовые альтернативы и отсутствие зависимости только от цвета. |
| Риски AI-generated content | NIST AI 600-1 — https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf | Ввести provenance, claim verification, review ownership и fail-closed disposition для неподтверждённых утверждений. |

Локальные числовые thresholds (число страниц, примеров, freshness period) не приписываются этим источникам: это repository policy и должна быть явно помечена как таковая. Все внешние verdicts в intent-артефактах до Slice 0.5 являются provisional и не закрывают citation-backed ruling.

## 5. Page quality contract

Каждая учебная или how-to страница обязана содержать:

1. Для кого она и какой observable result получает читатель.
2. Prerequisites и что можно пропустить.
3. Сначала введённые понятия, затем зависящие от них действия.
4. Один полный runnable example; дополнительные примеры только для реально иной ветки.
5. Expected result и независимый способ проверки.
6. Наиболее вероятные ошибки, read-only diagnosis и supported recovery/rollback.
7. Security/permission boundary там, где есть effects.
8. Ссылку на authoritative source/config/schema/script и соседний следующий шаг.

Reference-страницы вместо tutorial narrative дают полный field/command inventory, ограничения, defaults, failure semantics и минимальный корректный пример. Overview без практического маршрута не закрывает ни один evidence ID.

## 6. Вертикальные slices реализации

### Slice 0 — изоляция релиза и baseline gap inventory

- `files/responsibility`: отдельный worktree `release/2.6.3`; создать `docs/acceptance/v2.6.3-documentation-baseline.md` как честный inventory существующих страниц, команд, skills и пробелов.
- `consumes`: tag `v2.6.2`, текущие `README*.md`, `AGENTS.md`, `CLAUDE.md`, `docs/`, 34 `skills/*/SKILL.md`, schemas/config/Makefile и pinned upstream snapshots. Source-of-truth map обязан отдельно указать, какие operating facts принадлежат AGENTS/CLAUDE и где handbook только ссылается на них.
- `produces`: exact base SHA, source-of-truth map, reader/job coverage matrix, command inventory и список подтверждённых gaps.
- `failing evidence`: baseline matrix показывает отсутствие единого install→use→extend→test маршрута, document-project skill и executable documentation pipeline.
- `minimal green`: только baseline/coverage artifact; продуктовые документы ещё не пишутся.
- `refactor seam`: объединить дублирующие source rows, не менять существующие contracts.
- `focused verification`: `python3 skills/improve-skills/scripts/audit_skills.py --json`, upstream snapshot verify, `git diff --check`.
- `Outcome evidence`: подготавливает E2, E3, E4, E5 и честный denominator E6.

### Slice 0.5 — protected research, source rulings и lifecycle precondition

- `files/responsibility`: расширить `docs/acceptance/v2.6.3-documentation-baseline.md` матрицей восьми fetched sources; перенести узкий accepted-callback repair только в `scripts/harness/cli.py`, `scripts/harness/workflows/research.py`, `tests/harness/test_store.py` и `tests/harness/test_research_vertical.py`.
- `consumes`: exact fetch artifact operation `ce4f6af5-ea9b-42ed-aae1-904d6852a8ac`, source identities раздела 4.1 и уже зелёные local repair tests.
- `produces`: исчерпывающие claim-level adopt/adapt/reject rulings с citation, включая оба upstream reject-направления и documentation versioning; intent-stage внешние verdicts считаются provisional до этой записи. Invalid девятая citation явно не используется. Repair provenance: source patch SHA-256 `bf317dfb811232c85512565f4a57cbf1c50f5c22ee76a563d0e17936b2964748`; regression checks `CLI reconcile preserves an accepted callback as completion` и `cancelled accepted fetch recovers to the separate synthesis stage`. Exact cleanup больше не превращает accepted callback в cancellation.
- `failing evidence`: strict synthesis rejection воспроизводится; старый reconcile test показывает cancelled вместо complete; recovery отвергает несовпадающий digest/run/request/artifact.
- `minimal green`: только source rulings и узкий callback terminal-state/recovery repair; никаких иных lifecycle, provider или permission изменений.
- `refactor seam`: общий accepted-callback terminal predicate используется без ослабления обычной cancel semantics; research recovery остаётся payload-specific.
- `focused verification`: `tests/harness/test_store.py`, `test_research_vertical.py`, `test_runtime_sessions.py`, `make test-harness`, research contract validation и `git diff --check`.
- `Outcome evidence`: усиливает E5/E6 и явно ограничивает exception в E7.

### Slice 1 — RED для document-project и capability-gap contract

- `files/responsibility`: создать raw evaluation cases в `evals/cases/project-documentation.jsonl`, baseline result в `docs/acceptance/v2.6.3-document-project-skill-audit.md`, verdict records в `docs/acceptance/v2.6.3-document-project-skill-verdicts.json`.
- `consumes`: матрица раздела 4, system `skill-creator`, local `improve-skills`, pinned Matt/Superpowers и Outcome Contract.
- `produces`: минимум четыре pressure scenarios: обзор вместо handbook; термин до grounding; команды без expected/recovery; локальный green вместо outcome coverage.
- `failing evidence`: fresh-context исполнители без нового skill воспроизводят хотя бы три из четырёх заявленных failures; каждый результат сохраняется без подсказки ожидаемого ответа.
- `minimal green`: валидный capability inventory и baseline evidence, без написания skill.
- `refactor seam`: убрать сценарий, если control не воспроизводит failure; не авторить guidance для несуществующей проблемы.
- `focused verification`: strict verdict schema и JSONL parse; manual evidence ruling по каждому case.
- `Outcome evidence`: RED-часть E5.

### Slice 2 — создать `document-project` через skill-creator и improve-skills

- `files/responsibility`: создать `skills/document-project/SKILL.md`, `skills/document-project/agents/openai.yaml`, `skills/document-project/references/documentation-quality.md`, `skills/document-project/references/page-contracts.md`; обновить `.claude/skill-rules.json` и `config/skill-body-baseline.json` только по новому inventory.
- `consumes`: подтверждённые RED failures и классифицированные upstream/local capabilities.
- `produces`: concise skill normal path: preserve Outcome Contract → inventory sources/audiences/jobs → build concept dependency/IA → author by page type → verify examples/coverage → independent review → evidence-backed completion.
- `failing evidence`: structural tests сначала не находят новый skill, distinct strong-intent trigger, router positives/false positives, completion criterion и context pointers.
- `minimal green`: минимальный skill закрывает только наблюдённые failures; подробности вынесены в references, body остаётся в budget.
- `refactor seam`: удалить дубли с `improve-skills`, `learn`, `implementation-plan` и `review`; оставить точные context pointers и repository overrides.
- `focused verification`: `quick_validate.py`/repo structural audit, strict five-pass verdicts, router tests, instruction lint, skill budget и Codex adapter check.
- `Outcome evidence`: structural часть E5 и inventory часть E6.

### Slice 3 — GREEN/REFACTOR forward-test нового skill

- `files/responsibility`: расширить `evals/cases/project-documentation.jsonl`; добавить узкий standard-library runner `scripts/project-docs-eval-runner.py` только если существующий agent-eval runner не может выразить эти cases; сохранить results в `docs/acceptance/v2.6.3-document-project-skill-pressure.md`.
- `consumes`: exact new-skill bytes и те же baseline cases без leaked diagnosis. Forward-test запускается в fresh context исполнителем, который не видел authoring skill и обсуждение плана; его runtime/model/session identity сохраняется рядом с каждым result.
- `produces`: repeatable comparison baseline vs skill, variants для незнакомого проекта и уже частично документированного проекта.
- `failing evidence`: хотя бы один GREEN run до правки оставляет подтверждённый gap или создаёт новый loophole.
- `minimal green`: точечная guidance правка закрывает конкретный gap; не добавлять общий prose.
- `refactor seam`: сходящиеся instructions, один authoritative rule, один excellent example.
- `focused verification`: весь corpus, strict verdict audit, `make test-instruction-lint test-skill-budget test-codex-adapter`.
- `Outcome evidence`: полная E5.

### Slice 4 — docs gate и source-of-truth manifest

- `files/responsibility`: создать `tests/test_russian_documentation.py`; добавить `test-docs` в `.PHONY` и prerequisites агрегатного `test:` в `Makefile`; создать `docs/acceptance/v2.6.3-documentation-matrix.md`.
- `consumes`: page contract, current skill inventory, schema/config/script sources и будущий IA.
- `produces`: deterministic checks для required pages, index reachability, relative links, skill inventory, cross-runtime invocation, JSON/TOML examples, PipelineSpec compile, placeholders и обязательных page sections; source-of-truth manifest отдельно проверяет AGENTS.md/CLAUDE.md и запрещает handbook молча дублировать принадлежащие им operating facts вместо ссылки.
- `failing evidence`: тест падает на ещё отсутствующих страницах и document-project pipeline, а не на окружении или network.
- `minimal green`: test harness без docs content остаётся красным с точным списком gaps; затем зеленеет по мере slices 5–9.
- `refactor seam`: data-driven page/skill/command manifest; не строить отдельный documentation framework.
- `focused verification`: `make test-docs`, unit self-tests с намеренно битой ссылкой, пропущенным skill и invalid PipelineSpec.
- `Outcome evidence`: E6.

### Slice 5 — вход, mental model и первый полный результат

- `files/responsibility`: создать `docs/ru/index.md`, `getting-started.md`, `mental-model.md`, `first-project.md`; обновить `README.ru.md` и добавить одну нейтральную ссылку из `README.md`.
- `consumes`: canonical install/setup scripts, runtime capability matrix, session/dispatch/review/reap contracts.
- `produces`: четыре reader journeys и end-to-end tutorial от install до reaped wiki result.
- `failing evidence`: docs gate не находит entry path, runtime variants, expected results или fallback/rollback.
- `minimal green`: один честный путь для Claude, Codex и manual-agent branch без обещания parity там, где её нет.
- `refactor seam`: README остаётся landing page; подробности живут только в handbook.
- `focused verification`: link/section gate; выполнить cheap preflight/help commands в disposable checkout.
- `Outcome evidence`: E1 и основа E2.

### Slice 6 — skills, planning, sessions и review

- `files/responsibility`: создать `docs/ru/skills.md`, `planning.md`, `sessions-and-tasks.md`, `review.md`.
- `consumes`: exact 2.6.3 skill inventory, Outcome Contract schema, review topology, model routing и task-session docs.
- `produces`: полный skill catalog и практические decision tables; хорошие/плохие plan examples; lifecycle and recovery walkthroughs; Simple/Deep/Full и single-model fallback.
- `failing evidence`: inventory test перечисляет отсутствующие skills или invocation branch; page contract находит команды без expected/recovery.
- `minimal green`: каждый skill покрыт ровно один раз в reference table, а combinations показаны отдельными use cases.
- `refactor seam`: общие lifecycle facts ссылаются на sessions/review pages, не копируются в каждую skill row.
- `focused verification`: generated inventory comparison, router tests, docs links, review topology tests.
- `Outcome evidence`: E2.

### Slice 7 — PipelineSpec DSL и documentation pipeline

- `files/responsibility`: создать `docs/ru/pipelines.md`, `pipeline-dsl.md`, `documentation-pipeline.md`, `examples/pipelines/document-project-v1.json`; минимально обновить `scripts/harness/pipeline_builtins.py` semantic skill registry и `scripts/harness/custom_pipeline_contracts.py` verification check registry.
- `consumes`: PipelineSpec v1 schema, compiler policy, executable built-ins, document-project skill и `make test-docs`.
- `produces`: schema-valid custom pipeline `inventory → author → verify → review`, explicit approval card, bounded budget и `docs-tests` verification command.
- `failing evidence`: compile test сначала отвергает unknown semantic skill/check; docs test отвергает неполный field reference.
- `minimal green`: две регистрации и один data-only spec; ни один runtime primitive, transition rule, permission ceiling, side-effect ceiling, callback или lifecycle path не меняется.
- `refactor seam`: если existing generic check/skill carrier честно покрывает результат, убрать регистрацию; не ослаблять compiler ради примера.
- `focused verification`: custom pipeline parser/compiler/freeze tests, schema tests, `make test-docs`, exact diff proving no other harness behavior changed.
- `Outcome evidence`: E3 и no-regression часть E7.

### Slice 8 — wiki, research, operations и troubleshooting

- `files/responsibility`: создать `docs/ru/wiki-memory.md`, `documents-and-research.md`, `operations.md`, `troubleshooting.md`.
- `consumes`: writer/retrieval/DragonScale, ingest/normalization, protected research, routing/MCP/telemetry, failure-repair and upgrade-preflight contracts.
- `produces`: effect-aware guides с read-only diagnosis first, exact authority boundary и safe recovery.
- `failing evidence`: page contract обнаруживает отсутствующий permission/security/recovery раздел; command matrix находит неавторитетную ручную state edit.
- `minimal green`: описывать только supported actions и честно маркировать platform/optional limitations.
- `refactor seam`: ссылки на canonical contract вместо копирования внутренних state-machine details.
- `focused verification`: vault tests, document normalization/research isolation, routing/gateway/runtime hook tests, docs gate.
- `Outcome evidence`: оставшаяся E2 и operational часть E4.

### Slice 9 — maintainer guide, testing и cookbook

- `files/responsibility`: создать `docs/ru/development.md`, `testing.md`, `extending.md`, `upgrading-and-releasing.md`, `cookbook.md`, `docs/ru/reference/{commands,configuration,glossary}.md`.
- `consumes`: AGENTS/CLAUDE contracts, Makefile targets, coverage/code-quality config, release acceptance, adapters, existing release notes и все предыдущие pages.
- `produces`: maintainer journey и минимум 12 runnable use cases, включая новый skill и documentation pipeline.
- `failing evidence`: docs coverage matrix показывает непокрытые maintainer jobs, Make target или skill combination.
- `minimal green`: на каждую job есть один canonical route с cheap-first tests и bounded expensive/live checks.
- `refactor seam`: reference rows отделены от tutorials; commands не размножаются между страницами.
- `focused verification`: `make test-docs test-code-quality test-harness-coverage`, documented command smoke subset, link and inventory checks.
- `Outcome evidence`: E4 и практическая полнота E2/E3.

### Slice 10 — документационный dogfood и многоракурсное review

- `files/responsibility`: обновить `docs/acceptance/v2.6.3-documentation-matrix.md`; создать `docs/acceptance/v2.6.3-documentation-dogfood.md`.
- `consumes`: frozen handbook, skill, pipeline, fresh disposable checkout и exact Outcome Contract.
- `produces`: три независимых walkthrough: новичок install→first result; пользователь plan→pipeline→review→reap; maintainer skill/config change→tests→release dry run.
- `failing evidence`: любой шаг требует недокументированного знания, неверной команды или прямого чтения implementation class.
- `minimal green`: исправлять только страницу/source pointer, породившую failure; runtime менять нельзя.
- `refactor seam`: удалить дубли, обнаруженные dogfood, сохранив один authoritative path.
- `focused verification`: intent review проверяет цель/аудиторию/scope/coverage; implementation review проверяет техническую точность, security, maintainability и runnable evidence. При недоступности одной модели используется явно выбранный зарегистрированный single-model fallback, pipeline не ломается.
- `Outcome evidence`: E1–E6 перед release gate.

### Slice 11 — release 2.6.3

- `files/responsibility`: обновить version metadata, `CHANGELOG.md`, `CHANGELOG.ru.md`; создать `docs/releases/v2.6.3.md` и `docs/acceptance/v2.6.3-release-readiness.md`.
- `consumes`: exact reviewed HEAD, все evidence artifacts и чистый worktree.
- `produces`: release candidate с install/upgrade/rollback и traceability E1–E7; после freeze создаётся content-free exact-HEAD receipt `.vault-meta/release-evidence/v2.6.3-<short-head>.json` с полным 40-character HEAD, командами и exit codes, а tracked readiness ledger ссылается на него и не объявляет собственный commit authority.
- `failing evidence`: release-acceptance сначала требует 2.6.3 metadata/docs/evidence и запрещает непроверенную skill/pipeline drift.
- `minimal green`: только release-owned metadata и документы; никаких дополнительных feature fixes.
- `refactor seam`: release notes ссылаются на handbook и evidence, не дублируют их.
- `focused verification`: `make test`, `make acceptance-check`, `python3 scripts/validate-vault.py --summary`, `python3 scripts/codex-adapter.py --check`, `scripts/mcp-gateway/mcp-gateway.sh codex-sync --check`, `git diff --check`, final release review.
- `Outcome evidence`: E7 и финальная привязка всех evidence IDs.

## 7. Порядок и допустимый параллелизм

1. Slices 0–4 последовательны: denominator → RED → skill → forward-test → docs gate.
2. После зелёного contract Slice 5, Slice 6 и Slice 8 могут выполняться параллельно только в разных worktrees и без пересечения файлов.
3. Slice 7 зависит от skill и `make test-docs`; Slice 9 зависит от пользовательских страниц и pipeline guide.
4. Slice 10 выполняется только на собранном handbook; Slice 11 — только после закрытия findings.
5. Каждый параллельный task проходит свой полный sequential pipeline; параллелизм не встраивается внутрь PipelineSpec v1.

## 8. Release gates и stop conditions

- Нельзя считать документацию полной по числу страниц или слов; только coverage matrix и успешные walkthrough закрывают outcome.
- Нельзя добавлять команду, которая не подтверждена `--help`, тестом или authoritative source.
- Нельзя описывать ручное редактирование `.vault-meta` или обход harness ownership как recovery.
- Нельзя ослаблять permission/verification ceiling, чтобы documentation pipeline скомпилировался.
- Если новый skill не улучшает baseline pressure cases, он удаляется, а handbook исполняется через существующие skills.
- Если documentation pipeline требует нового primitive/controller, он исключается из 2.6.3; handbook всё равно документирует безопасный custom DSL, а gap фиксируется отдельно.
- Любой material finding intent/implementation/release review получает typed disposition; локальный green не заменяет terminal approval.
- Реальный live/provider smoke остаётся одним bounded проходом после unit/static/contract gates, а не способом искать дешёвые ошибки.

## 9. Финальная traceability

| Evidence | Основные carriers | Финальная проверка |
|---|---|---|
| E1 | getting-started, first-project, dogfood | clean-checkout walkthrough |
| E2 | index, skills, planning, sessions, review, wiki, cookbook | skill/job coverage matrix |
| E3 | pipelines, pipeline-dsl, example, documentation-pipeline | parse/compile/freeze + walkthrough |
| E4 | development, testing, extending, operations, release | maintainer walkthrough + full gate |
| E5 | document-project skill, audit, eval corpus | baseline vs GREEN/REFACTOR forward-test |
| E6 | `make test-docs`, documentation matrix | deterministic docs gate |
| E7 | release readiness and reviews | full tests, acceptance, vault/adapter checks, exact diff audit |

План завершён только когда каждая строка этой таблицы имеет exact artifact, command result и review disposition на одном release HEAD.

Результат: [[LLM Obsidian 2.6.3 Russian technical documentation]] (reaped 2026-08-04)
