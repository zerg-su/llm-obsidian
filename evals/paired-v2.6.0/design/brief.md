# Bounded callback-stall recovery

Design a recovery decision for a provider session that is still alive but has
not produced its required typed callback. The repository already owns durable
operation identity, exact process/surface ownership, 10/15/20-minute liveness
thresholds, one bounded nudge, one identity-bound restart, and typed attention.

The result must use those existing seams. It must not add a scheduler, a second
pipeline engine, a provider-specific lifecycle, focus/title/index guesses, or
another model call in a deterministic transition. A disposable prototype may
exercise a pure decision function, but production code must remain unchanged.

The durable design result belongs at
`docs/acceptance/v2.6-paired-design-result.md` and must contain: problem,
non-goals, invariants, ownership boundaries, two or three alternatives,
recommendation, data/control flow, failure recovery, rollout, rollback,
testable acceptance criteria, and the bounded prototype evidence.
