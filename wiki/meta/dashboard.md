---
type: meta
title: "Dashboard"
created: 2026-04-08
updated: 2026-08-01
tags:
  - meta
  - dashboard
status: evergreen
related:
  - "[[index]]"
  - "[[overview]]"
  - "[[log]]"
  - "[[concepts/_index]]"
  - "[[meta/_index]]"
  - "[[Compounding Knowledge]]"
  - "[[backlog]]"
  - "[[lint-report-2026-08-01]]"
sessions:
  - public-template-v2
---

# Wiki Dashboard

Navigation: [[index]] | [[overview]] | [[log]] | [[hot]] | [[meta/_index|meta index]] | [[backlog]]

The dashboard uses **Obsidian Bases**. A core Obsidian feature shipped in v1.9.10 (August 2025). No plugin install required.

> [!tip] Embedded Bases view
> The interactive dashboard lives in [[dashboard.base]]. Open that file directly, or use the embed below.

![[dashboard.base]]

---

## Vault Health

> [!info] Latest audit
> [[lint-report-2026-08-01]] records the RT09 baseline, bounded repairs, retained warnings, and exact verification evidence for the 2026-08-01 integration snapshot.

Run the read-only machine-checkable portions from the repository root:

```bash
python3 scripts/validate-vault.py
./scripts/allocate-address.sh --peek
./scripts/with-timeout 8 ./scripts/tiling-check.py --peek
./scripts/with-timeout 60 ./scripts/tiling-check.py
python3 scripts/pipeline-stats.py --days 30
python3 scripts/check-skill-budget.py
python3 skills/improve-skills/scripts/audit_skills.py --strict
```

> [!warning] Index refresh writes files
> `python3 scripts/reindex.py --folder-indexes` rewrites derived `.vault-meta/` indexes and folder `_index.md` blocks, and may restamp `wiki/index.md`. Run it only when intentionally refreshing indexes.

---

## Legacy Dataview Dashboard (Optional)

If you are on Obsidian < 1.9.10 or prefer Dataview, the queries below still work. Just install the Dataview community plugin.

### Recent Activity

```dataview
TABLE type, status, updated FROM "wiki" SORT updated DESC LIMIT 15
```

### Seed Pages (Need Development)

```dataview
LIST FROM "wiki" WHERE status = "seed" SORT updated ASC
```

### Entities Missing Sources

```dataview
LIST FROM "wiki/entities" WHERE !sources OR length(sources) = 0
```

### Open Questions

```dataview
LIST FROM "wiki/questions" WHERE status = "developing" OR status = "seed" SORT updated DESC
```

### Comparisons

```dataview
TABLE verdict FROM "wiki/comparisons" SORT updated DESC
```

### Sources

```dataview
TABLE author, date_published, updated FROM "wiki/sources" WHERE type = "source" SORT updated DESC LIMIT 10
```
