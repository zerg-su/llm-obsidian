#!/usr/bin/env bash
# Provision vault directories without clobbering a user's Obsidian settings.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="$ROOT"
RESET_OBSIDIAN=0
REPAIR_EXCALIDRAW=0
REPAIR_TASKS=0
EXCALIDRAW_LOCK_FILE="${EXCALIDRAW_LOCK_FILE:-$ROOT/config/obsidian-excalidraw-main.lock.json}"
TASKS_LOCK_FILE="${TASKS_LOCK_FILE:-$ROOT/config/obsidian-tasks.lock.json}"
TASKS_DEFAULTS_FILE="${TASKS_DEFAULTS_FILE:-$ROOT/config/obsidian-tasks.defaults.json}"
TASKS_SNIPPET_FILE="${TASKS_SNIPPET_FILE:-$ROOT/.obsidian/snippets/llm-obsidian-tasks.css}"

usage() {
  cat <<'EOF'
usage: bin/setup-vault.sh [--reset-obsidian] [--repair-excalidraw] [--repair-tasks] [vault-path]

Default behavior preserves every existing .obsidian file.  The explicit
--reset-obsidian flag replaces only app.json, appearance.json, and graph.json
with repository defaults after backing up the complete .obsidian directory.
Excalidraw main.js is installed from the pinned checksum when missing. An
existing checksum mismatch is preserved unless --repair-excalidraw is given;
repair also backs up the existing file before replacement.
Obsidian Tasks 8.2.2 is installed as a verified three-asset set. Existing
checksum mismatches are preserved unless --repair-tasks is given; Tasks user
settings are merged only for entirely absent top-level keys.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --reset-obsidian) RESET_OBSIDIAN=1 ;;
    --repair-excalidraw) REPAIR_EXCALIDRAW=1 ;;
    --repair-tasks) REPAIR_TASKS=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) printf 'ERROR: unknown option: %s\n' "$1" >&2; exit 2 ;;
    *) VAULT="$1" ;;
  esac
  shift
done

case "$VAULT" in
  /*) ;;
  *) VAULT="$PWD/$VAULT" ;;
esac
HAD_OBSIDIAN=0
[ -d "$VAULT/.obsidian" ] && HAD_OBSIDIAN=1
mkdir -p "$VAULT"
VAULT="$(cd "$VAULT" && pwd)"
OBSIDIAN="$VAULT/.obsidian"
printf 'Setting up llm-obsidian vault at: %s\n' "$VAULT"

BACKUP=""
ensure_backup_dir() {
  [ -n "$BACKUP" ] && return 0
  stamp="$(date '+%Y%m%d-%H%M%S')"
  backup_base="$VAULT/.obsidian-backups/$stamp"
  BACKUP="$backup_base"
  suffix=1
  while [ -e "$BACKUP" ]; do
    BACKUP="$backup_base-$suffix"
    suffix=$((suffix + 1))
  done
  mkdir -p "$BACKUP"
}

if [ "$RESET_OBSIDIAN" -eq 1 ] && [ "$HAD_OBSIDIAN" -eq 1 ]; then
  ensure_backup_dir
  cp -R "$OBSIDIAN/." "$BACKUP/"
  printf 'backup: %s\n' "$BACKUP"
fi

mkdir -p "$OBSIDIAN/snippets" "$VAULT/.raw" "$VAULT/_templates"
mkdir -p "$VAULT/wiki/concepts" "$VAULT/wiki/entities" "$VAULT/wiki/sources" "$VAULT/wiki/meta"

install_managed() {
  local name="$1"
  local src="$ROOT/.obsidian/$name"
  local dest="$OBSIDIAN/$name"
  local tmp
  if [ -e "$dest" ] && [ "$RESET_OBSIDIAN" -eq 0 ]; then
    printf 'keep: %s\n' "$dest"
    return 0
  fi
  tmp="$dest.tmp.$$"
  if [ "$VAULT" = "$ROOT" ]; then
    git -C "$ROOT" show "HEAD:.obsidian/$name" > "$tmp" || {
      rm -f "$tmp"
      printf 'ERROR: cannot restore tracked default .obsidian/%s\n' "$name" >&2
      return 1
    }
  else
    [ -f "$src" ] || { printf 'ERROR: template missing: %s\n' "$src" >&2; return 1; }
    cp "$src" "$tmp"
  fi
  mv "$tmp" "$dest"
  printf '%s: %s\n' "$([ "$RESET_OBSIDIAN" -eq 1 ] && printf reset || printf create)" "$dest"
}

install_managed graph.json
install_managed app.json
install_managed appearance.json

EXCALIDRAW="$OBSIDIAN/plugins/obsidian-excalidraw-plugin"
sha256_file() {
  python3 - "$1" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

load_excalidraw_lock() {
  local values
  if [ ! -f "$EXCALIDRAW_LOCK_FILE" ]; then
    printf 'ERROR: Excalidraw lock missing: %s\n' "$EXCALIDRAW_LOCK_FILE" >&2
    return 1
  fi
  if ! values=$(python3 - "$EXCALIDRAW_LOCK_FILE" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
version = data.get("version")
asset = data.get("asset")
url = asset.get("url") if isinstance(asset, dict) else None
sha256 = asset.get("sha256") if isinstance(asset, dict) else None
if not isinstance(version, str) or not version.strip():
    raise SystemExit("lock version must be a non-empty string")
if not isinstance(url, str) or not url.startswith("https://"):
    raise SystemExit("lock asset.url must be https")
if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
    raise SystemExit("lock asset.sha256 must be lowercase SHA-256")
print("\t".join((version, url, sha256)))
PY
  ); then
    printf 'ERROR: invalid Excalidraw lock: %s\n' "$EXCALIDRAW_LOCK_FILE" >&2
    return 1
  fi
  IFS=$'\t' read -r EXCALIDRAW_VERSION EXCALIDRAW_URL EXCALIDRAW_SHA256 <<< "$values"
}

install_excalidraw_main() {
  local replace_existing="$1"
  local tmp="$EXCALIDRAW/main.js.tmp.$$"
  local actual backup_file
  rm -f "$tmp"
  printf 'Downloading pinned Excalidraw %s main.js (~8MB)...\n' "$EXCALIDRAW_VERSION"
  if ! curl -fsSL "$EXCALIDRAW_URL" -o "$tmp"; then
    rm -f "$tmp"
    printf 'ERROR: Excalidraw download failed; existing file was not changed\n' >&2
    return 1
  fi
  actual=$(sha256_file "$tmp")
  if [ "$actual" != "$EXCALIDRAW_SHA256" ]; then
    rm -f "$tmp"
    printf 'ERROR: Excalidraw checksum mismatch (got %s, expected %s); existing file was not changed\n' \
      "$actual" "$EXCALIDRAW_SHA256" >&2
    return 1
  fi
  if [ "$replace_existing" -eq 1 ]; then
    ensure_backup_dir
    backup_file="$BACKUP/plugins/obsidian-excalidraw-plugin/main.js"
    mkdir -p "$(dirname "$backup_file")"
    if ! cp -p "$EXCALIDRAW/main.js" "$backup_file"; then
      rm -f "$tmp"
      printf 'ERROR: could not back up existing Excalidraw main.js\n' >&2
      return 1
    fi
    printf 'backup: %s\n' "$backup_file"
  fi
  if ! mv "$tmp" "$EXCALIDRAW/main.js"; then
    rm -f "$tmp"
    printf 'ERROR: could not install verified Excalidraw main.js\n' >&2
    return 1
  fi
  printf 'installed: Excalidraw %s main.js (sha256 %s)\n' \
    "$EXCALIDRAW_VERSION" "$EXCALIDRAW_SHA256"
}

if [ -f "$EXCALIDRAW/manifest.json" ]; then
  load_excalidraw_lock
  manifest_version=$(python3 - "$EXCALIDRAW/manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle).get("version")
print(value if isinstance(value, str) else "")
PY
  )
  if [ -f "$EXCALIDRAW/main.js" ]; then
    actual=$(sha256_file "$EXCALIDRAW/main.js")
    if [ "$actual" = "$EXCALIDRAW_SHA256" ]; then
      printf 'keep: %s (pinned Excalidraw %s)\n' "$EXCALIDRAW/main.js" "$EXCALIDRAW_VERSION"
    elif [ "$REPAIR_EXCALIDRAW" -eq 0 ]; then
      printf 'WARN: preserving existing Excalidraw main.js checksum %s (pinned %s expects %s); run bin/setup-vault.sh --repair-excalidraw "%s" to back up and replace it\n' \
        "$actual" "$EXCALIDRAW_VERSION" "$EXCALIDRAW_SHA256" "$VAULT" >&2
    elif [ "$manifest_version" != "$EXCALIDRAW_VERSION" ]; then
      printf 'ERROR: Excalidraw manifest version %s does not match pinned main.js version %s\n' \
        "${manifest_version:-unknown}" "$EXCALIDRAW_VERSION" >&2
      exit 1
    else
      install_excalidraw_main 1
    fi
  elif [ "$manifest_version" != "$EXCALIDRAW_VERSION" ]; then
    if [ "$REPAIR_EXCALIDRAW" -eq 1 ]; then
      printf 'ERROR: Excalidraw manifest version %s does not match pinned main.js version %s\n' \
        "${manifest_version:-unknown}" "$EXCALIDRAW_VERSION" >&2
      exit 1
    fi
    printf 'WARN: Excalidraw manifest version %s does not match pinned main.js version %s; skipping install so the vault bootstrap can continue\n' \
      "${manifest_version:-unknown}" "$EXCALIDRAW_VERSION" >&2
  else
    install_excalidraw_main 0
  fi
fi

tasks_args=(
  --vault "$VAULT"
  --lock "$TASKS_LOCK_FILE"
  --defaults "$TASKS_DEFAULTS_FILE"
  --snippet "$TASKS_SNIPPET_FILE"
)
[ "$REPAIR_TASKS" -eq 1 ] && tasks_args+=(--repair)
[ "$HAD_OBSIDIAN" -eq 0 ] && tasks_args+=(--fresh)
python3 "$ROOT/scripts/setup-obsidian-tasks.py" "${tasks_args[@]}"

printf '\nSetup complete. Open this folder as an Obsidian vault: %s\n' "$VAULT"
if [ -n "$BACKUP" ]; then
  printf 'Previous Obsidian configuration: %s\n' "$BACKUP"
fi
