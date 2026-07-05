# Q-by-Q Investigation Pattern

Shared reference for any skill that closes batched open questions through systematic evidence gathering.

Extracted from deprecated `researcher` sub-agent (removed 2026-05-23 in agent restructure, iter-2 Plan Step 5).

---

## Philosophy

Half of "questions for lead" close themselves through `git grep` + MCP read-only + context7. Lead is needed only for **policy decisions, approvals, budgets, access escalation**, or things genuinely outside git.

> «Если не нашёл — явно пишу "не найдено после поисков X, Y, Z". Никогда "вероятно".»

---

## Four-status taxonomy

Each question receives exactly one status:

| Status | Emoji | Meaning |
|---|---|---|
| **Answered** | ✅ | Concrete evidence found, question fully resolved |
| **Partial** | ⚠ | Some evidence, but missing critical piece — needs follow-up |
| **Dead-end** | ❌ | Searched exhaustively, no evidence — record the trail explicitly |
| **Needs lead** | 🔵 | Policy / approval / budget / access — explain WHY lead is required, not just «I don't know» |

---

## Per-question block format

```markdown
### Q<id>: <question text verbatim>

- **Status**: ✅ Answered | ⚠ Partial | ❌ Dead-end | 🔵 Needs lead
- **Evidence**: concrete files/lines, MCP outputs, context7 refs, URLs
- **Finding**: what was established (1-3 sentences)
- **Implication**: what this means for the plan / next action
- **Handoff**: <skill or agent> if relevant
```

Compact 5-15 lines per Q. Each Q reads independently — no «see Q12 above» chains.

---

## Investigation tool ladder

Apply in order; stop when evidence sufficient:

1. **Local clones** — `~/Projects/<your-repos>/...` — `git grep -n`, `git log --all --source -S '<term>'`, `git blame`. Use `--all` to include feature branches.
2. **Wiki vault** — `wiki/` in this repo — start with hot.md/index.md, then relevant sections. `address-map.tsv` for `c-NNNNNN` lookup.
3. **Memory** — `~/.claude/projects/.../memory/MEMORY.md` — durable feedback / reference rules.
4. **MCP read-only** — whatever read-only MCP servers are configured, e.g.:
   - cloud provider API — fact-check resources/versions/owners.
   - metrics / logs servers — fact-check runtime behavior.
   - code-hosting / issue-tracker server — PR and issue history, discussions.
5. **context7** — for «is X still current / deprecated?» — official docs.
6. **Web** — WebSearch / WebFetch — only after vault + MCP + docs exhausted.

---

## Report template

```markdown
# Open Questions Research — <YYYY-MM-DD>

## TL;DR
N questions: X answered ✅, Y partial ⚠, Z dead-end ❌, W needs lead 🔵.

## 1. Scope & methodology
- Source question lists (Management_Questions.md / open-questions section / chat)
- Tool ladder applied
- Limitations

## 2. Coverage log (mandatory)
- Repos grep'нутые (list + key commands)
- MCP calls (count per server)
- Files read
- Dark spots (couldn't reach, low confidence)

## 3. Q-by-Q answers
<one block per question, format above>

## 4. Cross-cutting observations
What surfaced during grep'ing that wasn't in the questions but should be flagged.

## 5. 🔵 Needs lead — list with rationale
For each: why specifically lead, what exactly needed (approval / policy / access / budget).

## 6. Reproducibility
Exact commands: grep, MCP calls, context7 queries.
```

---

## Rules

- **Don't invent evidence.** «Не найдено после поисков X, Y, Z» is a valid finding. Never «probably».
- **Every status backed by concrete evidence OR concrete dead-end trail.** Empty evidence = invalid block.
- **🔵 mandates rationale.** Just «нужен лид» — invalid. Must specify: policy decision / signing authority / access escalation / budget approval.
- **`ASSUMPTION:` prefix** for hypotheses inside Finding. Strip before final report or mark explicitly.
- **Coverage log mandatory.** «I checked X, Y, Z; dark spots: A» — verifiable.
- **Handoff with specific ask.** Not «cc @agent», but «@agent: confirm the exact setting for `<component>` since <date>».
- **No implementation work.** Research-only. If finding implies a code/config change → handoff to the relevant skill, don't write code from this skill.

---

## When to use this pattern

- Batch of open questions from a plan / management ask / handoff sheet.
- Pre-meeting prep — close everything closable before bothering humans.
- After incident — close «follow-up questions» before they go stale.

## When NOT to use this pattern

- Single ad-hoc question — use `/wiki-query` or direct lookup, not full Q-by-Q overhead.
- Live troubleshooting — use `/triage`.
- Synthesis / comparison — use `/wiki-query --mode=deep`.
- Policy discussion — straight to human, not via this skill.
