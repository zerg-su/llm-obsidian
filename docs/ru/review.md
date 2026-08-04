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
