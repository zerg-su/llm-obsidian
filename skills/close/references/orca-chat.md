# Close the current Orca chat through the host API

`queue-session-exit.py` owns this path after a clean synchronous turn-end.
It passes the exact `ORCA_AGENT_SESSION_ID` and vault root to
`scripts/orca-close-session.cjs`; do not invoke another close mechanism.

The helper resolves the selected Orca installation, validates its current
`CloseTab`/`WorktreeTabSelector` schemas, pins the runtime and checks that host
and CLI versions agree. `worktree show` resolves the exact vault address.
`session.tabs.list` must contain exactly one `agent-session` tab with this ID.
It calls `session.tabs.close` once with that tab ID and `reason: user`, then
reads the inventory again: the selected tab must be absent and neighboring
IDs must remain. No title changes, GUI clicks, model input or process kills.

`blocked` turn-end cannot invoke close. Unsupported APIs, ambiguous routing or
missing identity return manual/blocked status. A lost reply is reconciled by
reading inventory, never by repeating the mutation. If absence cannot be
verified, report the exact blocker and leave manual closure to the user.

Only the tab is removed; saved provider history remains available. There is
no CLI `chat close` command in the inspected build; browser `tab close` and
`terminal close` are different operations. The internal API is version-checked,
not assumed to exist on every Orca host.
