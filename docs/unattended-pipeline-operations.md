# Unattended pipeline operations

Operator guide for the `/dispatch` → `/review-dispatch` → `/reap-send` chain
when a task worktree runs with `interaction_policy: unattended`. Everything
here describes the code in `scripts/` and `skills/review-dispatch/scripts/` as
of this writing, plus the live CLI surfaces those scripts actually invoke
(`cmux`, `claude`, `codex`). It does not repeat the skill-authoring detail in
`skills/dispatch/SKILL.md`, `skills/review-dispatch/SKILL.md`, and
`skills/reap-send/SKILL.md` — read those for the step-by-step flow. This page
is what you check when a background task is running and you want to know what
it is allowed to do, what a given surface state means, or why something
stopped.

## Session labels and surfaces

Every unattended task lives across (at least) two cmux split surfaces in one
workspace:

- **Coordinator/wiki surface** — the vault-repo session that ran `/dispatch`
  and will run `/reap`. Recorded as `wiki_surface` in `.task-meta.json`.
- **Task/executor surface** — the split running the implementation agent
  (`executor_runtime`: `claude` or `codex`) in the task worktree. Recorded as
  `task_surface`.
- **Reviewer surface** — opened by `/review-dispatch` for the opposite model
  family (`reviewer_runtime` in `.review-meta.json`, `review_surface`).

All surface identity is by UUID, not the human-readable `surface:N` ref —
refs shift when panes are reordered or closed. Watchdog, escalation, and
close-arming code all key off `.task-meta.json` / `.review-meta.json`, never
off a live `cmux list-pane-surfaces` re-scan, so a stale surface record fails
loudly (`cmux send`/`notify` returns `not_found`) instead of silently
targeting the wrong pane.

## Supervisor / spec flow

Neither `/dispatch` nor `/review-dispatch` hand a long inline shell command to
`cmux send` — cmux typing is not a place to trust argv construction. Instead:

1. `cmux_agent_supervisor.py prepare-task` (task) or `spawn_review.py start`
   (reviewer) writes a validated JSON spec: `.task-agent-command.json` or
   `.review-agent-command.json` — `{version, kind, runtime, argv, prompt_file,
   env}`.
2. `cmux send` carries only a short, fixed supervisor invocation:
   `python3 scripts/cmux_agent_supervisor.py run --worktree <wt> --kind
   <task|reviewer> --surface <uuid>`.
3. The supervisor re-reads the spec from disk, revalidates it byte-for-byte
   against `.task-meta.json`/`.review-meta.json` (`validate_routing` in
   `scripts/cmux_agent_supervisor.py`), then execs the real agent
   (`claude ...` or `codex ...`) with the prompt file content appended as the
   final argv value — never interpolated through a shell string.

This split matters operationally: if `.task-agent-command.json` is edited by
hand to add a forbidden flag, the supervisor refuses to launch rather than
silently running a weakened command. Validation is not a one-time gate at
dispatch time — it re-runs every time the supervisor starts.

## Permission boundaries

The supervisor pins per-runtime safety independent of what `/dispatch` or
`/review-dispatch` intended, so a compromised or hand-edited spec still can't
escape the following:

**Task/executor (unattended only)**
- Claude: `--permission-mode auto`. Rejects
  `--dangerously-skip-permissions` or `--allowedTools` in argv.
- Codex: pinned `-a never -s workspace-write`. Rejects
  `--full-auto`, `--dangerously-bypass-approvals-and-sandbox`, `--sandbox`,
  explicit `--ask-for-approval`/`--approval-policy`, any `-c`/`--config`
  override, or `danger-full-access`.

**Reviewer (always, regardless of interaction policy)**
- Claude: `--permission-mode dontAsk`, tool surface pinned to exactly
  `Read,Glob,Grep,Write,Bash`, and `--allowedTools` locked to a fixed list:
  `Read`, `Glob`, `Grep`, one `Edit(./.review-outbox.json)` scoped write, a
  handful of read-only `git`/test/lint `Bash(...)` allowlist entries, and the
  typed submit command (`*send_review.py submit *`). Nothing else — a reviewer
  cannot touch product files, and it cannot run arbitrary Bash.
- Codex: `-s read-only -a never -c web_search="disabled"`. Same forbidden-flag
  set as the task runtime, so a reviewer can't be pointed at
  `danger-full-access` or given network back.

Live CLI confirmation (`claude --help`, `codex --help exec`, `codex exec
--help`) shows these are real, currently-supported flags — `-s
{read-only,workspace-write,danger-full-access}` and `-a
{untrusted,on-request,never}` on Codex; `--permission-mode`,
`--allowedTools`/`--allowed-tools`, and `--dangerously-skip-permissions` on
Claude. This doc does not restate the Codex sandbox/approval semantics —
`codex --help` is the source of truth if flag behavior changes upstream.

Claude reviewer spawns first go through `claude-subscription-check.py`, which
rejects API-key/provider-override auth and requires a first-party paid
subscription login — the reviewer is meant to be an interactive, billed Claude
Code session, not a headless API call. Correspondingly, **never use `claude
-p`/`--print` or `codex exec` for a reviewer or task split** — print/exec mode
hides the agent as a one-shot subprocess and breaks the
review-send/verify/finish loop, which depends on a persistent interactive
session the coordinator can inspect mid-run.

## Watchdog semantics

`cmux_task_watchdog.py` runs as a child of the supervisor, one process per
surface, serialized by an `fcntl` lock file (`.task-watchdog.lock` /
`.review-watchdog.lock`) so a restart never launches two observers for the
same surface.

- **Observe only.** It samples `cmux read-screen`, strips ANSI/decorative
  frame characters and known-volatile status text (context %, rate-limit
  countdowns, `/effort` tips) via regex, then hashes the remaining content.
  It never calls `cmux send`, `send-key`, or `close-surface` — grep the file
  and the only cmux subcommands it invokes are `read-screen`, `top`, and
  `notify`.
- **Thresholds** come from `watchdog_policy` in `.task-meta.json` (defaults:
  poll every 30s, warn at 900s / 15 min unchanged, alert at 1200s / 20 min).
  `task_contract.py` bounds these (`poll_seconds` 5–300, `warn_after_seconds`
  300–7200, `alert_after_seconds` 600–14400, alert must exceed warn) so a
  hand-edited policy can't set an absurd or inverted threshold.
- **States**: `starting` → `running` → `warning` (past warn threshold, no
  screen change) → `stalled` (past alert threshold) → `stopped` (agent
  process returned, watchdog exited cleanly) or `surface-gone` (the surface
  disappeared — `cmux read-screen` returned `not_found`). `degraded` means
  three consecutive screen-read failures; `contract-error` means
  `.task-meta.json`/`.review-meta.json` failed validation before the first
  sample.
- **Notifications** go out via `cmux notify`, retried at most once every 5
  minutes per stage, and explicitly say "observing only; sent no input,
  stopped nothing." Routing differs by kind: a *task* watchdog notifies the
  wiki/coordinator surface (`wiki_surface`); a *reviewer* watchdog notifies
  the executor/task surface (`.review-meta.json.executor_surface`), not the
  wiki surface — `resolve_route` in `cmux_task_watchdog.py` picks the
  coordinator target per `kind`. A `recovered` notification fires once the
  screen changes again after a warning/alert; CPU% (best-effort, from `cmux
  top --json`) is attached to the alert but never gates or suppresses it —
  a busy-looping process with an unchanged screen still alerts.
- Query live state anytime: `python3 scripts/cmux_task_watchdog.py status
  --worktree <wt> --kind task` (or `reviewer`).

Per the project mandate: do not treat a warning/alert as a request to
interrupt. Wait at least 15 minutes of no visible progress before checking in
at all; by 20 minutes it's reasonable to ask the surface for a short status,
not to cancel it.

## Typed review callbacks

The reviewer never talks back over free-form chat. `/review-send` (or the
Codex-side equivalent) submits a JSON object with `verdict` (`approve` /
`changes-requested` / `blocked`) and a `findings` array of `{severity,
title, ...}`, either directly or via `.review-outbox.json` (Claude) validated
by `spawn_review.py receive`/`submit`. `scripts/task_contract.py
review-action` is the single decision function the executor calls with that
payload plus the current verify iteration:

- `blocked` verdict, or any finding with `severity: blocking` → `escalate`
  (always, unattended or not — never auto-resolved).
- `approve` with no findings → `approve`.
- No findings but not `approve` → `escalate` (ambiguous callback, don't guess).
- Findings whose severities are all inside `review_policy.auto_resolve_severities`
  (`warning`/`nit` only, per this task's contract) and iteration is still
  under `max_verify_iterations` → `resolve` (fix and re-submit to the same
  reviewer via `verify`, which reuses `.review-cmux-surface` rather than
  spawning a new split).
- Otherwise → `escalate`.

`review_policy.escalate_severities` is contract-locked to exactly
`["blocking"]` — an unattended task cannot configure itself to silently
swallow a blocking finding.

## Auto-close and reap gating

`cmux_surface_lifecycle.py` is the only code path allowed to send `/exit` or
call `cmux close-surface`, and only when `surface_policy.auto_close` is `true`
in a `version: 2` `.task-meta.json`. Before arming task-surface close it
requires, in order:

1. No unresolved `.task-needs-attention.json` (see below).
2. `.task-reap-complete.json` present, with a `summary_sha256` that matches
   the live `.task-summary.json` on disk (the completion marker can't be
   stale relative to a summary that was edited afterward).
3. The recorded `result_path` resolves inside `<vault_root>/wiki/` and is an
   existing `.md` file — the reap must have actually landed a page, not just
   claimed to.
4. `git status` in the worktree shows no dirty files outside the
   `.task-*`/`.review-*`/`.wiki-*`/`.obsidian/workspace*.json` handoff
   prefixes and gitignored `.vault-meta/` derived state — nothing in-scope is
   left uncommitted.

Only then does it write a `<kind>-close-armed.json` sentinel, send `/exit`,
and return. The actual `cmux close-surface` call happens in
`cmux_agent_supervisor.py`'s `after-exit` step, which runs unconditionally
after the wrapped agent process returns and only closes if the sentinel is
present and matches the surface — so a surface that never got `/exit` (e.g.
escalation left it paused) is left open rather than closed out from under a
human who might still be looking at it. `require_origin_session` additionally
restricts arming/finalizing to the coordinator session that created the task
(`CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID` matches `origin_session`) — a
different session cannot finalize someone else's task.

## Escalation path

`task_escalation.py raise --category <cat> --reason ... --question ...`
(`categories`: `blocking-review`, `scope`, `public-interface`, `migration`,
`security`, `external-effect`, `contract-drift`, `permission`) writes
`.task-needs-attention.json` (`status: pending`) and sends a `cmux notify` to
the coordinator surface — it does not send input to any agent surface. While
that marker is pending, both `check-handoff` (reap validation) and
`cmux_surface_lifecycle.py`'s close-arming refuse to proceed, so an escalation
blocks the whole unattended tail (review resolution, reap, and close) until
the coordinator calls `task_escalation.py resolve --decision ...` — which
requires the resolving session to be the same `origin_session`, and relays
the decision by `cmux send` directly to the task surface (with a
`/`-scrubbing backspace burst first for Codex, whose input line needs
clearing before a fresh line).

## Diagnostics

- `python3 scripts/task_contract.py validate --meta .task-meta.json` —
  confirms the v2 contract shape and that the referenced plan file's SHA-256
  still matches what was approved at dispatch time.
- `python3 scripts/cmux_task_watchdog.py status --worktree . --kind task` /
  `--kind reviewer` — current watchdog state file as JSON.
- `cat .task-needs-attention.json` (if present) — pending/resolved escalation,
  category, reason, question.
- `cat .task-close-armed.json` / `.review-close-armed.json` (if present) —
  close is armed and waiting for the agent process to exit.
- `git status --short` inside the worktree — anything outside
  `.task-*`/`.review-*`/`.wiki-*` and gitignored `.vault-meta/` here is what
  blocks auto-close and what `/reap-send` will ask about.
- `python3 tests/test_task_lifecycle.py`, `python3
  tests/test_contract_schemas.py`, `bash tests/test_review_dispatch.sh` —
  hermetic coverage for the contract/lifecycle/review-dispatch code in this
  document; part of `make test`.
- `python3 scripts/pipeline-stats.py --days 30` — content-free task, review,
  escalation, watchdog, surface, and p50/p95 dogfood measurements. Metric
  definitions and small-sample guidance are in
  [`pipeline-observability.md`](pipeline-observability.md).

## Known trade-offs

- The watchdog's screen-diff heuristic can't distinguish "silently
  thinking for a long time" from "actually stuck" — it alerts on *no visible
  change*, not on *no progress*, by design (never sends input to check), so
  the 15/20-minute thresholds are a floor for human judgment, not proof of a
  hang.
- Auto-close only ever targets the exact surface recorded in metadata; it
  will never guess a UUID or close "the task pane" by inference — the flip
  side is that a worktree with a manually-edited or missing surface file
  degrades to "leave the surface open" rather than closing anything.
- Unattended review resolution is bounded (`max_verify_iterations`, default 2
  here) specifically so a persistently `changes-requested` loop terminates in
  an escalation instead of looping forever between executor and reviewer.
- Reviewer read-only enforcement is a permission-surface guarantee (pinned
  CLI flags plus a locked tool allowlist), not a sandbox: Codex reviewers get
  OS-level `-s read-only`; Claude reviewers get tool-level denial via
  `dontAsk` + allowlist, which is enforced by Claude Code's permission system
  rather than the OS.

## See also

- [`docs/runtime-capabilities.md`](runtime-capabilities.md) — which hook
  surfaces are Claude-only vs. shared across Claude/Codex.
- [`docs/pipeline-observability.md`](pipeline-observability.md) — lifecycle
  metrics, privacy contract, and the real-task acceptance window.
- `skills/dispatch/SKILL.md`, `skills/review-dispatch/SKILL.md`,
  `skills/reap-send/SKILL.md` — the authoring-level skill flow this page
  assumes.
