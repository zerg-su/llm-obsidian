---
name: decompose
description: "Build a Work Item DAG from accepted project knowledge via MAP, ACCEPT, and authorized MATERIALIZE. Use before implementation planning."
allowed-tools: Read Glob Grep Bash AskUserQuestion
---

# Decompose

Read [the architecture artifact contract](../../docs/skill-references/architecture-artifacts.md)
completely before acting. Decompose owns delivery decomposition and the Planning
Frontier. It does not own architecture reasoning, file-level planning, execution
fan-out, or runtime delivery state.

Resolve the active project by explicit project, then one exact contextual
match, otherwise clarify. Never guess by recency. Stay read-only through MAP and
ACCEPT.

## MAP

MAP builds or revises the `<Project> Work Graph` draft. It may consume durable
accepted vision, architecture, design, spec, contract, and decision knowledge,
plus explicitly accepted in-context knowledge. Conversation-only knowledge may
shape the draft, but cannot become a durable wikilink or revision pin; persist
and adopt it separately before a durable Work Graph or WI depends on it.

Accepted upstream architecture does not advance decomposition from MAP to
ACCEPT. ACCEPT requires an existing concrete MAP draft presented to the user,
who explicitly accepts that decomposition. Starting, building, or revising the
decomposition remains MAP.

Before mapping, fail closed on an outstanding recovery journal affecting the
project, a partial Work Graph/WI projection, or any projection disagreement.
Validate every durable upstream and `upstream_pins` total mapping: missing,
extra/orphan, duplicate, mismatched, malformed, stale, or superseded authority
does not silently feed decomposition or freshness.

Each Work Item contains exactly the delivery-level information needed for
later planning:

- Title;
- Outcome;
- Why;
- Source artifacts/upstream;
- Inputs;
- Produces;
- `depends_on`;
- Concurrency Constraints, only when a real shared-boundary constraint exists;
- Acceptance/Evidence.

A Work Item must not contain file paths, function/class names, edit order, TDD
steps, or file ownership. Those belong to `implementation-plan`.

Prove bidirectional traceability before acceptance. Every active accepted
required intent/spec/contract/decision is covered by at least one WI, explicitly
deferred, or Explicitly Out of Scope; a silent drop fails. Every WI traces to
accepted authoritative upstream intent; a speculative orphan fails.

## DAG gate

`depends_on` is the sole authoritative relation. Each edge is an exact
same-project Work Item page title. Before ACCEPT and before MATERIALIZE, validate
that every target resolves to a WI in this project, with no dangling target,
self edge, duplicate edge, or cycle; the result must be an acyclic graph with a
deterministic topological projection.

`blocks`, waves, and the Planning Frontier are derived from `depends_on` plus
real Concurrency Constraints. Never store a `parallel-safe-with` relation or
infer safety merely from different wording.

Validate every proposed project key, title, vault-wide collision, role path,
and selected-project containment with `scripts/architecture_paths.py` before
ACCEPT and repeat it before MATERIALIZE.

## ACCEPT

ACCEPT is conversational semantic acceptance of the decomposition. It causes
zero writes, zero address allocation/effects, and never ExitPlanMode. Clearly
state that ACCEPT is not persistence authorization. Retain the accepted MAP in
conversation until the user separately asks to persist it.

## MATERIALIZE

MATERIALIZE requires a separate explicit write authorization after ACCEPT.
Before any effect, require durable/resolvable authoritative upstream pages,
current total pins, a valid DAG, complete traceability, safe paths, no recovery
journal, and a consistent current projection.

After authorization only:

1. search before create and re-read every update target;
2. allocate new DragonScale addresses and attach current session provenance;
3. render the Work Graph and all WI pages together; WI pages own `depends_on`
   and the Work Graph carries only the derived aggregate projection;
4. attach `expected_sha256` to every update;
5. submit one bounded multi-page transaction through
   `scripts/vault-write.py`; never edit wiki pages, log, or hot directly;
6. on optimistic conflict, re-read and re-draft rather than overwriting;
7. if interrupted, stop consumption until the next writer invocation completes
   roll-forward and the projection validates again.

## Return and authority boundary

After MAP or MATERIALIZE, report coverage, WI outcomes and dependencies, the
derived Planning Frontier, constraints, deferred/out-of-scope intent, and any
remaining upstream problem.

If decomposition discovers an unresolved architecture/spec/contract concern,
emit the canonical Upstream Gap (source, blocking reason, affected work, and
required owner/action) and return it to `architecture`. Never resolve it in a
Work Item.

Decompose is not split: `split` fans out one already approved implementation
plan with execution/file-ownership authority. Decompose must not perform
implementation planning, choose files, dispatch work, or maintain pending /
running / delivered runtime state.
