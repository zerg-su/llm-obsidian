# Upstream skill library comparison

Refreshed: 2026-08-01. Subjects: `obra/superpowers` and `mattpocock/skills`, the
two third-party Claude Code skill libraries pinned under
`references/upstream-skills/`.

This is the reusable integration guidance for those pins. It records where the
two libraries agree, where they disagree, where LLM Obsidian already enforces
more than either states, and which upstream material is deliberately not
imported. `references/upstream-skills/README.md` owns the pin table and the
mechanical upgrade procedure; this file owns the judgement.

## Method

The pinned snapshots are the primary source. Every claim below cites the exact
snapshot file, which is byte-verified against `manifest.json` by
`references/upstream-skills/verify_snapshots.py`. Because the pin records an
upstream commit, each snapshot path has a stable upstream URL — for example
`skills/writing-skills/SKILL.md` in the superpowers pin is
`https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/writing-skills/SKILL.md`.

> **Live-upstream verification, 2026-08-01: no default-branch drift.** Protected
> research captured each official `main` history with a full HEAD SHA. The
> `obra/superpowers` HEAD was
> [`44c9b2d6e889982ac18c27d05a19fefe335194e1`](https://github.com/obra/superpowers/commit/44c9b2d6e889982ac18c27d05a19fefe335194e1)
> and the `mattpocock/skills` HEAD was
> [`2ab958093e83e0ec752e6c1c5932da465bf23e0c`](https://github.com/mattpocock/skills/commit/2ab958093e83e0ec752e6c1c5932da465bf23e0c),
> exactly matching both pins. The post-pin commit range is therefore empty, so
> none of the approved general-practice axes below has live drift and no snapshot
> or manifest update is warranted. Official mutable-head evidence:
> [Superpowers `main` history](https://github.com/obra/superpowers/commits/main),
> [Matt Pocock Skills `main` history](https://github.com/mattpocock/skills/commits/main).

The same protected capture checked release state separately from pin identity.
`obra/superpowers` showed latest release/tag
[`v6.2.0`](https://github.com/obra/superpowers/releases/tag/v6.2.0), dated
2026-07-24 at abbreviated commit `3dcbd5c`; `mattpocock/skills` showed latest
release/tag [`v1.1.0`](https://github.com/mattpocock/skills/releases/tag/v1.1.0),
dated 2026-07-08 at abbreviated commit `d574778` ([tags](https://github.com/mattpocock/skills/tags)).
Both release tags predate the captured 2026-07-28 HEADs. The packet did not
expose the tags' full commit SHAs, so it does not make a stronger ancestry claim;
that limitation does not affect the exact `HEAD == pin` no-drift result.

Two rules bound the extraction:

- **General practice only.** A rule is in scope when it survives removal of the
  upstream project's own tooling. Issue/PR mechanics, `gh` invocations, git
  worktree and branch-finishing procedure, installers, and plugin/marketplace
  wiring are out of scope by construction, not by preference.
- **A quoted upstream rule is evidence, not authority.** Adoption still passes
  through the normal repository contracts: `skills/improve-skills/references/quality-model.md`
  for skill quality, the dispatch/review/reap lifecycle for orchestration, and
  `docs/skill-references/failure-repair-contract.md` for repair.

## Pin state

| Snapshot | Upstream commit | `package.json` | `.claude-plugin/plugin.json` |
|---|---|---:|---:|
| `obra-superpowers/` | `44c9b2d6e889982ac18c27d05a19fefe335194e1` | 6.2.0 | 6.2.0 |
| `mattpocock-skills/` | `2ab958093e83e0ec752e6c1c5932da465bf23e0c` | 1.1.0 | 1.2.0 |

The mattpocock pin declares two different versions in two different upstream
files at the same commit, and its `CHANGELOG.md` top section is `1.1.0`. The
commit SHA — not any declared version — is the identity of the pin. Both numbers
are now recorded so an upgrade review cannot mistake one for a stale capture.

Snapshot bytes verify clean at this refresh: `mattpocock-skills` 141 files /
397020 bytes, `obra-superpowers` 95 files / 1174353 bytes.

### Retained-snapshot gaps

Two items the mattpocock `CHANGELOG.md` announces are absent from the retained
tree, so a downstream adapter cannot read the upstream prose for them:

- **Negative Space**, added alongside Negation as a Steering failure mode, is
  described only in `mattpocock-skills/CHANGELOG.md`; neither
  `skills/productivity/writing-great-skills/SKILL.md` nor its `GLOSSARY.md`
  carries the promised entry.
- **`docs/invocation.md`**, claimed by the 1.0.0 taxonomy rename, is not in the
  retained `docs/` tree. The user-invoked/model-invoked split is stated in
  `README.md` instead.

Treat `SKILL.md` as authoritative over the changelog and docs pages where they
disagree.

Both gaps remain current on the default branch: live `HEAD == pin` proves there
is no intervening tree in which upstream could have added either missing entry.
They remain first checks after a future HEAD change.

## Where the libraries differ

### Steering: prohibition versus positive form

The two libraries reach opposite defaults. `mattpocock-skills` names **Negation**
as a failure mode — a prohibition drags the forbidden behaviour into context and
makes it more available, so prompt the positive
(`skills/productivity/writing-great-skills/SKILL.md`). `obra-superpowers`
instead matches form to the failure: a *discipline* failure earns a prohibition
plus a rationalization table, while a *wrong-shaped output* earns a positive
recipe, and it reports prohibitions measurably backfiring on shaping problems
(`skills/writing-skills/SKILL.md`).

Our position is already the conditional one and stays: `quality-model.md` says
state the desired action positively, retain hard prohibitions where they protect
permissions, safety, lifecycle, or external effects, and pair each prohibition
with the safe action. The superpowers framing supplies the *reason* the
condition is drawn there, and is the better citation for it.

Adopt additionally: superpowers' ban on **nuance and exemption clauses**
(`skills/writing-skills/SKILL.md`). "Don't X unless it matters" reopens
negotiation, and an exemption does not scope the way its author expects. This is
a distinct, checkable defect our pruning pass does not yet name.

### Context budget: prose numbers versus an enforced closure

superpowers states numbers — SKILL.md under 500 lines, frequently-loaded skills
under 200 words, references one level deep, never `@`-link because it force-loads
(`skills/writing-skills/SKILL.md`, `skills/writing-skills/anthropic-best-practices.md`).
mattpocock states a three-rung ladder and the sharper claim that a pointer's
*wording*, not its target, decides whether the agent reaches the reference
(`skills/productivity/writing-great-skills/SKILL.md`).

LLM Obsidian already enforces more than either states. `scripts/check-skill-budget.py`
computes a **closure**: a skill's body plus every `references/*.md` it mentions,
counted unless the mentioning line marks the pointer `context:conditional`. That
makes mattpocock's wording claim machine-checkable and supersedes the prose
numbers. No change needed; record it so a future upgrade review does not import
the weaker rule as an improvement.

### TDD: hard law versus reference material

superpowers keeps TDD as an inviolable workflow — "NO PRODUCTION CODE WITHOUT A
FAILING TEST FIRST", code written before its test is deleted rather than adapted,
and RED and GREEN are both mandatory observed states
(`skills/test-driven-development/SKILL.md`). mattpocock moved the opposite way at
1.1.0: `tdd` is now reference-only, with the refactor stage relocated into
`code-review` (`skills/engineering/tdd/SKILL.md`).

We sit with superpowers, and for a stronger reason than preference: the
failure-repair contract already requires a regression test for every repo-owned
repair. Two upstream test rules are worth adopting because our tests largely
validate scripts and Markdown contracts, where both failure modes are easy to
hit:

- **Name the break.** Before writing a test body, name the production change
  that would make it fail, and say whether that change is a bug or a decision;
  if you cannot name one, redesign (`skills/test-driven-development/writing-good-tests.md`).
- **Do not assert on source text.** Asserting that a file contains a line
  "proves only that the source is the source" — run the artifact and assert its
  effect (same file). Agent-facing documentation is tested through the consuming
  agent's behaviour.
- **Regression red-green.** Write the test, watch it pass, revert the fix, watch
  it fail, restore (`skills/verification-before-completion/SKILL.md`). Without
  the revert you have not proven the test catches anything.

### Review: two axes versus reviewer integrity

mattpocock runs code review on two axes — Standards and Spec — as parallel
sub-agents so their contexts do not pollute each other, and reports findings
side by side, **never merged or reranked**
(`skills/engineering/code-review/SKILL.md`). superpowers instead invests in
reviewer integrity, and this is its strongest material:

- Reviewers are read-only: "do not mutate the working tree, the index, HEAD, or
  branch state in any way" (`skills/requesting-code-review/code-reviewer.md`).
- Treat the implementer's report as unverified claims, and **a stated rationale
  never downgrades a finding's severity** — "left it per YAGNI" is self-grading
  (`skills/subagent-driven-development/task-reviewer-prompt.md`).
- Do not pre-judge the reviewer: a prompt containing "do not flag" or "at most
  Minor" is you sparing yourself a loop
  (`skills/subagent-driven-development/SKILL.md`).
- The fix loop is bounded and terminates explicitly: a round cap, then each
  remaining finding is adjudicated with a written ruling or the task stops
  BLOCKED — **silent discards are forbidden** (same file).
- Re-review is scoped: each finding is ADDRESSED or NOT ADDRESSED, "attempted" is
  not addressed, and out-of-scope observations route to the ledger so they cannot
  extend the loop (`skills/subagent-driven-development/re-review-prompt.md`).

We already run read-only reviewers with a locked-down permission mode, and our
review policy already types severities into auto-resolve and escalate sets. The
gap is the **termination and disposition rule**: our contract says material
findings are applied or rejected, but does not state that a rejection needs a
recorded ruling, nor that "attempted" fails a re-review. Both are cheap
additions to the review lane and are the highest-value adoption in this refresh.

The two-axis split is worth adapting rather than adopting: our review already
fans out cross-model, so the transferable rule is the narrow one — **do not merge
or rerank findings across independent reviewers**, because one axis then masks
the other.

### Delegation: advisory isolation versus an enforced packet

Both libraries converge on context isolation. superpowers states that subagents
"should never inherit your session's context or history — you construct exactly
what they need", that artifacts must be handed over as **file paths** rather than
pasted (one observed dispatch reached 42k characters, 99% pasted history), and
that a controller must track progress in a **ledger** because conversation memory
does not survive compaction; controllers that lost their place re-dispatched
entire completed task sequences (`skills/subagent-driven-development/SKILL.md`).
mattpocock's research skill is the same shape: a background agent, primary
sources only, returning a single cited Markdown file
(`skills/engineering/research/SKILL.md`).

This is convergent validation of what we already enforce rather than advise: the
protected research workflow passes only a minimal ContextPacket plus validated
bounded files, and persists pointers and hashes rather than content. The
ledger-over-memory rule is the one piece we should state explicitly for
multi-task runs, since our own dogfood window is exactly the failure shape it
describes.

Adopt also: **specify the model explicitly per role**, because omitting it
silently inherits the session's most expensive model, tempered by "turn count
beats token price" (same file). Our `config/model-routing.toml` already pins
routes; the rule explains why the pin exists.

### Decomposition: mattpocock is materially stronger

superpowers' planning rules are mechanical and good — no placeholders ("TBD",
"add appropriate error handling", or a reference to an undefined type is a plan
failure), a task is right-sized when a fresh reviewer could reject it while
approving its neighbour, project-wide requirements live in one Global Constraints
header, and each task declares Consumes/Produces with exact signatures because
its implementer sees only that task (`skills/writing-plans/SKILL.md`).

mattpocock adds the shapes superpowers lacks:

- **Seams first.** A spec sketches its test seams before anything else, and
  "the fewer seams, the better — the ideal number is one"
  (`skills/engineering/to-spec/SKILL.md`).
- **Vertical slices and the frontier.** Each slice cuts a narrow complete path
  through every layer, is demoable alone, and is sized to one fresh context
  window; work any ticket whose blockers are done
  (`skills/engineering/to-tickets/SKILL.md`).
- **Expand–contract**, the explicit exception to vertical slicing. A wide
  refactor whose blast radius breaks call sites everywhere cannot be sliced
  green: expand the new form beside the old, migrate call sites in batches sized
  by blast radius, then contract the old form away (same file).
- **Fog versus out of scope.** For work larger than one session, name the
  destination first and then produce decisions, not deliverables; in-scope
  unknowns sit under "Not yet specified" and graduate, closed material sits under
  "Out of scope" and never does; the test for "fog or ticket?" is whether you can
  state the question precisely now, not whether you can answer it
  (`skills/engineering/wayfinder/SKILL.md`).

Adopt "no placeholders in plans" and expand–contract. Both are checkable and
neither touches lifecycle. The fog/out-of-scope split is a good fit for our
`wiki/plans/` pages and is the natural next increment.

### Debugging: both converge, each contributes one gate

superpowers requires root cause before any fix, instruments component boundaries
to find *where* a value breaks before proposing *why*, traces a bad value back to
its origin rather than fixing where the error surfaces, and — the sharpest rule —
stops after **three failed fixes** to question the architecture instead of
attempting a fourth: "this is NOT a failed hypothesis — this is a wrong
architecture" (`skills/systematic-debugging/SKILL.md`,
`skills/systematic-debugging/root-cause-tracing.md`).

mattpocock front-loads a different gate: Phase 1 is building a tight,
red-capable, deterministic, fast, agent-runnable loop with a named command
already run once, and **"no red-capable command, no Phase 2"**; then reproduce
and minimise until every remaining element is load-bearing, rank three to five
falsifiable hypotheses before testing any, and change one variable per probe. A
regression test goes in before the fix only at a correct seam — the absence of a
seam "is itself the finding" (`skills/engineering/diagnosing-bugs/SKILL.md`).

Adopt both gates for `/debug`: the Phase 1 entry gate and the three-fix
architectural breaker. They bound the loop at both ends and neither presumes a
tool.

### Prototypes and provenance

mattpocock treats a prototype as throwaway code that answers one design
question, then captures the prototype itself as a **primary source** and records
the verdict together with the question it settled
(`skills/engineering/prototype/SKILL.md`). That maps directly onto our
`Source-First Synthesis` rule and our `sessions:`/wikilink provenance, and is
worth stating in `/prototype`: the durable artifact is the answered question, not
the code.

## Not imported

The following upstream material is deliberately excluded. It is tool-coupled, or
it contradicts a repository-owned contract.

| Excluded | Reason |
|---|---|
| Issue/PR trackers, `triage`, PR-as-request surfaces, labels, sub-issue links, assignee-as-claim | GitHub/GitLab workflow mechanics; we have no issue tracker in the loop |
| `gh` invocations, including the GitHub review-thread reply path | External service effect outside our contracts |
| `using-git-worktrees`, `finishing-a-development-branch`, branch/merge/push/PR procedure | Worktree and branch lifecycle is coordinator- and harness-owned here |
| Installers and plugin wiring: `setup-matt-pocock-skills`, `claude plugins install`, `npx skills add`, marketplace files | Our install path is `bin/setup-vault.sh` plus the Codex adapter |
| Upstream workspace tooling: `.superpowers/sdd/`, `scripts/sdd-workspace`, `task-brief`, `review-package` | Adopt the concepts against our scripts, never the binaries |
| `docs/superpowers/specs/` and `plans/` path conventions | Collides with vault-owned `wiki/plans/` and the `scripts/vault-write.py` mutation path |
| Handoff and architecture reports written to the OS temp directory | Our vault is the durable store; a temp-dir artifact discards provenance. Keep the redaction and "reference, don't duplicate" rules |
| `claude --bg` and any non-interactive Claude for task splits | Contradicts the standing interactive-cmux reviewer rule |
| "Never pause between tasks; execute without stopping" | Contradicts the pre-flight question budget and the failure-repair contract, where a background task raises `mechanism-failure` and waits |
| `ask-matt` as a router skill | We already have `getting-started` plus the skill-router hook; a second index is duplication |
| "Announce at start: I'm using the X skill" | Narration noise; the router already owns selection |
| Absolute ban on gratitude or acknowledgement wording | Style policing, and by superpowers' own match-the-form rule a prohibition list is the wrong shape for it. Keep the substance: no performative agreement, verify a finding against the codebase before implementing it |
| Browser/CDN report rendering and the visual companion server | External asset fetches; our Artifact path already covers visual output |
| Per-harness tool-name references and harness-porting guides | Our runtime matrix is `docs/runtime-capabilities.md` |

## Adoption ledger

Ranked by value, each item scoped so it changes guidance rather than lifecycle.

| # | Adoption | Target | Upstream source |
|---:|---|---|---|
| 1 | Rejected findings need a recorded ruling; "attempted" is not addressed; no silent discards | review lane | superpowers `subagent-driven-development` |
| 2 | Do not merge or rerank findings across independent reviewers | review lane | mattpocock `code-review` |
| 3 | Phase 1 gate: no red-capable command already run once, no diagnosis | `/debug` | mattpocock `diagnosing-bugs` |
| 4 | Three-fix architectural breaker | `/debug` | superpowers `systematic-debugging` |
| 5 | Name the break; no source-text assertions; regression red-green by revert | `/tdd`, verification | superpowers `test-driven-development`, `verification-before-completion` |
| 6 | No placeholders in plans | `/design`, plan capture | superpowers `writing-plans` |
| 7 | Expand–contract as the explicit exception to vertical slicing | `/design` | mattpocock `to-tickets` |
| 8 | Ban nuance and exemption clauses | `quality-model.md` pruning pass | superpowers `writing-skills` |
| 9 | Ledger over memory for multi-task runs | dispatch lane | superpowers `subagent-driven-development` |
| 10 | A prototype's durable artifact is the answered question | `/prototype` | mattpocock `prototype` |

Nothing in this ledger is applied by this refresh. Each is a bounded, separately
reviewable change to one skill or contract, and items 1 and 2 touch the review
lane, so they need their own regression coverage.

## Re-pin decision

The snapshots are **not** re-vendored because the protected live check proved
that both selected upstream `main` HEADs are already the exact pinned commits.
The retained bytes and `manifest.json` are therefore the correct mechanical
state; rewriting either would create churn without a new upstream identity.

Every existing adopt/adapt/reject disposition above remains unchanged because
the post-pin range is empty. The next refresh should repeat the exact HEAD and
release checks, then inspect only approved general-practice changes if a HEAD
moves. Re-pin when a changed upstream commit alters retained evidence, not merely
because time passed or a release label differs from a package declaration.
