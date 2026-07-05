# AGENTS.md — agent-agnostic vault instructions

This repository is an Obsidian vault plus an LLM agent toolkit. CLAUDE.md is
the full operating manual (Russian); this file is the condensed agent-agnostic
contract for any coding agent (Claude Code today; Codex adapter on the
roadmap — the scripts below are plain bash/python and work from any agent).

## Core rules

1. The vault is `wiki/`. External sources are READ-ONLY; write only into this
   vault.
2. `wiki/log.md` and `wiki/hot.md` are written ONLY through
   `scripts/vault-write.py` (single JSON payload, deterministic caps). Never
   edit them directly.
3. Every page carries frontmatter: `type`, `status`, `created`, `updated`,
   `tags`, `sessions` (provenance), and a DragonScale `address: c-NNNNNN`
   allocated via `./scripts/allocate-address.sh`.
4. Wikilinks are `[[Page Name]]` — file names are vault-unique, no paths.
5. Search before writing: `scripts/semantic-search.py "<query>" --hybrid`
   (local ollama bge-m3 + BM25). Merge into existing pages instead of
   creating near-duplicates; `scripts/tiling-check.py` finds duplicate
   candidates.
6. Retrieval changes ship only with shifted metrics:
   `make bench-retrieval` against `.vault-meta/retrieval-goldset.jsonl`.
7. Validation: `scripts/validate-vault.py --summary` (caps, frontmatter,
   plans lifecycle). Tests: `make test` (hermetic, no network).

## Write path (what happens on turn end in Claude Code)

The Stop hook (`.claude/hooks/stop.sh`) reindexes `.vault-meta/`, rebuilds
the BM25 index, refreshes dense embeddings incrementally, backs up agent
memory, and auto-commits `wiki/ .raw/ .vault-meta/ .claude-memory/` under an
flock (parallel sessions do not collide). Other agents can run the same
script manually after writing pages.

## Danger zones

- Never commit credentials; `.gitignore` blocks common secret containers and
  `scripts/lib_sanitize.py` masks credential-looking strings in captured
  command logs and memory backups.
- `.vault-meta/` runtime indexes are derived state — regenerate, do not hand-edit.
