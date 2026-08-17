# Architecture Workflow v1 pressure set

## Purpose and boundary

This pressure set is the behavioral release contract for Architecture Workflow
v1. It exercises the carrier and authority boundaries in cases A-S without
turning the vault into runtime state. R0 specifies the scenarios; R2-R4 add
deterministic semantic proxies; R6 executes every case in a fresh context and
records the results; R7 repeats the read-only subset as dogfood.

The local artifact contract and approved release plan are authoritative. The
conceptual lineage is bounded to arc42/C4 for whole-system views, RFC/KEP and
MADR for bounded decisions, OpenSpec and Spec Kit for behavior and traceability,
and the vendored Superpowers/Matt Pocock references for conversational
acceptance, frontier thinking, and seams-first decomposition. None is a runtime
dependency.

## Execution contract

Each case runs in a fresh Claude or Codex session, or a Harness-launched
ephemeral lane, against one frozen `subject_head`. The record is committed at
`docs/acceptance/evidence/architecture-workflow-v1/pressure/<case-id>.json`,
where `<case-id>` is the lowercase letter. A session id may appear in only one
case record. Mutating and zero-effect cases run against a disposable fixture
vault, never this vault. Read-only cases may inspect a synthetic project fixture
but must leave both the product worktree and coordinator vault unchanged.

Every record has this shape:

```json
{
  "schema_version": 1,
  "case_id": "a",
  "prompt": "exact prompt delivered to the fresh session",
  "expected_carrier": "architecture",
  "observed_outcome": "bounded factual outcome",
  "verdict": "pass",
  "subject_head": "40-character Git object id",
  "effect_mode": "read-only",
  "fixture_vault": null,
  "pre_state_sha256": "64 lowercase hex characters",
  "post_state_sha256": "64 lowercase hex characters",
  "execution_provenance": {
    "session_id": "fresh non-empty identity",
    "timestamp": "RFC 3339 UTC timestamp",
    "route": {
      "runtime": "codex",
      "model": "exact routed model",
      "effort": "exact routed effort",
      "routing_sha256": "64 lowercase hex characters"
    },
    "prompt_sha256": "64 lowercase hex characters",
    "transcript_path": "docs/acceptance/evidence/architecture-workflow-v1/transcripts/a.md",
    "transcript_sha256": "64 lowercase hex characters",
    "harness_operation_id": null,
    "harness_receipt_id": null
  },
  "assertions": ["observable assertion established by the run"]
}
```

`effect_mode` is one of `read-only`, `zero-effect`, or `fixture-mutation`.
Every record carries pre/post hashes. They must match for `read-only` and
`zero-effect`; a zero-effect validation case still uses a disposable fixture.
`fixture_vault` is a normalized disposable-root identity for every
`zero-effect` or `fixture-mutation` case and is `null` only for read-only cases.
Transcript paths are deterministic, repo-relative, sanitized, digest-addressed
evidence. Harness ids are required only when Harness executed the case and must
both be absent otherwise. The registered route and the transcript metadata must
agree with the case record. Missing, duplicated, malformed, replayed, or
cross-subject provenance fails the R6 validator.

## Scenarios

### Case A — new-project architecture routing

- Prompt: `Спроектируй архитектуру нового проекта Atlas.`
- Setup: no Atlas project space; synthetic outcome context only.
- Expect: `architecture` resolves or clarifies the project, maps the Design
  Frontier, and proposes the next bounded concern without creating pages.
- Effect: read-only. Evidence: E2, E5, E10. Packages: R2, R5, R6.

### Case B — bounded concern stays with design

- Prompt: `Разберём recovery model Atlas и сравним варианты ownership state.`
- Setup: accepted synthetic Atlas architecture context.
- Expect: `design`, not `architecture`, owns alternatives and invariants.
- Effect: read-only. Evidence: E2, E5, E10. Packages: R2, R5, R6.

### Case C — external uncertainty routes to research

- Prompt: `Исследуй внешние гарантии idempotency у выбранного API для Atlas.`
- Setup: a bounded external-fact gap in synthetic project context.
- Expect: `research`, not either new carrier, owns external evidence.
- Effect: read-only. Evidence: E2, E10. Packages: R2, R6.

### Case D — empirical uncertainty routes to prototype

- Prompt: `Проверь прототипом, переживает ли этот lock crash/restart.`
- Setup: a falsifiable technical uncertainty in synthetic context.
- Expect: `prototype` owns the disposable experiment.
- Effect: read-only product/vault state. Evidence: E2, E10. Packages: R2, R6.

### Case E — accepted architecture enters decompose MAP

- Prompt: `Архитектура Atlas принята; разбей доставку на Work Items.`
- Setup: accepted durable synthetic architecture/spec/contract graph.
- Expect: `decompose` enters MAP and drafts a Work Graph without persistence.
- Effect: read-only. Evidence: E3, E5, E10. Packages: R3, R5, R6.

### Case F — decomposition acceptance has zero writes

- Prompt: `Принимаю эту декомпозицию.`
- Setup: MAP draft with valid Work Items in a disposable fixture vault.
- Expect: conversational ACCEPT only; no files, addresses, or writer effects.
- Effect: zero-effect fixture with equal hashes. Evidence: E3, E10. Packages:
  R3, R6.

### Case G — separate authorization materializes atomically

- Prompt: `Сохрани принятую декомпозицию Atlas в вики.`
- Setup: accepted MAP plus durable upstream pages in a disposable fixture.
- Expect: MATERIALIZE performs one bounded `vault-write.py` transaction for
  Work Graph and all Work Items, allocating addresses only after authorization.
- Effect: fixture-mutation. Evidence: E3, E8, E10. Packages: R1, R3, R6.

### Case H — one accepted Work Item enters implementation-plan

- Prompt: `Составь implementation plan для [[Atlas WI-001 — Recovery]].`
- Setup: one accepted, current, valid, durable Work Item and upstream graph.
- Expect: `implementation-plan` accepts exactly that one bounded outcome.
- Effect: read-only. Evidence: E4, E5, E10. Packages: R4, R5, R6.

### Case I — multiple outcomes return to decompose

- Prompt: `Составь один implementation plan сразу для WI-001, WI-002 и WI-003.`
- Setup: three independent accepted Work Items.
- Expect: `implementation-plan` refuses the oversized input and returns it to
  `decompose`; it does not invent one mega-plan.
- Effect: read-only. Evidence: E4, E5, E10. Packages: R4, R5, R6.

### Case J — unresolved architecture becomes an Upstream Gap

- Prompt: `Планируй WI-004, хотя ownership recovery всё ещё не решён.`
- Setup: accepted Work Item exposing an unresolved architecture concern.
- Expect: canonical Upstream Gap naming source, blocking reason, affected work,
  and required owner/action; no downstream resolution.
- Effect: read-only. Evidence: E4, E5, E10. Packages: R4, R5, R6.

### Case K — freshness is report-only and gates dependents

- Prompt: `Architecture revision changed from 2 to 3; continue WI-005.`
- Setup: WI pins revision 2 while authoritative upstream is revision 3.
- Expect: derived `needs-review`; unrelated work may continue, but dependent
  MATERIALIZE/implementation-plan must resolve freshness or emit Upstream Gap.
- Effect: read-only. Evidence: E1, E4, E10. Packages: R1, R4, R6.

### Case L — parallelism is derived, not stored

- Prompt: `WI-006 и WI-007 независимы; зафиксируй planning frontier.`
- Setup: two valid WIs with no dependency edge and no real concurrency clash.
- Expect: both appear on the derived frontier without `parallel-safe-with`.
- Effect: read-only. Evidence: E3, E10. Packages: R3, R6.

### Case M — dropped required intent fails loudly

- Prompt: `Прими декомпозицию, в которой обязательный Atlas Spec — Cancel Safety не покрыт.`
- Setup: one accepted required spec absent from coverage/deferred/out-of-scope.
- Expect: ACCEPT fails with the uncovered intent named explicitly.
- Effect: zero-effect fixture. Evidence: E3, E10. Packages: R3, R6.

### Case N — orphan Work Item fails traceability

- Prompt: `Добавь WI-099 без связи с vision, spec, contract или decision.`
- Setup: otherwise valid MAP draft.
- Expect: MAP/ACCEPT rejects the speculative orphan WI.
- Effect: zero-effect fixture. Evidence: E3, E10. Packages: R3, R6.

### Case O — approved-plan fan-out remains split

- Prompt: `План уже утверждён; разбей его на параллельные задачи исполнения.`
- Setup: one approved implementation plan.
- Expect: `split`, not `decompose`, owns execution/file-ownership fan-out.
- Effect: read-only. Evidence: E5, E10. Packages: R5, R6.

### Case P — partial projection fails closed until roll-forward

- Prompt: `Продолжи декомпозицию и спланируй WI после сбоя записи.`
- Setup: disposable fixture where a crash between page replacements leaves a
  detectable `vault-write.py` recovery journal and inconsistent Work projection.
- Expect: architecture/decompose refuse consumption; implementation-plan emits
  Upstream Gap before file/TDD planning; the next writer invocation rolls the
  exact transaction forward, after which consistency can validate.
- Effect: fixture-mutation. Evidence: E3, E4, E8, E10. Packages: R1, R3, R4, R6.

### Case Q — invalid Work DAG fails closed

- Prompt: `Прими и сохрани этот Work DAG.`
- Setup: subcases for dangling target, self-edge, duplicate edge, and cycle,
  plus one valid DAG with a deterministic topological projection.
- Expect: every invalid subcase fails before ACCEPT/MATERIALIZE; the positive
  counter-case yields the declared order.
- Effect: zero-effect fixture for negatives; fixture-mutation only for the
  separately recorded positive projection. Evidence: E3, E10. Packages: R3, R6.

### Case R — non-total upstream pins fail closed

- Prompt: `Прими и материализуй Work Graph с этими upstream pins.`
- Setup: subcases for missing, extra, duplicate, mismatched, malformed, and
  superseded-upstream pins, plus one total current mapping.
- Expect: each invalid mapping fails before freshness consumption and
  persistence; the valid mapping derives current.
- Effect: zero-effect fixture. Evidence: E1, E3, E10. Packages: R1, R3, R6.

### Case S — unsafe project paths and collisions fail closed

- Prompt: `Прими и сохрани новый Atlas project artifact.`
- Setup: subcases for separators, dot segments, traversal, empty/invalid keys
  or titles, leading/trailing punctuation, normalization/casefold collisions,
  cross-project title duplicates, and title-to-alias collisions; one valid path.
- Expect: invalid values fail before ACCEPT/MATERIALIZE and every valid resolved
  destination remains under `wiki/projects/atlas/`.
- Effect: zero-effect fixture. Evidence: E1, E3, E10. Packages: R1, R3, R6.

## R0 proportional check

R0 is documentation and frozen baseline evidence, so its TDD exemption is a
deterministic structural check: exactly one `### Case <A-S>` heading must exist,
with no duplicates or gaps. Behavioral RED/GREEN evidence begins in R1-R4 and
the executed fresh-context corpus becomes release-gating in R6.
