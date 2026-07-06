---
name: review-send
description: >
  Reviewer-side handoff for review-dispatch: validate .task-review*.md,
  callback executor, keep reviewer open. Triggers: review-send, send review
  back, return review to executor, отправь ревью.
allowed-tools: Read Write Edit Bash
---

# review-send

Run this inside a reviewer split created by `/review-dispatch` or the matching
`$<plugin>:review-dispatch` Codex command.
It is the review-side pair of `review-dispatch`, like `reap-send` is the
task-side pair of `reap`.

## Preconditions

The current directory must be a dispatch task worktree and must contain:

- `.review-meta.json`
- `.review-baseline-state.json`
- `.review-baseline-status.txt`
- `.task-cmux-surface`

## Flow

1. Write the review result to the output file named in `.review-meta.json`.
   Usually:
   - `.task-review.md` for the initial review.
   - `.task-review-verify.md` for the verification pass after fixes.
2. Run:

   ```bash
   python3 <vault-root>/skills/review-send/scripts/send_review.py send
   ```

3. The script blocks if any non-handoff file changed since the executor captured
   the review baseline. If blocked, report the mutation and do not callback.
4. If validation passes, the script sends the executor callback recorded in
   `.review-meta.json`.

The reviewer session stays open. Do not exit until the executor/user asks for
`review-dispatch finish`.

## Review Output Shape

Use markdown:

```markdown
# Cross-Model Review: <task-name>

Verdict: approve | changes-requested | blocked

## Findings

1. Severity: blocking | warning | nit
   File: path:line
   Issue: one sentence
   Rationale: why it matters
   Suggested fix: concrete change or "none"

## Verification Gaps

- Missing or skipped checks.

## Notes For Executor

- Short operational notes.
```

In verify mode, also include a short resolved/unresolved list for previous
findings.

## Do Not

- Do not edit product files, vault pages, commits, tickets, or GitLab comments.
- Do not close the cmux surface or agent process.
- Do not guess a callback surface if `.review-meta.json` is stale.
