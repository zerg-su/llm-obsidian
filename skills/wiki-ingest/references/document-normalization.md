# Local document normalization contract

Use `scripts/document-normalize.py` before reading local documents for
`wiki-ingest`. The normalizer accepts local files only and never changes the
original.

## Routing

| Input | Processor | Docling required |
|---|---|---:|
| Markdown, text, JSON, YAML, CSV | Python stdlib | No |
| Local HTML | Networkless stdlib cleanup | No |
| PDF | Docling text-first pipeline; selective page OCR | Yes |
| DOCX, PPTX, XLSX, ODT/ODS/ODP, EPUB | Docling standard pipeline | Yes |
| Scanned document image | Docling + EasyOCR | Yes |
| Whiteboard/general image | Native vision flow in `SKILL.md` | No |

The Docling profile is pinned to version `2.112.0`, uses accurate table mode,
and explicitly selects EasyOCR languages `ru,en`. A local Python API adapter
runs with `enable_remote_services=False`, `allow_external_plugins=False`,
offline Hugging Face/Transformers variables, a fixed `artifacts_path`, and
EasyOCR downloads disabled. Models are prefetched during machine setup.

## Commands

```bash
# Verify runtime and ru/en model bundle
python3 scripts/document-normalize.py check --json

# Normalize one source
python3 scripts/document-normalize.py normalize '/absolute/or/relative/file.pdf' --json

# Rebuild a derived cache entry
python3 scripts/document-normalize.py normalize 'file.pdf' --force --json

# Install or repair the isolated runtime
python3 scripts/install-docling.py install
```

`bin/setup-clean-machine.sh` installs Docling by default. Use
`--skip-docling` only for an intentionally lightweight setup.

## Result handling

- `ok`: use `artifacts.markdown`.
- `cached`: same content/profile hash was already converted; use the returned
  artifact without another model run.
- `needs_semantic_cleanup` (exit 5): deterministic cleanup succeeded, but the
  typed repair bundle contains a small number of ambiguous structural defects.
  Correct only those fragments while drafting in the current ingest turn. This
  is not a second model invocation and it never rewrites the cache.
- `low_quality`: the output is too short or its reported confidence is low;
  inspect and ask before ingesting.
- `needs_user_action` (exit 6 for repair limits): the semantic repair exceeds
  20 segments, 2,000 characters per segment, 20,000 characters total, or 15%
  of the document. Inspect and ask. Missing Docling/runtime models use the same
  typed status with exit 2 and embedded repair commands. An unattended task
  must escalate to its coordinator.
- `unsupported`: local file type or configured 250 MiB / 2000-page limit was
  exceeded. The source is not truncated.
- `conversion_failed`: converter error or 20-minute timeout. Preserve the
  source and report the reason.

PDFs first extract their native text layer with OCR disabled. Pages under 40
non-space native characters are OCR candidates only when detected picture
union covers at least 50% of the page or a rendered non-blank scan is detected.
Only contiguous candidate ranges are converted with OCR. Blank pages stay
blank. The quality gate evaluates the final assembled Markdown, never the
intermediate no-OCR result.

Profile v4 stores `document.raw.md`, deterministically cleaned `document.md`,
`document.docling.json`, page/OCR metadata, and an optional
`repair-bundle.json`. The cache key combines source SHA-256, processor version,
and profile SHA-256.

Exit-code contract: `0` ok/cached, `2` runtime unavailable, `3` low quality,
`4` unsupported/conversion failure, `5` bounded semantic cleanup, `6` user
action required for repair scope.

Repair the bundle by issue type, never by free-form rewriting:

- `probable_heading`: change Markdown structure only;
- `inline_numbered_list`: change whitespace and list markers only;
- `image_ocr_contamination`: delete only text explicitly marked with OCR
  provenance;
- `suspicious_mixed_script` / `replacement_character`: make the smallest
  spelling-level correction supported by adjacent context.

Preserve numeric tokens, URLs, image embeds, and claims. A cache hit may still
return `needs_semantic_cleanup`; use its existing bundle in the same turn.
Artifacts live under `.vault-meta/document-cache/` and are derived state. The
manifest records source path/hash, processor/profile, page/text metrics, and
artifact paths. Use the original source hash—not the Markdown derivative
hash—for `.raw/.manifest.json` provenance.

## Degraded fallback

Never silently send a binary source directly to a model. If the user explicitly
approves native binary reading for one failed source, label the result as a
degraded fallback in the source page and retain the converter failure in
provenance. This permission does not carry to other documents or sessions.
