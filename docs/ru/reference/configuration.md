# Справочник конфигурации

## Авторитетные carriers

| Surface | Source | Владелец и правило |
|---|---|---|
| Model/provider aliases и review defaults | `config/model-routing.toml` | Code-owned resolver; не hardcode в skill |
| Claude plugin marketplace | `.claude-plugin/` | Generated/release metadata |
| Codex plugin marketplace | `.codex-plugin/`, `.agents/plugins/marketplace.json` | `codex-adapter.py --apply` |
| MCP routes | `scripts/mcp-gateway/config.json` | Local config из committed example |
| MCP runtime port | `scripts/mcp-gateway/runtime.env` | Default `sync-config --apply` может атомарно создать из strict sibling example; custom/check paths не пишут |
| MCP credentials | `~/.config/mcp-gateway/secrets.env` | User-owned, mode 600, вне Git |
| Optional MCP profiles | `.mcp-profiles/` | Opt-in schema/context load |
| Memory backup | `config/memory-backup.example.json` | Disabled до explicit env/config source |
| Destructive command guard | `config/dcg/config.toml` | Optional defense in depth |
| Skill routing hints | `.claude/skill-rules.json` | Soft invocation hints, не permissions |
| Skill body budget | `config/skill-body-baseline.json` | Deterministic closure ratchet |
| PipelineSpec | `schemas/pipeline-spec-v1.schema.json` | Strict data-only public DSL |

## Model routing

Resolver связывает runtime, alias, concrete model, reasoning effort, service
tier и config fingerprint. Task/session snapshot сохраняет resolved route;
drift требует явного update/restart path. Unsupported literal model names в
skills/scripts обнаруживает lint.

## Optional components и fallback

- Нет Ollama/bge-m3: sparse retrieval остаётся поддерживаемым.
- Нет Docling: text formats работают; binary/OCR возвращает typed repair/fallback.
- Нет cmux: vault/retrieval/manual skills работают, visible task orchestration — нет.
- Нет одного review provider: выбирается явный single-model preset, а не скрытая
  подмена.
- MCP profile не включён: server schema не загружается в context.

## Изменение конфигурации

Сначала найдите example/schema и consumer tests. Не коммитьте secrets. Для
generated metadata меняйте canonical source и перегенерируйте adapter. Любые
migration, public-interface, permission или provider-default изменения требуют
отдельного решения и rollback evidence.

Источники: [`config/model-routing.toml`](../../../config/model-routing.toml),
[`docs/model-routing.md`](../../model-routing.md),
[`docs/runtime-capabilities.md`](../../runtime-capabilities.md),
[`docs/mcp-gateway.md`](../../mcp-gateway.md).
