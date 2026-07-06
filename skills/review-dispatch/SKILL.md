---
name: review-dispatch
description: >
  Stateful cross-model review loop for dispatch task-splits: open
  opposite-model reviewer, receive findings, optionally verify fixes in same
  session, finish after user approval. Supports full and light review modes.
  Triggers: review-dispatch, cross-model review, light cross-model review,
  Codex/Claude review, ревью другой моделью, лёгкое ревью другой моделью.
allowed-tools: Read Write Edit Bash AskUserQuestion
---

# Review Dispatch

Runs the review leg between task completion and `reap-send`. The executor keeps
ownership of the work. The reviewer is advisory and read-only for product/vault
files.

This skill is for task worktrees created by `/dispatch` or the matching
`$<plugin>:dispatch` Codex command. It
expects `.task-prompt.md`, `.task-meta.json`, and `.task-cmux-surface` in the
current directory.

## Model Defaults

- Claude reviewer default: `opus`.
- Codex reviewer default: `gpt-5.5`.
- Default reviewer runtime is the opposite model family from the executor:
  Codex executor -> Claude reviewer; Claude executor -> Codex reviewer.

## Modes

Review depth:

- `full` is the compatibility default and keeps the normal review gate.
- `light` is a fast independent pass for routine changes: top actionable
  correctness/regression/test/security findings only, no exhaustive checklist.
- Both modes use the same model defaults: Claude `opus`, Codex `gpt-5.5`.

### Start

Use after implementation, verification, and executor self-review:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start
```

For lightweight review, use:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start --light
```

`spawn` remains a backward-compatible alias for `start`.

The script writes `.review-prompt.md`, `.review-meta.json`,
`.review-cmux-surface`, `.review-baseline-state.json`, and
`.review-baseline-status.txt`, then opens a cmux right split with the opposite
model. It also writes `.task-review-skill` and `.task-review-send-skill` so no
agent has to guess plugin/slash syntax.

### Receive

Use when the reviewer split calls back after `$<plugin>:review-send` or
`/review-send`.

1. Read the file named by `.review-meta.json.output_file`:
   - `.task-review.md` for initial findings.
   - `.task-review-verify.md` for the follow-up verification pass.
2. Classify each finding in `.task-review-resolution.md`:
   - `applied`
   - `rejected`
   - `out-of-scope`
3. Apply only clearly correct, in-scope fixes. Ask before changing behavior,
   public interfaces, migrations, or risky operational config.
4. If fixes were made, run `verify` against the same reviewer session. In
   `light` mode, skip verify when no reviewer finding was applied.
5. After the reviewer approves or verifies the fixes, do your own final review.
6. Commit the accepted implementation before `finish` or `reap-send` if any
   non-handoff changes remain. Use the repo commit discipline:
   - Use the repo's normal commit discipline. If a local commit skill exists,
     use it; otherwise stage an explicit file list and run `git commit`.
   - Stage an explicit file list only; exclude `.task-*`, `.wiki-*`,
     `.review-*`, `.obsidian/workspace*.json`, and other UI/runtime state.
   - Never push.
   - If there is nothing to commit because the implementation was already
     committed, record `Commit: no changes` in the final summary.
7. Show the combined result to the user, including reviewer verdict, applied or
   rejected findings, checks, and the commit hash or `Commit: no changes`.
8. Only after user approval, run `finish`, then proceed to `reap-send`.

### Verify

Send the implementation back to the same reviewer session:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py verify
```

This reuses `.review-cmux-surface`; it must not create a new split. The reviewer
should check which prior findings are resolved or unresolved and then call
`review-send` again. The script writes the full follow-up prompt to
`.review-prompt-verify.md` and sends only a short file-reference handoff into
the existing reviewer TUI, avoiding large pasted prompts in cmux.

`verify` preserves the original review mode from `.review-meta.json`; a light
review remains light during the follow-up.

### Finish

Only after the user says the final result is acceptable:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py finish
```

This queues `/exit` to the reviewer agent process. It does not close the cmux
surface/tab. Codex reviewers may still require manual `/exit` in the reviewer
split if the TUI ignores injected slash commands.

## Review Contract

- Reviewer must not edit product files, commits, tickets, GitLab comments, or
  wiki pages.
- Reviewer may write only review handoff files: `.task-review*.md`,
  `.task-review-resolution.md` when asked, and `.review-*`.
- `review-send` blocks callback if non-handoff files changed since the executor
  captured the review baseline.
- Executor owns all edits and judgment. Model findings are evidence, not orders.
- Executor owns commits. A successful review gate should leave reviewed changes
  committed before `finish`/`reap-send`, unless no non-handoff changes remain.
- Final `## Wiki Summary` should include:
  `Cross-model review: <not run | passed | fixes applied | blocked>`.

## Resources

- `scripts/spawn_review.py` performs the cmux orchestration.
- `references/review-prompt-template.md` is rendered for reviewer turns.
