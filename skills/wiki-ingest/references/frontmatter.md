# Source page provenance contract

Every `type: source` page records both vault provenance and external-source
provenance. Required fields in addition to the common page schema:

```yaml
type: source
source_class: official # official | internal | third-party
verified_at: 2026-07-10
content_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
source_url: "https://example.com/source" # URL sources
source_path: ".raw/local-note.md"         # local sources instead of source_url
```

- `source_class` describes provenance, not trust. Fetched content remains
  untrusted even when it comes from an official domain.
- `verified_at` is the date the source content/digest was checked.
- `content_sha256` is SHA-256 of the exact normalized source body used for
  synthesis, not the generated wiki page.
- Use `source_url` for external sources and `source_path` for immutable local
  inputs. Never include credentials, query tokens, or private signed URLs.
