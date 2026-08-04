# Компилируемый pipeline для документации

## Для кого и результат

Для автора custom PipelineSpec, которому нужен воспроизводимый documentation
flow без расширения runtime registry. Результат — строгий JSON spec, который
компилируется на существующих `tdd`/`review` и остаётся неисполняемым без
explicit approval.

## Предварительные условия

- approved documentation plan и exact quality-contract content hash;
- знание [PipelineSpec v1](pipeline-dsl.md);
- зарегистрированные primitives/skills/checks версии 2.6.3;
- право только предложить и compile spec; execution/approval — отдельный этап.

## Пример

Файл
[`examples/pipelines/document-project-v1.json`](../../examples/pipelines/document-project-v1.json)
задаёт четыре шага:

```text
inventory (model_step + tdd)
  → author (model_step + tdd)
  → verify
  → review
  → terminal:completed
```

Два model steps — осмысленная delta к `engineering/change`: сначала создаётся
typed `documentation-inventory/v1`, затем authoring result. Spec использует
существующие checks `diff-check` и `instruction-lint`, deep review, initial
approval, bounded budget и SHA-256 quality-contract pointer. `make test-docs`
запускается release verification отдельно.

Компиляция через public test seam:

```bash
python3 tests/harness/test_custom_pipelines.py
make test-docs
```

## Ожидаемый результат и проверка

Первый тест печатает `published documentation pipeline compiles through the
strict contract` и завершает suite без ошибок. `make test-docs` проверяет 23
страницы, 34 skills, relative links, structured examples, source matrix и
повторно компилирует spec. Никакая operation при этих командах не запускается.

## Ошибки и восстановление

- `unknown semantic skill`: используйте существующий registry или остановитесь;
  не регистрируйте convenience carrier без доказанного gap.
- `unregistered verification check`: оставьте check внешним Make gate или
  запросите отдельное публичное решение; не вставляйте shell string.
- Hash context pointer устарел: read-only вычислите SHA-256 точного authoritative
  файла, обновите spec и повторите compile до approval.
- Spec эквивалентен built-in: удалите custom spec и используйте built-in.
- Compile green не разрешает execution; human approval остаётся обязательным.

## Источники истины

- [`schemas/pipeline-spec-v1.schema.json`](../../schemas/pipeline-spec-v1.schema.json).
- [`scripts/harness/custom_pipeline_contracts.py`](../../scripts/harness/custom_pipeline_contracts.py).
- [`scripts/harness/pipeline_builtins.py`](../../scripts/harness/pipeline_builtins.py).
- [`tests/harness/test_custom_pipelines.py`](../../tests/harness/test_custom_pipelines.py).
- [`docs/acceptance/v2.6.3-documentation-quality-contracts.md`](../acceptance/v2.6.3-documentation-quality-contracts.md).
