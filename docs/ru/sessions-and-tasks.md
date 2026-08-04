# Сессии, task worktree и resume

## Для кого и результат

Для координатора долгой задачи. Результат — понимание, какая session/worktree/
operation принадлежит task, как безопасно inspect/resume/reconcile и почему
cmux surface не является источником истины.

## Предварительные условия

- approved plan и task contract;
- cmux для отдельной видимой lane;
- запуск lifecycle только через `scripts/harness-cli.py` или соответствующий
  skill, не ручная оркестрация provider command.

## Пример

После dispatch используйте read-only lifecycle commands:

```bash
python3 scripts/harness-cli.py status
python3 scripts/harness-cli.py inspect
python3 scripts/harness-cli.py doctor
```

Если executor завершился, `resume` продолжает exact owned operation. Если
callback или terminal cleanup уже принят, `reconcile` сверяет typed identity и
не повторяет внешний эффект. `cancel` и `close` — разные terminal операции:
cancel останавливает operation, close завершает разрешённую lifecycle surface.

## Ожидаемый результат и проверка

Inspect показывает стабильные project/task/session/operation IDs, route,
worktree и state. Текущая ветка и cwd совпадают с task contract. Возобновление
не создаёт второй worktree и не перепривязывает архивную session к новому task.

## Ошибки и восстановление

- Unknown ownership/prompt/callback/upgrade state: переход в
  `attention-required`; не угадывайте по имени окна.
- Surface исчезла, operation жива: `doctor`, затем supported `resume`.
- Worktree dirty: определите владельца каждого diff; не reset и не удаляйте
  пользовательское состояние.
- Accepted callback отображался cancelled: в 2.6.3 exact recovery допускается
  только при совпадении callback digest, run, request и artifact identity.

## Источники истины

- [`docs/task-sessions.md`](../task-sessions.md).
- [`docs/unattended-pipeline-operations.md`](../unattended-pipeline-operations.md).
- [`scripts/harness-cli.py`](../../scripts/harness-cli.py).
- [`docs/skill-references/failure-repair-contract.md`](../skill-references/failure-repair-contract.md).
