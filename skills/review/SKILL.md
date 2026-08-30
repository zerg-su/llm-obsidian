---
name: review
description: Review outcomes, code, architecture, security, or specs through harness-owned Simple, Deep, or Full presets. Use before finalization.
allowed-tools: Read Glob Grep Bash Agent
---

# Review

Use after self-review, before finalization. Harness owns routing, sessions,
identity, callbacks, and budgets. Reviewers are product read-only; the executor
resolves findings.

Summary/reap unlock only when approval matches exact HEAD/profile and v4 summary
bytes. Only `--no-review` persists a typed bypass.

## Light Review

`review --light` is a separate, advisory path. It delegates one read-only
`native-subagent` on the current host and returns its result only in the current conversation.
It never starts Harness, writes review state, approves/finalizes a HEAD, or enters a
fix loop. `review`, `review --deep`, and `review --full` remain Lifecycle Review.

Accepted Light options are `--target`, `--base`, repeatable `--paths`,
`--include-untracked`, `--verify`, `--model`, and `--effort`. The target defaults to
the current Git root. Untracked files are excluded unless explicitly included.

Use the installed package root that owns this skill as `TOOL_ROOT`. It is read-only
tool/config authority, never an automatic coordinator vault. Build the snapshot with
`python3 "$TOOL_ROOT/scripts/review_target.py" light`; pass `--target`, `--base`, each
path as `--path`, and `--include-untracked` exactly as requested. The command emits
JSON to stdout and writes nothing. Stop before delegation on invalid Git/base/scope.

Resolve the child with:

```bash
python3 "$TOOL_ROOT/scripts/model_routing.py" --root "$TOOL_ROOT" \
  light-review --session-id "$("$TOOL_ROOT/scripts/current-session-id.sh")" \
  --model MODEL --effort EFFORT
```

Omit absent overrides and omit `--session-id` when the confirmed route is already in
the runtime environment. Default is exact current model and effort. Overrides must
stay on the current runtime: Claude aliases include Fable/Opus; Codex aliases include
Sol/Terra. A cross-runtime alias or a host that cannot enforce the resolved child
model/effort fails visibly before delegation; never silently substitute another
model, parent synthesis, shell-launched Claude/Codex, or Lifecycle Review.

On Codex, delegate through built-in Agent to project agent `light_reviewer`. On
Claude, delegate through Agent to plugin agent `llm-obsidian:light-reviewer`. Give it
the exact snapshot JSON, route JSON, target root, and `verify` boolean. Keep the same
child thread for one correction only. Do not open another window or agent.

Pipe the JSON-only result through `scripts/light_review_contract.py` with the exact
snapshot digest/runtime/model/effort. On one validation failure, paste the validator
error into the same child thread and validate its single correction. A second failure
returns an incomplete advisory result; never weaken the schema or fall back.

After validation, repeat the same snapshot command. If its digest differs, return
`incomplete` with a snapshot-drift coverage gap and do not auto-rerun. With `--verify`,
the child may run only safe, bounded, directly relevant checks. A check that needs
target writes or extra permission is `not-run` and a coverage gap. Without `--verify`,
run no test suite. Never write target files, Harness records, archives, or wiki pages;
the user may separately invoke `/save`.

## Lifecycle coordinator selection

Lifecycle Review separates the state-owning LLM Obsidian coordinator from the Git
target. Resolve the coordinator in this order: explicit `--vault-root`, verified
`LLM_OBSIDIAN_PROJECT_ROOT`, an LLM Obsidian ancestor of the current directory, then
one explicitly registered default. Invalid higher-priority input fails closed; never
fall through to a plugin cache or guess a sibling vault.

From the intended coordinator root, register the default only on an explicit user
action:

```bash
python3 scripts/review_coordinator.py register --vault-root "$PWD"
```

Registration writes only `~/.config/llm-obsidian/coordinators-v1.json` with private
permissions. Re-register the prior root or explicitly remove that file to roll back;
review never deletes or rewrites the registry automatically.

## Lifecycle target lease

Current-checkout Lifecycle Review starts only when the target is on a commit, has no
Git operation in progress, and its index, tracked worktree, and non-ignored untracked
set are clean. It records an observational lease over that exact target key, HEAD,
index tree, tracked worktree diff, and untracked-file content. Recheck the lease before
consuming callbacks and immediately before every provider effect. Any drift while the
gate is active is `attention-required`; do not accept the stale result, stash, commit,
reset, or create a worktree automatically.

`changes-requested` is the one released boundary: the executor may edit and commit the
fix, then resume only from a clean new HEAD with the same preset and purpose. Write the
typed finding resolution to the owner-only `resolution_path` returned by the prior
receipt. The coordinator rebinds the same task lineage and advances its bounded cycle;
it never writes `.task-review-resolution.json` or other Harness metadata into an
external target. If concurrent work is expected, recommend a user-created dedicated
branch/worktree before starting review.

This lease detects state at each observation boundary; a modify-and-revert that leaves
identical observable Git state between checks cannot be proven. Treat the reviewed
snapshot, not elapsed wall time, as the authority.

## Presets

- `review`: one holistic session on the selected model;
- `review --deep`: independent Anthropic/OpenAI holistic sessions at `xhigh` by
  default; an alias-backed `--runtime` or `--model` instead selects independent
  intent/engineering sessions on that model only;
- `review --full`: only when explicit, the four-lane
  `{Anthropic, OpenAI} × {intent, engineering}` grid at `xhigh`;
- `--cross-model`: Simple selects the opposite runtime. Deep/Full already use
  both providers; explicit overrides remain authoritative.

Deep/Full use `review_profiles.deep`; overrides require routing aliases. Full
is never inferred, rejects overrides, and cannot combine with `--deep`. Lane
IDs use `anthropic-*`/`openai-*`; concrete routes stay metadata.

Standalone Deep is unchanged. Finalization cycles 1–3 use only
`finalization-primary`; the third material failure freezes a read-only pivot packet;
cycles 4–5 add `finalization-independent` only after its accepted receipt,
without an availability probe. Explicit single-model always wins.

## Purpose boundaries

Use one purpose:

- `intent`: Outcome Contract, plan/design digests, dispositions and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` binds risk/receipts to terminal gate bytes; digest drift
stales them. Purpose review binds `--purpose` and `--boundary-input`. Plans use
`plan --plan <repo-plan>` (never legacy `current --plan`) to select `intent`,
compile protected artifacts, and resolve exact OIDs before launch.

## Outcome-first judgment

Implementer summaries/reports are claims. Holistic/intent lanes classify each
success-evidence item `established`, `missing`, or `contradicted` by inspection
and check non-goals for scope creep. Holistic/engineering lanes read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely and report its whole six-section review denominator, even when a
section is clean; repository-specific standards override its heuristics.
Transport, clean diffs, and local green are not outcome proof. Verify findings
against code; rejection requires technical evidence. Add no hidden lane, model
call, severity cap, reranking, vote, average, or loop.

Keep task evidence reviewer-observable at verdict;
[implementation-plan](../implementation-plan/SKILL.md) puts later evidence in
parent-owned `Post-review coordinator acceptance` outside its Outcome Contract.
The strict missing-evidence policy must not be weakened for a circular task
contract; return it for amendment.

## Flow

1. Dispatched v3/v4 tasks use `task-review-runner.py run --worktree <worktree>`.
   Plans use `plan --worktree <checkout> --plan <plan>` (add exact `--base`
   unless one parent changes it); otherwise use `current --target <checkout>
   --vault-root <coordinator>` with compatible preset, purpose, and boundary.
   Generated callbacks retain the exact low-level `--worktree` target plus
   `--vault-root`. Facade starts/resumes/
   returns a receipt; `review-runner.py` is low-level.
2. Keep ContextPacket/outbox owner-only and product read-only. Submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep lanes independent. Before effect, `FinalizationLedger` reserves each
   fresh exact-HEAD attempt and immutable terminal result. Material
   `changes-requested`/`approved` consumes a product cycle; mechanism outcomes
   release the slot into a bounded receipt. A clean resolved HEAD plus owner-only
   resolution evidence advances the same current-review cycle; cycle 4 needs the
   accepted pivot receipt. A fifth material failure
   exhausts the lineage; a sixth cycle has zero effect. Standalone keeps preset
   budgets.
4. The executor records typed rulings/checks and escalates protected boundaries.
   A plan finding may rebind retained lanes only when the exact Git delta changes
   the design artifact alone; Outcome, dispositions, or evidence-map changes
   require an amendment and fresh boundary.
5. After accepted receipts, terminal approval exits provider, then closes only
   its surface. Archive only exact operation/worktree/HEAD/profile evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; a second restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, rerank, push, publish,
or broaden scope. Dispatch paths come from `.task-meta.json`, never a generic
root; current review uses derived harness state and external owner-only scratch.
