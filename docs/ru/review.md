# Review: intent, engineering и release evidence

Review — harness-owned product-read-only workflow. Reviewer получает frozen
context, пишет только typed outbox/callback и не может исправлять product files.

## Presets

| Preset | Топология | Когда выбирать |
|---|---|---|
| Simple | Одна выбранная holistic lane | Малый локальный риск |
| Deep по умолчанию | Независимые Anthropic и OpenAI holistic lanes | Обычный outcome + engineering review |
| Deep single-model | Intent и engineering lanes одной явно выбранной модели | Один provider недоступен или так запросил пользователь |
| Full | Четыре provider × responsibility lanes | Только явный запрос для высокого denominator |

Risk policy не включает Full автоматически. Model alias разрешается через
`config/model-routing.toml`; hardcoded model names в skills и runners запрещены.

Advisory Light Review сохраняет каждый `coverage_gaps` из исходного snapshot в
начале child result и добавляет собственные gaps после них. Любой origin или
observed gap требует `incomplete`; `no-findings-observed` допустим только при
полном покрытии snapshot.
Exact target для Light и Lifecycle Review обязан существовать; отсутствующий
путь и broken symlink отклоняются до Git discovery, ownership и provider effect,
а не подменяются существующим родительским репозиторием.

## Review плана

План запускается только через code-owned facade:

```bash
python3 scripts/task-review-runner.py plan \
  --worktree "$(pwd)" \
  --plan wiki/plans/<exact-plan>.md
```

Facade всегда выбирает `purpose=intent`, до provider start проверяет единственный
Outcome Contract и независимые design/dispositions/evidence-map artifacts. Для
обычного single-parent commit, который меняет exact plan path, base выводится как
`HEAD^`; иначе нужен полный lowercase `--base <OID>`. ContextPacket и prompt
содержат exact base/head и literal команды `review-inspect.py`.

Design-only исправление с typed resolution и exact Git delta продолжает retained
lanes. Изменение Outcome, disposition или evidence map требует amendment и fresh
boundary.

Первый approval-capable review текущего checkout запускается через `current`
с явным `--plan <approved-plan>`. Outcome Contract плана обязан содержать
хотя бы один success-evidence item с парой `evidence_kind: behavior` и
ограниченным `subject`; generic или unbound evidence отклоняется до ownership,
scratch и provider effect. Harness больше не синтезирует циклическое «scope
корректен и готов» доказательство. Callback повторно использует сохранённый
hash-bound plan без `--plan`, а после `changes-requested` executor может явно
передать amended plan вместе с чистым committed resolution HEAD.
После доказанного zero-effect preflight failure callback также переиспользует
этот plan, но только для того же policy и того же HEAD с совпавшими hash.
Exact bytes plan захватываются до allocation UUID/owner, затем публикуются как
digest-named snapshot во внешнем owner scratch. Full plan hash, Outcome
Contract и reviewer ContextPacket читают этот snapshot, поэтому изменение
исходного plan после публикации owner не оставляет orphan scratch и не меняет
review boundary. Implicit callback-resume также строго валидирует и читает этот
snapshot, даже если исходный plan изменён или удалён; mutable source снова
читается только для явно переданного changed-HEAD amendment.
Concurrent-запуски одной цели сериализуются и получают task ID победителя.
Поздний `--new-lineage`, увидевший уже опубликованный replacement, также
присоединяется к нему, но только если target, policy, plan identity и exhausted
predecessor совпадают.
Создание UUID scratch входит в ту же cleanup-транзакцию, что и публикация
owner. `active.json` является commit point: ошибка после `os.replace`, но до
directory fsync, под admission lock восстанавливает прежний pointer (либо
удаляет первичный) до удаления нового scratch. Если точный rollback доказать
нельзя, scratch сохраняется и возвращается typed recovery error — active
pointer никогда сознательно не оставляется ссылаться на удалённый owner.

## Жизненный цикл finding

1. Harness замораживает точный HEAD, baseline, plan hash и review policy.
2. Reviewer проверяет весь outcome и engineering denominator.
3. Findings содержат severity, evidence, recommendation и verification gap.
4. Executor принимает или мотивированно отклоняет каждый finding в новом commit.
5. Verify возобновляет ту же reviewer lane на новом exact HEAD.
6. Approved archive становится входом reap.

`warning` не означает автоматическое scope expansion. Security, permission,
migration, destructive, public-interface и external-effect решения уходят
владельцу. Успех review закрывает раунд, не task.

## Fallback без скрытой подмены

Если provider недоступен, выберите явно разрешённый single-model preset или
остановитесь. Нельзя молча заменить модель, роль или depth: resolved route и
policy входят в operation identity. Same-session verification сохраняет
контекст reviewer и не запускает новый независимый verdict без причины.

## Проверяемые признаки

- Reviewer не изменил product files.
- Каждый finding привязан к файлу/контракту/команде, а не к предпочтению стиля.
- Evidence IDs Outcome Contract имеют established/missing/contradicted ruling.
- Архив соответствует exact reviewed SHA.

Источники: [`skills/review/SKILL.md`](../../skills/review/SKILL.md),
[`docs/task-sessions.md`](../task-sessions.md),
[`docs/model-routing.md`](../model-routing.md).
