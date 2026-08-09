---
name: dispatch
metadata:
  version: 1.7.0
description: Dispatch approved work through cmux or registered ephemeral review/schema execution.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /dispatch — approved task handoff

Open an isolated task under an approved pipeline. The coordinator approves the
plan; the task executes it. Inherit the current route. Continuable executor
work opens to the right of the cmux surface. A bounded review or schema-producing
step may instead use the compiled ephemeral profile below.

## Normal path

1. Read the approved execution profile before requiring `cmux`. Interactive or
   continuable work requires cmux; if unavailable, explain the dependency and
   offer to continue in the current session. Do not substitute tmux/background
   shell work. Only a registered bounded review/schema step may use a code-owned
   ephemeral adapter without cmux.
2. Parse a bounded description, `task_name`, repo name/path, base/branch intent,
   optional runtime/model/effort override, and optional explicit plan.
3. Resolve read-only Phase 1 through code:

   ```bash
   python3 scripts/dispatch-resolver.py --request <phase1.json>
   ```

   It must return one exact repo and one pending approved plan. Missing or
   ambiguous candidates are shown for selection; never guess. An explicitly
   named cross-session plan is allowed and remains visible as such. It returns
   at most five ranked context candidates; the model selects semantic relevance.
   Before echoing/logging a wikilink, verify that its exact target exists under `wiki/`.
4. Resolve the child route through `scripts/model_routing.py`. No override means
   the exact current-session route; named model/effort is explicit. Unknown
   model routing fails closed. Codex home/profile/plugin commands come from the
   target repo or coordinator `.codex/dispatch-env.toml`; never install or
   create them implicitly.
5. Write one unique ignored request at
   `.vault-meta/dispatch-requests/<request-id>.json` containing schema version,
   canonical UUID, task/description, absolute vault/repo/worktree, branch/base,
   absolute pending `plan_file`, optional executor override, verified context,
   reap type/title/`plan_mode`, and a review object (`mode`, `cross_model`, and optional
   expert `runtime`/`model`/`effort`). `skip` cannot carry review overrides.
   Freeze one code-owned pipeline selected during clarification:
   `lifecycle/default`, `engineering/change`, or `engineering/fix`. A fix also
   freezes `completion_policy=attention|autonomous`; other pipelines use
   `attention`. Never infer autonomous mode after approval. If the deterministic
   selector proves that no built-in expresses the approved task, propose one
   strict `PipelineSpec` from `schemas/pipeline-spec-v1.schema.json` under the
   same ignored scratch, set `pipeline=custom`, and include its absolute
   `custom_pipeline_spec`. Allow only registered sequential primitives/checks,
   typed transitions, bounded loops, `executor-default`, and approved context
   hashes. Reject commands, arbitrary paths/providers, and authority expansion.
   Custom is only for a proven semantic gap.
   Persist the optional additive `finalization_policy` exactly as compiled:
   at most five product cycles, `execution=ephemeral`, and only the registered
   `finalization-primary` and `finalization-independent` aliases. The public
   spec never names a provider CLI transport; cycle accounting and the pivot
   stay with the review skill and `FinalizationLedger`.
   Omit caller identity fields normally: the
   runner binds `CMUX_SURFACE_ID`, current session ID, and host-confirmed route.
   It never inspects the globally focused surface.
   Before built-in validate/start, when cmux is available, idempotently run
   `python3 <vault-root>/scripts/harness-dashboard.py open --vault <vault-root>
   --store <vault-root>/.vault-meta/harness --surface "$CMUX_SURFACE_ID"
   --root "<request-id>"` with the exact approved request UUID as the root, so
   the split observes only that request and its descendants; validate echoes
   the same command as `observer.argv`. Continue after a
   contained display failure because this observer remains
   external to Harness ownership.
6. Run `python3 <vault-root>/scripts/dispatch-runner.py validate --spec
   <request.json>` and show its typed route/hash echo-confirm block. Include the
   exact target, route, plan/context, interaction/review/reap/surface/watchdog
   policy, forbidden effects and, for custom, baseline delta, enforceable plus
   inherited authority, limits, stops and outcomes. Custom validation persists
   an owner-only `challenge_sha256` bound to every material input and origin;
   it is not authorization.
7. Run `python3 <vault-root>/scripts/dispatch-runner.py approve --spec
   <request.json> --challenge-sha256 <exact-validate-challenge>`. Only the
   host dialog can choose approve/reject/revise; argv/stdin cannot. Reject/revise
   are terminal. Approve returns `approval_token`.
8. Start once with `python3 <vault-root>/scripts/dispatch-runner.py start --spec
   <request.json> --approval-token <exact-one-shot-token>` for custom, omitting
   the token for built-ins. Never synthesize decisions/tokens or reuse a token;
   start consumes it atomically and rejects drift before effects.
9. Show the bounded typed launch result. When it returns
   `coordinator_action: return-to-idle-without-polling`, end this turn. Do not
   poll, wait, or run monitors; typed callbacks resume the idle coordinator.

## Runner contract

`dispatch-runner.py` owns worktree creation, route sync, prompt rendering,
v3 metadata, identity, and one `vault-write.py` transaction. The generic provider runtime owns the anchored split/workspace,
provider launch, callback relay, resume, and exact cleanup. A UUID is
claimed before mutation; launched requests are idempotent, while preparing or
failed requests fail closed.

The metadata freezes the exact review preset, its deterministic simple/deep/skip
budget (1/2/0), and the coordinator verification profile digest. Reap mode
`final` closes its plan; `shared` retains an unchanged pending plan.
Metadata binds `interaction_policy`, `approved_plan_sha256`,
`forbidden_actions`, and `watchdog_policy`.
`engineering/fix` runs one persistent executor
session through exact harness prompts for `reproduce`, `root-cause`,
`regression-test`, and `minimal-fix`. The model submits a bounded result through
`pipeline-step-submit.py`; the coordinator owns immutable receipts and never
replays an accepted phase. `attention` allows two total fix passes and returns a
typed decision when exhausted; explicitly approved `autonomous` allows three
and then fails terminally. Mechanism/security failures always return attention.
An approved custom definition uses the same store, FSM, supervisor, provider
session, verification, review, and reap boundary. The executor handles only the
current registered `.task-pipeline-step-request.json`; the harness selects the
next typed transition, enforces traversal/model-call limits, and resumes from
the first missing receipt. Stable typed results without a callback are submitted
by code without another model call. A content-free observer probes every 60
seconds, marks ten-minute idle, permits at most one 15-minute nudge and one
identity-bound 20-minute restart, then creates durable attention. Disabling
`features.custom_pipeline_authoring` blocks new custom definitions without
invalidating frozen active runs or built-ins.
After the semantic pipeline and verification finish, the task writes
`.task-summary.json` and remains available. The harness then drives
`task-review-runner.py` idempotently through callback consumption, executor
resolution, same-session verification, and terminal authorization. Material
findings arrive as a typed `.task-review.json` packet plus one exact-surface
notification; the task applies or rejects every finding in a new commit, or
uses the normal escalation contract. The code-owned provider runtime, not the
task or target repository, owns reviewer launch, routing, argv, trust prompts,
watchdog, and close-after-exit.
For an ephemeral step, that runtime may launch only through a registered
code-owned ephemeral adapter after subscription preflight, fixed argv
compilation, minimal ContextPacket construction, schema validation, bounded
capability checks, and durable receipts. Ambiguous auth/billing or a missing
capability returns the typed interactive disposition before model effect; it
never falls back to paid credits, another provider, or a hidden cmux session.
Arbitrary direct print-mode dispatch/reviewer commands remain forbidden, as do
manually reproduced adapter commands.
Unattended Codex stays `-a never` + `workspace-write`, with exact Git/session
write roots plus localhost-only loopback and cmux-socket policy. `DCG_CONFIG`
and trusted-`PATH` hardening currently belong to the classic compatibility
supervisor, not this generic harness path. Never reproduce either launch path's
shell commands manually.

The task may auto-repair only eligible repo-owned mechanism failures under the
central failure-repair contract. Scope/security/permission/external decisions
escalate to the coordinator. Push, deploy, publish, worktree deletion, and
scope expansion remain forbidden.

## Output

Report task name, branch, worktree, execution profile, runtime/model, and the
exact cmux surface when one exists. State that the task returns only for
escalation or final lifecycle callbacks. Branch and worktree remain local. A
successful dispatch is a launch, not task completion.

## Compatibility and recovery

Only for explicit classic interactive mode, old metadata, or read-only failure
diagnosis, load [compatibility.md](references/compatibility.md). <!-- context:conditional -->

Never clone without explicit approval, overwrite/delete an existing worktree,
write `wiki/hot.md`, use `cmux new-workspace`, or execute the delegated task in
the coordinator.
