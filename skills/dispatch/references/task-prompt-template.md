# Harness task prompt template

`dispatch-runner.py` renders the approved-plan branch below. The legacy branch
marker remains only as a deterministic template delimiter; v2.3 dispatch
rejects non-approved-plan requests.

```markdown
# Task: <task_name>

## Task description

<description from user, multi-line ok>

## Wiki context (pre-loaded)

- [[<wiki-page-1>]] — <one-line summary>
- [[<wiki-page-2>]] — ...
- [[<wiki-page-3>]] — ...

## Suggested sub-agents (optional, hint)

<generated only when the approved request contains explicit agent hints>

## Wiki access (read-only, live as you go)

The knowledge vault is `<vault-root>/wiki/`. Read `hot.md`, then `index.md`,
then the relevant domain index before an individual page. Do not edit anything
under `<vault-root>` from this task worktree.

## Working rules

- Writable task worktree (the only product checkout you may edit): <worktree-path>
- Source repository (read-only reference; never cd, edit, stage, or commit here): <repo-path>
- Base branch: <base-branch>; task branch: task/<task_name>
- Codex environment: <codex-home/profile or inherited>
- Review workflow: <review-skill>
- Reap workflow: <wiki-reap-command>
- Review/model defaults come from `<vault-root>/config/model-routing.toml`.
- Commit explicit files as you go. Never push, deploy, publish, delete the
  worktree/branch, or expand scope.
- Before every Git write, confirm that `pwd` is the writable task worktree and
  the current branch is the task branch. Stop and escalate on any mismatch.
- Preserve unrelated dirty work and `.obsidian` user state.

<!-- BRANCH A: rendered ONLY in plan-mode (instead of branch B) -->

## Approved plan (already reviewed — execute)

Read `<absolute path to wiki/plans/<file>.md>` first. Echo its goal and steps in
at most ten lines, then execute without another approval.

Validate the read-only contract with:
`python3 <vault-root>/scripts/task_contract.py validate`.
Treat `.task-meta.json` as read-only. With
`interaction_policy=unattended`, plan, review, verify, finish, and reap are
already authorized within the contract.

The watchdog is observer-only: it never sends you input, cancels work, or
closes a surface. The supervisor handles only exact native trust and
armed-exit dialogs.
Never wait on a permission prompt in this background pane.

Use `python3 <vault-root>/scripts/task_escalation.py raise ...` for a material
fork, scope drift, permission, dependency, security, public-interface,
migration, destructive action, or external effect, then remain paused.

For a defective repository-owned script, hook, skill, schema, callback, or
adapter: contain state; perform read-only diagnosis; raise
`mechanism-failure`; state the failed stage and mutation status; request coordinator classification;
and Remain paused. The coordinator may authorize a
narrow local reversible repair, but must ask the user at any permission,
dependency, security, public-interface, migration, destructive,
external-effect, scope, or ambiguous-state boundary. Follow
`<vault-root>/docs/skill-references/failure-repair-contract.md`.

<!-- END BRANCH A -->

<!-- BRANCH B: rendered ONLY in classic-mode (instead of branch A) -->

## Unsupported classic branch

This runner requires an approved plan and fails before launch otherwise.

<!-- END BRANCH B -->

## Harness completion

Use `scripts/harness-cli.py status|inspect|resume|reconcile|cancel|close|doctor`
only for a typed escalation, `attention-required`, or explicit coordinator
request; do not orchestrate cmux/model commands manually.
`scripts/harness-cli.py dashboard` is read-only: it projects the compiled
pipeline, parallel lanes, loop visits, and bounded recent issues for one owner,
and holds no lifecycle authority. Anything it cannot resolve exactly is
classified `request-coordinator-classification` rather than guessed.
Unknown ownership, prompt, callback, or upgrade state becomes
`attention-required`.

Commit the explicit product files, run the configured verification profile,
then write `.task-summary.json` with exactly this canonical JSON shape to
trigger the automatic review gate:

```json
<canonical-task-summary-json>
```

Do not
invoke a review runner or orchestrate its provider/cmux lifecycle yourself.
End the current model turn while keeping this session open. The code-owned observer owns healthy waiting; act again in this same session only on a typed callback wake, typed escalation, or explicit coordinator request. Material findings arrive in `.task-review.json` plus one typed surface notification.
Apply or reject every finding in a new commit, or use the normal escalation
contract; do not invoke the review runner. The harness continues same-session
verification and authorizes finalization. The provider worker delivers the
approved Wiki Summary through the internal callback broker; the coordinator owns
the one reap transaction and terminal cleanup. The task does not send a separate
reap command.
```
