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

Для `2.6.6-rc3` используйте один owner-controlled evidence root вне checkout.
Он не меняет exact HEAD, который проверяет. Сначала создайте и проверьте
inventory sidecar, передав ожидаемый HEAD независимо от содержимого sidecar:

```bash
RC3_EVIDENCE_ROOT=/private/tmp/llm-obsidian-2.6.6-rc3-evidence
mkdir -p "$RC3_EVIDENCE_ROOT"
RC3_HEAD=$(git rev-parse HEAD)
python3 scripts/rc3_inventory.py build --baseline b86a33d779bd8852915a4b875f12ef9a9b7366b3 --candidate "$RC3_HEAD" > "$RC3_EVIDENCE_ROOT/machine-inventory.json"
python3 scripts/rc3_inventory.py check "$RC3_EVIDENCE_ROOT/machine-inventory.json" --expected-baseline b86a33d779bd8852915a4b875f12ef9a9b7366b3 --expected-candidate "$RC3_HEAD"
python3 scripts/rc3_release_disposition.py budget --attempt-ledger-root "$RC3_EVIDENCE_ROOT"
```

Release candidate включает changelogs, version manifests, RC3 release notes,
prospective slice receipts и две coverage observations. Exact-head inventory,
attempt ledger, gate bundle, review callbacks и typed disposition создаются
после commit во внешнем evidence root. Поэтому они не делают проверяемый HEAD
stale. `release-final` запускается только с
`--attempt-ledger-root "$RC3_EVIDENCE_ROOT"`; runner резервирует попытку до
первой команды. После двух release review проверьте concrete disposition:

```bash
python3 scripts/rc3_release_disposition.py check "$RC3_EVIDENCE_ROOT/release-disposition.json" --gate-receipt "$RC3_EVIDENCE_ROOT/release/receipt.json" --attempt-ledger-root "$RC3_EVIDENCE_ROOT" --review-manifest "$RC3_EVIDENCE_ROOT/reviews.json" --finding-evidence "$RC3_EVIDENCE_ROOT/findings.json"
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
