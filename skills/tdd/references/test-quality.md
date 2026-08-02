# Test quality

Read this whenever writing or changing tests, fakes, or mocks.

## Select the seam

- Assert observable behavior through the durable caller interface. A test that
  reaches through private structure or searches source text is not behavior
  evidence.
- Prefer the cheapest seam that still exercises the owned behavior: pure unit
  policy first, state/decision matrix next, adapter integration last, bounded
  provider/live wiring only where a fake cannot establish the contract.
- A refactor that preserves the interface should not require rewriting behavior
  tests.

## Make RED meaningful

- Name the production change that should make the test fail and confirm that it
  fails for that reason.
- Derive each independent expectation from the contract or a separately
  reasoned fixture. Never reproduce the production algorithm in the assertion.
- Make the test mutation-sensitive: realistic wrong branches, missing side
  effects, duplicate effects, stale identities, and invalid transitions must be
  capable of turning it red.

## Use doubles honestly

- Mock only slow, nondeterministic, privileged, or external adapters. Keep
  domain policy, transitions, owned persistence, and cleanup semantics real.
- Test doubles return complete contract-shaped data. Do not omit inconvenient
  fields that production always supplies.
- A mock-only call assertion is not outcome evidence. Assert the resulting
  state, value, emitted record, or public error as well.

## Prove GREEN

- Run the focused test, its affected deterministic matrix, then the smallest
  integration boundary. Keep provider/OS acceptance bounded.
- Count never-executed statements in the denominator. Report exclusions and
  adapter gaps explicitly; do not hide them to reach a target.
- Refactor only while green and rerun the same evidence afterward.
