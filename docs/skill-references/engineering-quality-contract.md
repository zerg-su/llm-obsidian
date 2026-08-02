# Engineering quality contract

This technology-agnostic baseline applies when repository standards are silent.
Repository-specific standards override heuristics, but must not erase the
approved Outcome Contract or evidence requirements.

## Module shape

- Give each file or internal module one coherent responsibility and change
  reason. Optimize for locality: a maintainer should understand a change from a
  small neighborhood of code.
- Prefer a deep module: a small stable interface that hides substantial policy,
  state, validation, or integration detail. Interface leverage matters more
  than the number of classes or files.
- Treat approximately 200 physical lines as a review signal, not proof of poor
  design. New growth needs a cohesion decision; multi-thousand-line functions
  or mixed orchestration/policy/transport code require an extraction plan.
- Do not create middle-man or pass-through modules. An abstraction earns its
  existence by hiding decisions, owning invariants, or supporting real
  variation; production plus a test adapter counts as real variation.

## Dependencies and behavior

- Make dependency direction explicit. Core policy depends on stable values and
  interfaces; provider, process, filesystem, network, and UI adapters depend on
  the core.
- Keep state ownership singular and make transitions explicit. Preserve public
  identities, idempotency, retry, cleanup, and error handling contracts during
  extraction.
- Prefer clear domain values over loose dictionaries at durable boundaries.
  Validate once at ingress and emit errors with enough context to act without
  leaking secrets.
- Remove real duplication of knowledge. Do not remove similar-looking code when
  the concepts can change independently.

## Maintainability smells

Investigate divergent change, shotgun surgery, feature envy, speculative
generality, long mixed-responsibility functions, hidden temporal coupling,
parallel conditionals, and duplicated validation. A smell triggers judgment,
not automatic splitting. Record why the chosen boundary improves locality and
which interface/test seam proves it.

## Test quality

Tests exercise observable behavior through the durable interface, use an
independent expectation, fail for the intended pre-change reason, and survive
internal refactors. Mock only slow, nondeterministic, privileged, or external
adapters; keep policy, transitions, and owned side effects real. See the TDD
test-quality reference for the executable checklist.

## Completion

Local green, a shorter file, a clean diff, or successful review transport is
not outcome completion. Bind maintainability claims to the module map, public
contract, deterministic tests/matrix, honest coverage denominator, and declared
Outcome Contract evidence.
