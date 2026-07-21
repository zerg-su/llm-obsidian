"""Versioned shared prompt renderer for all acceptance cells."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROMPT_CONTRACT_VERSION = 1
VAULT_REINDEX_SCENARIOS = {
    "agenda-carry", "protected-web", "vault-capture", "obsidian-authoring",
    "cmux-lifecycle", "daily-summary", "dispatch-review-reap",
    "vault-maintenance", "ingest",
}

def prompt_text(
    row: dict[str, Any], scenario: dict[str, Any], sandbox: Path, outbox: Path,
    model: str, effort: str, commit: str, fixture: str,
    runner_fixture: dict[str, str] | None = None,
) -> str:
    reindex_contract = ""
    if row["scenario"] in VAULT_REINDEX_SCENARIOS:
        reindex_contract = """- When a fixture validates product pages after a `vault-write.py` mutation, run `python3 scripts/reindex.py`
  before the whole-vault validation. Refreshing this derived index is normal fixture procedure, not a
  product repair. Do not add an extra whole-vault validation after the fixture's final page cleanup.
"""
    if row["skill"] == "close":
        cleanup_contract = f"""- This current acceptance surface is the only session fixture. Do not create or close another surface.
- Leave the exact saved page in place for runner proof. The runner owns its transactional deletion,
  disposable bookkeeping, operation outbox, clone, scratch directory, and exact surface cleanup.
- Do not remove `.acceptance-sandbox.json`, pass `--tmp-root`/`--state-root`, or override `TMPDIR`/`TMP`/`TEMP`."""
    elif row["skill"] == "autoresearch":
        cleanup_contract = f"""- Complete one protected research run and independently validate its filed pages and provenance.
- Leave the exact filed output pages and any generated index links in place for runner proof. The runner
  resolves the one operation-bound run state, validates the vault, removes those exact outputs through
  one optimistic vault-write transaction, restores tracked indexes, and cleans the disposable clone.
- Close external processes through the documented autoresearch lifecycle, but do not manually delete
  product pages, indexes, task-session state, `.acceptance-sandbox.json`, the operation outbox, or scratch.
- After `research-isolation.py start` or `receive` returns, finish the coordinator turn immediately and
  wait for the exact typed callback. Do not poll marker/state files, inspect child screens, sleep, or loop;
  the callback starts the next turn and preserves the observable unattended lifecycle.
- Do not run `git restore`, `git checkout`, `git stash`, pass `--tmp-root`/`--state-root`, or override
  `TMPDIR`/`TMP`/`TEMP`."""
    elif row["scenario"] == "dispatch-review-reap" and runner_fixture is None:
        cleanup_contract = f"""- Treat every nested branch, worktree, task-session entry, and review operation under
  `{sandbox / '.vault-meta' / 'acceptance-worktrees'}` as runner-owned lifecycle proof.
- Exercise the named skill and close its task/reviewer processes through the documented lifecycle, but do not
  manually remove, prune, merge, restore, stash, or rewrite runner-owned branches, worktrees, registry state,
  review artifacts, `.acceptance-sandbox.json`, the operation outbox, or scratch.
- Leave the exact task-bound plan, reap result, and review archive pages in place. The outer runner resolves
  them from durable task/review markers, rejects any unbound product residue, and deletes the whole clone.
  Clean only unbound fixture scratch; never rewrite writer-owned log/hot bookkeeping.
- Use `LLM_OBSIDIAN_WORKTREES` for every nested dispatch; do not invent another worktree root or pass
  `--tmp-root`/`--state-root`, and do not override `TMPDIR`/`TMP`/`TEMP`."""
    elif runner_fixture is None:
        cleanup_contract = f"""- Clean every disposable page, branch, worktree, surface, process, and scratch file you create before reporting pass.
- Do not remove `.acceptance-sandbox.json`; it is the runner-owned cleanup marker.
- The runner owns the disposable clone, its ignored task-session registry, the operation outbox,
  `{sandbox / '.vault-meta' / 'acceptance-worktrees'}`, and its run-scoped temporary directory.
  Use `LLM_OBSIDIAN_WORKTREES` for every nested dispatch; do not invent another worktree root.
  Do not run `git restore`, `git checkout`, `git stash`,
  manually delete those runner-owned paths, pass `--tmp-root`/`--state-root`, or override
  `TMPDIR`/`TMP`/`TEMP`. Remove the fixture's product output and close external processes/surfaces;
  the runner validates allowed vault bookkeeping and deletes the clone.
- Validate product output before removing disposable pages. After removal, append-only log/hot/index
  bookkeeping may still name those discarded pages; report it as runner-owned disposable bookkeeping
  instead of requiring a second whole-vault validation or treating it as a product failure. Never put
  `wiki/log.md` or `wiki/hot.md` in cleanup `pages` operations: they are writer-owned and intentionally
  retain this disposable history until the outer runner deletes the clone."""
    elif runner_fixture.get("fixture_kind") == "review":
        cleanup_contract = f"""- The exact approved task at `{runner_fixture['nested_worktree']}` is runner-owned proof.
- Do not create or approve a plan, task, branch, worktree, or metadata, and never enter Plan Mode.
- After starting review, return idle without polling; typed callbacks start later turns automatically.
- Close the reviewer through verified `finish`. Leave task plan/worktree/branch/registry/review artifacts
  in place for outer proof and clone cleanup. Clean no runner-owned lifecycle state manually."""
    else:
        cleanup_contract = f"""- This cell's plan, result page, review archive, task branch, exact nested worktree,
  task-session registry, and lifecycle markers are runner-owned proof artifacts. Leave them in place.
- Close the task and reviewer agent processes through their documented lifecycle. Do not manually close,
  delete, merge, restore, stash, or rewrite their branches, worktrees, registry, or proof artifacts.
- Do not remove `.acceptance-sandbox.json`; it is the runner-owned cleanup marker.
- Use only the exact runner-bound nested worktree `{runner_fixture['nested_worktree']}`.
  Do not pass `--tmp-root`/`--state-root` or override `TMPDIR`/`TMP`/`TEMP`.
- After `dispatch-runner.py start` returns, finish the coordinator turn and return
  to the idle prompt. Do not shell-poll task files, inspect cmux in a loop, or call
  agent wait tools. Typed review/reap callbacks begin later turns automatically.
- Do not publish the acceptance agent outbox in that launch turn. Publish it only
  in the later final-reap callback turn, after the durable lifecycle proof below
  has been validated; returning idle without an outbox keeps this cell running.
- If the single exact `dispatch-runner.py start` invocation exits non-zero, do
  not retry it, perform open-ended diagnosis, or clean runner-owned state.
  Publish the typed fail/blocked outbox immediately with that bounded error;
  the acceptance runner owns containment and exact cleanup for this path.
- After the exact final reap runner returns `status: complete`, publish the
  typed pass outbox immediately. Do not enumerate proof files or invoke
  `dispatch_acceptance_proof` yourself: the outer acceptance runner performs
  that independent durable-proof check before accepting the cell.
- Validate the task result before the typed outbox. The runner independently proves the exact commit,
  typed review, archived task, final reap, and plan closure, then deletes the disposable clone."""
    if row["skill"] == "close":
        final_contract = f"""For this close fixture only, the typed outbox is the penultimate action. Write it before exit,
then make your final tool call exactly `python3 scripts/queue-session-exit.py`. Perform no tool calls
after that command and end the turn immediately. The outer runner independently proves process exit,
surface retention, exact-surface cleanup, and removal of the saved fixture page."""
    elif row["skill"] == "autoresearch":
        final_contract = (
            "Do not merely describe a hypothetical test. After validating the protected run and its "
            "filed pages, write the outbox as the final action; runner-owned cleanup begins afterward."
        )
    else:
        final_contract = "Do not merely describe a hypothetical test. The outbox is the final action after cleanup."
    if runner_fixture is not None and runner_fixture.get("fixture_kind") == "review":
        interaction_contract = (
            "This fixture spans typed callback turns. After each launch, end the current turn at the "
            "idle prompt; continue only when the trusted callback starts the next turn. Never use Plan "
            "Mode, ask a human, or shell-poll while waiting."
        )
    else:
        interaction_contract = (
            "Complete this fixture in one bounded agent turn. If the named skill would normally "
            "ask the user a question or present a quiz/draft for a later reply, that observable "
            "response is the end of this acceptance interaction: record it and continue to "
            "cleanup and the typed outbox instead of waiting for another human message."
        )
    return f"""# Live release acceptance operation

You are running one real, bounded acceptance cell in a disposable local clone.

- Phase: `{row['phase']}`
- Runtime: `{row['runtime']}`
- Effective model: `{model}`
- Effective effort: `{effort}`
- Skill: `{row['skill']}`
- Scenario: `{row['scenario']}`
- Expected: {row['expected']}
- Network class: `{scenario['network']}`
- Source commit: `{commit}`

Read `{sandbox / 'skills' / row['skill'] / 'SKILL.md'}` completely and exercise that skill faithfully.
Scenario instructions: {scenario['instructions']}

Exact skill fixture (treat this as the complete end-user request for the cell):

> {fixture}

{interaction_contract}

Hard boundaries:

- Work only inside `{sandbox}` and disposable nested paths it creates.
- Never push, publish, deploy, send communication, access credential material, or mutate the source checkout.
- Native Claude/Codex processes and their opposite-model reviewers may use an already authenticated
  subscription session. Never read, copy, print, export, or request its credential material.
- A public web read is allowed only when the declared network class permits it.
- If authentication is required, return `blocked` and name only the credential class; never print a value.
- Install nothing unless it is already covered by an explicit local noninteractive fixture. Missing optional dependencies must produce a visible blocked/degraded result.
{reindex_contract}{cleanup_contract}
- Exercise the exact live fixture once. Do not precede it with a `--no-spawn`/dry-run copy of the flow.
- Preserve real first-failure evidence; do not turn a retry into a clean pass without mentioning it.
- An acceptance cell must not repair or edit product scripts, skills, tests, hooks, or configuration. If a
  repo-owned mechanism fails, preserve the evidence and report it; the outer coordinator owns any fix and rerun.

Finally write exactly one JSON object to `{outbox}` using this shape:

```json
{{
  "schema_version": 1,
  "phase": "{row['phase']}",
  "skill": "{row['skill']}",
  "runtime": "{row['runtime']}",
  "scenario": "{row['scenario']}",
  "verdict": "pass | fail | blocked | n-a",
  "model": "{model}",
  "effort": "{effort}",
  "actual": "bounded observed behavior",
  "cleanup": "bounded cleanup proof",
  "evidence": "bounded commands/artifacts/status proof without content or secrets",
  "defect": "required for fail/blocked",
  "decision": "required for n-a"
}}
```

{final_contract}
"""
