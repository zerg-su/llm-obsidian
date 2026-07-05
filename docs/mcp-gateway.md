# MCP HTTP Gateway

Run one set of long-lived MCP server processes per machine, and let every Claude Code session (or any other MCP client) talk to them over local HTTP instead of spawning its own stdio copies.

Everything lives in `scripts/mcp-gateway/`; the service is managed by `mcp-gateway.sh`.

## Why a gateway

stdio MCP servers are spawned per client session. With N open terminals and M servers in `.mcp.json` you run N x M processes: 7 terminals x 30 servers is 200+ processes, most of them idle duplicates, each paying its own npx/uvx cold start and its own RAM.

With the gateway you run 1 gateway + M children per machine, constant no matter how many sessions are open:

- **One process set per machine.** Each child starts once; every session shares it.
- **Reconnect resilience.** Claude Code reconnects to HTTP MCP servers automatically (exponential backoff), so restarting the gateway mid-session is safe. A dead stdio child was gone for good.
- **No per-session cold starts.** Package resolution and SDK init happen at gateway start, not on every new terminal.

**Explicit caveat:** HTTP does NOT shrink tool schemas in your context window. Schemas are loaded into every session regardless of transport, and past a budget (~200K characters) subagents start failing to boot. The schema-budget tool is profiles: keep `.mcp.json` lean and move heavy or rarely-used servers into on-demand profile files. See [.mcp-profiles/README.md](../.mcp-profiles/README.md).

## Architecture

The gateway is [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy): one Go binary that spawns stdio children and/or proxies remote URL children, exposing each at its own HTTP route. It runs as a single launchd service, `io.llm-obsidian.mcp-gateway`, listening on `127.0.0.1:9090` (override with `MCP_GATEWAY_PORT`; binary path with `MCP_PROXY_BIN`; label with `MCP_GATEWAY_LABEL`).

**Route mapping rule.** One name is used in four places and must match exactly:

| Place | Value (example) |
|---|---|
| `config.json` child key | `github` |
| Gateway route | `http://127.0.0.1:9090/github/mcp` |
| `.mcp.json` client key | `github` |
| Tool names inside sessions | `mcp__github__*` |

Renaming a server later breaks every permission rule and every agent `tools:` list that references the `mcp__<name>__*` prefix. Pick names carefully and keep them.

Two child kinds in `config.json`:

```json
"github": {
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github@2025.4.8"],
  "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PAT}" }
}
```

A **stdio child**: the gateway spawns and owns the process. And a **url child**: the gateway proxies to an already-running HTTP server, local or hosted:

```json
"context7": {
  "url": "https://mcp.context7.com/mcp",
  "transportType": "streamable-http",
  "headers": { "CONTEXT7_API_KEY": "${CONTEXT7_API_KEY}" }
}
```

**Secrets indirection.** `config.json` is committed to git and contains no secrets, only `${VAR}` placeholders (in `env` and `headers` alike). `mcp-gateway.sh run` sources `~/.config/mcp-gateway/secrets.env` (plain `KEY=value` lines, chmod 600, never committed) into the environment, and mcp-proxy expands the placeholders from env. `doctor` derives the required key list from `config.json` and tells you what is missing.

**Version pins.** npm children are pinned inside their `args` (`package@X.Y.Z`). PyPI children are installed as real binaries via `uv tool install` (no uvx nanny process, no network at start); their pins and `--with` constraints live in `tools.json`. Never edit versions by hand: `mcp-gateway.sh update` does the registry lookup, per-package confirmation, install, restart, and smoke.

## Install

From a bare macOS machine:

```bash
# 1. mcp-proxy binary
mkdir -p ~/.local/bin
# Download the release asset for your platform from
#   https://github.com/TBXark/mcp-proxy/releases
# save it as ~/.local/bin/mcp-proxy, then:
chmod +x ~/.local/bin/mcp-proxy

# 2. Gateway config (lives in git, holds no secrets)
cd scripts/mcp-gateway
cp config.json.example config.json

# 3. Secrets file (never committed)
mkdir -p ~/.config/mcp-gateway
cp secrets.env.example ~/.config/mcp-gateway/secrets.env
chmod 600 ~/.config/mcp-gateway/secrets.env

# 4. Flagship child: context7 (library documentation MCP) needs exactly one key.
#    Get a free key from the https://context7.com dashboard and fill in the
#    CONTEXT7_API_KEY= line in ~/.config/mcp-gateway/secrets.env

# 5. Point Claude Code at the gateway
cd ../..   # repo root
cp .mcp.json.example .mcp.json

# 6. Pre-flight, install, verify
scripts/mcp-gateway/mcp-gateway.sh doctor    # lists every missing piece with a fix hint
scripts/mcp-gateway/mcp-gateway.sh install   # pins + launchd plist + bootstrap + autostart
scripts/mcp-gateway/mcp-gateway.sh health    # real MCP handshake against the first child
```

New Claude Code sessions in the repo pick the servers up from `.mcp.json` automatically.

## Commands reference

```
mcp-gateway.sh {run|install|start|stop|restart|status|health|smoke [server]|update [--check|--yes] [name...]|sync-tools|doctor|logs [n]}
```

| Command | What it does |
|---|---|
| `run` | Foreground gateway (what launchd executes): load secrets, wait for network, exec mcp-proxy. |
| `install` | Sync uv pins, write the launchd plist, bootstrap the service, enable autostart. |
| `start` | Bootstrap (or kickstart) the service. |
| `stop` | Bootout the service; MCP disappears for all sessions until `start`. |
| `restart` | `kickstart -k`; safe mid-session, clients reconnect on their own. |
| `status` | Loaded + pid + per-route liveness (cheap 404 check, under a second, no handshake). |
| `health` | Full MCP handshake (initialize + tools/list) against the first configured child (canary). |
| `smoke [server]` | Handshake every endpoint, or one server / comma-separated list. |
| `update [--check\|--yes] [name...]` | Registry lookup current -> latest, per-package confirm; on apply: restart + smoke. |
| `sync-tools` | Install every `tools.json` pin via `uv tool install` (fresh machine, or after cleaning uv). |
| `doctor` | Read-only pre-flight: binaries, uv pins, secrets keys, AWS profiles, plist. |
| `logs [n]` | Tail `~/Library/Logs/mcp-gateway.{out,err}.log`. |

## Adding a server

1. **Choose the name.** It is the config.json key, the `.mcp.json` key, and the `mcp__<name>__*` tool prefix; all three must be the same string, and renaming later breaks permission rules and agent tool lists. The name `workspace` is reserved by mcp-proxy; do not use it.
2. **Add the child to `config.json`.** stdio example (pin the package version):

   ```json
   "search": {
     "command": "npx",
     "args": ["-y", "example-search-mcp@1.4.2"],
     "env": { "SEARCH_API_TOKEN": "${SEARCH_API_TOKEN}" }
   }
   ```

   url example: `{ "url": "https://mcp.example.com/mcp", "transportType": "streamable-http", "headers": { "Authorization": "Bearer ${EXAMPLE_TOKEN}" } }`
3. **Add the `${VAR}` lines** you referenced to `~/.config/mcp-gateway/secrets.env`.
4. **Add the client pointer** to `.mcp.json` (or a profile file, if it is a heavy server):

   ```json
   "search": { "type": "http", "url": "http://127.0.0.1:9090/search/mcp" }
   ```
5. **Verify:** `mcp-gateway.sh restart`, then `mcp-gateway.sh smoke search`, then `mcp-gateway.sh doctor`.

If the server ships an oversized tool set, trim it at the gateway with the optional per-child `options.toolFilter` (`mode: "allow"` keeps only the listed tools; `mode: "block"` drops them):

```json
"search": {
  "command": "npx",
  "args": ["-y", "example-search-mcp@1.4.2"],
  "env": { "SEARCH_API_TOKEN": "${SEARCH_API_TOKEN}" },
  "options": {
    "toolFilter": { "mode": "allow", "list": ["search", "get_document", "list_sources"] }
  }
}
```

## Advanced patterns

Two patterns the base install does not ship but that fit the gateway cleanly.

**Header multiplexing: N logical servers, one helper process.** Some MCP servers select their backend per request via HTTP headers (for example a multi-tenant monitoring server that reads the target instance URL and token from headers). Run ONE instance of that server as a second launchd service on its own port (model its plist on the one `install` writes), then declare several url children that all point at it with different headers:

```json
"monitor-staging": {
  "url": "http://127.0.0.1:8686/mcp",
  "headers": {
    "X-Instance-URL": "https://monitor.staging.example.com",
    "X-Instance-Token": "${MONITOR_STAGING_TOKEN}"
  }
},
"monitor-prod": {
  "url": "http://127.0.0.1:8686/mcp",
  "headers": {
    "X-Instance-URL": "https://monitor.prod.example.com",
    "X-Instance-Token": "${MONITOR_PROD_TOKEN}"
  }
}
```

Each logical child keeps its own route and `mcp__<name>__*` prefix, so permissions and agents can still distinguish environments while the machine runs a single helper process.

**Runtime-init children.** Some MCP servers cannot take connection config from env or flags; they expose an `initialize_connection`-style tool that must be called after startup. Pattern: a small post-start worker that (a) polls `http://127.0.0.1:9090/<name>/mcp` until the route registers (mcp-proxy returns 404 until the child is up), (b) performs the MCP handshake, and (c) calls the init tool with values read from `secrets.env`. Background the worker from the `run` case before the `exec`, or run it as its own launchd job. `smoke.py` already contains a minimal `handshake()` + `call_tool()` client to crib from. Remember that one child serves ALL sessions: the init is global, and re-running it with different arguments switches every connected session at once.

## Operations and DR

- **Secrets backup.** `secrets.env` deliberately never enters git. Either mirror it as a single note in your password manager (update the note when you add keys; `doctor` prints the authoritative list), or keep no backup at all and reissue per key: every value is secondary and can be regenerated from the service that issued it.
- **Moving to a new machine.** `config.json`, `tools.json`, and the scripts are in git; the plist is generated by `install`. Carry over only `~/.config/mcp-gateway/secrets.env` (chmod 600) and, if any child uses `AWS_PROFILE`, your `~/.aws/` config. Then: `doctor` -> `install` -> `health`.
- **Logs.** `~/Library/Logs/mcp-gateway.out.log` and `.err.log`; `mcp-gateway.sh logs 100` tails both.
- **KeepAlive.** The plist sets `RunAtLoad` (starts at login) and `KeepAlive` (launchd restarts the gateway if the process dies). launchd watches the gateway process only, not individual children; see the gotchas below.

## Gotchas

- **launchd starts before the network is up.** After a reboot, stdio children that touch the network on start (package resolve, SDK init) die, and their routes stay 404. This is why `run` calls `wait_for_network`: it probes the first remote url child from your config (falling back to github.com) for up to ~90 seconds before starting mcp-proxy. If a boot race still slips through, `mcp-gateway.sh restart`.
- **mcp-proxy does not restart dead children.** A child that crashes after startup leaves its route 404 forever; `panicIfInvalid` only applies at boot. The fix is always a service restart (`restart`), which launchd also performs automatically if the whole gateway process dies.
- **There is no `/_healthz`.** Liveness is checked with real traffic: `status` does a cheap route-registration check (404 = child never came up), `health` does a full MCP handshake against the first configured child, `smoke` sweeps all of them.
- **Huge tool sets eat context regardless of transport.** Trim at the gateway with `toolFilter`, or move the server into a profile so only sessions that need it pay the schema cost ([.mcp-profiles/README.md](../.mcp-profiles/README.md)).
- **npx/uvx cold starts.** A first boot after cleaning package caches can take minutes, and `smoke` will FAIL on children that are still warming up; `status` warns when the gateway is younger than ~3 minutes. Prefer pinned `uv tool install` binaries for PyPI children: no wrapper process, no network at start, and updates go through `mcp-gateway.sh update` only.
- **Shell env vars are invisible to launchd.** Keys exported from fish or zsh config never reach the service. That is exactly why the `run` wrapper sources `~/.config/mcp-gateway/secrets.env` itself instead of relying on your shell (or on the plist) to provide the environment.
- **Routes answer on both `/<name>/` and `/<name>/mcp`** (subtree routing). Use `/<name>/mcp` in client configs.
- **Children are shared state.** One process serves every session; any server that keeps per-connection state (an active connection selected via a tool call, a cache, a login) shares it across all terminals. For a single user this is usually a feature, but keep it in mind.
