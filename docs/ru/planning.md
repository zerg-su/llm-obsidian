# Планирование по Outcome Contract

## Для кого и результат

Для автора изменения до dispatch. Результат — утверждённый план с ownership,
зависимостями, RED/GREEN evidence и stop conditions, который сохраняет исходную
цель и не выдаёт локальный шаг за completion.

## Предварительные условия

- сформулированы observable outcome, evidence items, scope и non-goals;
- изучены authoritative files и существующие тестовые seams;
- неразрешённые public-interface/security/migration решения вынесены к владельцу.

## Пример

Слабая формулировка: «обновить docs и тесты». Проверяемая формулировка:

```text
Slice: добавить справочник команды.
Ownership: docs/ru/reference/commands.md, tests/test_russian_documentation.py.
Consumes: CLI --help и существующий command contract.
Produces: одна reference row и deterministic coverage assertion.
RED: тест на отсутствие команды падает.
GREEN: строка и invocation присутствуют, links валидны.
Refactor: общий manifest, без нового runtime registry.
Evidence: exact test command и diff-check.
Stop: любое изменение CLI behavior требует отдельного решения.
```

Для multi-file работы вызовите `implementation-plan`; для сохранения approved
version — `save-plan`. Review плана проверяет intent до product mutation.

## Ожидаемый результат и проверка

Хороший план позволяет назначить каждый файл одному slice, объясняет порядок
через consumes/produces и называет failing evidence до GREEN. Проверьте, что
каждый Outcome evidence ID имеет хотя бы один producing slice, а каждый non-goal
имеет защитную проверку или diff boundary.

## Ошибки и восстановление

- План перечисляет действия, но не результат: вернитесь к Outcome Contract.
- Несколько slices владеют одним файлом: объедините ownership или задайте
  последовательный handoff.
- Тест уже зелёный до изменения: он не доказывает RED; найдите
  mutation-sensitive assertion или запишите, что новое поведение не нужно.
- План требует network/publish/migration без разрешения: остановитесь на
  escalation boundary.

## Источники истины

- [`skills/implementation-plan/SKILL.md`](../../skills/implementation-plan/SKILL.md).
- [`skills/save-plan/SKILL.md`](../../skills/save-plan/SKILL.md).
- [`skills/review/SKILL.md`](../../skills/review/SKILL.md).
- [`AGENTS.md`](../../AGENTS.md), Failure-to-repair и Core rules.
