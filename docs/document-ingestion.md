# Local document ingestion

`wiki-ingest` converts external local documents into stable Markdown before an
LLM reads them. This keeps extraction deterministic, preserves provenance, and
avoids spending model context on PDF internals or repeated OCR.

## Data flow

```text
read-only source
  -> extension/size policy
  -> stdlib fast path OR isolated Docling
  -> quality and limit checks
  -> content-addressed derived cache
  -> wiki-ingest synthesis
  -> one vault-write transaction
```

Markdown, plain text, JSON, YAML, CSV, and local HTML do not start Docling.
PDF, DOCX, PPTX, XLSX, ODT/ODS/ODP, EPUB, and scanned document images use the
Docling standard pipeline. General photographs, diagrams, and whiteboards keep
the native vision path because their meaning is not primarily document text.

## Installation and repair

The normal clean-machine bootstrap installs the document runtime:

```bash
bash bin/setup-clean-machine.sh
```

It uses `uv` and an isolated Python 3.12 environment under
`~/.local/share/llm-obsidian/docling/2.112.0/`. On macOS, a missing `uv` is
installed through Homebrew. The bootstrap then installs
`docling[easyocr]==2.112.0` and downloads only the layout, TableFormer, and
EasyOCR model bundles needed by the default profile. Model weights are user
cache/runtime data and are not committed to the repository. On the current
Apple Silicon installation the complete isolated environment occupies roughly
1.8 GiB; `--skip-docling` avoids that cost on machines that ingest text only.

```bash
# Intentionally omit the heavy dependency
bash bin/setup-clean-machine.sh --skip-docling

# Install or repair only Docling
python3 scripts/install-docling.py install

# Read-only diagnostics
python3 scripts/install-docling.py check
python3 scripts/document-normalize.py check --json
```

## Normalization interface

```bash
python3 scripts/document-normalize.py normalize '/path/to/file.pdf' --json
```

Successful JSON points to `artifacts.markdown`, the optional lossless
`document.docling.json`, and its cache root. It also records the immutable
source path, size, SHA-256, processor/profile versions, page count, extracted
character count, and available confidence samples.

The default safety limits are 250 MiB, 2000 pages, and 20 minutes per document.
Limits reject the source without silently truncating it. CLI flags can raise
them for a deliberate one-off conversion.

Derived artifacts live under `.vault-meta/document-cache/` and are gitignored.
The key combines the source SHA-256, converter version, and processing profile,
so unchanged files are not converted twice and a profile upgrade cannot reuse
stale output.

## Russian and English OCR

Digital text layers do not depend on OCR language. For scanned regions the
profile explicitly selects EasyOCR and passes `ru,en`; it does not rely on
Docling's platform-dependent `auto` selection. Setup additionally materializes
EasyOCR's Cyrillic recognition weight (`cyrillic_g2.pth`), which the generic
Docling model downloader does not fetch by itself. This follows Docling's documented
engine-specific language contract:

- <https://docling-project.github.io/docling/reference/pipeline_options/>
- <https://docling-project.github.io/docling/reference/cli/>

The normalizer uses accurate table mode and referenced image artifacts. It does
not force OCR over a usable digital text layer, avoiding needless recognition
errors in ordinary PDFs.

## Trust boundary and fallback

The source is local and read-only. The Docling command is invoked with remote
services and external plugins disabled, remote HTML image fetching disabled,
and Hugging Face/Transformers offline flags enabled. Required models are
prefetched during setup; ingestion never enables an on-demand network fallback.

If Docling or its model manifest is absent, binary normalization returns
`needs_user_action` with exact install/check commands. Text formats continue to
work. An unattended task escalates that typed result to its coordinator instead
of waiting in a hidden terminal. Direct model-native reading of a binary file is
allowed only after the user explicitly approves that degraded fallback for the
specific source.

`low_quality` keeps its derived artifact for inspection but blocks automatic
vault writes. `unsupported` and `conversion_failed` preserve the source and
report the reason. No result silently bypasses the normal transactional
`vault-write.py` page/manifest/log/hot path.

## Validation

```bash
make test-document-normalize  # hermetic; fake converter, no network/models
make test-documents           # real pinned Docling and generated ru/en fixtures
make test                     # full repository regression suite
```
