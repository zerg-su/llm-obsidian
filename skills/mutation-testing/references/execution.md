# Executing an isolated mutation plan

Requires the toolkit's existing Python 3 and Git on macOS/Linux. The verifier
can be any existing local command. No mutation engine is required.

Write a plan outside the source tree, using exact text from the current artifact:

```json
{
  "schema_version": 1,
  "command": ["make", "test-unit"],
  "timeout_seconds": 60,
  "mutations": [
    {
      "id": "missing-guard",
      "path": "src/limits.py",
      "before": "if count >= limit:",
      "after": "if count > limit:",
      "reason": "allow one request past the configured boundary",
      "expected_failure": "test_rejects_at_limit must observe rejection at equality"
    }
  ]
}
```

The Python filename is illustrative; paths and commands have no language filter.
Each `before` must match exactly once; `after` may be empty. Each mutation changes
one regular UTF-8 artifact. IDs are unique lowercase names, up to 48 characters;
`baseline` and `control` are reserved. A batch contains 1–8 independent mutations.
Use a fresh invocation/plan for a different verifier or changed tests.

```bash
python3 /absolute/skill-directory/scripts/run_mutations.py \
  --source /absolute/project-root \
  --plan /absolute/scratch/plan.json
```

The first output line is a newly allocated private evidence directory. Optional
`--output /absolute/new-directory` must be outside/disjoint from the project and
must not exist. Paths and commands are argv, not strings interpolated into a shell.
For a pipeline, prefer an existing project script rather than a constructed shell
command. A working directory below the root can be selected by a project-supported
argument such as `make -C module test`.

Tracked current files and nonignored untracked files are copied; deletions remain
absent. Staged and working content may differ: tests use the current working bytes,
while the original raw index and stage listing are fingerprinted, not rewritten.
Ignored files are absent unless selected with repeatable `--include dependency-dir`.
Include only dependencies required by the chosen verifier; do not copy credentials
or point imports at the original production directory. Internal symlinks are
rebased into the copy; external links, excluded link targets, special files,
unresolved index conflicts and in-scope submodules fail before verification.

For unrelated bulky trees/submodules, repeat `--exclude relative-path` only after
checking they are outside the verifier's dependency closure. Exclusions are in
`source-manifest.json` and outside its content-integrity claim. Default copy limit
is 256 MiB; `--max-bytes` can raise it deliberately for existing dependencies.
Original Git metadata is never copied. A Git-dependent build that cannot run in
this file snapshot needs another proven isolation adapter; do not weaken baseline
or alter its tests to force a pass. The helper does not preserve an index inside
the copy or reconstruct submodules/LFS content absent from the worktree.

Evidence includes `plan.json`, `source-manifest.json`, `report.json`, individual
logs, and the untouched `snapshot/`. Copies used by verifiers are removed after
each run. On SIGINT/SIGTERM, the owned process group is stopped and the report is
incomplete; a SIGKILL/crash may leave private scratch/children, so inspect only the
reported owned directory and its processes before cleanup. Never restore files
into the source or blindly resume a leftover mutant. Start a fresh batch.

`source_unchanged` compares inventoried file bytes/modes/links, current inventory,
HEAD, index bytes and staged entries before/after. It detects drift without undoing
someone else's edits. It does not monitor excluded/ignored files or prevent test
commands from accessing external paths. Baseline/final control use fresh copies;
the agent must attribute failures from logs, not infer KILLED from exit status.
Exit 0 means collection with GREEN controls and unchanged inventoried source;
exit 1 is failed control, interruption or drift; exit 2 is invalid input/setup.
