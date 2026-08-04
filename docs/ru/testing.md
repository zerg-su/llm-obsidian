# Тестирование: от дешёвого сигнала до release evidence

## Для кого и результат

Для разработчика и release maintainer. Результат — mutation-sensitive focused
evidence, полный hermetic suite и отдельное понимание того, что требует network
или provider lifecycle.

## Предварительные условия

- запуск из правильного task worktree;
- Python/Git и repository dependencies;
- product behavior и expected failure сформулированы до теста;
- no-network `make test` не подменяет live acceptance.

## Пример

Cheap-first порядок для docs/runtime-adjacent release:

```bash
make test-docs
make test-instruction-lint
make test-skill-budget
make test-codex-adapter
python3 tests/harness/test_custom_pipelines.py
git diff --check v2.6.2..HEAD
make test
make test-harness-coverage
python3 scripts/validate-vault.py --summary
```

Retrieval change дополнительно требует `make bench-retrieval`. Model-free
four-cell contract — `make acceptance-check`; provider-backed cells — отдельный
authorized `make acceptance-live`, который resume только affected fingerprints.

## Ожидаемый результат и проверка

Каждая команда exit 0 и печатает собственный denominator. `make test-docs`
проверяет pages, links, skill inventory, JSON/TOML, required page contracts,
source matrix и PipelineSpec compile. Full suite остаётся hermetic и не требует
Ollama/network. `make test-harness-coverage` нужен перед release candidate и
после изменения harness runtime: он считает AST statement-line denominator,
включая ни разу не выполненные строки, печатает weighted percentage и critical
module floors. Ratchet failure означает уменьшение обязательного покрытия или
непокрытый harness module, а не допустимый skip. Exact command/exit-code receipt
фиксируется рядом с HEAD.

## Ошибки и восстановление

- Новый тест зелёный до реализации: усилите seam или удалите ложный scenario.
- Flaky timing assertion: проверяйте stable state/hash/status, не wall-clock noise.
- Optional dependency missing: отделите applicable skip от обязательного
  release blocker; не устанавливайте dependency без разрешения.
- Full suite падает вне diff: воспроизведите и установите ownership; не меняйте
  unrelated behavior ради зелёного цвета.
- Live acceptance требует provider/external effect: используйте только уже
  авторизованный harness path.

## Источники истины

- [`Makefile`](../../Makefile).
- [`tests/test_russian_documentation.py`](../../tests/test_russian_documentation.py).
- [`scripts/release-acceptance.py`](../../scripts/release-acceptance.py).
- [`AGENTS.md`](../../AGENTS.md), validation и retrieval metrics.
