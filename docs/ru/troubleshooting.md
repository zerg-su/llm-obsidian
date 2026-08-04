# Устранение неполадок без потери ownership

## Для кого и результат

Для пользователя или оператора при ошибке vault, skill, task, research,
gateway или release gate. Результат — read-only diagnosis, точный владелец
состояния и поддерживаемое восстановление либо `attention-required`.

## Предварительные условия

- не повторять destructive/external command;
- сохранить cwd, branch, operation ID и исходный stderr;
- отличать product failure от repo-owned mechanism failure;
- неизвестное состояние не «лечить» прямой правкой derived files.

## Пример

Начните с минимального снимка:

```bash
pwd
git branch --show-current
git status --short
python3 scripts/harness-cli.py status
python3 scripts/harness-cli.py inspect <operation-id>
python3 scripts/validate-vault.py --summary
```

| Симптом | Read-only diagnosis | Поддерживаемое действие |
|---|---|---|
| Skill не найден в Codex | plugin list, новый ли thread | Adapter sync/install, затем новый thread |
| Sparse работает, dense нет | session preflight, Ollama model list | Продолжить sparse или явно установить optional enhancement |
| Vault hash conflict | перечитать page и current hash | Новая transaction с optimistic hash |
| Stop блокирует exit | validation output | Исправить page; synchronous close только после green |
| Task surface исчезла | harness status/doctor | Resume exact operation |
| Callback/owner неизвестен | inspect/reconcile | `attention-required`, без догадки |
| Gateway unhealthy | gateway doctor/health | Исправить owner config/secret permissions, restart |
| Research citation чужая | compare fetch artifact URLs | Reject synthesis; не частично merge |
| Release gate красный | exact failing command | Исправить или записать blocker; не publish |

## Ожидаемый результат и проверка

После recovery повторите ту же независимую проверку, которая обнаружила
симптом. Git diff не содержит broad cleanup, credentials или чужой dirty work.
Operation identity и terminal state согласованы; повторный external effect не
произошёл.

## Ошибки и восстановление

- Один и тот же repo-owned механизм воспроизводимо нарушает documented
  behavior: contain, read-only root cause, raise `mechanism-failure`, затем
  narrow reversible repair только после classification и с regression test.
- Missing permission/dependency/user input — не mechanism failure; запросите
  владельца один раз.
- Correct schema/conflict rejection — полезная защита, а не повод ослаблять gate.
- Security, public interface, migration, destructive и external-effect границы
  всегда требуют отдельного решения.

## Источники истины

- [`docs/skill-references/failure-repair-contract.md`](../skill-references/failure-repair-contract.md).
- [`AGENTS.md`](../../AGENTS.md), Failure-to-repair.
- [`docs/unattended-pipeline-operations.md`](../unattended-pipeline-operations.md).
- [`docs/mcp-gateway.md`](../mcp-gateway.md).
