---
name: dispatch
metadata:
  version: 1.4.0
description: Spawn an isolated Claude/Codex task worktree in cmux and hand it an approved plan; requires cmux.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /dispatch — approved task handoff

Use this skill to open an isolated task to the right of the current cmux
surface. The coordinator shapes and approves the plan; the task executes it.
The default route inherits the current session. Do not use a same-runtime
external split when an internal agent satisfies the request unless the user
explicitly asks for a visible persistent session.

## Normal path

1. Require `cmux` before parsing. If unavailable, explain the dependency and
   offer to continue in the current session; do not substitute tmux/background
   shell work.
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
   reap type/title, and review mode. Omit caller identity fields normally: the
   runner binds `CMUX_SURFACE_ID`, current session ID, and host-confirmed route.
   It never inspects the globally focused surface.
6. Show the typed route/hash and one echo-confirm block:

   ```bash
   python3 <vault-root>/scripts/dispatch-runner.py validate --spec <request.json>
   ```

   Include exact repo/base/branch/worktree, runtime/model/effort, plan, selected
   context, `interaction_policy`, review/reap/surface/watchdog policy, and all
   forbidden effects. Ask once. Do not mutate or spawn before explicit approval.
7. After approval, start the exact UUID once:

   ```bash
   python3 <vault-root>/scripts/dispatch-runner.py start --spec <request.json>
   ```

8. Show the bounded typed launch result. When it returns
   `coordinator_action: return-to-idle-without-polling`, end this turn. Do not
   poll, wait, or run monitors; typed callbacks resume the idle coordinator.

## Runner contract

`dispatch-runner.py` owns worktree creation, route sync, prompt rendering,
v3 `.task-meta.json`, exact task identity, an anchored right split, supervisor
launch, verification, and one `vault-write.py` log transaction. A UUID is
claimed before mutation; launched requests are idempotent, while preparing or
failed requests fail closed. A pre-launch blank child may close only by its
exact surface UUID.

The metadata retains `interaction_policy`, `approved_plan_sha256`,
`forbidden_actions`, and `watchdog_policy`. `cmux_agent_supervisor.py` owns
runtime argv, trust prompts, process lifecycle, watchdog, and close-after-exit.
Unattended Codex stays `-a never` + `workspace-write`, with exact Git/session
write roots, `DCG_CONFIG`, localhost-only loopback policy, and trusted `PATH`.
Never weaken those controls or reproduce their shell commands manually.

The task may auto-repair only eligible repo-owned mechanism failures under the
central failure-repair contract. Scope/security/permission/external decisions
escalate to the coordinator. Push, deploy, publish, worktree deletion, and
scope expansion remain forbidden.

## Output

Report task name, branch, worktree, exact cmux surface, runtime/model, and that
the task returns only for escalation or final lifecycle callbacks. Branch and
worktree remain local. A successful dispatch is a launch, not task completion.

## Compatibility and recovery

Only for explicit classic interactive mode, old metadata, or read-only failure
diagnosis, load [compatibility.md](references/compatibility.md). <!-- context:conditional -->

Never clone without explicit approval, overwrite/delete an existing worktree,
write `wiki/hot.md`, use `cmux new-workspace`, or execute the delegated task in
the coordinator.
