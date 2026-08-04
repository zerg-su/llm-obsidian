# Документы, ingest и защищённый research

## Для кого и результат

Для аналитика, которому нужны локальный документ или текущий web-source в wiki.
Результат — нормализованный Markdown с provenance либо cited synthesis из
validated protected artifact, без смешивания vault-aware и network contexts.

## Предварительные условия

- Local ingest принимает Markdown, text, HTML, PDF, Office, EPUB и scans;
- Python core обязателен; Docling/EasyOCR optional для сложных форматов/OCR;
- web research запускается через `research`, прямой browser из vault-aware
  context запрещён;
- `unsafe-research` используется только по явному принятию риска.

## Пример

Для локального PDF попросите:

```text
ingest ~/Downloads/architecture.pdf в wiki, сохрани provenance и проверь дубликаты
```

`wiki-ingest` вычисляет source hash, нормализует документ, использует cache для
неизменных bytes, отделяет `.raw/.manifest.json` и выполняет одну vault
transaction. Для текущего web-вопроса вызовите `research`: networked fetcher не
видит vault, networkless synthesizer получает только validated artifact и
явно выбранный контекст, а coordinator выполняет единственную запись.

## Ожидаемый результат и проверка

Local page содержит источник, hash/manifest, session provenance и корректные
links; повторный ingest неизменного файла не создаёт дубликат. Research answer
цитирует только URLs из fetch artifact. Проверки:

```bash
python3 scripts/validate-vault.py --summary
make test-document-normalize
make test-research-isolation
```

## Ошибки и восстановление

- Missing Docling/OCR: typed action предлагает setup или text-only fallback;
  binary не отправляется модели молча.
- Плохой extraction: сохраните source identity, остановите merge и исправьте
  conversion dependency; не выдавайте summary за raw source.
- Citation отсутствует в fetch artifact: synthesis отвергается полностью как
  evidence.
- Cancelled receipt с принятым callback: 2.6.3 восстанавливает только exact
  digest/run/request/artifact match; обычный cancel остаётся cancelled.
- Нужен single-context web: запросите `unsafe-research` явно, не используйте как
  автоматический fallback.

## Источники истины

- [`docs/document-ingestion.md`](../document-ingestion.md).
- [`skills/wiki-ingest/SKILL.md`](../../skills/wiki-ingest/SKILL.md).
- [`skills/research/SKILL.md`](../../skills/research/SKILL.md).
- [`skills/unsafe-research/SKILL.md`](../../skills/unsafe-research/SKILL.md).
- [`docs/acceptance/v2.6.3-documentation-baseline.md`](../acceptance/v2.6.3-documentation-baseline.md).
