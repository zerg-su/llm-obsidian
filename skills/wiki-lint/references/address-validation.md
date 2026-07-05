# Address Validation (DragonScale Mechanism 2) — полная спецификация

Read on demand из wiki-lint check 9. Канонические правила классификации, 6 проверок и формат секции отчёта.

## Address Validation (DragonScale Mechanism 2 MVP)

**Opt-in feature.** Address Validation runs only if the vault is using DragonScale, detected by:

```bash
if [ -x ./scripts/allocate-address.sh ] && [ -f ./.vault-meta/address-counter.txt ]; then
  DRAGONSCALE_ADDRESSES=1
else
  DRAGONSCALE_ADDRESSES=0
fi
```

When `DRAGONSCALE_ADDRESSES=0`, skip this entire section. Missing `address:` fields are not flagged, not even informationally. Pages that happen to have an `address:` field are passed through unvalidated (treat as user-managed metadata).

When `DRAGONSCALE_ADDRESSES=1`, proceed with the rollout baseline and checks below.

Rollout baseline: **2026-04-23** (Phase 2 ship date in vaults that adopted DragonScale on that day). Vaults that adopted DragonScale later should override this baseline by setting the earliest `created:` date of any addressed page as their personal rollout date. Record the chosen baseline at the top of `.vault-meta/legacy-pages.txt` as a commented line: `# rollout: YYYY-MM-DD`.

### Classification rule (applied per page)

Before validating anything, classify the page:

| Classification | Criteria |
|---|---|
| **Meta / fold / daily / excluded** | `type == "daily"` (date pages under `wiki/daily/`, high-churn, one per day) OR file is in `wiki/folds/` OR filename in `{_index.md, index.md, log.md, hot.md, overview.md, dashboard.md, dashboard.base, Wiki Map.md, getting-started.md}`. Address not required. |
| **Post-rollout (must have address)** | `type` is not meta/fold/daily AND frontmatter `created:` date is >= 2026-04-23 AND file path is NOT in the legacy baseline manifest. |
| **Legacy (backfill-eligible)** | `type` is not meta/fold/daily AND frontmatter `created:` date is < 2026-04-23 OR file path IS in the legacy baseline manifest. Address not required until backfill. |

**Legacy baseline manifest**: optional file at `.vault-meta/legacy-pages.txt`, one relative path per line. Pages listed there are treated as legacy regardless of `created:` date. Use this to grandfather pages whose `created:` metadata is wrong or missing.

### Validation checks (run in order)

1. **Format check**: any page with `address:` set must match one of:
   - `^c-[0-9]{6}$` — post-rollout creation address.
   - `^l-[0-9]{6}$` — legacy-backfill address.
   - Pages under `wiki/folds/` use `fold_id`, not `address`; do not apply the `c-`/`l-` regex there.

2. **Uniqueness check**: no two pages share the same address value. Report both paths.

3. **Counter consistency**: `./scripts/allocate-address.sh --peek` returns the next counter value. Every observed `c-NNNNNN` must satisfy `NNNNNN < peek_value`. Violation = counter drift.

4. **Post-rollout enforcement**: every page classified as "post-rollout (must have address)" that LACKS the `address:` field is a lint **error**, not informational. This prevents the silent-regression path where a new page skips address assignment.

5. **Legacy identification**: every page classified as "legacy" that LACKS an address is informational. The lint report lists them under "Pending backfill" with total count.

6. **Address-map consistency** (`.raw/.manifest.json`): for every page path in `address_map`, the page must exist and its frontmatter `address` must match the mapping. Mismatches are errors (either a rename dropped the map update, or a manual edit diverged).

### Lint posture summary

- Pages that HAVE an address with bad format: **error**.
- Pages that HAVE colliding addresses: **error**.
- Pages classified **post-rollout** WITHOUT an address: **error**.
- Pages classified **legacy** WITHOUT an address: **informational** (expected).
- Meta, fold, and daily (`type: daily`) pages without `address`: **ignored** (not applicable).
- Counter drift (observed counter >= peek): **error**.
- Address-map mismatch: **error**.

Lint only observes. Do NOT auto-assign missing addresses during lint. Assignment is `wiki-ingest`'s responsibility only.

### Output section in the lint report

```markdown
## Address Validation

- Counter state: `$(./scripts/allocate-address.sh --peek)`
- Highest c- address observed: c-XXXXXX
- Post-rollout pages checked: N (X passing, Y errors)
- Legacy pages pending backfill: M

### Errors
- [[Page Name]]: invalid address format `{value}`. Expected `c-NNNNNN` or `l-NNNNNN`.
- [[Page A]] and [[Page B]] share address `c-000042`.
- [[Post-Rollout Page]]: missing address. Page created 2026-04-25 (post-rollout); address required. Run wiki-ingest or manually run `./scripts/allocate-address.sh` and add to frontmatter.
- [[Page Name]] has address `c-000100` but counter peek is `50`. Counter drift; run `./scripts/allocate-address.sh --rebuild`.
- `.raw/.manifest.json` maps `wiki/foo.md` -> `c-000010` but page frontmatter has `c-000012`. Resolve mismatch.

### Pending backfill (informational)
- M legacy pages without addresses. See `.vault-meta/legacy-pages.txt` for the canonical legacy set, or filter by `created:` < 2026-04-23.
```

---

