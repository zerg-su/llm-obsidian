---
type: review
status: active
created: 2026-08-01
updated: 2026-08-01
tags: [review, harness]
sessions: []
review_id: "7ccfc543-9d3f-4e0d-9a44-89378ec073cd"
address: "c-000048"
---

# Cross-model review — 7ccfc543-9d3f-4e0d-9a44-89378ec073cd — 7d7bbd1d3ef8

Final verdict: `approve`.

## Bound evidence

- Operation: `7ccfc543-9d3f-4e0d-9a44-89378ec073cd`
- Run: `ca8ae34dcbd0d2402e94fd8afe135738`
- Mode: `simple`
- HEAD: `54bfac80c754b723c05b6b90c77071d2f70b1c73`
- Verification profile: `scoped` (`7724ff1e1e5dd664008315264d34a55bd53d35d9d10b66d797c540841108df82`)

## Axis: holistic

- Verdict: `approve`
- Verification iteration: 1

### Findings

- **holistic-unattributed-hint-asymmetry · minor · 'unattributed' is now a first-class uncovered category on the verdict side but not on the hint side, so a skill whose only router evidence came from an unattributed session is listed as having 'no router evidence in any runtime'.**
  - File: `scripts/pipeline-stats.py:253`
  - Evidence: Line 247-248 adds 'unattributed' to uncovered_runtimes, but the hinted filter at lines 249-254 still intersects only with SKILL_CAPABLE_RUNTIMES - SKILL_OBSERVABLE_RUNTIMES, i.e. {'codex'}. Router hits are normalized through normalize_runtime (line 361), so a hit from a session with no runtime marker lands in hint_runtimes as 'unknown' and is never rescued: the skill falls into the line 671-672 list captioned 'no invocation or router evidence in any runtime', which is false for it. This is live, not hypothetical - .vault-meta/router-hits.jsonl carries 462 records tagged "runtime": "unknown" out of 1899. A second consequence of the same asymmetry: unattributed_agent_activity is counted only from the pipeline-events seam (lines 417-420), yet the UserPromptSubmit router only ever fires inside an agent session, so an unknown-tagged router hit is by construction agent activity - a window with unattributed agent prompts but no unattributed agent-op event still reaches the complete-verdict branch at line 664. The exposure is bounded: the whole section is prefixed 'Not a dead-weight verdict ... Confirm with the user before removing any skill below', so no unsafe action is unlocked by the wording alone. The new run_report_unattributed_agent test exercises this path (its router tag clamps to 'unknown') but asserts nothing about where /beta lands, so the behavior is untested.
  - Recommendation: Widen the hint intersection to cover unattributed activity as well (e.g. intersect against KNOWN_RUNTIMES - SKILL_OBSERVABLE_RUNTIMES), and count unknown-tagged router hits toward unattributed agent activity since a router hit implies an agent prompt. Then assert /beta's placement in run_report_unattributed_agent.
- **holistic-docs-agent-op-enumeration · minor · The doc that defines the uncovered boundary enumerates 6 of the 8 agent-driven ops and overstates how often unattributed orchestration occurs.**
  - File: `docs/pipeline-observability.md:56`
  - Evidence: Lines 55-58 list agent-run, review-round, surface-lifecycle, task-complete, task-escalation and model-turn, while AGENT_DRIVEN_OPS in scripts/pipeline-stats.py:87-96 also contains review-round-start and model-turn-incomplete - a reader auditing the gate against the doc finds two extra triggers. Line 54-55 also says agent-driven orchestration 'frequently lands there': in the live .vault-meta/pipeline-events.jsonl only 24 records carry an agent-driven op under runtime 'unknown' (2026-07-18 to 2026-07-29) against roughly 1200 unattributed records overall, the bulk being Stop-hook script operations that correctly do not block the verdict.
  - Recommendation: Complete the op list (or say 'the orchestration ops in AGENT_DRIVEN_OPS' and point at the constant), and replace 'frequently' with an accurate characterisation - unattributed orchestration is a small minority of unknown records, which is precisely why the gate keys on the op rather than on the tag.

## Verification gaps

- None

## Residual risks

- None

## Notes for executor

- None

## Archive boundary

Raw prompts, transcripts, commands, sockets, and cmux/process identifiers are excluded.
