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
make test-harness-coverage
make acceptance-check
python3 scripts/validate-vault.py --summary
git diff --check v2.6.6-rc2..HEAD
```

Для `2.6.6-rc3` сначала проверьте code-owned inventory и механический budget:

```bash
python3 scripts/rc3_inventory.py build --baseline b86a33d779bd8852915a4b875f12ef9a9b7366b3 --candidate "$(git rev-parse HEAD)"
python3 scripts/rc3_release_disposition.py budget docs/acceptance/v2.6.6-rc3-attempt-ledger.json
```

Release candidate включает changelogs, version manifests, RC3 release notes,
machine inventory, prospective slice receipts, две coverage observations,
attempt ledger и typed final disposition. Exact gate receipt содержит полный
40-character SHA, profile digest, команды, exit codes и output digests. После
получения двух review verdicts проверьте concrete disposition:

```bash
python3 scripts/rc3_release_disposition.py check docs/acceptance/v2.6.6-rc3-release-disposition.json --gate-receipt docs/acceptance/evidence/v2.6.6-rc3/release/receipt.json
```

Новый product commit делает gate и disposition stale и требует нового
механически учтённого candidate attempt.

## Ожидаемый результат и проверка

Все version carriers согласованы; generated adapter check не показывает drift;
full suite и model-free acceptance зелёные; release readiness перечисляет RC3-E1–E9
и non-goals. В этой задаче terminal state — готовая документация и commits,
не опубликованный релиз.

## Ошибки и восстановление

- Preflight сообщает breaking config/schema change: остановитесь до approved
  migration/rollback plan.
- Новый plugin не виден: начните новый thread; не создавайте legacy symlink с
  duplicate discovery.
- Upgrade state неизвестен активной operation: harness переводит её в
  `attention-required`; не resume на догадке.
- Gate красный: release blocked; исправление создаёт новый exact HEAD и новый
  full-profile attempt. Unpublished и test-only попытки также расходуют budget;
  шестая попытка запрещена.
- Rollback: вернитесь к зафиксированной версии repository/plugin через
  поддерживаемый installation path; user data/secrets не удаляются.

## Источники истины

- [`README.ru.md`](../../README.ru.md), install и release evidence.
- [`docs/runtime-capabilities.md`](../runtime-capabilities.md).
- [`scripts/release-acceptance.py`](../../scripts/release-acceptance.py).
- [`config/model-routing.toml`](../../config/model-routing.toml).
