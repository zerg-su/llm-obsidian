---
type: review
status: active
created: 2026-08-01
updated: 2026-08-01
tags: [review, harness]
sessions: []
review_id: "49137cfd-5cb4-4ec0-a555-cf0a882c9c42"
address: "c-000052"
---

# Cross-model review — 49137cfd-5cb4-4ec0-a555-cf0a882c9c42 — 27f3e34be030

Final verdict: `approve`.

## Bound evidence

- Operation: `49137cfd-5cb4-4ec0-a555-cf0a882c9c42`
- Run: `758844729ab769a43b9d939e93cd6937`
- Mode: `simple`
- HEAD: `a3ee3c5e59a72a031a24b5354b5f4f848833806f`
- Verification profile: `scoped` (`7724ff1e1e5dd664008315264d34a55bd53d35d9d10b66d797c540841108df82`)

## Axis: holistic

- Verdict: `approve`
- Verification iteration: 1

### Findings

- **holistic-v1-residual-task-id-owner-fallback · minor · One narrow path still derives the operation owner from task metadata: when the single matched candidate is an unparseable record whose recorded owner is empty, the 'or task_id' fallback emits an --owner command that contradicts that row's own path_owner_id evidence.**
  - File: `scripts/upgrade-preflight.py:288`
  - Evidence: The round correctly changed the defaults at scripts/upgrade-preflight.py:283-284 to empty strings, so zero-candidate and multi-candidate worktrees now fail closed into 'inspect-identity-evidence'. Lines 288-291 still read owner_id = str(candidate_identity.get('owner_id') or task_id). A candidate identity carries an empty owner_id only for an unparseable record (lines 169-178, where owner_id is str(raw_spec.get('owner_id') or '')). That row is keyed by raw_operation_id, which falls back to operation_path.stem (lines 157-162), so a corrupt .vault-meta/harness/owners/<other>/operations/<task_id>.json matches a valid v3 worktree as its single candidate. The worktree recovery then emits '--owner <task_id> inspect <task_id>' while identity.operation_identity in the same row reports path_owner_id = <other>. The operation row itself avoids this by passing path_owner_id (line 189). Read-only and non-destructive, and the conflicting evidence is present in the packet, so this does not block.
  - Recommendation: Drop the 'or task_id' fallbacks at lines 288-291 and let the empty defaults flow into the incomplete-identity guard, or take path_owner_id/path_operation_id from candidate_identity when the recorded values are empty, so the emitted command never disagrees with the row's own evidence.
- **holistic-v1-doc-omits-evidence-only-ambiguous-variant · minor · The packet contract prose still describes ambiguous rows uniformly as requiring ownership reconciliation, without noting the new evidence-only variant that deliberately carries no inspect or recovery command.**
  - File: `docs/unattended-pipeline-operations.md:67`
  - Evidence: docs/unattended-pipeline-operations.md:67-68 says 'Ambiguous rows require exact ownership reconciliation', and lines 72-73 were correctly updated for cancel_command and identity.operation_identity. Neither mentions the 'inspect-identity-evidence' action now returned for an incomplete worktree identity (scripts/upgrade-preflight.py:116-125) or a missing stale worktree path (lines 85-92), whose recovery objects intentionally omit inspect_command and recovery_command. Since the packet stays at schema_version 1, this prose is the only contract a consumer can read, and a consumer that assumes every ambiguous row has an inspect_command would KeyError.
  - Recommendation: Add one sentence stating that an ambiguous row with an incomplete identity returns 'inspect-identity-evidence' with guidance and evidence only, and that command arrays are absent by design in that case.

## Verification gaps

- None

## Residual risks

- None

## Notes for executor

- None

## Archive boundary

Raw prompts, transcripts, commands, sockets, and cmux/process identifiers are excluded.
