---
name: draft
metadata:
  version: 1.1.0
description: |
  Communication ADVISOR, not sender: 2-3 alternative draft replies for any external communication (email, issue comment, forum post, chat message) + historical hints from the wiki + a redaction pass; the user copies and posts themselves.
  Triggers: сформируй ответ, как ответить, помоги ответить, draft a reply.
allowed-tools: Read Glob Grep Bash AskUserQuestion WebFetch
---

# /draft — 2-3 alternative drafts + historical hints, ADVISOR

The user describes a situation / pastes a thread → Claude composes 2-3 alternative replies in different tones/scopes, adds non-obvious historical hints from the wiki, and runs a redaction pass. The user picks one and copies it themselves — the skill does NOT post anything.

## Input

```
/draft <context — thread URL, pasted message, or description>
```

Examples:
- `/draft need to reply to a GitHub issue asking about the status of the fix`
- `/draft how to reply to code-review feedback about the module structure`
- `/draft a colleague asked in chat how our note-taking workflow is organized`

---

## Phase 0: Pre-flight (mandatory — AskUserQuestion)

Single `AskUserQuestion` with 4 questions:

1. **Target channel**: `email` / `issue or PR comment` / `forum post` / `chat message` / `internal note` (private note — slang allowed, redaction relaxed).
2. **Tone**:
   - `formal-neutral` (default — professional, factual).
   - `terse` (1-2 sentences).
   - `detailed` (explanation + definition of done + next step).
   - `friendly` (casual chat).
3. **Scope**:
   - `answer-only` (reply only, do not reframe the scope).
   - `answer + suggested-action` (include a recommendation for what the recipient should do).
   - `pushback` (explicitly disagree, politely).
4. **Constraints** (what NOT to mention):
   - personal tooling and local setup details.
   - prior incidents or history (if sensitive).
   - specific account names / IDs / hostnames.
   - private vault refs (`[[wikilinks]]`, addresses, session IDs).
   - target dates unless explicitly requested.

If invoked from another skill's chain — scope and source material are pre-loaded.

---

## Phase 1: Read source (read-only)

If a URL — fetch it:

```python
WebFetch(url='<thread / issue / post URL>', prompt='Extract the message(s) that need a reply, with author and context')
```

If a paste / description — parse as-is.

---

## Phase 2: Wiki search for historical hints

`Grep wiki/` for:
- The topic / project / tool mentioned in the source thread.
- Prior decisions in `wiki/decisions/` on a similar subject.
- Open or answered questions in `wiki/questions/`.
- Related research in `wiki/sources/` and `wiki/concepts/`.

Filter: top-3 historical hints. Each contributes at most 1 sentence to a final draft (if appropriate).

Anti-pattern: dumping 10 references — that is noise. Pick the most non-obvious + non-trivial.

---

## Phase 3: Compose 2-3 alternative drafts (in-memory)

Each draft = full text + rationale + tradeoffs. Variants must differ in SCOPE / TONE / STANCE, not in synonyms.

Example shape:

```markdown
### Variant 1 — terse + factual

> The fix landed yesterday; the error rate has been flat since 23:50. Remaining
> follow-ups: clean up the config drift and raise the alert severity.

**Rationale**: scannable in 10 seconds, facts + actionable items.
**Tradeoff**: does not explain why the workaround was chosen (a reviewer may ask).

### Variant 2 — detailed + reasoning

> The fix was applied manually rather than through the normal release path
> because of configuration drift discovered during rollout. Follow-ups:
> 1) reconcile the drift and open a PR with the permanent fix; 2) raise the
> alert severity so this class of failure pages earlier; 3) add a monitor
> for the metric that would have caught it.

**Rationale**: explains the "why" — useful if the thread contained questions.
**Tradeoff**: longer, requires > 30 seconds of reader time.

### Variant 3 — pushback + question

> The fix works, but before closing this out — do we understand why the limit
> was hardcoded in the first place? If it is historical legacy without a
> current reason, we should remove the hardcode rather than patch around it.
> If there is an active reason, I would like the owner to confirm it first.

**Rationale**: surfaces the underlying open question instead of closing on inertia.
**Tradeoff**: extends the thread's scope; may be unwanted if urgency is high.
```

---

## Phase 3.5: Fact check (mandatory if drafts contain technical claims)

The draft goes to an external audience — a factual error in a version / flag / behavior claim damages credibility more than any stylistic issue. Before redaction:

1. Extract verifiable technical claims from all 2-3 variants (versions, commands, API behavior).
2. Verify against official documentation (WebFetch the project's docs / changelog; use available docs tooling if configured).
3. If a claim is not confirmed — rewrite it, or mark it in the draft with a caveat ("needs verification") and note this in the variant's rationale.
4. Purely conversational replies with no technical facts — skip this phase.

## Phase 4: Redaction pass (mandatory)

Every draft passes through these checks before being shown:

1. No slang or colloquialisms — neutral technical language.
2. No `[[wikilinks]]` / `c-NNNNNN` addresses — they render broken outside the vault.
3. No internal jargon or private wiki cross-refs in external text (personal tracking markers, process words like "handoff", "dispatch", "reap").
4. No internal hostnames, IP addresses, credentials, tokens, or personal data.
5. No personal tooling details (shell setup, local scripts, agent tool names).
6. No target dates unless the user explicitly supplied one.
7. Language matches the target channel; identifiers and code stay in English.

Show a **diff-style preview** of what was replaced in the final output (1-line summary per redaction).

---

## Phase 5: Historical hints (optional, max 3)

If Phase 2 found relevant historical context — append after the drafts:

```markdown
### Historical hints (optional reference)

- **Prior decision (2026-03-12)**: a similar approach was already chosen and documented; consider linking your reply to it (in your own words).
- **Open question**: the wiki has an unanswered question on this exact topic — replying may also close it.
- **Prior research**: notes from an earlier deep dive contradict one claim in the thread; double-check before posting.
```

These are not for inclusion in the draft — they are **for the user** before choosing a variant.

---

## Phase 6: Output

```markdown
## Draft replies for <target channel>

### Variant 1 — <tone tag>
<text>
**Rationale**: ...
**Tradeoff**: ...

### Variant 2 — <tone tag>
<text>
**Rationale**: ...

### Variant 3 — <tone tag>  (optional)
<text>
...

### Redactions applied
- "[[Vector Store Choice]]" → "our earlier comparison" (1 occurrence)
- internal hostname → removed (1)
- "target 2026-05-30" → removed

### Historical hints
- ...
- ...

---

**Next step**: the user copies the chosen variant and posts it to <target> themselves.
Claude does NOT post.
```

---

## Reuse pointers

- Redaction principles are shared with any skill that publishes text outside the vault.
- Historical hints come from `wiki/decisions/`, `wiki/questions/`, `wiki/sources/`, `wiki/concepts/`.
- Chain in: any review-type skill that produces suggested comments.

---

## Anti-patterns

- ❌ Auto-posting anywhere (API, MCP write tools, CLI) — the skill is an ADVISOR; the user posts.
- ❌ A single variant instead of 2-3 — the point is alternatives. Minimum 2.
- ❌ Copying the source text verbatim as a "reply variant" — that is echo, not drafting.
- ❌ Dumping 10 wiki references instead of top-3 non-obvious historical hints — noise.
- ❌ Skipping the redaction pass — internal jargon leaks to an external audience.
- ❌ Including personal tooling / local environment details — irrelevant to the recipient.
- ❌ Including `[[wikilinks]]` — broken render outside Obsidian.
- ❌ Inventing target dates the user did not give.
- ❌ Variants differing by trivial wording — they must differ in SCOPE / TONE / STANCE.
