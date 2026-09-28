# Semantic mutation testing: integration and acceptance

Date: 2026-09-28. Scope: the new `mutation-testing` skill, its local copy runner,
TDD handoff, discovery, documentation and focused tests. Existing unrelated dirty
work was outside this change. No mutation framework, provider call or dependency
installation was added. Claude and Codex discover the same canonical skill folder.

## Source comparison and capability decisions

Primary files were fetched through protected research and read completely. URLs
below refer to the fetched `main` versions; immutable upstream commit IDs were
unavailable. Linked engine-specific companion workflows were not audited.
The local instructions and runner are independently written, not vendored text.

| Relevant idea | Source | Disposition and local carrier |
|---|---|---|
| Realistic faults: guards, boundaries, defaults, returns, argument order, side effects | [Honnibal](https://github.com/honnibal/claude-skills/blob/main/mutation-testing.md.txt) | Adopted: skill step 3; forward test found authorization and journal-observation gaps. |
| Green baseline, one fault at a time, 3–8 candidates | Honnibal | Adopted with a smaller total scope: 1–8 per batch, normally 3–8 across the changed behavior rather than per file; helper uses fresh copies and a final control. |
| Clean tree plus `git checkout` restoration | Honnibal | Rejected: capture current dirty bytes and preserve the original index; originals never receive mutants. |
| Diagnose the actual failing assertion, rather than merely observing a failure | Honnibal's diagnostic-quality idea | Equivalent: strict KILLED attribution and exact test/log evidence; no separate Clear/Indirect/Cascading scoring system. |
| Observe consumer behavior, not source text; independent expected outcomes | [Superpowers writing-good-tests](https://github.com/obra/superpowers/blob/main/skills/test-driven-development/writing-good-tests.md) | Adopted: skill steps 3–4 and existing TDD test-quality reference. Shell/config test executes a consumer and checks admission at the boundary. |
| Final conceptual mutation check | Superpowers | Equivalent local carrier with stronger evidence: actual mutation execution instead of a thought experiment alone. |
| Run a new assertion on the mutant and correct original; targeted distinguishing inputs | [Slay](https://github.com/chloebrett/slay/blob/main/.claude/skills/mutation-testing/SKILL.md) | Adopted: same three mutants before/after two assertions; 1/3 then 3/3 killed. |
| MUTATE before REFACTOR, JS/TS branch filters | Slay | Rejected for this integration: run after completed green behavior/refactoring; no language extension filter or committed-diff dependency. |
| Bounded cost, equivalence triage, retained evidence | [citypaul](https://github.com/citypaul/.dotfiles/blob/main/claude/.claude/skills/mutation-testing/SKILL.md) | Adopted: bounded cases/timeouts, justified EQUIVALENT, private report/log retention. Failures/rechecks consume the declared budget. |
| Timeout is inconclusive; survivor severity is not product risk | [Trail of Bits](https://github.com/trailofbits/skills/blob/main/plugins/mutation-testing/skills/mutation-testing/SKILL.md) | Adopted concept in independently authored classification; no upstream prose or CC BY-SA package copied. |
| Mandatory Stryker/mutmut/mewt/muton/cargo-mutants setup | citypaul, Trail of Bits, [secondsky](https://github.com/secondsky/claude-skills/blob/main/plugins/mutation-testing/skills/mutation-testing/SKILL.md) | Rejected from the core; an already available engine is optional and its output does not replace semantic classification. |
| Fixed mutation/coverage percentages | Slay, secondsky | Rejected: report a selected-sample ratio with all excluded verdicts, never project coverage or a universal acceptance threshold. |
| Crypto-specific vector generation and companion agents | [Vector Forge](https://github.com/trailofbits/skills/blob/main/plugins/trailmark/skills/vector-forge/SKILL.md) | Rejected from general core; independent expected behavior is already covered by the local TDD quality contract. |

The fetched Honnibal, Superpowers, Slay and secondsky root licenses are MIT.
citypaul's root MIT notice allows nested overrides; those were not exhaustively
checked. Trail of Bits uses CC BY-SA 4.0. No copied upstream implementation or
substantial instruction text is shipped; links credit conceptual sources.

## Method 1: system skill-creator

Concrete use cases: dirty post-TDD code, shell configuration consumed by a script,
report-only test audit, authorized survivor strengthening, and unavailable
behavioral verification of agent instructions. Discovery stays automatic with a
precise post-GREEN description and strong-intent router patterns.

Normal-path reasoning is in SKILL.md; execution/schema details are in one linked
reference; repeated isolation mechanics are implemented by a stdlib/Git helper.
Mutations and test selection remain agent decisions. File safety and copy disposal
are deterministic. The core does not depend on any mutation engine.

The literal system `quick_validate.py` passed for both `mutation-testing` and
`tdd`. Default Python lacked PyYAML; it was executed with already cached PyYAML
6.0.3 via a process-local import path. Nothing was installed or added to dependencies.

An independent native agent used only the skill, its references and raw fixture
artifacts. It was not given expected findings. First it ran report-only; then it
received a separate fixture-only authorization to strengthen tests. Both source
preservation and observed assertion failures were checked outside the runner.

## Method 2: improve-skills

Protected behavior was established before editing: existing TDD approval and
RED/GREEN contracts, original dirty work, provider/runtime routing, writer paths,
and dispatch/review/reap lifecycle. The new capability and TDD handoff were the
authorized product change; the subsequent quality pass did not alter that scope.

Invocation, hierarchy, steering, pruning and goal preservation passed for both
skills. The exhaustive schema-v1 records are in
[`mutation-testing-verdicts.json`](mutation-testing-verdicts.json).
Each normal step has observable completion evidence; sample score, runner exit
zero and a green baseline are explicitly insufficient to close the parent task.

## Behavioral evidence

Portable, path-normalized receipts/logs with raw-artifact digests are retained in
[`behavioral-evidence.json`](evidence/mutation-testing/behavioral-evidence.json).

| Execution | Observed outcome |
|---|---|
| Independent initial run, dirty index/worktree plus untracked notes | Baseline/control GREEN; boundary KILLED, role guard and journal append SURVIVED. Source bytes, raw index and staged/unstaged state preserved. |
| Independent authorized test strengthening | Two behavioral assertions added; same plan gives 3 KILLED, 0 SURVIVED; original `make check` GREEN. Only the test file changed. |
| Runner tested against three deliberate defects | Disabling baseline rejection, ignoring source drift, and sharing temporary state each failed its corresponding regression assertion. Baseline/final control GREEN. |
| Independent instructions-only scenario | Negated verification duties still passed the substring check. Agent reported INCONCLUSIVE / missing consumer seam, not behavioral SURVIVED or adequacy; source unchanged. |
| Hermetic CLI suite | 15 cases: dirty-state preservation, baseline failure, syntax failure observations, timeout, fresh scratch, deleted/untracked/ignored files, real shell-config consumer, link boundaries, exclusions, source drift, ambiguous/escaping edits, destination ownership and SIGTERM. |

The fresh-scratch regression was first RED, exposing contamination between runs;
each verifier now gets its own temporary directory. The forward evaluator's
evidence-portability observation also led to command/cwd/artifact hash receipts.
These evidence-backed changes were followed by the focused suite and runner
self-mutations; no source-text assertion substitutes for behavioral testing.

## Validation and limits

Passing: 39-skill structural audit; scoped five-pass verdict validation; literal
system validator; 82 router cases; Russian handbook inventory (39 skills); RC3/RC4
documentation contracts; skill workstreams; improve-skills suite; instruction lint;
Codex generated-manifest drift check; focused mutation suite; `git diff --check`.

The whole working tree is not release-green. Pre-existing/unrelated checks remain:

- Skill-body baseline: `save` exceeds its recorded body by 726 bytes / 5 lines.
  Only the new skill and TDD entries were updated; the old violation was not hidden.
- Codex adapter suite: A7 expects a GitHub marketplace source, while the repository
  manifest uses the local `./` source. The other 21 cases pass.
- Broad docs suite: the live `scripts/` ratchet sees 308/307 files and
  116689/116538 lines; this change adds its helper under `skills/`, not `scripts/`.
- Broad code-quality suite reaches a pre-existing release-evidence assertion
  comparing the Codex cachebuster version to the plain release version.
- Release acceptance requires a clean HEAD; existing dirty work and these
  uncommitted changes correctly prevent a release receipt.

The snapshot is file isolation, not an OS security boundary. It requires a local
Git project and an existing verifier that can run from a file copy. Git-dependent
builds, in-scope submodules, external symlinks and missing ignored dependencies
are explicit constraints, not silently approximated. SIGKILL may leave private
scratch/processes; original source is never an undo target. Claims of unchanged
source cover the recorded inventory/index, not excluded/ignored files.
