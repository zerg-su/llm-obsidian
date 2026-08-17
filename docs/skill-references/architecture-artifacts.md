# Architecture artifact contract

This is the single authoritative contract for Architecture Workflow v1.
`architecture` and `decompose` read it completely. `implementation-plan` reads
it completely whenever the input carrier is a Work Item. Existing vault,
decision, plan, writer, and Harness contracts remain authoritative where this
reference does not add a narrower project-artifact rule.

## Authority and frontiers

The workflow has three distinct frontiers:

- The **Design Frontier** belongs to `architecture`: the next highest-value
  unresolved project concern and the accepted project knowledge around it.
- The **Planning Frontier** belongs to `decompose`: accepted Work Items whose
  declared dependencies and real concurrency constraints allow planning next.
- The **Execution Frontier** belongs to the existing Harness after an approved
  implementation plan. The wiki is not runtime delivery state.

Architecture orchestrates; `design` reasons about one bounded concern.
`decompose` structures accepted delivery outcomes; `implementation-plan` maps
one bounded outcome to files and TDD slices; `split` fans out an already
approved plan. The invariant across every boundary is: downstream artifacts
may discover upstream problems, but may not silently resolve them. They return
the problem to its owning stage.

## Physical mapping and roles

Project knowledge lives under `wiki/projects/<project>/`, where `<project>` is
the validated project key. Every project artifact keeps the standard required
frontmatter (`type`, `status`, `created`, `updated`, `tags`, `sessions`, and a
DragonScale `address`) and adds:

```yaml
type: project
artifact_role: hub | vision | architecture | design | spec | contract | work-graph | work-item
project_key: atlas
project_display_name: Atlas
artifact_revision: 1
```

Canonical placement is:

- `wiki/projects/<project>/<Project>.md` — the `hub`;
- `wiki/projects/<project>/<Project> Vision.md` — `vision`;
- `wiki/projects/<project>/<Project> Architecture.md` — `architecture`;
- `wiki/projects/<project>/design/` — bounded `design` artifacts;
- `wiki/projects/<project>/specs/` — observable `spec` artifacts;
- `wiki/projects/<project>/contracts/` — owner-boundary `contract` artifacts;
- `wiki/projects/<project>/work/` — the `work-graph` and `work-item` pages.

Decisions remain `type: decision` in `wiki/decisions/`. Executable plans remain
`type: plan` in `wiki/plans/`. A Work Item never goes in `wiki/plans/`.

Roles are deliberately distinct:

- **VISION** states what/why: goals, non-goals, principles, constraints,
  quality goals, and operating context.
- **ARCHITECTURE** is the whole-system overview/map, at an arc42/C4 zoomed-out
  altitude.
- **DESIGN** owns alternatives, invariants, boundaries, state/failure models,
  and trade-offs for one bounded concern.
- **SPEC** states required observable and verifiable behavior.
- **CONTRACT** states obligations at a system/component/owner boundary. SPEC
  and CONTRACT never collapse into one generic kind.
- **DECISION/ADR** records a significant durable choice in the existing
  decision carrier.
- **WORK GRAPH** is an aggregate decomposition map, not the source of edge
  truth after materialization.
- **WORK ITEM** is one bounded delivery outcome.
- **IMPLEMENTATION PLAN** is the existing executable file/TDD carrier for one
  Work Item or one legacy bounded outcome.

The hub is a map, not a content store. It indexes Vision, Architecture, Design,
Specs, Contracts, Work, Decisions, Plans, and Fog with path-free wikilinks.

## Identity and path safety

Identity is the globally unique title (the path-free wikilink target) plus the
existing DragonScale address. There is no artifact_id. Titles are project
prefixed even though folders provide locality; generic names such as
`vision.md`, `README.md`, and `WI-001.md` are forbidden.

The project key is 1-64 lowercase ASCII characters from `[a-z0-9-]`, with no
leading or trailing hyphen. The project display name is the canonical hub page
title/stem and the hub records both display name and key.

An artifact title is non-empty, at most 120 characters, and is either the exact
project display name or begins with that name plus a space. It contains no path
separator, no `..` sequence, no control character, none of `/ \ : | # ^ [ ]`,
and no leading/trailing dot or space.

The vault-wide collision key is `NFC(title).casefold()`. It is checked against
the complete existing WikiCatalog link namespace: page stems/titles and aliases
alike. Cross-project duplicates, normalization/case collisions, and
title-to-alias collisions fail closed. An update excludes only the current
page's own identity entries; it does not hide another owner of the same key.

The bounded validator `scripts/architecture_paths.py` owns the grammar,
collision, role-to-folder mapping, symlink rejection for the project namespace,
and resolved-destination containment under the selected `wiki/projects/<project>/` root. Both carriers invoke it before ACCEPT
and again before MATERIALIZE. `vault-write.py` still supplies generic
`wiki/` confinement, but it is not the project boundary: a path such as
`wiki/projects/alpha/../beta` remains unacceptable even though it stays under
`wiki/`.

## Revisions, pins, and freshness

`artifact_revision` is a positive integer starting at 1. Increment it only for
a material downstream-relevant change; formatting and index churn do not
increment it.

`upstream` is a list of quoted path-free wikilinks naming durable semantic
authorities. `upstream_pins` is a list of `"Title@N"` strings. `@revision`
never appears inside a wikilink. Pins obey a total mapping invariant:

- every durable authoritative upstream has exactly one pin;
- the pin title matches the wikilink title exactly;
- the revision is a positive integer;
- a missing pin, orphan/extra pin, duplicate pin, mismatched title, malformed
  value, or pin to a superseded upstream fails closed.

Legacy adoption is touched-on-use. Never invent a revision for an old upstream.
A separately authorized metadata update makes the upstream revision durable
before any downstream page pins it.

Freshness is derived and report-only; no watcher or carrier mutates status:

- `current`: every pin matches its authoritative upstream revision;
- `needs-review`: authority changed and impact is not yet assessed, including
  a pin to an upstream that has since been superseded;
- `stale`: review confirmed incompatibility or the required authority is no
  longer usable.

`accepted + needs-review` is valid stored state. Unrelated work may continue.
Dependent MATERIALIZE or implementation planning must first review the finding
to `current`/`stale`, or emit an Upstream Gap; it never silently consumes the
old pin.

## Lifecycle, Fog, and scope

Project artifact lifecycle is `draft | review | accepted | superseded`.
Existing decision authority remains `active | accepted`; a superseded artifact
or decision is never authoritative. Lifecycle and freshness are separate axes.

**Open Questions / Fog** contains an essential unresolved question that remains
part of the design frontier. **Explicitly Out of Scope** contains a conscious
exclusion and must not resurface automatically as a gap. Vision and bounded
design artifacts carry both sections even when one is empty.

## Persistence and recovery

`architecture` and `decompose` are read-only while reasoning. Semantic ACCEPT
is conversational only, has zero writes and zero address effects: never ExitPlanMode
or the plan-capture path. Persistence requires a separate explicit
authorization after semantic acceptance.

Authorized persistence follows the complete existing writer protocol:

1. search before create and merge into the exact existing artifact;
2. allocate an address only after authorization;
3. include required frontmatter and current session provenance;
4. validate paths and graph/pin invariants again;
5. use `expected_sha256` for every update;
6. submit all pages in one bounded transaction through
   `scripts/vault-write.py`; never edit wiki pages, log, or hot directly;
7. on optimistic conflict, re-read and re-draft rather than overwriting.

One bounded transaction has journaled roll-forward semantics, not simultaneous
all-pages visibility. A crash between page replacements leaves a detectable
recovery journal. The next writer invocation rolls the exact transaction
forward. Until recovery completes, no project carrier may consume the partial
projection.

Addresses are never preallocated for a MAP or ACCEPT draft. Durable project
artifacts may link and pin only durable/resolvable upstream pages;
conversation-only knowledge must first be adopted through its separately
authorized persistence flow.

## Work Graph and Work Item

`decompose` has three distinct states:

- **MAP** drafts or revises the `<Project> Work Graph`, negotiates Work Item
  boundaries/dependencies, and proves coverage. Individual WI pages need not
  exist and nothing is written.
- **ACCEPT** records conversational semantic agreement only. It has zero writes
  and zero address effects and is not persistence authorization.
- **MATERIALIZE** follows only a separate explicit write authorization. It
  creates/updates the Work Graph and every WI page in one bounded writer
  transaction.

Acceptance of upstream architecture or other project knowledge establishes
input readiness; it does not advance decomposition from MAP to ACCEPT. ACCEPT
requires an existing concrete MAP draft already presented to the user and an
explicit acceptance of that decomposition. A request to begin, build, or revise
the Work Graph remains MAP.

Each Work Item contains Title, Outcome, Why, source artifacts/upstream, Inputs,
Produces, `depends_on`, real Concurrency Constraints when present, and
Acceptance/Evidence. Work Items forbid implementation-level file paths,
function/class names, edit order, and TDD steps.

After MATERIALIZE, each WI's `depends_on` list is the sole authoritative edge
relation. Every value is an exact same-project Work Item page title. Validation
before ACCEPT and MATERIALIZE rejects a dangling target, self edge, duplicate
edge, and every cyclic graph, enforcing an acyclic DAG. A valid graph has a deterministic topological
projection.

The Work Graph's `blocks`, waves, and Planning Frontier are derived aggregates.
There is no stored `parallel-safe-with` graph. Parallelism is derived from the
DAG plus explicit real concurrency constraints.

Bidirectional traceability is mandatory. Every active accepted required
vision/spec/contract/decision intent is covered by one or more WIs, explicitly
deferred, or Explicitly Out of Scope; a silent drop fails. Every WI traces to
accepted authoritative upstream intent; a speculative orphan WI fails.

## Durable consumption

Durable consumption fails closed when any of these holds:

- a durable link/pin would target conversation-only knowledge;
- an outstanding `vault-write.py` recovery journal affects the project;
- Work Graph projection and authoritative WI pages are partial or inconsistent;
- a pin mapping is non-total or malformed;
- consumed authority is superseded, `needs-review`, or `stale`.

`architecture` and `decompose` refuse to consume that state. When the input is a
Work Item, `implementation-plan` reads this contract and the durable WI and
upstream pages before any file/TDD planning. It verifies that the WI is
accepted, project artifacts are accepted, decisions are active or accepted,
pins are total/current, and recovery/projection state is consistent. Otherwise
it emits the canonical Upstream Gap: source artifact/decision, why downstream
work cannot proceed, affected artifacts/work, and required owner/action.
`implementation-plan` remains the response carrier for that planning request:
it addresses the gap to the owning upstream carrier, but does not transfer the
user's planning request, re-route the whole request, answer as the upstream
carrier, or resolve the gap itself.

Active project selection is exact-or-clarify: explicit project, else one exact
contextual match, else clarify. Never guess by recency. A missing project space
may be proposed but cannot be created without explicit persistence authority.

## Conceptual lineage

The local contract owns every rule above; upstream references are concepts only
and never runtime/toolchain dependencies:

- arc42 Introduction/Goals, Building Block View, Decisions, and Quality
  Requirements inform goals, whole-system views, explicit decisions, and
  measurable scenarios.
- C4 System Context and zoom levels inform a valuable whole-system altitude
  without requiring every possible view.
- MADR informs bounded context, drivers, alternatives, outcome, consequences,
  and confirmation while decisions remain in the existing decision carrier.
- the Rust RFC template and Kubernetes KEP template inform explicit rationale,
  alternatives, unresolved questions, non-goals, risks, test plans, and
  lifecycle readiness.
- OpenSpec's artifact graph informs dependency-aware readiness; this contract
  deliberately replaces file-existence completion with accepted durable
  authority, revisions, and report-only freshness.
- GitHub Spec Kit informs requirements coverage and independently valuable
  delivery slices; this contract deliberately keeps file paths and TDD below
  the Work Item boundary.
- the vendored BMAD/Superpowers/Matt Pocock material informs conversational
  refinement, frontier thinking, bounded handoffs, and fix-at-source discipline
  without importing its lifecycle or tools.
