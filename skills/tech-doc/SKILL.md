---
name: tech-doc
description: >-
  Write or audit technical docs (runbook, README, how-to) in plain STE style
  with verified commands. Use for документация, ранбук.
---

# Tech Doc

Read [doc-structure.md](references/doc-structure.md) and
[plain-style.md](../../docs/skill-references/plain-style.md) completely before
step 2.

Input: target file or topic, reader, languages (EN, RU or both). Ask only what
the prompt and the repository do not answer (Pre-flight rule in `CLAUDE.md`).
Default reader: an on-call engineer who did not build the system.

1. **Mode.** `write` creates or rewrites a document. `audit` returns findings
   only: quote → rule (file and section) → fix. Audit mode runs steps 2, 3
   and 5 on the existing text, then step 6, and edits nothing. Done when the
   mode and the target paths are fixed.
2. **Type and outline.** Select the type in doc-structure section 1. Write the
   section outline with stable IDs (C-x, P-x). Omit a section that has no
   content and keep the order of the rest. Done when each reader job maps
   to one section.
3. **Facts.** For every command, path, variable, resource name and version,
   read the source: code, configuration, `--help` or a verified output. Mark
   each fact without a source `(unverified)` in the document and list it in
   the report. Done when every fact has a source or is marked.
4. **Write.** Apply doc-structure and plain-style. In an existing document keep
   the IDs and the user's content. Change the structure only when the user asks.
   Write a language pair from one outline. Done when every step that changes
   state has "Expected:" and, where failure is realistic, "If not:".
5. **Check.** Run `python3 skills/tech-doc/scripts/doc_lint.py <file>`. Add
   `--pair <other-language-file>` for a pair. Fix the errors. Then read each
   procedure as a reader who has only this file. Done when doc-lint reports 0
   errors or the report gives a reason for each remaining error.
6. **Report.** List the changed sections, the unverified facts and the checks
   with their results. Say that a procedure works only if it was run.

Boundaries:

- Edit only the documentation files that the user named or approved.
- For pages under `wiki/`, give the text to `/save`. Do not edit `wiki/`
  directly.
- Do not commit, push or publish. For a document in another repository or
  for other people, also apply
  [redaction-rules.md](../../docs/skill-references/redaction-rules.md): no
  vault links, no secrets.

Completion: the target files (or the audit findings), doc-lint results, and
the unverified list are in the report.
