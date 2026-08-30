# Lifecycle Review

## Coordinator and target

The LLM Obsidian coordinator owns state; the Git target may be another repo.
Resolve coordinator priority as explicit `--vault-root`, verified
`LLM_OBSIDIAN_PROJECT_ROOT`, LLM Obsidian cwd ancestor, then explicitly
registered default. Invalid higher priority fails closed; never use plugin cache
or guess a sibling vault. Explicit registration only:

```bash
python3 scripts/review_coordinator.py register --vault-root "$PWD"
```

This writes private `~/.config/llm-obsidian/coordinators-v1.json`; review never
rewrites or removes it automatically.

Current-checkout review starts only on a commit with no Git operation and clean
index, tracked tree, and non-ignored untracked set. Its observational lease
binds target key, HEAD, index/worktree, and untracked content. Recheck before
callbacks and every provider effect. Active drift rejects stale results and
writes typed attention; never stash, commit, reset, or create a worktree.

Without `--base`, current review deliberately packages only `git show HEAD`.
For a multi-commit task, pass `current --base <ref>`: Harness resolves the
merge-base to an exact commit, binds it in policy and lease, packages the
cumulative `base..HEAD` diff plus both exact object IDs, and repeats the exact
resolved base in callback wake commands. The base must remain an ancestor after
a committed resolution; never infer a branch base for approval-capable review.

`changes-requested` releases the executor boundary. Resume only after the fix is
committed to a clean new HEAD with the same preset/purpose. Write typed finding
resolution to the receipt's owner-only `resolution_path`. The coordinator
rebinds the same bounded lineage and never writes `.task-review-resolution.json`
into an external target. For concurrent work, recommend a user-created dedicated
branch/worktree before review. Modify-and-revert between observations cannot be
proven; the observed snapshot is authority.

## Presets and routing

- Simple: one holistic session on the selected model.
- Deep: default independent Anthropic/OpenAI holistic sessions at `xhigh`; an
  alias-backed model/runtime override instead uses independent intent and
  engineering lanes on that model.
- Full: explicit only, four lanes `{Anthropic, OpenAI} × {intent, engineering}`
  at `xhigh`; rejects overrides and `--deep`.
- `--cross-model`: Simple chooses the opposite runtime. Deep/Full already span
  both; explicit overrides win.

Finalization cycles 1–3 use `finalization-primary`. The third material failure
freezes a read-only pivot; cycles 4–5 add `finalization-independent` only after
its accepted receipt. Five material failures exhaust the lineage; cycle six has
zero effect. Explicit single-model remains authoritative.

## Flow

1. Resolve the exact target/coordinator and use the public runner. Generated
   callbacks bind both exact roots. Current review uses external owner scratch.
2. Keep ContextPacket/outbox owner-only and product read-only. Submit axis JSON
   only through the generated `harness/review_submit.py` command.
3. Keep lanes independent. Reserve each exact-HEAD attempt before provider
   effect. A clean resolved HEAD plus owner-only evidence advances one cycle.
4. Record typed rulings/checks and escalate protected boundaries. Plan-only
   design changes may rebind retained lanes; Outcome, dispositions, or evidence
   map changes require amendment and fresh boundary.
5. Accepted terminal approval exits the provider, closes only its surface, and
   archives exact evidence. One changed scope boundary permits one compact
   reevaluation; then require attention.

Dispatch paths come from `.task-meta.json`, never a generic root. Do not add
topology, rerank, edit product, open a second verification surface, or broaden
scope.
