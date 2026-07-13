---
name: review-send
description: >
  Submit a typed, product-write-free reviewer result to a review-dispatch executor.
  Use only inside an active reviewer split; not for editing reviewed files.
allowed-tools: Read Write Bash
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

1. Assemble the JSON object described in the active `.review-prompt*.md`.
2. Follow the runtime-specific typed transport:

   - Claude: Write only `.review-outbox.json`, then run the prompt's exact
     `submission_command --input-file ...` with no pipe/heredoc.
   - Codex: atomically publish the JSON in the isolated scratch outbox named by
     the prompt; the trusted supervisor invokes the submission command.

3. The script validates schema/run/mode and blocks if any non-handoff file
   changed since the executor captured the review baseline. A valid Claude
   outbox is removed after callback. If blocked, report and do not callback.
4. If validation passes, the script atomically writes the canonical object to
   `<worktree>/.review-callback.json` and sends only that exact file reference
   to the executor. The callback remains bounded even for a large review.

The reviewer session stays open after callback. Do not exit yourself: the
executor may send verify, then `review-dispatch finish` arms and closes an
approved unattended reviewer surface after process return.

The executor validates the relay again, removes it after successful receipt,
writes canonical `.task-review*.json`, and deterministically renders
`.task-review*.md`. Legacy compressed callbacks remain receive-only compatible.

## Do Not

- Do not write product or handoff files. Claude's sole write exception is
  `.review-outbox.json`; Codex writes no file.
- Do not close the cmux surface or agent process yourself.
- Do not guess a callback surface if `.review-meta.json` is stale.
