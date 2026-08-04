# Разработка в task worktree

## Для кого и результат

Для maintainer'а, меняющего scripts, skills, config, schemas или docs. Результат
— ограниченный diff в правильной ветке, regression evidence и сохранённые
runtime/public contracts.

## Предварительные условия

- approved plan и точные writable worktree/branch/base;
- source checkout остаётся read-only reference;
- unrelated dirty work и `.obsidian` user state сохраняются;
- перед Git write всегда проверяются cwd и branch.

## Пример

```bash
pwd
git branch --show-current
git status --short
git diff --stat
```

Для behavior change используйте TDD slice: добавить mutation-sensitive RED,
внести минимальный GREEN, убрать duplication, запустить focused suite и
`git diff --check`. Коммитьте explicit paths. Для defect сначала `debug`
устанавливает reproduction/root cause; correct rejection нового behavior не
является mechanism failure.

## Ожидаемый результат и проверка

Diff ограничен plan ownership, не содержит credentials/derived state и не
меняет runtime/provider/permission semantics без явного evidence. Focused tests
зелёные; full suite выполняется на release candidate HEAD. Commit messages
описывают outcome, а не процесс агента.

## Ошибки и восстановление

- Cwd/branch mismatch перед Git write: немедленно остановитесь и эскалируйте.
- Dirty overlap неизвестного владельца: read-only inspect; не reset/checkout.
- Repo-owned mechanism сломан: contain, read-only diagnosis, typed
  `mechanism-failure`, затем только классифицированный узкий reversible repair
  с regression test.
- Требуется dependency/public API/migration/security/external effect: отдельное
  решение; тесты не дают автоматического разрешения.

## Источники истины

- [`AGENTS.md`](../../AGENTS.md).
- [`CLAUDE.md`](../../CLAUDE.md).
- [`docs/skill-references/failure-repair-contract.md`](../skill-references/failure-repair-contract.md).
- [`skills/tdd/SKILL.md`](../../skills/tdd/SKILL.md).
- [`skills/debug/SKILL.md`](../../skills/debug/SKILL.md).
