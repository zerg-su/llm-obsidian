---
type: runbook
status: draft
created: 2026-08-01
updated: 2026-08-01
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

Expected: all command-evidence tests pass.

## 5. Run runtime-hook coverage

```bash
python3 tests/test_runtime_hooks.py
```

Expected: all runtime-hook parity tests pass.

## Validation evidence

Draft evidence only: RT10 v3 produced ordinary agent-executed discovery records with `outcome: unknown` plus one user-attested success for provenance session `019fab00-3160-7380-8920-4b20183afb76`; neither proves validation. A fresh code-owned validation run is required before this page may become stable or receive `last_validated`. Test redaction with `scripts/command_evidence.py self-test-sanitization`; never search captured logs for a literal secret or token.

## What NOT to do

Do not treat unknown or user-attested outcomes as agent-verified, inline secrets, search logs with literal secret material, or run these checks from another checkout or branch.
