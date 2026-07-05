---
name: wiki
version: 1.0.0
description: >
  Claude + Obsidian knowledge companion router: bootstraps a vault (SCAFFOLD)
  and routes operations to sub-skills. Slash-only since 2026-06-10 — the vault
  is built, scaffold is a one-time op (unfreeze by removing disable-model-invocation).
disable-model-invocation: true
allowed-tools: Read Write Edit Glob Grep Bash
---

# wiki — router + scaffolder

Two roles only:

1. **Routing table** — sends operations to the right sub-skill. Sub-skills do the work.
2. **Vault scaffolding** — first-time setup of a new vault (one-shot, rarely run after init).

For day-to-day operations see [[daily-pipeline-guide]] (`wiki/meta/daily-pipeline-guide.md`).

---

## Routing table

| User says | Sub-skill |
|---|---|
| «ingest <source>», «обработай это», «add this» | `wiki-ingest` |
| «что ты знаешь про X», «query:» | `wiki-query` |
| «lint», «health check», «wiki audit» | `wiki-lint` |
| «сохрани это», «save this», «file this» | `save` |
| «/autoresearch <topic>», «research X» | `autoresearch` |
| «/canvas», «add to canvas» | `canvas` |
| «fold log», «log rollup» | `wiki-fold` |
| «scaffold», «set up vault», «create knowledge base» | this skill (SCAFFOLD below) |

Full skill catalog and trigger phrasebook: [[daily-pipeline-guide]] sections «Skill catalog» + «Auto-triggers vs manual».

---

## SCAFFOLD operation (vault bootstrap)

Trigger: user describes what a new vault is for (NOT this existing llm-obsidian vault — that's already scaffolded).

Steps:

1. Read `references/modes.md` to pick wiki mode (A-F).
2. Ask: «What is this vault for?» (one question).
3. Create `wiki/` folder structure per chosen mode.
4. Create domain pages + `_index.md` sub-indexes.
5. Create `wiki/{index, log, hot, overview}.md`.
6. Create `_templates/` per note type.
7. Visual customization: read `references/css-snippets.md`, create `.obsidian/snippets/vault-colors.css`.
8. Create vault CLAUDE.md (template below).
9. Initialize git: read `references/git-setup.md`.
10. Present + ask for adjustments.

### Vault CLAUDE.md template

```markdown
# [WIKI NAME]: LLM Wiki

Mode: [MODE A/B/C/D/E/F]
Purpose: [ONE SENTENCE]
Owner: [NAME]
Created: YYYY-MM-DD

## Structure
[paste folder map from chosen mode]

## Conventions
- YAML frontmatter required: type, status, created, updated, tags
- Wikilinks `[[Note Name]]` (unique filenames, no paths)
- `.raw/` source documents — never modify
- `wiki/index.md` — master catalog, update on every ingest
- `wiki/log.md` — append-only, new entries at TOP

## Operations
- Ingest: drop in `.raw/`, say "ingest [filename]"
- Query: ask any question
- Lint: "lint the wiki"
```

---

## Cross-project referencing

Any other Claude Code project can pull from this vault without duplicating context. In that project's CLAUDE.md:

```markdown
## Wiki Knowledge Base
Path: ~/path/to/vault

1. Read wiki/hot.md first (~500 words recent)
2. If not enough → wiki/index.md (full catalog)
3. Domain specifics → wiki/<domain>/_index.md
4. Only then individual pages

NOT for: general coding questions, things already in current project files.
```

---

## See also

- `references/modes.md` — 6 vault modes for SCAFFOLD operation
- `references/css-snippets.md` — visual customization
- `references/git-setup.md` — git initialization
