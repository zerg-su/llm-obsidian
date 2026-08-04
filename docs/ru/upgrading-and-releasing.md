# Upgrade, rollback и release candidate

## Для кого и результат

Для maintainer'а, обновляющего установленный vault/toolkit или готовящего release
candidate. Результат — exact-version metadata и проверенный HEAD без push, tag,
publish или release.

## Предварительные условия

- clean/understood worktree и резервная копия user-owned config/secrets;
- прочитаны release notes между текущей и целевой версиями;
- provider routes/dependencies проверены preflight;
- external release effects отдельно авторизованы.

## Пример

До upgrade выполните read-only проверки и сохраните текущий commit:

```bash
git rev-parse HEAD
git status --short
python3 scripts/session-preflight.py
scripts/mcp-gateway/mcp-gateway.sh doctor
```

После получения новых repository bytes регенерируйте adapters через
`python3 scripts/codex-adapter.py --apply`, синхронизируйте MCP config только при
намеренном выборе, начните новый Codex thread и запустите exact verification:

```bash
make test-docs
make test
make acceptance-check
python3 scripts/validate-vault.py --summary
git diff --check
```

Release candidate включает changelogs, manifests версии 2.6.3, release notes,
acceptance/readiness ledgers и command evidence одного HEAD.

## Ожидаемый результат и проверка

Все version carriers согласованы; generated adapter check не показывает drift;
full suite и model-free acceptance зелёные; release readiness перечисляет E1–E7
и non-goals. В этой задаче terminal state — готовая документация и commits,
не опубликованный релиз.

## Ошибки и восстановление

- Preflight сообщает breaking config/schema change: остановитесь до approved
  migration/rollback plan.
- Новый plugin не виден: начните новый thread; не создавайте legacy symlink с
  duplicate discovery.
- Upgrade state неизвестен активной operation: harness переводит её в
  `attention-required`; не resume на догадке.
- Gate красный: release blocked; исправьте exact failure на том же candidate
  HEAD и повторите affected checks.
- Rollback: вернитесь к зафиксированной версии repository/plugin через
  поддерживаемый installation path; user data/secrets не удаляются.

## Источники истины

- [`README.ru.md`](../../README.ru.md), install и release evidence.
- [`docs/runtime-capabilities.md`](../runtime-capabilities.md).
- [`scripts/release-acceptance.py`](../../scripts/release-acceptance.py).
- [`config/model-routing.toml`](../../config/model-routing.toml).
