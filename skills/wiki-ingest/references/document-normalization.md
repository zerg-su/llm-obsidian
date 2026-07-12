# Local document normalization contract

Use `scripts/document-normalize.py` before reading local documents for
`wiki-ingest`. The normalizer accepts local files only and never changes the
original.

## Routing

| Input | Processor | Docling required |
|---|---|---:|
| Markdown, text, JSON, YAML, CSV | Python stdlib | No |
| Local HTML | Networkless stdlib cleanup | No |
| PDF, DOCX, PPTX, XLSX, ODT/ODS/ODP, EPUB | Docling standard pipeline | Yes |
| Scanned document image | Docling + EasyOCR | Yes |
| Whiteboard/general image | Native vision flow in `SKILL.md` | No |

The Docling profile is pinned to version `2.112.0`, uses accurate table mode,
and explicitly selects EasyOCR languages `ru,en`. It disables remote services,
external plugins, VLM presets, HTML remote-image fetching, and runtime model
downloads. The CLI contract includes `--no-enable-remote-services` and
`--no-allow-external-plugins`. Models are prefetched during machine setup.

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
- `low_quality`: the output is too short or its reported confidence is low;
  inspect and ask before ingesting.
- `needs_user_action`: Docling/runtime models are missing. Surface the embedded
  repair commands. An unattended task must escalate to its coordinator.
- `unsupported`: local file type or configured 250 MiB / 2000-page limit was
  exceeded. The source is not truncated.
- `conversion_failed`: converter error or 20-minute timeout. Preserve the
  source and report the reason.

The cache key combines source SHA-256, processor version, and profile SHA-256.
Artifacts live under `.vault-meta/document-cache/` and are derived state. The
manifest records source path/hash, processor/profile, page/text metrics, and
artifact paths. Use the original source hash—not the Markdown derivative
hash—for `.raw/.manifest.json` provenance.

## Degraded fallback

Never silently send a binary source directly to a model. If the user explicitly
approves native binary reading for one failed source, label the result as a
degraded fallback in the source page and retain the converter failure in
provenance. This permission does not carry to other documents or sessions.
