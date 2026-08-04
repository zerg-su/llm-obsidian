# Первый проект: от идеи до проверенного результата

## Для кого и результат

Для пользователя, уже прошедшего установку. Результат — ограниченное изменение
в отдельном worktree, независимый review и typed итог, готовый к coordinator-owned
reap. Пример не выполняет push или release.

## Предварительные условия

- чистый project checkout и работающий `git status`;
- cmux для видимого multi-session dispatch;
- установленный Claude/Codex plugin;
- задача, которую можно сформулировать через outcome, evidence и non-goals.

## Пример

В coordinator-сессии сформулируйте:

```text
Сделай clarify для изменения справки CLI. Результат: новый пример виден в help,
проверяется существующим test target. Не менять runtime permissions и не публиковать.
```

После уточнения попросите `implementation-plan` с file ownership,
consumes/produces и cheap-first checks, сохраните утверждённый план через
`save-plan`, затем dispatch через встроенный `engineering/change`. Executor
работает в созданном task worktree; координатор не копирует product changes в
source checkout. После тестов запросите Deep review. Findings исправляются в
том же task и проверяются в той же review lane. Reap выполняется только после
typed summary и approved review.

## Ожидаемый результат и проверка

Проверьте lifecycle через harness, не по визуальному заголовку окна:

```bash
python3 scripts/harness-cli.py status
python3 scripts/harness-cli.py inspect
git status --short
```

Ожидаются стабильные task/operation identities, точный worktree и review state.
Diff содержит только утверждённые файлы; evidence указывает точные команды и
результаты. Reap-ready не означает, что push или release разрешены.

## Ошибки и восстановление

- План ещё не утверждён: не dispatch; вернитесь к clarify/plan review.
- cmux недоступен: видимый dispatch заблокирован; обычную read-only работу можно
  продолжить вручную, но нельзя имитировать harness lifecycle.
- Неизвестная ownership/callback state: `inspect`, затем `reconcile`; при
  `attention-required` остановитесь и следуйте typed reason.
- Review нашёл scope/security boundary: не чините за пределами плана; поднимите
  escalation.
- Executor завершился: используйте `resume` exact operation, не создавайте
  новый task с тем же смыслом.

## Источники истины

- [`docs/task-sessions.md`](../task-sessions.md).
- [`docs/unattended-pipeline-operations.md`](../unattended-pipeline-operations.md).
- [`skills/dispatch/SKILL.md`](../../skills/dispatch/SKILL.md).
- [`skills/review/SKILL.md`](../../skills/review/SKILL.md).
- [`skills/reap/SKILL.md`](../../skills/reap/SKILL.md).
