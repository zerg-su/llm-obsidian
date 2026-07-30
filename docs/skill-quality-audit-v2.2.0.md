# Skill quality audit v2.2.0

Date: 2026-07-29

Scope: all final `skills/*/SKILL.md` packages. The audit applies the four
passes from `improve-skills`: invocation, information hierarchy, steering, and
pruning. It preserves triggers, permissions, tool routing, schemas, vault
writer paths, cmux placement, dispatch/review/reap lifecycle, protected
research, retries, escalation, and completion semantics.

Upstream model: Matt Pocock `writing-great-skills`, pinned at commit
`2ab958093e83e0ec752e6c1c5932da465bf23e0c`.

## Verdicts

| Skill | Verdict | Evidence or smallest correction |
|---|---|---|
| `agenda` | no-change | Four passes complete; code-owned transaction and warning contract are already concise and checkable. |
| `autoresearch` | no-change | Isolation, callbacks, and filing branches are co-located and safety-critical; no proven no-op or duplication. |
| `backlog` | fix | Removed the unsupported direct `/backlog drop` suggestion; Drop remains the existing promote choice. |
| `canvas` | fix | Aligned Claude with existing manual-only Codex metadata and resolved the `overview.md` versus `index.md` contradiction. |
| `clarify` | no-change | One-question interview and alignment gate have explicit branch and completion behavior. |
| `close` | no-change | Ordered save/exit steps and final-tool boundary are already precise. |
| `daily` | no-change | Evidence, runtime synthesis, validation, and transaction stages have explicit failure boundaries. |
| `defuddle` | no-change | Normal and fallback branches are distinct; protected tool changes would be behavioral. |
| `dispatch` | no-change | Normal orchestration is compacted behind code-owned runners and conditional compatibility reference. |
| `dispatch-workspace` | no-change | Correctly discloses shared behavior through the dispatch contract and changes placement only. |
| `distill-runbook` | no-change | Collection, drafting, filing, and panic branch have concrete output criteria. |
| `draft` | no-change | Source, fact-check, redaction, and output branches are explicit; external-send prohibition is safety-critical. |
| `find-session` | no-change | Retrieval, scoring, edge cases, and output bounds are deterministic and co-located. |
| `improve-skills` | fix | Added the requested explicit-only governance capability, deterministic audit, quality model, and completion criteria. |
| `journal` | no-change | Commands are the authoritative interface; date and mutation behavior are concise. |
| `learn` | no-change | Mode dispatch separates study, authoring, quiz, and practice without confirmed semantic duplication. |
| `obsidian-bases` | no-change | Flat reference has one coherent syntax domain and no ordered workflow to split. |
| `obsidian-markdown` | fix | Replaced the false blanket ban on nested YAML with the actual target-schema rule and required structured provenance. |
| `reap` | no-change | Code-owned finalization and conditional compatibility branch already use progressive disclosure. |
| `reap-send` | no-change | Task-side handoff, duplicate safety, and coordinator ownership are precise. |
| `review-dispatch` | no-change | Route, receive, verify, finish, and safety invariants remain one authoritative lifecycle. |
| `review-send` | no-change | Reviewer write exception and supervised transport are narrowly specified. |
| `save` | no-change | Large body remains live across distinct type, transaction, metadata, and quality branches; restructuring lacks failure evidence. |
| `save-plan` | no-change | Identification, metadata, filing, confirmation, and edge cases have explicit completion behavior. |
| `unsafe-research` | no-change | Explicit authorization and fixed current-session route are compact safety invariants. |
| `wiki` | fix | Aligned Claude with the existing explicit-only Codex/router contract. |
| `wiki-fold` | no-change | Status, dry-run, commit, reversal, and invariants are deterministic and bounded. |
| `wiki-ingest` | fix | Replaced a malformed Obsidian-style link to a repository reference with its real local path. |
| `wiki-lint` | fix | Removed stale duplicated skill-budget rules, delegated to repo-owned checks, and reconciled contradictory stub auto-fix guidance. |
| `wiki-query` | no-change | Quick, standard, deep, filing, and gap branches have explicit stop conditions. |

Inventory proof: 30 verdicts for 30 installed skills, with no omissions or
duplicates.

## Policy decisions

- `metadata.version` remains optional skill metadata. This audit does not add or
  enforce it piecemeal; making it required would need a separate all-skill
  migration, schema rule, and deterministic check.
- Versioned acceptance prompt baselines are retained as release evidence. Only
  the current release baseline is executable; older files remain inert for
  historical reproduction.

## Net effect

- Existing skills changed: 6.
- Existing skills unchanged after all four passes: 23.
- New explicit-only skill: 1.
- cmux, dispatch, review, reap, protected-research, callback, and writer
  mechanics changed: 0.

The structural audit reports `30 audited, 0 errors, 0 warnings`. Release
acceptance is registered as 30 skills × 2 runtimes with a reviewed 60-prompt
offline baseline. The live model matrix is intentionally outside this
behavior-preserving release scope.
