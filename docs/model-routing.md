# Model routing

The dispatch pipeline chooses the executor runtime first, pins the repository
model/effort defaults, then sends review to the opposite model family. Explicit
per-task and command-line overrides remain authoritative.

## Active routing

| Role | Default | How it is resolved |
| --- | --- | --- |
| Codex executor | `gpt-5.6-sol`, effort `high` | Repo-local Codex configs and the supervisor agree on the defaults; the generated task command pins both unless task metadata explicitly overrides them. |
| Claude executor | `fable`, effort `high` | The supervisor pins both values unless task metadata explicitly overrides them. |
| Reviewer for a Codex executor | Claude `fable`, effort `high` | Task metadata wins over `.codex/dispatch-env.toml`, then CLI flags may override that resolved choice. The subscription preflight runs before a real Claude review. |
| Reviewer for a Claude executor | Codex `gpt-5.6-sol`, effort `high` | Task metadata wins over repository defaults; the launcher passes the model and a post-model reasoning-effort override explicitly. |

The normal full and light review modes use the same defaults. Model aliases or
identifiers named in task metadata and CLI flags remain intentional opt-ins;
historical task/review records are not rewritten when repository defaults move.

## Resolution order

For review, `spawn_review.py` merges the vault `.codex/dispatch-env.toml` and a
worktree-local copy, with worktree values winning. Explicit task metadata then
wins over those repository defaults, and command-line `--model` or `--effort`
wins last. The resolved runtime, model, effort, and generated argv are recorded
in `.review-meta.json` and `.review-agent-command.json`.

For a task, dispatch records `codex_home`, `codex_profile`, plus optional
`model` and `effort` overrides in `.task-meta.json`. When either is absent, the
supervisor pins the runtime default above. This distinction matters:

- an alias such as `fable` intentionally follows the subscription alias;
- a model identifier such as `gpt-5.6-sol` selects that named Codex model;
- an explicit task value remains stable even if repository defaults change.

Intentional repository exceptions are narrow: `.codex/profiles/deep.toml`
keeps `max` effort for explicit deep work, and the daily summarizer stays on
`gpt-5.6-terra` low because it is a bounded specialized subagent. Historical
wiki pages, archived reviews, and test fixtures representing old records remain
unchanged.

The supervisor validates the generated command against task/review metadata and
the required sandbox flags before starting the agent. Generated metadata is
runtime state and must not be committed.

## Safe verification

Run these commands from the repository root. They print only model-routing
fields and do not inspect credentials or provider environment variables.

Show the repository reviewer defaults:

```bash
python3 - <<'PY'
import tomllib
from pathlib import Path

cfg = tomllib.loads(Path(".codex/dispatch-env.toml").read_text())["codex_dispatch"]
keys = ("codex_review_model", "codex_review_effort", "claude_review_model", "claude_review_effort")
print({key: cfg[key] for key in keys})
PY
```

Show the selected active Codex model without dumping the rest of either config:

```bash
python3 - <<'PY'
import tomllib
from pathlib import Path

for path in (
    Path.home() / ".codex/config.toml",
    Path.home() / ".codex/llm-obsidian-mcp.config.toml",
):
    data = tomllib.loads(path.read_text())
    print(path, {key: data.get(key) for key in ("model", "model_reasoning_effort")})
PY
```

In a dispatched task worktree, confirm whether the executor inherits the Codex
default or has an explicit override:

```bash
python3 - <<'PY'
import json
from pathlib import Path

meta = json.loads(Path(".task-meta.json").read_text())
keys = ("executor_runtime", "model", "effort", "codex_home", "codex_profile")
print({key: meta.get(key) for key in keys})
PY
```

Finally, exercise the command generation and lifecycle contracts in temporary,
hermetic fixtures:

```bash
bash tests/test_review_dispatch.sh
python3 tests/test_task_lifecycle.py
make test
```

`tests/test_review_dispatch.sh` asserts Claude `fable` high, Codex
`gpt-5.6-sol` high, explicit task/CLI override preservation, opposite-family
selection, and supervisor rejection of weakened reviewer commands.

## Updating defaults

Treat a default change as a routing change, not a documentation-only edit.
Update `.codex/dispatch-env.toml`, the fallback constants and help text in
`spawn_review.py`, dispatch/review skill guidance, and the corresponding routing
assertions in `tests/test_review_dispatch.sh` together. If the Codex executor
default changes, update the managed active Codex global/profile configuration
through the normal setup or MCP sync workflow; do not add a task-level model
override merely to hide a stale environment. Record the resolved model in the
acceptance result, while leaving historical wiki pages and prior evaluation
records unchanged.
