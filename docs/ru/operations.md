# Эксплуатация локальной системы

## Для кого и результат

Для оператора долгоживущего vault. Результат — проверенные gateway, retrieval,
session preflight, harness и Stop pipeline без прямых правок derived state и без
неявного external effect.

## Предварительные условия

- root vault, Git/Python и owner-controlled user config;
- MCP secrets вне Git, mode 600;
- понимание, какие optional services установлены;
- status/doctor до repair.

## Пример

Ежедневный read-only осмотр:

```bash
python3 scripts/session-preflight.py
scripts/mcp-gateway/mcp-gateway.sh doctor
scripts/mcp-gateway/mcp-gateway.sh health
python3 scripts/harness-cli.py status
python3 scripts/validate-vault.py --summary
```

Gateway install и `codex-sync --apply` меняют user-owned runtime config, поэтому
выполняются только намеренно. Sparse retrieval работает без gateway, Ollama и
Obsidian GUI. Monitoring events content-free: IDs, relative paths и numeric
counters, без prompts/queries/page bodies.

## Ожидаемый результат и проверка

Preflight один раз сообщает optional degradation и точную repair-команду.
Gateway health отвечает только для настроенных routes. Harness status не
показывает unknown ownership. Vault validation зелёный; `git status --short`
не содержит secrets или случайной `.vault-meta` derived state.

## Ошибки и восстановление

- Gateway unhealthy: `doctor`, проверка owner config/secrets permissions, затем
  supported restart; не печатайте secret values.
- Ollama/bge-m3 отсутствует: оставайтесь на sparse либо установите enhancement
  по явному решению.
- Stop validation blocked: не queue exit; используйте synchronous close path
  только после исправления named error.
- Harness state unknown: `inspect`/`doctor`; не запускайте provider/cmux вручную.
- Lock busy: определите живого владельца, ждите или эскалируйте; lock file не
  удаляется вслепую.

## Источники истины

- [`docs/runtime-capabilities.md`](../runtime-capabilities.md).
- [`docs/mcp-gateway.md`](../mcp-gateway.md).
- [`docs/pipeline-observability.md`](../pipeline-observability.md).
- [`AGENTS.md`](../../AGENTS.md), Write path и telemetry boundary.
