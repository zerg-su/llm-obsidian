#!/usr/bin/env bash
# Bootstrap this repo on a fresh macOS machine.
#
# Scope:
#   - vault provisioning
#   - MCP gateway local config + mcp-proxy binary
#   - Codex plugin/MCP generated metadata
#   - optional launchd install after secrets are in place
#
# The script is intentionally idempotent: secrets and user config are preserved.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GW="$ROOT/scripts/mcp-gateway"
SECRETS_DIR="$HOME/.config/mcp-gateway"
SECRETS="$SECRETS_DIR/secrets.env"
LOCAL_BIN="$HOME/.local/bin"
PROXY_BIN="${MCP_PROXY_BIN:-$LOCAL_BIN/mcp-proxy}"
PROXY_LOCK="$GW/mcp-proxy.lock.json"
PYTHON=""

DO_INSTALL_SERVICE=0
DO_INSTALL_CODEX=0
DO_SETUP_VAULT=1
DO_PROXY=1
RESET_OBSIDIAN=0
REPAIR_EXCALIDRAW=0
DRY_RUN=0

usage() {
  cat <<EOF
usage: bin/setup-clean-machine.sh [options]

Fresh-machine bootstrap for llm-obsidian.

On macOS the bootstrap verifies Xcode Command Line Tools first. If they are
missing it opens the system installer and exits; finish that installer, then
rerun this command.

Options:
  --install-service       Run mcp-gateway.sh install after provisioning.
                          Requires a usable ~/.config/mcp-gateway/secrets.env.
  --install-codex-plugin  If codex is available, add this repo marketplace and plugin.
  --reset-obsidian        Back up .obsidian, then restore the three managed defaults.
  --repair-excalidraw     Back up and replace a mismatched Excalidraw main.js
                          with the pinned, checksum-verified artifact.
  --skip-vault            Do not run bin/setup-vault.sh.
  --skip-proxy            Do not verify/install the pinned mcp-proxy artifact.
  --check                 Dry-run: print what would be created/installed.
  -h, --help              Show this help.

After the first run, fill CONTEXT7_API_KEY in:
  $SECRETS
Then run:
  scripts/mcp-gateway/mcp-gateway.sh install
  scripts/mcp-gateway/mcp-gateway.sh health
EOF
}

log() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

copy_if_missing() {
  local src="$1" dest="$2" mode="${3:-}"
  if [ -e "$dest" ]; then
    log "keep: $dest"
    return 0
  fi
  log "create: $dest"
  run mkdir -p "$(dirname "$dest")"
  run cp "$src" "$dest"
  if [ -n "$mode" ]; then
    run chmod "$mode" "$dest"
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required. Install it and rerun."
}

macos_clt_ready() {
  [ "$(uname -s)" = "Darwin" ] || return 0
  command -v xcode-select >/dev/null 2>&1 || return 1
  command -v xcrun >/dev/null 2>&1 || return 1
  local developer_dir
  developer_dir="$(xcode-select -p 2>/dev/null)" || return 1
  [ -d "$developer_dir" ] || return 1
  xcrun --find clang >/dev/null 2>&1 || return 1
}

ensure_macos_clt() {
  [ "$(uname -s)" = "Darwin" ] || return 0
  if macos_clt_ready; then
    log "pre-flight: macOS Command Line Tools at $(xcode-select -p)"
    return 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "+ xcode-select --install"
    warn "macOS Command Line Tools are missing; a real run opens the system installer and then stops"
    return 0
  fi

  log "install: macOS Command Line Tools"
  if ! xcode-select --install; then
    die "could not open the Command Line Tools installer. Run 'xcode-select --install' manually, finish it, then rerun this bootstrap."
  fi
  die "Command Line Tools installer opened. Finish the macOS dialog, verify with 'xcode-select -p', then rerun this bootstrap."
}

select_python() {
  local candidate version seen=""
  for candidate in "$(command -v python3 2>/dev/null || true)" \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    case " $seen " in *" $candidate "*) continue ;; esac
    seen="$seen $candidate"
    if version="$($candidate -c 'import sys; sys.version_info >= (3, 9) or sys.exit(1); print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"; then
      PYTHON="$candidate"
      export PATH="$(dirname "$PYTHON"):$PATH"
      log "pre-flight: Python $version at $PYTHON"
      return 0
    fi
  done
  die "a runnable Python 3.9+ is required. On macOS finish Command Line Tools setup or run 'brew install python', then rerun."
}

install_mcp_proxy() {
  if [ "$DRY_RUN" -eq 1 ]; then
    "$PYTHON" "$GW/install-proxy.py" --lock "$PROXY_LOCK" --dest "$PROXY_BIN" --plan
    return 0
  fi
  "$PYTHON" "$GW/install-proxy.py" --lock "$PROXY_LOCK" --dest "$PROXY_BIN"
}

install_codex_plugin() {
  if ! command -v codex >/dev/null 2>&1; then
    warn "codex CLI not found; generated Codex files, skipped plugin install"
    return 0
  fi
  log "install: Codex marketplace + plugin"
  run codex plugin marketplace add "$ROOT"
  run codex plugin add llm-obsidian@llm-obsidian-codex
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-service) DO_INSTALL_SERVICE=1 ;;
    --install-codex-plugin) DO_INSTALL_CODEX=1 ;;
    --reset-obsidian) RESET_OBSIDIAN=1 ;;
    --repair-excalidraw) REPAIR_EXCALIDRAW=1 ;;
    --skip-vault) DO_SETUP_VAULT=0 ;;
    --skip-proxy) DO_PROXY=0 ;;
    --check) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

case "$(uname -s)" in
  Darwin) ;;
  *) warn "non-macOS detected; launchd install is macOS-only, but config generation can still run" ;;
esac

ensure_macos_clt
select_python

log "repo: $ROOT"

if [ "$DO_SETUP_VAULT" -eq 1 ]; then
  vault_args=()
  [ "$RESET_OBSIDIAN" -eq 1 ] && vault_args+=(--reset-obsidian)
  [ "$REPAIR_EXCALIDRAW" -eq 1 ] && vault_args+=(--repair-excalidraw)
  # Bash 3.2 (stock macOS) treats an empty array expansion as unbound under
  # set -u. Conditional expansion keeps the flag-less clean-machine path safe.
  run bash "$ROOT/bin/setup-vault.sh" ${vault_args[@]+"${vault_args[@]}"} "$ROOT"
fi

copy_if_missing "$GW/runtime.env.example" "$GW/runtime.env"
copy_if_missing "$GW/secrets.env.example" "$SECRETS" 600

log "synchronize: gateway + MCP client port"
run "$PYTHON" "$GW/config-sync.py" --apply

if [ "$DO_PROXY" -eq 1 ]; then
  install_mcp_proxy
fi

log "generate: Codex plugin metadata"
run "$PYTHON" "$ROOT/scripts/codex-adapter.py" --apply

log "generate: Codex MCP TOML"
run "$GW/mcp-gateway.sh" codex-sync --apply

if [ "$DO_INSTALL_CODEX" -eq 1 ]; then
  install_codex_plugin
fi

log "pre-flight: MCP gateway doctor"
if [ "$DRY_RUN" -eq 1 ]; then
  log "+ $GW/mcp-gateway.sh doctor"
else
  "$GW/mcp-gateway.sh" doctor || true
fi

if [ "$DO_INSTALL_SERVICE" -eq 1 ]; then
  run "$GW/mcp-gateway.sh" install
fi

cat <<EOF

Done.

Required manual step:
  edit $SECRETS and set CONTEXT7_API_KEY=...

Then verify:
  scripts/mcp-gateway/mcp-gateway.sh install
  scripts/mcp-gateway/mcp-gateway.sh health

For Codex plugin installation:
  bin/setup-clean-machine.sh --install-codex-plugin --skip-vault --skip-proxy
EOF
