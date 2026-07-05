---
name: wiki-lint
version: 1.0.0
description: >
  Health check the Obsidian wiki vault. Finds orphan pages, dead wikilinks, stale claims,
  missing cross-references, frontmatter gaps, and empty sections. Creates or updates
  Dataview dashboards. Triggers on: "lint", "health check",
  "clean up wiki", "check the wiki", "wiki maintenance", "find orphans", "wiki audit".
allowed-tools: Read Glob Grep Bash Write Edit AskUserQuestion
---

# wiki-lint: Wiki Health Check

Run lint after every 10-15 ingests, or weekly. Ask before auto-fixing anything. Output a lint report to `wiki/meta/reports/lint-report-YYYY-MM-DD.md`.

---

## Lint Checks

**Step 0 (MANDATORY, before everything): run the deterministic validator.**

```bash
python3 scripts/validate-vault.py
```

Its FAIL lines (hot caps, fold lag, questions status, frontmatter gaps, panic-runbook
freshness, skill-description budget) go verbatim into the report as 🔴 items — do not
re-derive these checks by hand, the script is the source of truth for every machine-checkable cap.
LLM judgement below is for what the script cannot see (semantics, staleness, dead links).

Work through these in order:

1. **Orphan pages**. Wiki pages with no inbound wikilinks. They exist but nothing points to them. Exclude from the orphan report (orphan by design): `wiki/plans/*` (auto-captured ExitPlanMode archive), `wiki/meta/sessions/*` и `wiki/meta/reports/*` (навигация через log.md/folds, не через inbound links).
2. **Dead links**. Wikilinks that reference a page that does not exist. **Apply the exclusion set below before counting** — without it the raw number is inflated 15-20x by intentional non-bugs.

   ### Dead-link exclusions (do NOT count these as dead)

   | Excluded target shape | Why it is not a bug |
   |---|---|
   | `[[feedback_*]]`, `[[project_*]]`, `[[reference_*]]` | Memory-system slugs. Live in the memory dir, not `wiki/`. Linking memory by slug is an intentional convention. |
   | `[[X]]`, `[[Foo]]`, `[[Page Name]]`, `[[Note Title]]`, `[[Title]]`, `[[...]]`, `[[<...>]]` | Placeholder/example links inside templates, `_index.md` schemas, and prior lint-report examples. |
   | Path-style `[[services/...]]`, `[[runbooks/...]]`, `[[decisions/...]]`, `[[goals/...]]` | Literal ellipsis in folder `_index.md` templates, not real targets. |
   | Asset/suffix links `[[*.png]]`, `[[*.sh]]`, `[[*.md]]`, `[[*.base]]` | Embeds/assets or accidental suffixes, not page wikilinks. |
   | Links whose source page is `lint-report-*`, `log-archive-*`, or a `_index.md` template | Example/illustrative links, not live cross-references. |

   After exclusions, the **genuine** dead links split into three buckets — report them separately, do NOT bulk-delete:
   - **Renamed/moved**: target exists under a different name → fix the link to point to the real page (the only auto-fixable bucket, and only after confirming the target is semantically the same page).
   - **Frontier (not yet created)**: hub concept referenced by intent (`[[VictoriaMetrics]]`, `[[ArgoCD]]`, `[[EKS]]`). Leave as a forward-link per the vault's lazy-fill convention («не создаём заглушки заранее»). Do NOT delete, do NOT auto-stub.
   - **Obsolete intent**: planned page no longer relevant → remove the link (human judgement, never automatic).
3. **Stale claims**. Assertions on older pages that newer sources have contradicted or updated.
4. **Missing pages**. Concepts or entities mentioned in multiple pages but lacking their own page.
5. **Missing cross-references**. Entities mentioned in a page but not linked.
6. **Frontmatter gaps**. Pages missing required fields (type, status, created, updated, tags). 🔴 FAIL for files with **no frontmatter at all** — they are invisible to every retrieval index (reindex skips them, so /wiki-query, semantic search and tiling never see them); `python3 scripts/reindex.py` prints the INVISIBLE list.
7. **Empty sections**. Headings with no content underneath.
8. **Stale index entries**. Items in `wiki/index.md` pointing to renamed or deleted pages.
9. **Address validity** (DragonScale Mechanism 2, opt-in). Validate `address:` fields, uniqueness, counter, post-rollout enforcement. Summary in **Address Validation** below; full spec — `references/address-validation.md`.
10. **Semantic tiling** (DragonScale Mechanism 3, opt-in). Flag candidate duplicate pages via embedding cosine similarity. Summary in **Semantic Tiling** below; full spec — `references/semantic-tiling.md`.
11. **Frontmatter discipline** (per план Phase 4.9 P1). Pages with bloated or stale frontmatter. See the **Frontmatter Discipline** section below.
12. **Hot cache size** (added 2026-06-09 after the 99KB incident). Covered by Step 0 (`validate-vault.py` `hot` check: 800 words, 15 one-line bullets ≤160 chars, 8 threads, 120-word narrative). Here only: 🔴 FAIL if the frontmatter `updated:` line is longer than ~40 chars — summary content appended into the date field (the exact failure mode that once grew hot.md to 99KB).
13. **Pipeline usage stats** (added 2026-06-10). Run `./scripts/pipeline-stats.py --days 30` and include its output as a report section. Skills with 0 explicit invocations are **dead-weight candidates** (их descriptions — налог на system prompt каждой сессии), но с оговоркой из самого отчёта: триггер-фразовые вызовы не учитываются — перед удалением скилла подтвердить у пользователя. Router-правила с сотнями hints и ~0 follow-through — кандидаты на сужение паттернов в `.claude/skill-rules.json`.
14. **Stale pending plans** (added 2026-06-10; deterministic since 2026-07-03). Covered by Step 0 (`validate-vault.py` `plans` check: status vocabulary pending|executed|abandoned, executed needs a `Результат:` link, pending >30d → WARN). Do NOT re-count manually — surface the Step 0 output and suggest per stale item: close via `plan_close` (vault-write payload) or set `status: abandoned`.
15. **Wiki-drift spot-check candidates** (added 2026-06-10). From `.vault-meta/index.jsonl` pick the 5 oldest-by-`updated` pages with `type: service` and `status: solid` → report section «Stale-risk: verify via MCP». Lint только перечисляет кандидатов на сверку с живой инфрой — verification руками/через MCP по желанию пользователя.

---

## Frontmatter Discipline (P1)

Beyond the basic «missing required fields» check (item 6), flag these WARN-level patterns:

- **`related:` overflow** — `related:` array > 8 entries. Better: short list + long cross-references in the body. Hard cap is not enforced; this is a hint to declutter.
- **Frontmatter block > 25 lines** — compact visibility lost. Suggest extracting auxiliary fields into the body, or merging arrays into flow-style.
- **`tags:` missing** — every page except `_templates/` and `log.md` should have at least one tag. Untagged pages don't appear in `.vault-meta/tag-index.json` and can't be found by tag-filter queries.
- **`status: developing` > 30 days** — page hasn't moved out of `developing` since `updated:` field. Either promote to `evergreen`/`mature`, mark `superseded`/`closed`, or move into `wiki/questions/` if it's an unresolved open question.
- **Missing `sessions:` array** — any page outside `log.md` and `_templates/` without `sessions:` is a provenance gap (per memory `feedback_session_id_in_frontmatter`).

All five issues are **WARN** (not blocking). Report under `## Frontmatter Discipline` section in the lint report. Suggest concrete remediation per finding (which tag to add, which status to set, which session id to backfill).

Source: `.vault-meta/index.jsonl` already has all the fields needed — no need to re-parse wiki/ in the lint script.

---

## Lint Report Format

Create at `wiki/meta/reports/lint-report-YYYY-MM-DD.md`:

```markdown
---
type: meta
title: "Lint Report YYYY-MM-DD"
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [meta, lint]
status: developing
---

# Lint Report: YYYY-MM-DD

## Summary
- Pages scanned: N
- Issues found: N
- Auto-fixed: N
- Needs review: N

## Orphan Pages
- [[Page Name]]: no inbound links. Suggest: link from [[Related Page]] or delete.

## Dead Links
- [[Missing Page]]: referenced in [[Source Page]] but does not exist. Suggest: create stub or remove link.

## Missing Pages
- "concept name": mentioned in [[Page A]], [[Page B]], [[Page C]]. Suggest: create a concept page.

## Frontmatter Gaps
- [[Page Name]]: missing fields: status, tags

## Stale Claims
- [[Page Name]]: claim "X" may conflict with newer source [[Newer Source]].

## Cross-Reference Gaps
- [[Entity Name]] mentioned in [[Page A]] without a wikilink.
```

---

## Naming Conventions

Enforce these during lint:

| Element | Convention | Example |
|---------|-----------|---------|
| Filenames | Title Case with spaces | `Machine Learning.md` |
| Folders | lowercase with dashes | `wiki/data-models/` |
| Tags | lowercase, hierarchical | `#domain/architecture` |
| Wikilinks | match filename exactly | `[[Machine Learning]]` |

Filenames must be unique across the vault. Wikilinks work without paths only if filenames are unique.

---

## Writing Style Check

During lint, flag pages that violate the style guide:

- Not declarative present tense ("X basically does Y" instead of "X does Y")
- Missing source citations where claims are made
- Uncertainty not flagged with `> [!gap]`
- Contradictions not flagged with `> [!contradiction]`

---

## Dataview Dashboard

Create or update `wiki/meta/dashboard.md` with these queries:

````markdown
---
type: meta
title: "Dashboard"
updated: YYYY-MM-DD
---
# Wiki Dashboard

## Recent Activity
```dataview
TABLE type, status, updated FROM "wiki" SORT updated DESC LIMIT 15
```

## Seed Pages (Need Development)
```dataview
LIST FROM "wiki" WHERE status = "seed" SORT updated ASC
```

## Entities Missing Sources
```dataview
LIST FROM "wiki/entities" WHERE !sources OR length(sources) = 0
```

## Open Questions
```dataview
LIST FROM "wiki/questions" WHERE answer_quality = "draft" SORT created DESC
```
````

---

## Canvas Map

**Disabled in this vault (2026-06-10):** Obsidian canvas core-plugin выключен, `/canvas` заморожен (zero usage за 2 месяца) — canvas-карты НЕ генерировать. Если canvas вернут (включить core-plugin + снять disable-model-invocation у /canvas), спецификация формата — JSON Canvas, см. `skills/canvas/`.

---

## Address Validation

Opt-in (детект: `./scripts/allocate-address.sh` исполняемый + `.vault-meta/address-counter.txt` существует; иначе скип целиком). Полная спецификация — **Read** `references/address-validation.md`: классификация страниц (meta/fold excluded, post-rollout >= 2026-04-23 must-have, legacy backfill-eligible по манифесту `.vault-meta/legacy-pages.txt`), 6 проверок (формат `c-/l-NNNNNN`, уникальность, counter peek, post-rollout enforcement = error, legacy = informational, `.raw/.manifest.json` consistency) и формат секции отчёта. Lint только наблюдает — НЕ присваивает адреса.

## Skill Size & Frontmatter Discipline (iter-2 Step 9)

Anthropic Agent Skills spec и community guidance ставят жёсткий потолок 500 строк на SKILL.md (reliability регрессирует выше) и ~15 000 char бюджет на сумму descriptions всех skills (это часть system prompt'а каждой сессии). Lint их валидирует.

### Scope

Scan the single `skills/` root of this repo. **Note**: the system prompt of every session loads the descriptions of ALL installed skills, so the aggregate budget below is computed across everything found under `skills/`.

```bash
find skills -name SKILL.md -not -path '*/node_modules/*'
```

### Checks (per SKILL.md)

| Severity | Condition | Suggested action |
|---|---|---|
| 🔴 FAIL | `wc -l SKILL.md > 500` | Split: process в SKILL.md, context в `references/*.md`. См. memory rule про size discipline. |
| 🟡 WARN | `wc -l SKILL.md > 400` | Приближается к лимиту — пора выносить tables / templates в `references/`. |
| 🟡 WARN | Missing `version:` in frontmatter | Add `version: 1.0.0` (semver: PATCH desc tweak, MINOR new mode/ref, MAJOR breaking pre-flight changes). |
| 🟡 WARN | Frontmatter `description` > 800 chars | Description — trigger, не документация. Перенести длинную часть в body. |
| 🟡 WARN | Skill has Write/Edit/Bash в `allowed-tools` AND **no** `AskUserQuestion` AND no `read-only`/`reference` marker in body | Probably needs pre-flight clarification (`feedback_skill_preflight_clarification`). Confirm by reading skill purpose. |

### Aggregate checks (across all SKILL.md)

```bash
# Sum char count of all frontmatter descriptions
total_chars=$(python3 - <<'PY'
import re, sys
from pathlib import Path
total = 0
roots = [Path("skills")]
for p in [q for r in roots for q in r.rglob("SKILL.md")]:
    text = p.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        desc = re.search(r"^description:\s*[|>]?\s*\n?(.*?)(?=^[a-z_-]+:|^\Z)", m.group(1), re.S | re.M)
        if desc:
            total += len(desc.group(1).strip())
print(total)
PY
)
```

| Severity | Condition | Action |
|---|---|---|
| 🔴 FAIL | `total_chars > 15000` | Hard Anthropic limit. Сократить descriptions начиная с самых раздутых. |
| 🟡 WARN | `total_chars > 8000` | Половина бюджета. Оставить запас под user-global и плагинные skills. |

### Report section

```markdown
## Skill Size & Frontmatter Discipline

- Total SKILL.md files scanned: N
- Total description chars (all skills): M / 15000

### 🔴 Errors
- skills/foo/SKILL.md: 542 lines (> 500). Suggest split: ...

### 🟡 Warnings
- skills/bar/SKILL.md: 428 lines (> 400). Consider extracting <section> to references/.
- skills/baz/SKILL.md: missing `version:` field.
- skills/qux/SKILL.md: has Write/Edit/Bash but no AskUserQuestion (likely needs pre-flight per feedback_skill_preflight_clarification).
```

---

## Semantic Tiling

Opt-in (требует локальный ollama + `bge-m3`; детект через `./scripts/tiling-check.py --peek`, exit 10 = ollama недоступен → skip, exit 11 = модель не скачана). Полная спецификация — **Read** `references/semantic-tiling.md`: exit-коды, scope/exclusions, security posture (remote ollama только с `--allow-remote-ollama`), bands (bge-m3 калиброван: error >=0.92 / review 0.85-0.92; свежий seed 0.90/0.85 не калиброван до релейбла — пороги привязаны к модели), процедура калибровки, scale-лимиты (warn >500 страниц, hard-fail >5000). Read-only, no auto-merge. Отчёт: `./scripts/tiling-check.py --report wiki/meta/reports/tiling-report-YYYY-MM-DD.md`.

## Before Auto-Fixing

Always show the lint report first. Ask: "Should I fix these automatically, or do you want to review each one?"

Safe to auto-fix:
- Adding missing frontmatter fields with placeholder values
- Creating stub pages for missing entities
- Adding wikilinks for unlinked mentions

Needs review before fixing:
- Deleting orphan pages (they might be intentionally isolated)
- Resolving contradictions (requires human judgment)
- Merging duplicate pages
