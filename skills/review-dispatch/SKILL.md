---
name: review-dispatch
description: >
  Cross-model full/light review for dispatch tasks: findings, same-session
  verification, escalation, and safe reviewer close. Triggers: review-dispatch,
  cross-model review, Codex/Claude review, ревью другой моделью.
allowed-tools: Read Write Edit Bash AskUserQuestion
---

# Review Dispatch

Runs the review leg between task completion and `reap-send`. The executor keeps
ownership of the work. The reviewer is advisory and read-only for product/vault
files.

This skill normally reviews task worktrees created by `/dispatch` or the
matching `$<plugin>:dispatch` Codex command. An explicit coordinator review may
target the primary vault checkout itself. Both paths expect `.task-prompt.md`,
`.task-meta.json`, and `.task-cmux-surface` in the current directory.

## Model Defaults

- Reviewer defaults are resolved once from `config/model-routing.toml`.
- Explicit per-task or CLI model/effort choices override repository defaults;
  Codex effort is preserved after `--model` through a validated
  `model_reasoning_effort` override.
- Default reviewer runtime is the opposite model family from the executor:
  Codex executor -> Claude reviewer; Claude executor -> Codex reviewer.
- A generic request for bounded same-model review/work should use the host's
  internal subagent mechanism. `--same-model` is only for an explicit visible
  separate-window request; it creates/reuses a normal cmux review lane.

## Modes

Review depth:

- `full` is the compatibility default and keeps the normal review gate.
- `light` is a fast independent pass for routine changes: top actionable
  correctness/regression/test/security findings only, no exhaustive checklist.
- Both modes use the same central runtime defaults.
- v2/v3 unattended tasks take the mode from `.task-meta.json`; CLI flags remain
  explicit overrides. Legacy v1 tasks keep the full interactive default.

### Start

Use after implementation, verification, and executor self-review:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start
```

For lightweight review, use:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start --light
```

To review changes made directly in the primary vault and archive the validated
cycle automatically, opt in explicitly:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start \
  --coordinator-review --vault-root <vault-root>
```

This mode rejects linked worktrees, inferred vaults, and non-primary checkouts.

`spawn` remains a backward-compatible alias for `start`.

For legacy metadata the script writes `.review-prompt.md`, `.review-meta.json`,
`.review-history.json`,
`.review-cmux-surface`, `.review-baseline-state.json`, and
`.review-baseline-status.txt`, then opens a cmux right split with the opposite
model in a product-read-only, loopback-only, external-network-disabled
execution profile. It also writes `.task-review-skill` and
`.task-review-send-skill` so no
agent has to guess plugin/slash syntax.

For v3 metadata those artifacts live under the exact namespaced broker
operation directory printed by `start`; follow-up `receive`, `verify`,
`status`, `archive`, and `finish` commands must carry that exact
`--operation-dir`. Two coordinators or two model lanes in one project therefore
cannot overwrite each other's metadata, baseline, relay, or result. One
`review_id` is the operation ID; every initial/verify round has a distinct
`run_id`.

After a v3 operation is claimed, any preparation, anchored-spawn, registry, or
send failure before the supervisor starts marks that exact operation `failed`
before the error returns. Retrying an already-active operation reports its
identity and an exact `task_sessions.py fail-operation` recovery command; it
must never be reported as ordinary queued work. Use that command only after
the coordinator confirms the recorded launcher/surface is gone.

The split receives only a short `scripts/cmux_agent_supervisor.py` command;
validated argv/env live in `.review-agent-command.json`, so cmux cannot truncate
a long shell wrapper. For v2 unattended tasks, the supervisor also runs
`scripts/cmux_task_watchdog.py`: it hashes a statusline-normalized viewport,
notifies the executor after the approved warning/alert thresholds, and never
sends keys, cancels the reviewer, or closes the surface. It stops when the
reviewer process returns. Legacy metadata leaves it disabled.

Claude reviewers run in locked-down `dontAsk` with Read/Glob/Grep, recognized
read-only Bash/git inspection, exact cwd-relative Python/shell test entrypoints
whose permission patterns end in `.py`/`.sh`, one cwd-anchored `Edit`
permission covering the Write tool only for `.review-outbox.json`, and the
typed callback command. The prompt requires no arguments or shell composition;
the Claude wildcard matcher is not treated as an argv validator. The callback
validates and removes that isolated
outbox; anything else is denied without prompting. Codex reviewers keep
`approval=never`, start with `workspace-write` rooted at an owner-only temporary
scratch directory outside the product worktree, and receive no additional write
roots. The scratch lives under the canonical vault's gitignored
`.vault-meta/review-runtimes/` hierarchy, whose project trust is already established
by normal Codex setup, so a fresh runtime does not trigger the new-directory trust
prompt. The supervisor accepts only an empty generated child at that exact location;
no trust entry is added to the user's Codex config. For an explicit
`--coordinator-review` of the canonical vault, the sanctioned scratch root is
inside the reviewed checkout by construction; the supervisor permits only that
exact generated, owner-only, empty, gitignored location. Reviewers write typed JSON only
to the scratch `.review-outbox.json`. Codex reviewer launches also disable hooks for
that session so user/project lifecycle hooks cannot mutate the vault from review context.
The trusted supervisor—not the reviewer—polls that exact outbox, runs the schema
and baseline validator, forwards the callback through cmux, and removes the
outbox. The Codex reviewer therefore never receives cmux-socket access while
still being able to run diagnostics that need temporary files.
Claude may also run the exact `bash scripts/dcg-test-suite.sh` DCG policy
smoke; this does not grant a wildcard script permission.

Before a real Claude split is created, `claude-subscription-check.py` verifies
first-party paid subscription auth and rejects API/provider overrides. Dry-run
prompt generation does not require a local Claude login.

Claude reviewers must run as interactive Claude Code sessions in a cmux split.
Never use `claude -p` or `claude --print` for review-dispatch: print-mode hides
the reviewer as a subprocess, prevents the user from continuing the review
session, and breaks the `/review-send` / verify / finish loop.

Do not interrupt a reviewer that is visibly active: spinner, token counters,
tool activity, or changing screen means it is working. Wait at least 15 minutes
without visible progress before intervening; by 20 minutes, inspect and ask the
reviewer for a concise status or verdict instead of blindly cancelling.

### Receive

Use when the reviewer split calls back after `$<plugin>:review-send` or
`/review-send`.

1. The normal callback contains only a short reference to the validated relay:

   ```bash
   python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py receive \
     --relay-file <worktree>/.review-callback.json
   ```

   This exact-path file is atomically published by trusted `review-send`, then
   schema/run/mode-validated again and removed by `receive`. The callback itself
   stays short even when the review is large, so cmux never has to paste a
   compressed report into the executor composer. Legacy `--payload-b64 TOKEN`
   callbacks remain accepted for in-flight sessions created by older versions.

   After the command succeeds, read the
   file named by `.review-meta.json.output_file`:
   - `.task-review.md` for initial findings.
   - `.task-review-verify.md` for the follow-up verification pass.
   The bounded human task-description section and every validated callback are
   also appended idempotently to the stable `.review-history.json` cycle. A later `verify` snapshots the executor's
   `.task-review-resolution.md` into the round it resolves before rotating to
   the next `run_id`. `verify` fails closed when the latest round has findings
   but this resolution file is absent; a new cycle clears stale round artifacts.
2. Classify each finding in `.task-review-resolution.md`:
   - `applied`
   - `rejected`
   - `out-of-scope`
3. Follow `.review-meta.json.recommended_action`. Unattended `warning|nit`
   findings may be applied or rejected with evidence; `blocking`, `blocked`,
   scope/public-interface/migration/security/external-effect changes escalate.
4. For unattended `changes-requested`, run `verify` after the resolution even
   when all findings were rejected. Reuse the same reviewer and stop after the
   configured maximum (default two); legacy interactive behavior is unchanged.
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
7. Interactive tasks show the combined result and wait. For unattended tasks,
   an `approve` callback is sufficient to run `finish` and continue.
8. Never auto-resolve a blocking/scope escalation.

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

Approval finishes one review cycle, not the task. `finish` captures typed cmux
resume metadata, exits the agent, closes only the exact surface after process
return, and leaves the lane checkpoint for a later review of the same exact
task/model/domain. Missing or corrupt resume data is reported visibly and the
later operation continues in a fresh session with its full packet. Effort
changes apply only at a new start/resume boundary, never in-band.

### Finish

After interactive approval, or immediately after unattended approve:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py finish
```

Before exit, `finish` archives the validated cycle when it runs in an explicit
primary-vault `--coordinator-review`. Inside an isolated task worktree it
writes only
`.review-archive-request.json`; coordinator `/reap` performs the authorized
`vault-write.py` transaction and links the archive from the task result. The
archive keeps the original task request, validated findings, per-round executor
resolutions, verification gaps, residual risks, reviewer/runtime/model/mode,
and final verdict. It does not copy raw orchestration/reviewer prompts,
compressed payloads, command logs, sockets, or cmux IDs.

The coordinator vault is resolved in fail-closed order: explicit
`--vault-root`, `.task-meta.json.vault_root`, the approved `plan_file`, prior
review metadata, then the script location for legacy tasks. This matters when
the repository reviews a linked worktree copy of itself: a task worktree is
never allowed to treat its own `wiki/` as the coordinator archive target.

For unattended tasks `finish` then arms a surface-bound sentinel, queues `/exit`, and
lets the launch wrapper call `cmux close-surface` only after the agent process
actually returns. If `/exit` is ignored or close fails, the surface stays open.
If the surface closes but the broker terminal transition fails, the exact
sentinel remains for an idempotent `cmux_surface_lifecycle.py after-exit`
retry and the error prints the exact `fail-operation` fallback.
Interactive/legacy tasks keep the old exit-without-close behavior.

## Review Contract

- Reviewer must not edit product files, commits, tickets, GitLab comments, or
  wiki pages.
- Reviewer cannot write product files. Claude may write only the isolated
  `.review-outbox.json`; the callback validates/removes it, and the executor
  owns canonical JSON and Markdown writes. Codex may write only inside its
  external scratch root; the product worktree remains read-only.
- `review-send` blocks callback if non-handoff files changed since the executor
  captured the review baseline.
- Executor owns all edits and judgment. Model findings are evidence, not orders.
- Executor owns commits. A successful review gate should leave reviewed changes
  committed before `finish`/`reap-send`, unless no non-handoff changes remain.
- Final `## Wiki Summary` should include:
  `Cross-model review: <not run | passed | fixes applied | blocked>`.
- One review cycle has one stable `review_id`; each initial/verification round
  has its own contract `run_id`. The durable page lives under
  `wiki/meta/reviews/` and is updated idempotently through `vault-write.py`.

## Resources

- `scripts/spawn_review.py` performs the cmux orchestration.
- `scripts/archive_review.py` renders and transactionally files durable review history.
- `references/review-prompt-template.md` is rendered for reviewer turns.
