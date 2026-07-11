#!/usr/bin/env bash
# mcp-gateway.sh — manage the local MCP HTTP gateway (TBXark/mcp-proxy).
#
# One set of long-lived MCP child processes per machine; every Claude Code
# session (and any other MCP client) talks to them over HTTP at
# 127.0.0.1:$GATEWAY_PORT instead of spawning its own stdio copies.
#
# Files:
#   scripts/mcp-gateway/config.json          gateway children (copy config.json.example)
#   scripts/mcp-gateway/runtime.env          canonical local gateway port
#   scripts/mcp-gateway/tools.json           version pins for uv-tool children ({} if none)
#   ~/.config/mcp-gateway/secrets.env        KEY=value lines; ${KEY} placeholders in
#                                            config.json are expanded by mcp-proxy from env
#   ~/Library/LaunchAgents/<label>.plist     autostart (macOS launchd)
#
# Docs: docs/mcp-gateway.md (install, add-a-server checklist, DR, gotchas).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="$HOME/.config/mcp-gateway"
SECRETS="$CONF_DIR/secrets.env"
CONFIG="$SCRIPT_DIR/config.json"
RUNTIME_ENV="${MCP_GATEWAY_RUNTIME:-$SCRIPT_DIR/runtime.env}"
PROXY_BIN="${MCP_PROXY_BIN:-$HOME/.local/bin/mcp-proxy}"
GATEWAY_PORT=""
LABEL_GW="${MCP_GATEWAY_LABEL:-io.llm-obsidian.mcp-gateway}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"

require_config() {
  if ! GATEWAY_PORT="$(python3 "$SCRIPT_DIR/config-sync.py" --runtime-env "$RUNTIME_ENV" --print-port)"; then
    echo "  -> cp $SCRIPT_DIR/runtime.env.example $RUNTIME_ENV" >&2
    exit 1
  fi
  export MCP_GATEWAY_PORT="$GATEWAY_PORT"
  [ -f "$CONFIG" ] || {
    echo "ERROR: $CONFIG not found." >&2
    echo "  -> cp $SCRIPT_DIR/config.json.example $CONFIG   # then edit your servers" >&2
    exit 1
  }
  if ! python3 "$SCRIPT_DIR/config-sync.py" \
      --runtime-env "$RUNTIME_ENV" --check --gateway-only --quiet; then
    echo "ERROR: gateway port drift; run: $0 sync-config --apply" >&2
    exit 1
  fi
}

load_secrets() {
  [ -f "$SECRETS" ] || {
    echo "ERROR: $SECRETS not found." >&2
    echo "  -> mkdir -p $CONF_DIR && cp $SCRIPT_DIR/secrets.env.example $SECRETS && chmod 600 $SECRETS" >&2
    exit 1
  }
  set -a
  # shellcheck disable=SC1090
  . "$SECRETS"
  set +a
}

wait_for_network() {
  # stdio children may hit the network on start (package resolve, SDK init);
  # after a reboot launchd can start the gateway before the network is up, the
  # child dies, and mcp-proxy does not restart dead children — the route stays
  # 404. Probe a host derived from the config (first remote url-child, else
  # github) for up to ~90s; any HTTP answer (including 4xx) means network is up.
  local probe
  probe=$(python3 - "$CONFIG" <<'PYEOF'
import json, sys
from urllib.parse import urlparse
cfg = json.load(open(sys.argv[1]))
for child in cfg.get("mcpServers", {}).values():
    url = child.get("url", "")
    if url.startswith("http") and "127.0.0.1" not in url and "localhost" not in url:
        u = urlparse(url)
        print(f"{u.scheme}://{u.netloc}/")
        break
else:
    print("https://github.com/")
PYEOF
)
  for _ in $(seq 1 15); do
    if curl -s -o /dev/null --max-time 3 "$probe"; then
      return 0
    fi
    echo "waiting for network before starting mcp-proxy..." >&2
    sleep 3
  done
  echo "WARN: network still unreachable after ~90s, starting anyway" >&2
}

write_plist() {
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"
  cat > "$AGENTS_DIR/$LABEL_GW.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_GW</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SCRIPT_DIR/mcp-gateway.sh</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/mcp-gateway.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mcp-gateway.err.log</string>
</dict>
</plist>
PLIST
}

case "${1:-}" in
  run)
    # foreground: what launchd executes
    require_config
    load_secrets
    wait_for_network
    exec "$PROXY_BIN" -config "$CONFIG"
    ;;
  install)
    require_config
    [ -x "$PROXY_BIN" ] || { echo "ERROR: $PROXY_BIN not found — download the TBXark/mcp-proxy release binary first (docs/mcp-gateway.md)" >&2; exit 1; }
    python3 "$SCRIPT_DIR/update-pins.py" "$CONFIG" --sync
    load_secrets
    write_plist
    launchctl bootout "gui/$UID_NUM/$LABEL_GW" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$AGENTS_DIR/$LABEL_GW.plist"
    echo "installed + bootstrapped: $LABEL_GW (port $GATEWAY_PORT)"
    ;;
  start)
    launchctl bootstrap "gui/$UID_NUM" "$AGENTS_DIR/$LABEL_GW.plist" 2>/dev/null || launchctl kickstart "gui/$UID_NUM/$LABEL_GW"
    ;;
  stop)
    launchctl bootout "gui/$UID_NUM/$LABEL_GW" 2>/dev/null || true
    echo "stopped"
    ;;
  restart)
    launchctl kickstart -k "gui/$UID_NUM/$LABEL_GW"
    ;;
  status)
    require_config
    gw_pid=""
    if launchctl print "gui/$UID_NUM/$LABEL_GW" >/dev/null 2>&1; then
      gw_pid="$(launchctl print "gui/$UID_NUM/$LABEL_GW" | awk '/pid =/ {print $3; exit}')"
      echo "$LABEL_GW: loaded (pid ${gw_pid:-n/a})"
    else
      echo "$LABEL_GW: NOT loaded"
    fi
    if [ -f "$AGENTS_DIR/$LABEL_GW.plist" ]; then
      echo "autostart: on (launchd RunAtLoad at login + KeepAlive on crash)"
    else
      echo "autostart: OFF — run mcp-gateway.sh install"
    fi
    # which MCP routes are live: /<name>/mcp registers only for children that came up
    if ! python3 "$SCRIPT_DIR/smoke.py" "$CONFIG" "$GATEWAY_PORT" --routes; then
      # DOWN right after start = children still booting (binaries ~seconds, npx/uvx up to minutes)
      uptime_gw="$(ps -o etime= -p "$gw_pid" 2>/dev/null | tr -d ' ')"
      case "$uptime_gw" in
        [0-2]:[0-9][0-9]|0[0-2]:[0-9][0-9])
          echo "note: gateway started ${uptime_gw} ago — children may still be booting, retry status in a minute" ;;
      esac
    fi
    ;;
  health)
    # mcp-proxy has no /_healthz — health = a real MCP handshake (initialize +
    # tools/list) against the first configured child; full sweep: smoke.
    require_config
    canary=$(python3 -c "import json,sys; print(next(iter(json.load(open(sys.argv[1]))['mcpServers'])))" "$CONFIG")
    echo "canary handshake ($canary; all servers: mcp-gateway.sh smoke):"
    python3 "$SCRIPT_DIR/smoke.py" "$CONFIG" "$GATEWAY_PORT" "$canary"
    ;;
  smoke)
    require_config
    python3 "$SCRIPT_DIR/smoke.py" "$CONFIG" "$GATEWAY_PORT" "${2:-}"
    ;;
  logs)
    tail -n "${2:-40}" "$LOG_DIR/mcp-gateway.err.log" "$LOG_DIR/mcp-gateway.out.log" 2>/dev/null
    ;;
  doctor)
    # pre-flight for a bare machine: what is present, what is missing (read-only)
    require_config
    python3 - "$SCRIPT_DIR" "$CONFIG" "$SECRETS" "$AGENTS_DIR" "$LABEL_GW" "$PROXY_BIN" <<'PYEOF'
import json, os, re, shutil, subprocess, sys

script_dir, config, secrets, agents_dir, label_gw, proxy_bin = sys.argv[1:7]
home = os.path.expanduser("~")
fails = 0

def check(ok, msg, hint=""):
    global fails
    print(("OK   " if ok else "FAIL ") + msg)
    if not ok:
        fails += 1
        if hint:
            print(f"     -> {hint}")

cfg = json.load(open(config))
children = cfg.get("mcpServers", {})
commands = {c.get("command", "").split("/")[-1] for c in children.values() if c.get("command")}

check(os.access(os.path.expanduser(proxy_bin), os.X_OK), f"binary {os.path.basename(proxy_bin)}",
      "download the TBXark/mcp-proxy release binary -> ~/.local/bin/, chmod +x")
if commands & {"npx", "node"}:
    check(shutil.which("npx") is not None, "command npx", "brew install node")
check(shutil.which("curl") is not None, "command curl", "ships with macOS")

# absolute-path stdio commands must exist
for name, child in sorted(children.items()):
    cmd = child.get("command", "")
    if cmd and "/" in cmd:
        path = os.path.expanduser(cmd)
        check(os.access(path, os.X_OK), f"child binary for {name}: {cmd}",
              "install it or fix the command path in config.json")

# uv-tool pins (tools.json) — empty dict means nothing to check
tools_path = f"{script_dir}/tools.json"
tools = {}
if os.path.isfile(tools_path):
    tools = {k: v for k, v in json.load(open(tools_path)).items() if not k.startswith("_")}
if tools:
    check(shutil.which("uv") is not None, "command uv", "brew install uv")
    try:
        installed = subprocess.check_output(["uv", "tool", "list"], text=True)
    except Exception:
        installed = ""
    for pkg, meta in sorted(tools.items()):
        norm = re.sub(r"[._]+", "-", pkg)  # uv tool list shows the PEP 503 normalized name
        check(f"{norm} v{meta['version']}" in installed, f"uv tool {pkg}=={meta['version']}",
              "mcp-gateway.sh sync-tools")

# secrets: every ${VAR} referenced by the config must exist in secrets.env
needed = sorted(set(re.findall(r"\$\{(\w+)\}", open(config).read())))
if os.path.isfile(secrets):
    have = set()
    for line in open(secrets):
        m = re.match(r"([A-Za-z_]\w*)=", line.strip())
        if m:
            have.add(m.group(1))
    for var in needed:
        check(var in have, f"secrets.env: {var}", f"add {var}=... to {secrets}")
else:
    check(False, f"secrets file {secrets}",
          f"cp {script_dir}/secrets.env.example {secrets} && chmod 600 {secrets}")

# AWS profiles referenced by children (skipped when none configured)
profiles = sorted({c.get("env", {}).get("AWS_PROFILE") for c in children.values()
                   if c.get("env", {}).get("AWS_PROFILE")})
if profiles:
    aws_cfg = open(f"{home}/.aws/config").read() if os.path.isfile(f"{home}/.aws/config") else ""
    for prof in profiles:
        check(f"[profile {prof}]" in aws_cfg or f"[{prof}]" in aws_cfg,
              f"aws profile {prof}", "configure it in ~/.aws/config")

check(os.path.isfile(f"{agents_dir}/{label_gw}.plist"), f"launchd plist {label_gw}",
      "mcp-gateway.sh install")

print(f"--- {'all OK' if fails == 0 else str(fails) + ' problem(s)'}")
sys.exit(1 if fails else 0)
PYEOF
    ;;
  sync-tools)
    # install/upgrade all PyPI pins from tools.json via uv tool install (idempotent)
    require_config
    python3 "$SCRIPT_DIR/update-pins.py" "$CONFIG" --sync
    ;;
  update)
    # check for newer child package versions (PyPI/npm), show current -> latest,
    # per-package confirmation; on apply — restart + full smoke.
    # selective: mcp-gateway.sh update <server-or-package>
    require_config
    shift
    rc=0
    python3 "$SCRIPT_DIR/update-pins.py" "$CONFIG" "$@" || rc=$?
    if [ "$rc" -eq 10 ]; then
      echo "restarting gateway to pick up new pins..."
      "$0" restart
      sleep 20
      "$0" smoke
    elif [ "$rc" -ne 0 ]; then
      exit "$rc"
    fi
    ;;
  codex-sync)
    # Sync Codex MCP TOML from .mcp.json/.mcp-profiles. Dry-run by default.
    shift
    if ! python3 "$SCRIPT_DIR/config-sync.py" --runtime-env "$RUNTIME_ENV" --check --quiet; then
      echo "ERROR: synchronize local MCP JSON first: $0 sync-config --apply" >&2
      exit 1
    fi
    python3 "$SCRIPT_DIR/codex-sync.py" "$@"
    ;;
  schema-check)
    require_config
    python3 "$SCRIPT_DIR/schema-lock.py" --check --config "$CONFIG" --port "$GATEWAY_PORT"
    ;;
  schema-lock)
    require_config
    shift
    python3 "$SCRIPT_DIR/schema-lock.py" "$@" --config "$CONFIG" --port "$GATEWAY_PORT"
    ;;
  sync-config)
    # Materialize the runtime.env port in gateway + default MCP client JSON.
    shift
    if [ "$#" -eq 0 ]; then
      set -- --check
    fi
    python3 "$SCRIPT_DIR/config-sync.py" --runtime-env "$RUNTIME_ENV" "$@"
    ;;
  *)
    echo "usage: mcp-gateway.sh {run|install|start|stop|restart|status|health|smoke [server]|schema-check|schema-lock --apply|update [--check|--yes] [name...]|sync-tools|sync-config [--check|--apply]|codex-sync [--check|--apply] [--only-profile name]|doctor|logs [n]}"
    exit 1
    ;;
esac
