# Ментальная модель: один результат, несколько владельцев

LLM Obsidian — одновременно Markdown-vault и toolkit. Vault хранит долговременные
знания; skills задают дисциплину reasoning; scripts владеют повторяемой
механикой; harness связывает task, worktree, provider session, review и reap.

## Сущности в порядке зависимостей

1. **Vault** — Git-репозиторий с `wiki/`, `docs/`, skills и scripts.
2. **Wiki page** — канонический Markdown с frontmatter, provenance, address и
   pathless wikilinks.
3. **Skill** — versioned инструкция о том, когда и как выполнять один тип работы.
4. **Outcome Contract** — неизменная цель, evidence, scope/non-goals и границы.
5. **Plan** — проверенный маршрут к Outcome Contract, а не замена результата.
6. **Task/worktree** — изолированная Git-ветка и единственная разрешённая
   product-write область.
7. **Harness operation** — typed state и identity конкретного workflow.
8. **Pipeline** — скомпилированная последовательность primitives и schemas.
9. **Review** — product-read-only независимая проверка intent и engineering.
10. **Reap** — одна coordinator-owned транзакция, связывающая итог, review и wiki.

## Владение состоянием

| Состояние | Владелец | Что нельзя подменять |
|---|---|---|
| Wiki page/log/hot | `vault-write.py` и Stop pipeline | Прямую правку log/hot |
| Product files | Точный task worktree | Source checkout или соседнюю ветку |
| Model route | `config/model-routing.toml` + task snapshot | Случайный CLI default |
| Operation lifecycle | Harness store/workflow | Заголовок cmux или догадку по времени |
| Review findings | Typed callback/archive | Свободный текст из чужой сессии |
| Terminal completion | Coordinator/reap | Зелёный локальный тест |

## Нормальный поток

`clarify → plan → dispatch → execute → verify → review → fixes/verify → reap`.
Успешный review закрывает раунд, но не task. Local green доказывает только
конкретную проверку. Outcome считается достигнутым, когда evidence map закрыт на
одном exact HEAD, а non-goals не нарушены.

## Отказоустойчивость

Повторный запуск опирается на opaque IDs, hashes и owner-owned store. Неизвестные
ownership, callback, prompt или upgrade state переходят в
`attention-required`. Механизм может автоматически чиниться только в узкой
локальной reversible границе failure-repair contract; permission, security,
migration, public interface и external effects требуют решения владельца.

## Где читать дальше

- Практический lifecycle: [первый проект](first-project.md).
- Identity и resume: [сессии и задачи](sessions-and-tasks.md).
- DSL и budgets: [PipelineSpec](pipeline-dsl.md).
- Термины: [глоссарий](reference/glossary.md).

Источники: [`AGENTS.md`](../../AGENTS.md), [`docs/task-sessions.md`](../task-sessions.md),
[`docs/unattended-pipeline-operations.md`](../unattended-pipeline-operations.md).
