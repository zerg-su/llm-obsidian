# Close the current Orca chat tab

Use only after `ready_for_ui_close` with `turn_end.status: clean`. The runner's
`session_id` is the current chat identity. Never use an inherited cmux ID.

Orca currently has no `chat close` CLI command. `tab close` addresses browser
tabs; `terminal close` addresses PTYs. Neither closes a native chat. The
`computer --session` flag is a snapshot namespace, not a chat selector.

1. Load the installed `orca-cli` skill, resolve its session executable and read
   its version-matched guide. Use available native Computer Use tooling to
   observe Orca's window. If no visible window or GUI permission is available,
   report saved/manual-close and stop. Do not change permissions automatically.
2. To bind the visible tab to this session, use the documented command:
   `ORCA chat rename --session <session_id> --name "Saved · close-<session_id>" --json`
   with the exact receipt ID and resolved executable. This temporary title is
   deliberate: it remains in history and makes the closing target unambiguous.
   If rename fails, keep the tab open and report manual close.
3. Take a fresh native UI snapshot. Locate exactly one chat tab named
   `Saved · close-<session_id>` and its own close button. Close that button only.
   Do not send a global close shortcut, close a window, or use a title from an
   older snapshot. If the title is absent/ambiguous or its button cannot be
   identified, keep the tab open and report manual close and the new title.
4. If the tool remains usable, observe that this marked tab disappeared.
   A click receipt alone is not proof of closure. Follow the computer tool's
   completion protocol. Do not perform further repository work after closure.

Before the close click, give a short status: what was saved, whether turn-end
succeeded, and that this tab is about to close. If closure cannot be verified,
say so; do not repeat a blind click. Closing a tab does not delete its history.
