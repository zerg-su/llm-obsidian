---
type: runbook
status: stable
created: 2026-08-01
updated: 2026-08-01
last_validated: 2026-08-01
tags: [rt10, foundation, verification, codex]
sessions: [019fab00-3160-7380-8920-4b20183afb76]
address: c-000060
---

# RT10 Foundation Verification

Use this runbook in `/Users/zak/Projects/worktrees/llm-obsidian-2-6-foundation` on branch `task/llm-obsidian-2-6-foundation`.

## 1. Check the Codex adapter

```bash
python3 scripts/codex-adapter.py --check
```

Expected: `codex-adapter: no changes`.

## 2. Check the MCP gateway sync

```bash
scripts/mcp-gateway/mcp-gateway.sh codex-sync --check
```

Expected: `codex-sync: no changes`.

## 3. Verify upstream skill snapshots

```bash
python3 references/upstream-skills/verify_snapshots.py
```

Expected: both snapshots match `manifest.json`.

## 4. Run command-evidence coverage

```bash
python3 tests/test_command_evidence.py
```

Expected: all command-evidence tests pass, including that the sanitization fixture is generated internally.

## 5. Run runtime-hook coverage

```bash
python3 tests/test_runtime_hooks.py
```

Expected: all runtime-hook parity tests pass.

## Validation evidence

Validated 2026-08-01 with five code-owned, validation-grade agent checks, all `outcome: success` and `exit_code: 0`: `rt10-v4-adapter`, `rt10-v4-gateway`, `rt10-v4-snapshots`, `rt10-v4-evidence`, and `rt10-v4-hooks`.

The live v4 functional checks and typed callback passed. Its signal-less CLI cleanup seam was repaired deterministically post-run and is covered by parity regressions for both CLI reconciliation and runtime-manager cleanup.

Wiki provenance session: `019fab00-3160-7380-8920-4b20183afb76`. Worker execution session: `019fbd86-a244-7d90-acd7-69d562544dca`.

One v4 user-attested success was ingested for `scripts/mcp-gateway/mcp-gateway.sh codex-sync --check`, with the reported excerpt `codex sync configuration clean`. It is user-reported and was not agent-verified. Ordinary PostToolUse discovery records with `outcome: unknown`, including historical records for this provenance, do not prove validation.

## What NOT to do

Do not treat unknown or user-attested outcomes as agent-verified, inline secrets, search logs with literal secret material, or run these checks from another checkout or branch.
