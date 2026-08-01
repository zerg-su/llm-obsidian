---
type: review
status: active
created: 2026-08-01
updated: 2026-08-01
tags: [review, harness]
sessions: []
review_id: "133542f0-7e4f-47c2-9c86-673fd877ddb1"
address: "c-000056"
---

# Cross-model review — 133542f0-7e4f-47c2-9c86-673fd877ddb1 — eead86d4aecb

Final verdict: `approve`.

## Bound evidence

- Operation: `133542f0-7e4f-47c2-9c86-673fd877ddb1`
- Run: `65f07c766d81dec36c1013f7855a9a45`
- Mode: `simple`
- HEAD: `83e0977129f05d0cee56a2086613a0480b7d235a`
- Verification profile: `scoped` (`7724ff1e1e5dd664008315264d34a55bd53d35d9d10b66d797c540841108df82`)

## Axis: holistic

- Verdict: `approve`
- Verification iteration: 0

### Findings

- **holistic-1-live-upstream-unverified · minor · RT08 asked for a refresh against current upstream primary sources; the refresh is against the pinned snapshots only, because protected research could not launch. The limitation is disclosed accurately and prominently, so this is a coordinator scoping decision rather than an artifact defect.**
  - File: `docs/upstream-skills-comparison.md:22`
  - Evidence: The task contract in the approved plan requires refreshing the comparison "against current upstream primary sources". No outbound source was consulted: the doc's limitation block (docs/upstream-skills-comparison.md:22-30) states "live-upstream drift was not verified" and "no outbound source was consulted"; the same limitation is repeated in the wiki page callout, wiki/log.md, and the commit message. The blocking cause is filed as an open defect in docs/feature-gaps.md:34-70, and I confirmed its central mechanism in product code: scripts/harness/runtime_worker.py:219-261 takes the cmux wrapper-shim branch when CMUX_SURFACE_ID equals the operation surface_id and returns (shim, *argv[1:]), and RESEARCH_PATH is exactly "/usr/bin:/bin:/usr/sbin:/sbin" at runtime_worker.py:96. The executor correctly declined to widen RESEARCH_PATH or to fix the shared provider launch path inside this task. Separately, no re-pin was performed, which is consistent: with no verified view of current upstream there is no basis to choose a new commit.
  - Recommendation: Do not ask this lane to resolve it; the repair is a separate task on the shared provider launch path and is already filed. The coordinator should decide explicitly whether RT08 counts as a completed useful task on the delivered pinned-snapshot judgement, or whether a replacement/follow-up runs once the protected-research wrapper defect is repaired, and record that decision in the dogfood report.
- **holistic-2-pin-record-duplication · minor · The commit/version pin record now exists in four places, two of them added by this change, while this doc declares that README.md owns the pin table. That widens the drift surface the change set out to reduce.**
  - File: `docs/upstream-skills-comparison.md:45`
  - Evidence: docs/upstream-skills-comparison.md:10-11 states "references/upstream-skills/README.md owns the pin table and the mechanical upgrade procedure; this file owns the judgement", yet lines 43-48 carry a second pin table with the same commits and both declared versions. references/upstream-skills/README.md:27-30 carries the third copy and references/upstream-skills/manifest.json:7-11,30-34 the machine-readable fourth. The wiki page adds a fifth rendering with 7-character abbreviated commits (44c9b2d, 2ab9580) on a page whose own text says the commit is the pin's identity. All four currently agree and match the snapshots exactly (mattpocock package.json 1.1.0 / plugin.json 1.2.0, superpowers 6.2.0 / 6.2.0, manifest files+bytes 141/397020 and 95/1174353), so this is a maintainability risk rather than a present inaccuracy. The next re-pin must hand-update three prose copies, which is the failure mode that produced the 1.2.0-vs-1.1.0 ambiguity this change fixed.
  - Recommendation: On the next re-pin, reduce the comparison doc's Pin state section to the commits it actually cites plus a pointer to the README/manifest, or add the comparison doc and the wiki page to the upgrade-review checklist in references/upstream-skills/README.md:62-78 so all copies are updated in one pass. Prefer full commit SHAs over abbreviations wherever a pin is presented as identity.
- **holistic-3-doc-not-in-index · minor · The new durable integration guidance is not listed in the repository's "Further documentation" index, so it is discoverable only from the snapshot README.**
  - File: `README.md:370`
  - Evidence: README.md:353-370 is a "Further documentation" table enumerating docs/model-routing.md, docs/runtime-capabilities.md, docs/dragonscale-guide.md and others; docs/upstream-skills-comparison.md is absent. The only inbound repository link is references/upstream-skills/README.md:36-38 (path ../../docs/upstream-skills-comparison.md, which resolves correctly). A grep for "upstream-skills" in README.md returns no match. The table is not strictly exhaustive today — docs/feature-gaps.md is likewise referenced only in prose — so this is a discoverability gap, not a broken contract.
  - Recommendation: Add one row for the upstream skill comparison to the README.md "Further documentation" table and the matching row to README.ru.md so the bilingual indexes stay in sync. Optional and safe to defer, since the snapshot README already links it.

## Verification gaps

- None

## Residual risks

- None

## Notes for executor

- None

## Archive boundary

Raw prompts, transcripts, commands, sockets, and cmux/process identifiers are excluded.
