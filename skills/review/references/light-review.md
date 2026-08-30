# Light Review

`review --light` delegates one read-only `native-subagent` on the current host
and returns advisory findings only in the conversation. It never starts
Harness, writes state, approves/finalizes a HEAD, or enters a fix loop.

Options: `--target`, `--base`, repeatable `--paths`, `--include-untracked`,
`--verify`, `--model`, and `--effort`. Target defaults to the current Git root;
untracked files require explicit inclusion.

Treat the installed package root as `TOOL_ROOT`, read-only tool authority. Make
one private temporary directory outside the target, and build `snapshot.json`
there with `python3 "$TOOL_ROOT/scripts/review_target.py" light`, passing the
requested target/base/path/include-untracked flags exactly. Stop on invalid
Git/base/scope, and remove the temporary directory after validation.

Resolve the child route with:

```bash
python3 "$TOOL_ROOT/scripts/model_routing.py" --root "$TOOL_ROOT" \
  light-review --session-id "$("$TOOL_ROOT/scripts/current-session-id.sh")" \
  --model MODEL --effort EFFORT
```

Omit absent overrides and omit `--session-id` when the environment already
confirms the route. Default is the current model/effort. Overrides stay on the
current runtime: Claude supports Fable/Opus; Codex supports Sol/Terra. If the
host cannot enforce the route, fail visibly. Never use a cross-runtime,
shell-launched provider, parent synthesis, fallback, or Lifecycle Review.

On Codex use built-in Agent project agent `light_reviewer`; on Claude use Agent
plugin agent `llm-obsidian:light-reviewer`. Supply exact snapshot JSON, route
JSON, target root, and `verify`. Do not open another window or nested agent.

Validate JSON-only output through `scripts/light_review_contract.py` with
`--snapshot-file snapshot.json`, exact runtime/model/effort, and `--verify` iff
verification was requested. The validator binds digest, target, HEAD, base,
paths, untracked inclusion, and verification mode. On failure, give the error
to the same child for one correction. A second failure returns `incomplete`;
never weaken the schema. Repeat the same snapshot afterward. Digest drift
returns `incomplete` with a coverage gap and no automatic rerun.

With `--verify`, run only safe, bounded, directly relevant checks. Anything
requiring writes or permission is `not-run` plus a coverage gap. Without it,
run no tests. Never write target files, Harness records, archives, or wiki pages.
