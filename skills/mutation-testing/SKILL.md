---
name: mutation-testing
description: Use after TDD to test green verifiers with isolated, language-agnostic behavioral mutations.
---

# Mutation Testing

Challenge the tests of an approved, implemented behavior after TDD is green.
Keep the original outcome and acceptance criteria: this bounded check adds
evidence about test sensitivity, not product correctness or task completion.
Standalone requests authorize local mutation experiments; strengthen original
tests only within existing edit authorization. Never mutate original artifacts.

## 1. Bind the experiment

Read the changed production artifacts and their existing tests. Discover the
canonical verification command from project instructions, TDD evidence, build
scripts and CI; choose the smallest command that executes the relevant behavior.
Inspect what it runs: commands must use the copy, local test resources and
existing dependencies. A directory copy is not an OS/network sandbox. Live
services, deployments, writes through absolute paths and provider calls need
their existing authorization and isolation; otherwise report a blocked seam.

Record the behavior, files, command argv, expected assertion/result, runtime
dependencies, and budget. Start with 3–8 high-value mutations across the scoped
change, fewer when justified; record omissions instead of expanding per file.
Mutation engines already available in the project are optional accelerators.
Do not install a framework or introduce a test runner for this check.

## 2. Capture and run

Use this skill's `scripts/run_mutations.py`; resolve its absolute path from the
loaded skill directory, independently of the target project's current directory.
Read [execution.md](references/execution.md) for the plan format and commands.
It copies current tracked and nonignored untracked bytes, including dirty edits
and deletions, into a private directory outside the source; Git index stays put.
Explicitly include ignored dependencies needed by the verifier and disclose any
unrelated exclusions. Never use stash, checkout/reset or an original-tree edit
as a substitute for a snapshot. If capture is unsupported, report the constraint.

The runner validates the plan, requires a GREEN baseline, makes one fresh copy
per mutation, changes one artifact, records verifier output, discards that copy,
then runs an unmutated final control. Tests stay identical throughout the batch.
Baseline/control failure or source drift invalidates the batch; diagnose before
retrying. Interrupted experiments remain incomplete; original files need no undo.

## 3. Choose behavior-changing mutations

For each mutation, state the plausible defect and observation a good test should
reject before running it. Change one decision, boundary, default, argument,
return value, required side effect or guard; prefer realistic defects to random
operator swaps. Keep tests and verification configuration unchanged.

Code, shell, SQL and configuration are eligible when an existing consumer test
observes their effect. For prompts/documents/workflows, exercise consumer behavior
through the existing harness; source grep or schema validity alone is not evidence
of a changed outcome. Without that seam, report untested behavior, not a pass.

## 4. Classify evidence

Read each log and confirm the executed copy/target and expected behavioral failure.
Runner `passed`/`failed`/`timeout` fields are observations, never a semantic verdict.

| Verdict | Required evidence |
|---|---|
| KILLED | Baseline/control GREEN; the intended changed behavior causes the relevant assertion or observable contract check to fail. |
| SURVIVED | Valid behavior-changing mutant executes and the relevant verifier remains GREEN. Name the missing observation. |
| EQUIVALENT | Explain why behavior is unchanged within the declared contract; a surviving mutant alone does not prove equivalence. |
| INVALID | Mutant cannot execute the intended behavior, e.g. syntax, compile or import failure. |
| INCONCLUSIVE | Timeout, flaky/unrelated failure, uncertain target execution, interrupted run or insufficient evidence. |

Report counts for all verdicts, skipped scope, and each surviving behavior.
An optional `KILLED / (KILLED + SURVIVED)` ratio describes this selected sample
only; zero denominator is N/A. Never label it project coverage or chase 100%.

## 5. Close or strengthen

For an authorized test improvement, use the TDD skill's test-quality reference.
Add the smallest behavioral assertion, preserving the intended production code.
Capture a new batch: prove the strengthened test GREEN on the original and RED
for the formerly surviving mutant, then run the affected original integration
checks. In report-only scope, return the proposed missing tests instead.

Finish with source-manifest/plan identities, exact commands, control outcomes,
per-mutation classifications and log paths, source-unchanged result, exclusions,
survivors and unresolved evidence. Preserve the private evidence directory until
the report is consumed; remove only that owned directory when cleanup is wanted.
Green tests, collected evidence and a high sample score do not close the parent's
delivery/review gates. Return this report to the existing TDD/review coordinator;
do not create a separate dispatch/reap lifecycle or invoke new provider sessions.
