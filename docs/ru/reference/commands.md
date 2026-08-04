# Справочник команд

Команды запускаются из root repository, если не указано иное. Сначала
используйте `--help`; handbook не расширяет permissions.

## Vault и retrieval

| Команда | Эффект / результат |
|---|---|
| `python3 scripts/validate-vault.py --summary` | Read-only contract validation |
| `python3 scripts/retrieve.py "QUERY" --top 5 --json` | Read-only section retrieval |
| `scripts/current-session-id.sh` | Печатает Claude/Codex session ID или `unknown` |
| `scripts/allocate-address.sh` | Выделяет DragonScale address через owner contract |
| `python3 scripts/vault-write.py --help` | Описывает transactional writer; не запускает write |

## Setup и adapters

| Команда | Эффект / результат |
|---|---|
| `bash bin/setup-clean-machine.sh` | Mutating bootstrap local machine/vault |
| `python3 scripts/codex-adapter.py --apply` | Генерирует Codex plugin metadata |
| `scripts/mcp-gateway/mcp-gateway.sh doctor` | Read-only diagnosis |
| `scripts/mcp-gateway/mcp-gateway.sh health` | Проверяет configured HTTP routes |
| `scripts/mcp-gateway/mcp-gateway.sh sync-config --apply` | Синхронизирует gateway/client JSON; только default path может создать missing `runtime.env` из validated sibling example |
| `scripts/mcp-gateway/mcp-gateway.sh install` | Устанавливает/обновляет local gateway service |
| `scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply` | Меняет Codex MCP config |

## Harness lifecycle

| Команда | Назначение |
|---|---|
| `python3 scripts/harness-cli.py status` | Compact lifecycle status |
| `python3 scripts/harness-cli.py inspect` | Exact ownership/state details |
| `python3 scripts/harness-cli.py resume` | Resume owned operation |
| `python3 scripts/harness-cli.py reconcile` | Reconcile typed callback/terminal state |
| `python3 scripts/harness-cli.py cancel` | Cancel exact operation |
| `python3 scripts/harness-cli.py close` | Close exact allowed lifecycle |
| `python3 scripts/harness-cli.py doctor` | Read-only harness diagnosis |

Команды требуют собственные identifiers/arguments; используйте `--help` и
skills `dispatch`/`review`/`reap` вместо ручной provider/cmux orchestration.

## Tests и acceptance

| Команда | Denominator |
|---|---|
| `make test-docs` | Russian handbook + PipelineSpec |
| `make test` | Полный hermetic suite |
| `make bench-retrieval` | RU/EN retrieval metrics |
| `make acceptance-check` | Model-free acceptance contract |
| `make acceptance-live` | Authorized provider-backed affected cells |
| `make test-instruction-lint` | Skill/instruction safety contracts |
| `make test-skill-budget` | Registry/body budget |
| `make test-codex-adapter` | Generated marketplace parity |
| `git diff --check` | Whitespace/conflict-marker hygiene |

## Git write boundary

Перед `git add` и `git commit` выполните `pwd` и
`git branch --show-current`. Коммитьте explicit files. Push, tag, publish,
deploy и release не следуют автоматически из green tests.

Источники: [`Makefile`](../../../Makefile), [`scripts/`](../../../scripts/),
[`AGENTS.md`](../../../AGENTS.md).
