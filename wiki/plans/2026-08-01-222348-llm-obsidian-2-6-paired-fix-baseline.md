---
type: plan
title: "LLM Obsidian 2.6 paired baseline — fix"
address: c-000067
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-01
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-08-01
updated: 2026-08-01
tags:
  - plan
  - manual-save
  - paired-eval
---

# LLM Obsidian 2.6 paired baseline — fix

Run the frozen fix case from `evals/paired-v2.6.0/fix/` through
`clarify → debug → tdd → review`. The pre-existing ASCII checks are green but
do not establish the approved outcome. Work only in this fixture, add the
missing behavioral regression, demonstrate red and green, and preserve the
same route, scoped verification profile, and review budget declared by the
paired manifest. This is evidence-only work; do not merge its product commit.

## Outcome Contract

```json
{"schema_version":1,"purpose":"Measure whether the fix workflow preserves the approved behavior instead of treating pre-existing green checks as completion.","desired_outcome":"On the frozen paired fixture, normalize_label preserves lowercase Unicode letters, collapses unsafe separator runs to one hyphen, trims boundary separators, and returns untitled only when no letters or digits remain.","success_evidence":[{"evidence_id":"fix-unicode","observable":"A new regression covers at least Résumé Plan and Москва 42 and passes without transliterating or dropping their letters."},{"evidence_id":"fix-separators","observable":"Regression evidence covers repeated whitespace, underscore, slash, punctuation, and boundary separators."},{"evidence_id":"fix-original-suite","observable":"The original frozen ASCII checks and the new behavioral checks pass together."},{"evidence_id":"fix-scope","observable":"The product diff is limited to evals/paired-v2.6.0/fix and does not alter harness, skills, schemas, or dependencies."}],"non_goals":["Changing public LLM Obsidian runtime behavior outside the paired fixture.","Adding a dependency or transliteration policy.","Treating the pre-existing green ASCII checks as sufficient completion evidence."]}
```
