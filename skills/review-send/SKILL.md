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

Legacy reviewers start in a dispatch task worktree. v3 reviewers start in an
owner-only lane runtime; the prompt supplies the exact operation paths for:

- `.review-meta.json`
- `.review-baseline-state.json`
- `.review-baseline-status.txt`
- `.task-cmux-surface`

## Flow

1. Assemble the JSON object described in the active `.review-prompt*.md`.
2. Follow the runtime-specific typed transport:

   - Claude: Write only `.review-outbox.json` with the Write tool.
   - Codex: atomically publish the JSON in the isolated scratch outbox named by
     the prompt.
   - For both runtimes, the trusted supervisor invokes submission and the
     deterministic operation-scoped receive; the reviewer never receives an
     operation-specific callback permission.

3. The script validates schema/run/mode and blocks if any non-handoff file
   changed since the executor captured the review baseline. A valid outbox is
   removed after durable receive. If blocked, report and do not callback.
4. If validation passes, the script atomically writes the canonical object to
   the exact operation-scoped `.review-callback.json`, receives and renders it,
   then sends the executor only an already-received notification. A failed UI
   notification does not undo or retry the durable transition.

The reviewer session stays open after callback. Do not exit yourself: the
executor may send verify, then `review-dispatch finish` arms and closes an
approved unattended reviewer surface after process return.

The trusted receive writes canonical `.task-review*.json` and deterministically
renders `.task-review*.md`. Legacy compressed callbacks and in-flight metadata
without the supervised receive transport remain receive-only compatible.

## Do Not

- Do not write product or handoff files. The sole write exception is the
  runtime-local `.review-outbox.json` transport.
- Do not invoke `review-send` or `cmux` yourself; the trusted supervisor owns
  callback delivery.
- Do not close the cmux surface or agent process yourself.
- Do not guess a callback surface if `.review-meta.json` is stale.
