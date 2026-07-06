#!/usr/bin/env bash
# Install Destructive Command Guard (dcg) with this repo's portable config and
# hook registrations for Codex CLI and Claude Code.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_SRC="$REPO_ROOT/config/dcg/config.toml"
HOOK_TEMPLATE="$REPO_ROOT/.github/hooks/dcg.json"
DCG_INSTALL_URL="${DCG_INSTALL_URL:-https://raw.githubusercontent.com/Dicklesworthstone/destructive_command_guard/main/install.sh}"
INSTALL_AGENTS="both"
RUN_SMOKE=1
SKIP_BINARY=0
CHECK_ONLY=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: bin/setup-dcg.sh [options]

Options:
  --check          Validate local dcg/config/hook state without writing.
  --codex-only    Install only ~/.codex/hooks.json registration.
  --claude-only   Install only ~/.claude/settings.json registration.
  --skip-binary   Do not install dcg if it is missing.
  --no-smoke      Skip scripts/dcg-test-suite.sh after install.
  --force         Pass --force to the upstream dcg installer when needed.
  -h, --help      Show this help.

Environment:
  DCG_BIN          Override dcg binary path (default: PATH dcg or ~/.local/bin/dcg).
  DCG_INSTALL_URL Override upstream install.sh URL.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --codex-only) INSTALL_AGENTS="codex" ;;
    --claude-only) INSTALL_AGENTS="claude" ;;
    --skip-binary) SKIP_BINARY=1 ;;
    --no-smoke) RUN_SMOKE=0 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

need_file() {
  [ -f "$1" ] || { echo "ERROR: missing $1" >&2; exit 1; }
}

backup_file() {
  local path="$1"
  [ -e "$path" ] || return 0
  local stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  cp -p "$path" "$path.bak-dcg-$stamp"
}

find_dcg() {
  if [ -n "${DCG_BIN:-}" ]; then
    printf '%s\n' "$DCG_BIN"
    return
  fi
  if command -v dcg >/dev/null 2>&1; then
    command -v dcg
    return
  fi
  printf '%s/.local/bin/dcg\n' "$HOME"
}

install_dcg_binary() {
  local dcg_bin="$1"
  if [ -x "$dcg_bin" ]; then
    return 0
  fi
  if [ "$SKIP_BINARY" = "1" ]; then
    echo "ERROR: dcg not found at $dcg_bin and --skip-binary was used" >&2
    exit 1
  fi
  if [ "$CHECK_ONLY" = "1" ]; then
    echo "MISSING dcg binary: $dcg_bin"
    return 1
  fi
  command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required to install dcg" >&2; exit 1; }
  local force_arg=""
  [ "$FORCE" = "1" ] && force_arg="--force"
  curl -fsSL "$DCG_INSTALL_URL?$(date +%s)" | bash -s -- --easy-mode $force_arg
}

install_config() {
  local dst="$HOME/.config/dcg/config.toml"
  if [ "$CHECK_ONLY" = "1" ]; then
    if [ -f "$dst" ] && cmp -s "$CONFIG_SRC" "$dst"; then
      echo "OK config: $dst"
      return 0
    fi
    echo "DRIFT config: $dst"
    return 1
  fi
  mkdir -p "$(dirname "$dst")" "$HOME/.local/share/dcg"
  if [ -f "$dst" ] && ! cmp -s "$CONFIG_SRC" "$dst"; then
    backup_file "$dst"
  fi
  install -m 0644 "$CONFIG_SRC" "$dst"
  echo "installed config: $dst"
}

merge_json_hooks() {
  local target="$1"
  local mode="$2"
  local dcg_bin="$3"
  local check_only="$4"
  python3 - "$target" "$mode" "$dcg_bin" "$check_only" "$HOOK_TEMPLATE" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1]).expanduser()
mode = sys.argv[2]
dcg_bin = sys.argv[3]
check_only = sys.argv[4] == "1"
template_path = pathlib.Path(sys.argv[5])

if mode not in {"codex", "claude"}:
    raise SystemExit(f"bad mode: {mode}")

if target.exists():
    data = json.loads(target.read_text(encoding="utf-8"))
else:
    data = {}

hooks = data.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])

def is_dcg_entry(entry):
    if entry.get("matcher") != "Bash":
        return False
    for hook in entry.get("hooks", []):
        command = hook.get("command") or hook.get("bash") or hook.get("powershell")
        if isinstance(command, str) and pathlib.Path(command).name == "dcg":
            return True
    return False

pre[:] = [entry for entry in pre if not is_dcg_entry(entry)]
template = json.loads(template_path.read_text(encoding="utf-8"))
entry = template["hooks"]["PreToolUse"][0]
entry = json.loads(json.dumps(entry).replace("__DCG_BIN__", dcg_bin))
pre.append(entry)

text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
old = target.read_text(encoding="utf-8") if target.exists() else ""
if check_only:
    if old == text:
        print(f"OK {mode} hook: {target}")
        raise SystemExit(0)
    print(f"DRIFT {mode} hook: {target}")
    raise SystemExit(1)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text, encoding="utf-8")
print(f"installed {mode} hook: {target}")
PY
}

install_hooks() {
  local dcg_bin="$1"
  local rc=0
  case "$INSTALL_AGENTS" in
    both|codex)
      if [ "$CHECK_ONLY" != "1" ]; then
        backup_file "$HOME/.codex/hooks.json"
      fi
      merge_json_hooks "$HOME/.codex/hooks.json" codex "$dcg_bin" "$CHECK_ONLY" || rc=1
      ;;
  esac
  case "$INSTALL_AGENTS" in
    both|claude)
      if [ "$CHECK_ONLY" != "1" ]; then
        backup_file "$HOME/.claude/settings.json"
      fi
      merge_json_hooks "$HOME/.claude/settings.json" claude "$dcg_bin" "$CHECK_ONLY" || rc=1
      ;;
  esac
  return "$rc"
}

run_smoke() {
  local dcg_bin="$1"
  [ "$RUN_SMOKE" = "1" ] || return 0
  [ "$CHECK_ONLY" = "0" ] || return 0
  PATH="$(dirname "$dcg_bin"):$PATH" bash "$REPO_ROOT/scripts/dcg-test-suite.sh"
}

need_file "$CONFIG_SRC"
need_file "$HOOK_TEMPLATE"
DCG_BIN_PATH="$(find_dcg)"

rc=0
install_dcg_binary "$DCG_BIN_PATH" || rc=1
if [ "$CHECK_ONLY" = "0" ] && command -v dcg >/dev/null 2>&1; then
  DCG_BIN_PATH="$(command -v dcg)"
fi
install_config || rc=1
install_hooks "$DCG_BIN_PATH" || rc=1

if [ "$CHECK_ONLY" = "1" ]; then
  exit "$rc"
fi

"$DCG_BIN_PATH" --version
"$DCG_BIN_PATH" doctor || true
run_smoke "$DCG_BIN_PATH"
