# Skill quality model

This is a behavior-preserving adaptation of Matt Pocock's
`writing-great-skills`, pinned locally at
`references/upstream-skills/mattpocock-skills/` commit
`2ab958093e83e0ec752e6c1c5932da465bf23e0c`. The upstream vocabulary is
retained where it sharpens review; LLM Obsidian's repository contracts remain
authoritative.

## Invocation pass

- Decide whether the skill is model-invoked or explicit-only from its actual
  use, not personal preference.
- For model invocation, make the description state the capability and one
  trigger per distinct branch. Collapse synonymous trigger restatements.
- For explicit-only invocation, keep Claude and Codex metadata aligned.
- Preserve existing trigger coverage unless evidence shows a false positive,
  false negative, or cross-runtime mismatch.

Pass when invocation mode is intentional, cross-runtime metadata agrees, and
each description branch earns its permanent context load.

## Information hierarchy pass

- Keep ordered actions needed on every run in `SKILL.md`.
- Put definitions and conditional details behind a context pointer only when a
  real branch can skip them.
- Word each pointer with the exact condition that requires loading its target.
- Co-locate a concept's rule, caveat, and completion condition.
- Keep shared behavior in one authoritative source.

Pass when the normal path is legible without hiding mandatory material or
duplicating a contract.

## Steering pass

- End every ordered step on a checkable completion criterion.
- Make the criterion exhaustive where partial coverage is a realistic failure.
- Prefer a precise leading word only when it removes repeated explanation.
- State the desired action positively. Retain hard prohibitions when they
  protect permissions, safety, lifecycle, or external effects, and pair them
  with the safe action.
- Treat premature completion as an observed failure: sharpen the criterion
  before splitting a sequence.

Pass when the agent can determine that each step is complete and safety
guardrails still point to the permitted behavior.

## Pruning pass

Classify candidate text before changing it:

| Failure mode | Evidence required | Smallest correction |
|---|---|---|
| Duplication | The same behavior has two authoritative statements | Keep one source and point to it |
| Sediment | A line describes a path, tool, or state that no longer exists | Remove or update the stale line |
| Sprawl | A conditional branch obscures the normal path | Disclose that branch behind a precise pointer |
| No-op | Removing the sentence leaves model behavior unchanged | Delete the whole sentence |
| Negation | A prohibition primes the unwanted behavior without a positive route | Lead with the permitted action; retain the hard guardrail if required |

Absence of proof yields `no-change`, not a cosmetic rewrite.

## Goal-preservation pass

- Name the approved overall input and desired outcome that the skill receives;
  do not derive a replacement goal from its local mechanics.
- Name the skill's permitted local subgoal and explain how it serves that
  unchanged outcome.
- List plausible completion proxies such as a green focused test, clean diff,
  accepted callback, written page, committed report, or completed local step.
- Name the declared outcome evidence required before any outcome-level
  completion claim. Local green may establish one evidence item, but cannot
  silently establish the whole result.
- If the skill cannot preserve the outcome or evidence identity without a
  workflow, authority, permission, or model-call change, use `defer`.

Pass when the local subgoal is explicitly subordinate to the approved outcome,
completion proxies are identified, and required outcome evidence—not local
mechanical success—controls the completion claim.

## LLM Obsidian preservation gate

A quality-only pass preserves:

- user-visible capability and trigger branches;
- read/write and approval boundaries;
- `allowed-tools`, runtime and agent routing;
- cmux placement, dispatch/review/reap lifecycle, and callback schemas;
- vault writer, provenance, retrieval, and protected-research contracts;
- command order where order is part of safety or idempotency;
- error, retry, escalation, and completion semantics.

If a proposed improvement changes any item above, mark it `defer`. Test
coverage does not turn a behavioral change into a quality-only edit.

## Audit record

For each skill record:

```text
skill: <name>
verdict: fix | no-change | defer
passes:
  invocation: pass | <finding-id>
  hierarchy: pass | <finding-id>
  steering: pass | <finding-id>
  pruning: pass | <finding-id>
  goal_preservation: pass | <finding-id>
overall_input: <approved task/contract input preserved by the skill>
overall_outcome: <approved observable outcome preserved by the skill>
local_subgoal: <bounded goal this skill may pursue>
completion_proxies: [<local success that cannot close the outcome>]
required_outcome_evidence: [<evidence required for the outcome claim>]
evidence: <file/section or deterministic finding>
change: <smallest correction, or none>
behavior_proof: <why protected behavior is unchanged>
```

Validate the record with `audit_skills.py --verdicts <file> --strict`. The audit is complete only
when every installed skill has exactly one five-pass verdict record and the
record inventory matches the audited inventory exactly.
