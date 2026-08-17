---
name: architecture
description: "Orchestrate project architecture artifacts and the next design frontier. Use for new or continuing whole-system architecture and unresolved project design."
allowed-tools: Skill Read Glob Grep Bash AskUserQuestion
---

# Architecture

Read [the architecture artifact contract](../../docs/skill-references/architecture-artifacts.md)
completely before acting. This skill owns project artifact orchestration and the
Design Frontier; it does not replace any reasoning or delivery carrier.

Stay read-only during discovery, reasoning, handoffs, and semantic acceptance.

## Resolve and inspect

Resolve the active project in this order: an explicit project from the user,
then one exact contextual match, otherwise clarify. Never guess by recency. A
missing project space may be proposed but is not created implicitly.

Read the complete artifact graph, including the hub; accepted/draft/review and
superseded pages; artifact revisions, `upstream`, and `upstream_pins`; decisions
and plans indexed from the hub; Open Questions / Fog; Explicitly Out of Scope;
Upstream Gaps; and derived freshness findings.

Fail closed if a recovery journal affects the project or the Work Graph/Work
Item projection is partial or inconsistent. Do not consume or summarize the
partial projection as current knowledge. Report the exact recovery/consistency
condition and stop the dependent path.

## Advance one frontier concern

Select the highest-value bounded unresolved concern on the Design Frontier.
Prefer the concern that unlocks declared outcomes/evidence or retires the most
material risk. Do not impose a fixed document phase order and do not fill every
artifact role mechanically.

Make exactly one explicit handoff according to the unknown:

- outcome, constraint, or non-goal ambiguity → `clarify`;
- bounded alternatives, invariants, ownership, state, or failure reasoning →
  `design`;
- external factual uncertainty → `research`;
- one falsifiable empirical uncertainty → `prototype`;
- mapping accepted design onto an existing codebase → `codebase-design`;
- intent/design consistency judgment → `review`.

Give the invoked carrier the relevant accepted context, exact unresolved
question, authority boundary, and expected return artifact. Collect its result
back into project context; never silently promote a proposal, assumption, or
ADR candidate to accepted authority.

Present the user-facing delta: what was learned or decided, affected artifacts
and pins, remaining Fog, freshness impact, and the recommended next frontier.
Request conversational semantic acceptance. Semantic acceptance is not
persistence, causes zero writes and address effects, and never uses
ExitPlanMode.

## Persist only after separate authorization

Persistence requires separate explicit persistence authorization after the
user accepts the semantic delta. Before ACCEPT, validate every proposed project
key, title, collision, role destination, and containment with
`scripts/architecture_paths.py`; repeat the validation before MATERIALIZE or
any writer call.

After authorization only:

1. search before create and re-read every update target;
2. require durable/resolvable authoritative upstream pages and a total current
   pin mapping;
3. allocate each new DragonScale address and attach current session provenance;
4. render all creates/updates, with `expected_sha256` on every update;
5. submit one bounded transaction through `scripts/vault-write.py` and never
   edit wiki pages, log, or hot directly;
6. on an optimistic conflict, re-read and re-draft; never overwrite;
7. after a writer interruption, require roll-forward recovery and consistent
   projection before any further consumption.

## Authority boundary

Architecture may map state, choose the next concern, and orchestrate handoffs.
It must not decide alternatives or invariants in place of `design`, and must
not silently accept decisions. It must not perform work decomposition. It must
not fan out an approved plan or acquire Execution Frontier authority.
Architecture must not perform implementation planning.

When downstream work exposes an unresolved architecture/spec/contract concern,
record or surface the Upstream Gap and return it to the owning artifact. Never
repair the upstream issue inside a downstream artifact.
