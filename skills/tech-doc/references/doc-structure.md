# Document structure for `/tech-doc`

Language and word rules: [plain-style.md](../../../docs/skill-references/plain-style.md).
Read [v2.6.3-documentation-quality-contracts.md](../../../docs/acceptance/v2.6.3-documentation-quality-contracts.md)
only when the document covers several audiences or a whole project (source
hierarchy, audience matrix). This file defines only the structure.

Structure model: S1000D and ATA iSpec 2200 procedures (applicability, signal
words, steps with expected results) and the QRH order (frequent first,
reference last).

## 1. Select the document type

| Type | Reader job | Section order |
|---|---|---|
| Usage | Use a tool, template or library | Purpose → How to include or run → Minimal example → Parameters table (name, description, type, default, required) → Required variables or secrets |
| Operations manual | Operate, repair and restore a system | See section 2 |
| Explanation | Understand a design | Problem → Decision → Consequences → Alternatives not taken |

A usage document fits on one screen when possible. Add depth only through
links or later sections.

## 2. Operations manual: section order

1. Header table: applies to (system, versions, account, region), software,
   revision date, related documents. If a language pair exists, one line
   links to the other version.
2. Quick reference:
   - the system in 2–3 lines.
   - mandatory rules (5 or fewer).
   - frequent commands in one code block. The comment above each command
     gives its location mark and, if it has one, its C-x ID.
   - table "Situation → procedure".
3. Prerequisites: tools, access, one-time setup with the expected result.
4. Conventions: location marks and signal words.
5. Common operations C-1…C-n: short, reused by procedures.
6. Fault isolation table: Symptom | Check | Action.
7. Procedures P-1…P-n.
8. System description: topology, access, backups.
9. Repository layout: files in the repository and on the host.
10. Development checks.

Keep C-x and P-x IDs stable. Do not renumber them when you reorder sections.

## 3. Location marks

Put a mark before each command or step that runs in a specific place:

| Mark (EN / RU) | Meaning |
|---|---|
| `[LOCAL]` / `[ЛОКАЛЬНО]` | Operator machine. Name the directory when it matters |
| `[LOCAL UNIT]` / `[ЛОКАЛЬНО, ЮНИТ]` | Operator machine, infrastructure unit directory |
| `[ON NODE <n or name>]` / `[НА НОДЕ <n or name>]` | Shell on a host, by number or name |
| `[MONGO SHELL]`, `[PSQL]`, … | Interactive client |

Define the marks once in "Conventions". Use names that the reader understands
without the definition.

## 4. Signal words

- `WARNING` — risk of data loss or full outage.
- `CAUTION` — risk of degraded service or loss of one component.
- `NOTE` — information for correct execution.

Put the signal word before the step it applies to. If the hazard starts in
the middle of a procedure, make the signal word the first line of the step
where it starts. Keep the step numbering continuous. State the hazard and the
safe action in the same paragraph.

## 5. Procedure template

```markdown
### P-n. <Verb + object> (<what is kept or changed>)

Use when: <symptoms or triggers>.

WARNING: <hazard>. <Safe action>.

1. <Precondition check, usually a C-x with gloss>.
2. [LOCAL] <One action>.
   `<command>`
   Expected: <observable result>.
   If not: <stop, or go to P-m (gloss)>.
3. ...

Rollback: <steps or "not possible, reason">.
```

Common operation (C-x) template: heading `### C-n. <Verb + object> [mark]`,
then the command block, then "Expected:". Add "If not:" only when failure is
realistic. A C-x has no "Use when" and no "Rollback".

Rules:

1. One action per step. A command block is part of its step.
2. Every step that changes state has "Expected:" and, when a failure is
   realistic, "If not:".
3. Every Terraform or other plan/apply step lists the expected changes and says
   "stop on any other change".
4. Commands work in bash and fish when the readers use both. Use placeholders
   `<name>`, not shell variables.
5. Take every command, path, variable and resource name from the repository,
   the configuration or a verified output. Mark everything else
   `(unverified)` in the text.

## 6. References between sections

Write a short gloss after each reference to a defined ID:

- `C-6 (stop mongod)`
- `P-1 step 10 (access check)`
- inside parentheses: `(C-5 — find and step down the primary)`

A table cell that contains only IDs, where the row explains them, needs no gloss.

## 7. Language pairs

1. Both files have the same headings, IDs, references and commands.
2. Each file links to the other at the top.
3. Check with `python3 skills/tech-doc/scripts/doc_lint.py <en.md> --pair <ru.md>`.
