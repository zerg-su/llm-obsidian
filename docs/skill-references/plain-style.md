# Plain technical style — shared

Single source of truth for the writing style of agent replies to the user,
`/plain-talk` and `/tech-doc`. Target: about 80 % of ASD-STE100 Simplified
Technical English. Use the strict rules as targets. Break a rule only when the
split text becomes less clear, for example a long command or a quoted error.

Precedence: an output format that a skill defines (`/save` page, `/draft` +
[redaction-rules.md](redaction-rules.md), log and hot entries) wins over this
file. Code, commands, quotes and file names stay as they are.

Sources:

- ASD-STE100 Issue 9 (ASD, free standard): sentence and word rules.
  Karpathy, X, 2026-10-02 (`x.com/karpathy/status/2105819303471976479`):
  "80% of the way to ASD-STE100" for LLM output.
- GOST 2.601 / 2.610: form of Russian operating documents (infinitive).
- `petergyang/no-ai-slop` (MIT): word lists and patterns in sections 3–4,
  adapted for technical text.

## 1. Sentences

1. Instruction sentence: 20 words or fewer. Descriptive sentence: 25 words or fewer.
2. One instruction per sentence. Two actions in one sentence only if they occur at the same time.
3. Active voice. Use passive voice only when the actor is unknown.
4. No semicolons in prose. Write two sentences or a list.
5. Put the result or the action first, then the reason.
6. One paragraph = one topic, 6 sentences or fewer.
7. Instruction form:
   - English: imperative ("Stop mongod.").
   - Russian documents: infinitive ("Остановить mongod.").
   - Russian chat replies: the user's own register ("Запусти…").

## 2. Words

1. One term = one meaning. Define each term once. Do not swap synonyms for the
   same object (synonym cycling). Example: if "member" means a replica set
   member, do not call it "node" or "instance" in the next sentence. If you
   cannot tell whether two names mean the same object, keep both and ask.
2. Use the common word:

   | Avoid | Use |
   |---|---|
   | `utilize`, `leverage` | `use` |
   | `facilitate` | `help`, `let` |
   | `ensure` | `make sure` |
   | `in order to` | `to` |
   | `prior to` | `before` |
   | `subsequently` | `then` |
   | `terminate`, `commence` | `stop`, `start` |
   | `sufficient`, `additional` | `enough`, `more` |

   NOTE: STE approves `approximately`. The cheat sheet attached to the Karpathy
   post marks it as unapproved — that sheet has errors.
3. Give numbers with units. Put exact names (files, commands, keys) in backticks.
4. Russian: keep technical terms in English (`CLAUDE.md` language rule), or
   in the form the input already uses (`реплика-сет`, `бэкап`). Do not
   translate them. For text published outside the vault, also apply
   [redaction-rules.md](redaction-rules.md).

## 3. Word lists (machine-read by `skills/tech-doc/scripts/doc_lint.py`)

Banned words — replace or delete. Severity: error.

```text lint:banned-en
delve, foster, leverage, utilize, facilitate, empower, streamline, robust, cutting-edge, paradigm shift, game changer, this is huge, this changes everything, tapestry, realm, beacon, multifaceted, meticulous, intricate, paramount, transformative, elevate, embark, supercharge, harness, ever-evolving
```

Often-empty phrases — keep only when the phrase carries meaning. Severity: warning.

```text lint:empty-en
literally, honestly, simply, actually, truly, fundamentally, importantly, crucially, inherently, inevitably, it's worth noting, it's important to note, at the end of the day, when it comes to, at its core, in today's world, in the age of, in the world of, the reality is, the truth is, in terms of, with regard to, in order to, going forward, in this article, let's dive in
```

```text lint:empty-ru
важно отметить, стоит отметить, следует отметить, необходимо отметить, ключевой момент, играет важную роль, играет ключевую роль, является неотъемлемой частью, на самом деле, по сути, в современном мире, давайте разберёмся, давайте рассмотрим, в заключение, подводя итог, как видно, иными словами, другими словами, не просто
```

Pattern phrases from section 4 — rewrite the sentence. Severity: warning.

```text lint:patterns-en
here's the thing, here's what i mean, let me be clear, i'll be honest, the uncomfortable truth is, what most people get wrong, what nobody tells you, the part everyone misses, a testament to, marks a pivotal moment, plays a vital role, plays a key role, plays a crucial role, underscores its significance, the key point is, as you can see, this distinction matters, experts agree, industry reports suggest, many argue, widely regarded as, studies show, what if i told you, plot twist, think about it, in conclusion, changes everything
```

```text lint:patterns-ru
по мнению экспертов, эксперты считают, как известно, меняет всё, что если я скажу, это не просто
```

## 4. Patterns to cut

| Pattern | Example | Fix |
|---|---|---|
| Binary contrast | `This is not X. It's Y.` | State Y |
| Throat-clearing opener | `Here's the thing`, `Давайте разберёмся` | Start with the point |
| Faux-insight setup | `What most people get wrong` | State the fact |
| Colon reveal in prose | `The best part: it learns.` | Plain sentence. Labels in lists (`Expected:`) are correct |
| Trailing `-ing` analysis | `…, highlighting the commitment` | Delete or state the fact |
| Importance puffery | `plays a vital role`, `играет ключевую роль` | Show the effect |
| Interpretive metadiscourse | `The key point is`, `Важно отметить` | Delete the frame |
| Weasel attribution | `experts agree`, `рекомендуется` without source | Name the source or the reason |
| Fake-strong verb | `serves as a hub for` | `is`, `stores`, `sends` |
| Synonym cycling | agent → assistant → tool for one thing | One term (section 2) |
| Negative listing | `Not a X. Not a Y. A Z.` | State Z |
| Dramatic fragments | `That's it. That's the whole thing.` | Full sentence or delete |
| Rhetorical setup | `What if I told you…`, `Plot twist:` | State the fact |
| Fake-profound kicker, recap ending | `In conclusion…`, `Подводя итог` | End on the last fact or next step |
| Formatting slop | bold in every line, a heading for 2 lines | Format only what the reader scans |
| Em dash overuse | three dashes in one paragraph | Split the sentence. A dash in a definition (`C-5 — find the primary`) is correct |

Not taken from no-ai-slop:

- Voice preservation. Technical text is impersonal and uniform.
- The "robotic rhythm" check. Procedures must repeat the same step shape.

## 5. Replies to the user

1. First line: the answer or the result. Then the evidence. Then the next step.
2. Say what you verified and what you did not verify. Do not hedge a verified fact.
3. Lists for 3 or more parallel items. Tables for comparisons. Code blocks for
   commands, with the place where the command runs.
4. Reply in the user's language.

## 6. Mechanical check

`python3 skills/tech-doc/scripts/doc_lint.py <file> [--pair <other-language-file>]` checks
sentence length, semicolons, the word lists in section 3, references to
defined IDs without a gloss, and EN/RU structure parity. The script cannot
tell an instruction from a description. It reports more than 25 words as an
error and more than 20 words as a warning. A clean result is one
piece of evidence. It does not prove that the text is correct or clear.
