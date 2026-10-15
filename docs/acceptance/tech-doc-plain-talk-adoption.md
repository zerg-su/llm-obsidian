# `tech-doc` and `plain-talk` skills: adoption record

Date: 2026-10-15. Plan: `flickering-splashing-zebra` (approved by the owner).

## Owner decisions

| Decision | Repository rule it overrides | Reason |
|---|---|---|
| Add both skills without a RED/GREEN evaluation | `docs/ru/extending.md`: fresh-context baseline must fail 3 of 4 cases. Precedent: `document-project` rejected in 2.6.3 | Owner request. The goal is a consistent house style, not a repair of a baseline failure |
| Description budget 7500 → 7800 bytes (`scripts/check-skill-budget.py`) | Registry cap is an owner boundary | Other skill descriptions stay unchanged. Registry after the change: 41 skills, 7767 bytes |
| Always-on reply style stays in the owner vault | — | Upstream `CLAUDE.md` and `AGENTS.md` change only in the skill list, because RC4 evidence pins them. A vault that wants the style on every reply adds one line that points to `docs/skill-references/plain-style.md` |

Because no RED/GREEN evaluation ran, this record makes no claim of a
behavioral improvement over the no-skill baseline.

## Sources and what was taken

| Source | Taken | Not taken |
|---|---|---|
| ASD-STE100 Issue 9 | Sentence length 20/25, one instruction per sentence, active voice, no semicolons, common words, one term = one meaning | The full 900-word dictionary |
| Karpathy, X, 2026-10-02 | "80% of the way to ASD-STE100" as the target | The cheat sheet image (it has dictionary errors) |
| S1000D, ATA iSpec 2200, QRH | Applicability header, WARNING/CAUTION/NOTE before the step, step → expected result → deviation, quick reference first | Data-module XML |
| GOST 2.601 / 2.610 | Infinitive form for Russian operating documents | Formal document set |
| `petergyang/no-ai-slop` (MIT) | Word and phrase lists, 16 patterns, audit mode (quote → rule → fix), "What changed" list | Voice preservation, robotic-rhythm check, satire mode. `just` removed from the empty-phrase list: in technical text it often means "only" |
| `v2.6.3-documentation-quality-contracts.md` | Linked from `doc-structure.md`, not copied | — |

## Carriers

- `docs/skill-references/plain-style.md`: single source of the style and of
  the machine-read word lists.
- `skills/tech-doc/` (SKILL.md, `references/doc-structure.md`, `agents/openai.yaml`).
- `skills/plain-talk/` (SKILL.md, `agents/openai.yaml`).
- `skills/tech-doc/scripts/doc_lint.py` + `tests/test_doc_lint.py` (`make test-doc-lint`).
  It is a tool of the skill, so it lives in the skill directory, like
  `skills/improve-skills/scripts/audit_skills.py`.
- Router rules and cases, body baseline entries, `docs/ru/skills.md` rows,
  `CLAUDE.md` skill line.

## Fresh-context smoke runs

Each run used a sub-agent that read only the skill files. These runs check
that the instructions can be followed. They are not a RED/GREEN evaluation.

| Skill | Task | Result | Instruction defects reported → fixed |
|---|---|---|---|
| `tech-doc` | English Redis runbook from a fact sheet | doc-lint 0 errors, 13 facts marked unverified | 10 → unverified mark, default reader, omitted sections, C-x template, mid-procedure signal word, host-name marks, single-language header, conditional contract pointer, redaction scope, lint limits |
| `plain-talk` | Russian rewrite + English audit | All facts kept, doc-lint 0 errors, 12 audit findings | 8 → unsupported puffery, length unit, optional next step, label language, audit language, term forms, synonym doubt |

The runs also found two doc-lint gaps. Both are fixed with tests. Banned
words now match `-s/-ed/-ing` forms such as `empowers`. Section 4 phrases are
in `lint:patterns-en` and `lint:patterns-ru`.

## Checks

Focused checks pass on the release tree: `test_doc_lint.py`,
`test_skill_router.sh`, `test_russian_documentation.py`, `test_skill_budget.py`,
`audit_skills.py --strict` and `--verdicts`, `test_improve_skills.py`,
`test_engineering_eval_runner.py`, `test_rc4_opus_findings.py`,
`lint-instructions.py`, `test_codex_adapter.sh`, `codex-adapter.py --check`.
The full `make test` suite was not run.
