# LLM Obsidian 2.6.0 skill-quality post-audit

## Scope and identity

- Frozen baseline: `d50990dc9c76b659c82978a0c8644b9bdbd99b29`.
- Integrated skill audit boundary: release 2.6.0 after the paired architecture stop.
- Inventory: `clarify`, `design`, `prototype`, `save-plan`, `debug`, `tdd`, `review`, `reap`, `improve-skills`.
- Verdict evidence: `docs/skill-quality-post-audit-v2.6.0.json`.
- Upstream references remain the pinned Superpowers `writing-skills` and Matt Pocock `writing-great-skills` snapshots; no foreign orchestration was imported.

## Skill identities

| Skill | Integrated SHA-256 |
|---|---|
| `clarify` | `485b26a4ece6c3e41876da61b6682dfd85cd86421531b3116c5c3bb49387d57f` |
| `design` | `22a7e33f03b961461e497266930add09d86d334af2e2c1aca27633ab4ba7d254` |
| `prototype` | `a32d59df117271297db363fba291a26b8ec4b64d780e632cf709f372deefc19d` |
| `save-plan` | `598ecb6e34a1398981964029ca750cc7466ee335b094827929bf0898c6da0b74` |
| `debug` | `a9974bd61add686a7ab4e242a6629a1feb66e999e8ad5b10ef96a118b16157ad` |
| `tdd` | `6a55bd2eaedbc57cfd5fe49b1a9e737b43df515c2ed4494b5046bb05f9ba592c` |
| `review` | `38b024033f8f084669fdf67dba74ff0efbf5ed90878afc9533b7fa8cd2bad36c` |
| `reap` | `a71a177a71ca57711521b0557e9d0f84a65dacd1b98323530d98baf848738a65` |
| `improve-skills` | `6502e6be4d8e35d1a35e7a3d47f829d6a021454de153124ac05059a026eede8b` |

## Finding closure

| Baseline finding | Integrated evidence | Post-audit disposition |
|---|---|---|
| `A-CLA-01` | `skills/clarify/SKILL.md`; `tests/test_skill_workstream_a.py` | resolved |
| `A-DES-01` | `skills/design/SKILL.md`; `tests/test_skill_workstream_a.py`; `docs/acceptance/v2.6-paired-design-architecture-stop.md` | resolved at the general skill boundary; fixture-specific recovery prescriptions were removed after they failed the frozen no-regression gate |
| `A-PRO-01` | `skills/prototype/SKILL.md`; `tests/test_skill_workstream_a.py` | resolved |
| `A-SVP-01` | `skills/save-plan/SKILL.md`; `tests/test_skill_workstream_a.py` | resolved; review minor `HOL-004` is closed by the explicit pre-allocation and pre-writer contract-validation order assertions in `tests/test_skill_workstream_a.py` |
| `B-DBG-01` / Fable `SPEC-004` | `skills/debug/SKILL.md`; `tests/test_skill_workstream_b.py`; `tests/harness/test_engineering_skills.py`; applied review findings `HOL-001` and `HOL-003` | resolved; a missing correct regression seam is architecture evidence and cannot be replaced by a shallow test |
| `B-TDD-01` | `skills/tdd/SKILL.md`; `tests/test_skill_workstream_b.py`; `tests/harness/test_release_transition_matrix.py`; `scripts/harness-coverage-audit.py` | resolved; stateful work now requires a transition inventory, fast exhaustive matrix, complete coverage denominator, and external-adapter-only mocks |
| `C-REV-01` | `skills/review/SKILL.md`; outcome-aware review harness; `tests/test_workstream_c_review_reap.py` | resolved |
| `C-REA-01` | `skills/reap/SKILL.md`; Wiki Summary v2 harness; `tests/test_workstream_c_review_reap.py` | resolved |
| `I-IMP-01` | fifth `goal_preservation` pass, strict verdict schema, `tests/test_improve_skills.py` | resolved |

Every retained edit maps to one frozen finding. Recovery-specific additions from
`b42cff6`, `a33be75`, `430b31f`, and `6dc9128` were not retained: the third
frozen forward test showed that they steered the design toward an executor
boundary contradicted by the inspected runtime. Protected invocation, tool,
writer, permission, routing, lifecycle, callback, and cleanup boundaries remain
in force.

## Goal preservation

The exhaustive schema-v1 verdict set names, for every in-scope skill, the approved input and outcome, the permitted local subgoal, completion proxies that cannot close the user outcome, and required outcome evidence. Eight post-audit verdicts are `no-change`; `debug` is `fix` for the later Fable `SPEC-004` evidence-seam finding. All five quality passes remain green.

Local success remains subordinate to the release Outcome Contract. A green focused test, clean diff, accepted callback, written page, task summary, or successful reap cannot by itself establish the desired outcome.

## Deterministic gates

| Gate | Result |
|---|---|
| `audit_skills.py --strict` | PASS — 32 audited, 0 errors, 0 warnings |
| exhaustive nine-skill verdict validation | PASS |
| `make test-skill-workstreams test-improve-skills` | PASS |
| `make test-instruction-lint test-skill-budget` | PASS |
| `python3 tests/harness/test_engineering_skills.py` | PASS — trigger, completion marker, authorization and loop contracts restored |
| `make test-router test-codex-adapter` | PASS — router 56/56; adapter 22/22 |
| `python3 scripts/codex-adapter.py --check` | PASS — no drift |
| five engineering pressure scenarios | PASS — four in the aggregate run plus the corrected TDD orientation rerun; see `docs/acceptance/v2.6-engineering-skill-pressure.md` |

## Behavioral acceptance boundary

The static post-audit and frozen paired post-change comparison are complete.
`docs/acceptance/v2.6-paired-comparison.json` reports no regression: both cases
remain achieved with all four evidence items established, zero interventions,
unchanged model/review rounds, zero duplicate effects, and no increase in
callback failures. Four real-task dogfood classes and the negative semantic-
drift smoke are recorded separately in
`docs/acceptance/v2.6-real-task-dogfood.md`.

Workstream B's emergency fresh-review boundary exhausted its intentionally zero verification budget after identifying `HOL-003`. The finding was fixed and deterministically proven, but that task has no terminal reap receipt; exact integrated behavior therefore remains subject to the single final deep release review rather than being represented as task-level approval.
