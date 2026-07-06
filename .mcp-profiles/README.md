# .mcp-profiles/ — on-demand MCP server sets

Heavy or rarely-used MCP servers do not belong in the default `.mcp.json`:
**tool schemas are loaded into every session's context regardless of
transport**, and past ~200K characters of schemas subagents start failing to
boot. The HTTP gateway saves processes and RAM, but it does NOT shrink
schemas — profiles are the schema-budget escape hatch.

Pattern:

1. Keep the default `.mcp.json` lean (the servers you use daily).
2. Put each optional server set into a profile file here, same format as
   `.mcp.json` (HTTP pointers at the gateway):

   ```json
   { "mcpServers": {
       "my-heavy-server": { "type": "http", "url": "http://127.0.0.1:9090/my-heavy-server/mcp" }
   } }
   ```

3. Start a session with the profile ADDED to the base set:

   ```bash
   claude --mcp-config .mcp-profiles/<name>.json
   ```

   (`--strict-mcp-config` would use ONLY the profile instead.)

   For Codex, first mirror the profiles into TOML:

   ```bash
   scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
   codex --profile llm-obsidian-<name>
   ```

   The default base profile is written as `~/.codex/llm-obsidian-mcp.config.toml`;
   each `.mcp-profiles/<name>.json` becomes
   `~/.codex/llm-obsidian-<name>.config.toml`.

Gotchas:

- `--mcp-config` is variadic: `claude --mcp-config file.json "prompt"` parses
  the prompt as a second config path. Separate them with `--` or send the
  prompt after startup.
- A server living in a profile must be REMOVED from `.mcp.json` (and from
  `enabledMcpjsonServers` in `.claude/settings.local.json` if present),
  otherwise the approve-all dialog re-adds it to the default set.
- The gateway side does not care about profiles: all children are always
  running; profiles only control which schemas a session loads.
