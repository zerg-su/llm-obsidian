#!/usr/bin/env bash
# setup-vault preserves user config and installs pinned plugins fail-closed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d /tmp/setup-vault-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

FAKE_BIN="$TMP/fake-bin"
FAKE_CURL_LOG="$TMP/fake-curl.log"
TASKS_ASSETS="$TMP/tasks-assets"
mkdir -p "$FAKE_BIN" "$TASKS_ASSETS"
: > "$FAKE_CURL_LOG"
printf 'pinned tasks main\n' > "$TASKS_ASSETS/main.js"
printf '{"id":"obsidian-tasks-plugin","name":"Tasks","version":"8.2.2","minAppVersion":"1.8.7"}\n' \
  > "$TASKS_ASSETS/manifest.json"
printf '/* pinned tasks styles */\n' > "$TASKS_ASSETS/styles.css"

TASKS_LOCK="$TMP/tasks.lock.json"
main_sha=$(shasum -a 256 "$TASKS_ASSETS/main.js" | awk '{print $1}')
manifest_sha=$(shasum -a 256 "$TASKS_ASSETS/manifest.json" | awk '{print $1}')
styles_sha=$(shasum -a 256 "$TASKS_ASSETS/styles.css" | awk '{print $1}')
python3 - "$TASKS_LOCK" "$main_sha" "$manifest_sha" "$styles_sha" <<'PY'
import json
import sys

path, main, manifest, styles = sys.argv[1:]
value = {
    "project": "obsidian-tasks-group/obsidian-tasks",
    "plugin_id": "obsidian-tasks-plugin",
    "version": "8.2.2",
    "assets": {
        "main.js": {"url": "https://example.invalid/tasks/8.2.2/main.js", "sha256": main},
        "manifest.json": {"url": "https://example.invalid/tasks/8.2.2/manifest.json", "sha256": manifest},
        "styles.css": {"url": "https://example.invalid/tasks/8.2.2/styles.css", "sha256": styles},
    },
}
open(path, "w", encoding="utf-8").write(json.dumps(value) + "\n")
PY

cat > "$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
[ -n "$out" ]
[ -n "$url" ]
printf '%s\n' "$url" >> "$FAKE_CURL_LOG"
if [[ "$url" == *"/tasks/"* ]]; then
  name="${url##*/}"
  if [ "${TASKS_BAD_ASSET:-}" = "$name" ]; then
    printf 'tampered tasks asset\n' > "$out"
  else
    cp "$FAKE_TASKS_ASSETS/$name" "$out"
  fi
else
  cp "$FAKE_CURL_SOURCE" "$out"
fi
EOF
chmod +x "$FAKE_BIN/curl"

run_setup() {
  PATH="$FAKE_BIN:$PATH" \
  FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  FAKE_TASKS_ASSETS="$TASKS_ASSETS" \
  TASKS_LOCK_FILE="$TASKS_LOCK" \
  TASKS_DEFAULTS_FILE="$ROOT/config/obsidian-tasks.defaults.json" \
  TASKS_SNIPPET_FILE="$ROOT/.obsidian/snippets/llm-obsidian-tasks.css" \
  bash "$ROOT/bin/setup-vault.sh" "$@"
}

# Existing app/appearance/graph remain untouched unless reset is explicit.
VAULT="$TMP/vault"
mkdir -p "$VAULT/.obsidian"
printf '{"custom":true}\n' > "$VAULT/.obsidian/app.json"
printf '{"customGraph":true}\n' > "$VAULT/.obsidian/graph.json"
printf '{"customAppearance":true}\n' > "$VAULT/.obsidian/appearance.json"
run_setup "$VAULT" >/dev/null
grep -q '"custom":true' "$VAULT/.obsidian/app.json"
grep -q '"customGraph":true' "$VAULT/.obsidian/graph.json"
grep -q '"customAppearance":true' "$VAULT/.obsidian/appearance.json"
first_hash=$(shasum -a 256 "$VAULT/.obsidian/app.json" | awk '{print $1}')
run_setup "$VAULT" >/dev/null
second_hash=$(shasum -a 256 "$VAULT/.obsidian/app.json" | awk '{print $1}')
[[ "$first_hash" == "$second_hash" ]]

out=$(run_setup --reset-obsidian "$VAULT")
backup=$(printf '%s\n' "$out" | awk '/^backup: / {sub(/^backup: /, ""); print; exit}')
[[ -d "$backup" ]]
grep -q '"custom":true' "$backup/app.json"
cmp -s "$ROOT/.obsidian/app.json" "$VAULT/.obsidian/app.json"
cmp -s "$ROOT/.obsidian/graph.json" "$VAULT/.obsidian/graph.json"
python3 - "$ROOT/config/obsidian-excalidraw-main.lock.json" \
  "$ROOT/.obsidian/plugins/obsidian-excalidraw-plugin/manifest.json" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
manifest = json.load(open(sys.argv[2], encoding="utf-8"))
assert lock["project"] == "zsviczian/obsidian-excalidraw-plugin"
assert lock["version"] == manifest["version"]
PY

# Fresh Tasks install: all assets, defaults, plugin enablement, and CSS snippet.
FRESH="$TMP/fresh"
run_setup "$FRESH" >/dev/null
for name in main.js manifest.json styles.css; do
  cmp -s "$TASKS_ASSETS/$name" "$FRESH/.obsidian/plugins/obsidian-tasks-plugin/$name"
done
python3 - "$FRESH" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) / ".obsidian"
plugins = json.loads((root / "community-plugins.json").read_text())
data = json.loads((root / "plugins/obsidian-tasks-plugin/data.json").read_text())
appearance = json.loads((root / "appearance.json").read_text())
assert plugins == ["obsidian-tasks-plugin"]
assert data["taskFormat"] == "tasksPluginEmoji"
assert data["filenameAsDateFolders"] == ["wiki/daily"]
assert any(item["symbol"] == ">" and item["type"] == "NON_TASK" for item in data["statusSettings"]["customStatuses"])
assert "llm-obsidian-tasks" in appearance["enabledCssSnippets"]
PY

# A partially missing pinned set restores only the absent file and preserves
# already verified assets byte-for-byte.
PARTIAL="$TMP/partial-tasks"
run_setup "$PARTIAL" >/dev/null
partial_main_hash=$(shasum -a 256 "$PARTIAL/.obsidian/plugins/obsidian-tasks-plugin/main.js" | awk '{print $1}')
partial_manifest_hash=$(shasum -a 256 "$PARTIAL/.obsidian/plugins/obsidian-tasks-plugin/manifest.json" | awk '{print $1}')
rm "$PARTIAL/.obsidian/plugins/obsidian-tasks-plugin/styles.css"
run_setup "$PARTIAL" >/dev/null
cmp -s "$TASKS_ASSETS/styles.css" "$PARTIAL/.obsidian/plugins/obsidian-tasks-plugin/styles.css"
[[ "$partial_main_hash" == "$(shasum -a 256 "$PARTIAL/.obsidian/plugins/obsidian-tasks-plugin/main.js" | awk '{print $1}')" ]]
[[ "$partial_manifest_hash" == "$(shasum -a 256 "$PARTIAL/.obsidian/plugins/obsidian-tasks-plugin/manifest.json" | awk '{print $1}')" ]]

# Existing Tasks settings: preserve nested arrays/objects, add only absent top-level keys.
EXISTING="$TMP/existing-tasks"
mkdir -p "$EXISTING/.obsidian/plugins/obsidian-tasks-plugin"
printf '["user-plugin"]\n' > "$EXISTING/.obsidian/community-plugins.json"
printf '{}\n' > "$EXISTING/.obsidian/app.json"
printf '{}\n' > "$EXISTING/.obsidian/graph.json"
printf '{}\n' > "$EXISTING/.obsidian/appearance.json"
cat > "$EXISTING/.obsidian/plugins/obsidian-tasks-plugin/data.json" <<'EOF'
{
  "filenameAsDateFolders": ["Personal/Daily"],
  "statusSettings": {
    "coreStatuses": [],
    "customStatuses": [{"symbol":"?","name":"Question","nextStatusSymbol":" ","availableAsCommand":false,"type":"TODO"}]
  }
}
EOF
run_setup "$EXISTING" > "$TMP/existing.out" 2>&1
python3 - "$EXISTING" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) / ".obsidian"
plugins = json.loads((root / "community-plugins.json").read_text())
data = json.loads((root / "plugins/obsidian-tasks-plugin/data.json").read_text())
assert plugins == ["user-plugin", "obsidian-tasks-plugin"]
assert data["filenameAsDateFolders"] == ["Personal/Daily"]
assert data["statusSettings"]["customStatuses"][0]["symbol"] == "?"
assert data["taskFormat"] == "tasksPluginEmoji"
PY
grep -q "no '>' Migrated/NON_TASK status" "$TMP/existing.out"
backup_data=$(find "$EXISTING/.obsidian-backups" -path '*/plugins/obsidian-tasks-plugin/data.json' -print -quit)
[[ -n "$backup_data" ]]
data_hash=$(shasum -a 256 "$EXISTING/.obsidian/plugins/obsidian-tasks-plugin/data.json" | awk '{print $1}')
run_setup "$EXISTING" >/dev/null 2>&1
[[ "$data_hash" == "$(shasum -a 256 "$EXISTING/.obsidian/plugins/obsidian-tasks-plugin/data.json" | awk '{print $1}')" ]]

# Mismatched Tasks assets are preserved by default, repaired only explicitly.
CUSTOM="$TMP/custom-tasks"
run_setup "$CUSTOM" >/dev/null
printf 'user custom tasks build\n' > "$CUSTOM/.obsidian/plugins/obsidian-tasks-plugin/main.js"
cp "$CUSTOM/.obsidian/plugins/obsidian-tasks-plugin/main.js" "$TMP/custom-tasks-main.js"
run_setup "$CUSTOM" > "$TMP/tasks-preserve.out" 2>&1
cmp -s "$TMP/custom-tasks-main.js" "$CUSTOM/.obsidian/plugins/obsidian-tasks-plugin/main.js"
grep -q -- '--repair-tasks' "$TMP/tasks-preserve.out"
run_setup --repair-tasks "$CUSTOM" > "$TMP/tasks-repair.out" 2>&1
cmp -s "$TASKS_ASSETS/main.js" "$CUSTOM/.obsidian/plugins/obsidian-tasks-plugin/main.js"
backup_main=$(find "$CUSTOM/.obsidian-backups" -path '*/plugins/obsidian-tasks-plugin/main.js' -print -quit)
[[ -n "$backup_main" ]]
cmp -s "$TMP/custom-tasks-main.js" "$backup_main"

# Bad download and malformed user JSON both fail before partial plugin enablement.
BAD="$TMP/bad-tasks"
set +e
TASKS_BAD_ASSET=main.js run_setup "$BAD" > "$TMP/bad-tasks.out" 2>&1
bad_rc=$?
set -e
[[ "$bad_rc" != 0 ]]
[[ ! -e "$BAD/.obsidian/plugins/obsidian-tasks-plugin/main.js" ]]
grep -q 'checksum mismatch' "$TMP/bad-tasks.out"

MALFORMED="$TMP/malformed"
mkdir -p "$MALFORMED/.obsidian"
printf '{}\n' > "$MALFORMED/.obsidian/app.json"
printf '{}\n' > "$MALFORMED/.obsidian/graph.json"
printf '{}\n' > "$MALFORMED/.obsidian/appearance.json"
printf '{broken\n' > "$MALFORMED/.obsidian/community-plugins.json"
set +e
run_setup "$MALFORMED" > "$TMP/malformed.out" 2>&1
malformed_rc=$?
set -e
[[ "$malformed_rc" != 0 ]]
[[ ! -e "$MALFORMED/.obsidian/plugins/obsidian-tasks-plugin/main.js" ]]
grep -q '^{broken' "$MALFORMED/.obsidian/community-plugins.json"
grep -q 'community-plugins' "$TMP/malformed.out"

# Existing pinned Excalidraw flow remains unchanged.
GOOD_MAIN="$TMP/pinned-main.js"
BAD_MAIN="$TMP/bad-main.js"
printf 'pinned excalidraw main\n' > "$GOOD_MAIN"
printf 'tampered download\n' > "$BAD_MAIN"
GOOD_SHA=$(shasum -a 256 "$GOOD_MAIN" | awk '{print $1}')
LOCK="$TMP/excalidraw.lock.json"
python3 - "$LOCK" "$GOOD_SHA" <<'PY'
import json
import sys
open(sys.argv[1], "w").write(json.dumps({
    "version": "2.22.0",
    "asset": {"url": "https://example.invalid/excalidraw/2.22.0/main.js", "sha256": sys.argv[2]},
}) + "\n")
PY

make_plugin_vault() {
  local target="$1"
  mkdir -p "$target/.obsidian/plugins/obsidian-excalidraw-plugin"
  printf '%s\n' '{"id":"obsidian-excalidraw-plugin","name":"Excalidraw","version":"2.22.0"}' \
    > "$target/.obsidian/plugins/obsidian-excalidraw-plugin/manifest.json"
}

MISSING="$TMP/missing-main"
make_plugin_vault "$MISSING"
FAKE_CURL_SOURCE="$GOOD_MAIN" EXCALIDRAW_LOCK_FILE="$LOCK" run_setup "$MISSING" >/dev/null
cmp -s "$GOOD_MAIN" "$MISSING/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"

EXCAL_MISMATCH="$TMP/excal-mismatch"
make_plugin_vault "$EXCAL_MISMATCH"
cp "$BAD_MAIN" "$EXCAL_MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"
FAKE_CURL_SOURCE="$GOOD_MAIN" EXCALIDRAW_LOCK_FILE="$LOCK" run_setup "$EXCAL_MISMATCH" > "$TMP/excal-preserve.out" 2>&1
cmp -s "$BAD_MAIN" "$EXCAL_MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"
FAKE_CURL_SOURCE="$GOOD_MAIN" EXCALIDRAW_LOCK_FILE="$LOCK" run_setup --repair-excalidraw "$EXCAL_MISMATCH" >/dev/null
cmp -s "$GOOD_MAIN" "$EXCAL_MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"

[[ -z "$(find "$TMP" -name '*.tmp.*' -print -quit)" ]]
echo "Passed: setup-vault preservation plus pinned Tasks and Excalidraw repair"
