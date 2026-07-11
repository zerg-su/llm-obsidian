# Model routing

The dispatch pipeline chooses the executor runtime first, then sends review to
the opposite model family. Repository configuration pins reviewer models, but a
Codex executor normally inherits the active Codex configuration instead of
receiving a redundant `--model` argument.

## Active routing

| Role | Default | How it is resolved |
| --- | --- | --- |
| Codex executor | `gpt-5.6-sol` | `skills/dispatch/SKILL.md` leaves the model unset unless the user names one. `scripts/cmux_agent_supervisor.py` then omits `--model`, so Codex loads the configured default from the selected `CODEX_HOME` and profile. |
| Claude executor | `opus` | The supervisor passes `--model opus` when dispatch metadata has no explicit Claude model. |
| Reviewer for a Codex executor | Claude `opus`, effort `medium` | `.codex/dispatch-env.toml` supplies `claude_review_model` and `claude_review_effort`; `skills/review-dispatch/scripts/spawn_review.py` writes both into the validated reviewer command. The subscription preflight runs before a real Claude review. |
| Reviewer for a Claude executor | Codex `gpt-5.6-sol` | `.codex/dispatch-env.toml` supplies `codex_review_model`; the review launcher passes it explicitly with `--model`. |

The repository uses the subscription-backed Claude `opus` alias rather than a
pinned provider model ID. For this acceptance the alias currently resolves to
Opus 4.8, but the alias can advance without a repository change. By contrast,
`gpt-5.6-sol` is the explicit Codex model identifier used by the current local
configuration and by Codex reviewer launches.

Fable is never selected by fallback routing. It is an explicit opt-in: pass
`--model fable` to `spawn_review.py start`, or explicitly request Fable when
dispatching a Claude task. The normal full and light review modes use the same
model defaults.

## Resolution order

For review, `spawn_review.py` merges the vault `.codex/dispatch-env.toml` and a
worktree-local copy, with the worktree values winning. A command-line `--model`
or `--effort` then overrides the merged value for that review run. The resolved
runtime, model, effort, and generated argv are recorded in `.review-meta.json`
and `.review-agent-command.json`.

For a Codex task, dispatch records `codex_home`, `codex_profile`, and an optional
model in `.task-meta.json`. The supervisor adds `--model` only when that optional
value is non-empty. This distinction matters:

- an alias such as `opus` intentionally follows the subscription alias;
- a model identifier such as `gpt-5.6-sol` selects that named Codex model;
- an unset Codex task model delegates selection to the active Codex config.

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
keys = ("codex_review_model", "claude_review_model", "claude_review_effort")
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
keys = ("executor_runtime", "model", "codex_home", "codex_profile")
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

`tests/test_review_dispatch.sh` asserts Claude `opus` plus medium effort, Codex
`gpt-5.6-sol`, explicit Fable preservation, opposite-family selection, and
supervisor rejection of weakened reviewer commands.

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
