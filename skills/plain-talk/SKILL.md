---
name: plain-talk
description: >-
  Rewrite, explain or audit text in plain technical style (~80% ASD-STE100).
  Use for объясни проще, plain, STE.
---

# Plain Talk

Read [plain-style.md](../../docs/skill-references/plain-style.md) completely
before step 2. `CLAUDE.md` or `AGENTS.md` may apply the same rules to every
reply. This skill is the strict pass on request.

1. **Mode.** `explain` answers a question or explains a topic, a log or an
   earlier reply. `rewrite` changes a given text or the previous reply.
   `audit` returns findings only: quote → rule (section) → fix, in the
   user's language. Audit mode runs step 4 on the input text and skips steps
   2, 3 and 5. Done when the mode and the input text are fixed.
2. **Point.** Write one sentence with the main answer or result. Keep the
   meaning. Add no claims, numbers or examples that the input does not
   support. Delete an evaluative claim that no fact in the input supports,
   and list it in "What changed". Do not invent an effect to replace it.
   Done when the point sentence exists.
3. **Write.** Point first, then evidence, then the next step if the input
   has one. Apply plain-style sections 1–5 as strict rules. Keep code,
   commands, quotes and names unchanged. Done when each sentence has one
   instruction, or one fact with its reason.
4. **Check.** For text longer than 150 words, write it to a file under
   `/tmp` and run `python3 skills/tech-doc/scripts/doc_lint.py <file>`. For shorter text,
   check sections 1–4 by hand. Done when there are 0 errors or each
   remaining one has a stated reason. In audit mode, done when each
   violation is listed as a finding.
5. **Output.** Give the text in the user's language. For `rewrite`, add
   "What changed" (in Russian: «Что изменилось») with 5 items or fewer.

An output format defined by another skill (`/save`, `/draft` with
redaction rules) wins over this style.

Completion: the text passes step 4, and the meaning matches the input.
