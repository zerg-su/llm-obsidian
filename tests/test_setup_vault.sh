#!/usr/bin/env bash
# setup-vault preserves user config unless reset is explicit and backed up.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d /tmp/setup-vault-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
VAULT="$TMP/vault"
mkdir -p "$VAULT/.obsidian"
printf '{"custom":true}\n' > "$VAULT/.obsidian/app.json"
printf '{"customGraph":true}\n' > "$VAULT/.obsidian/graph.json"
printf '{"customAppearance":true}\n' > "$VAULT/.obsidian/appearance.json"

bash "$ROOT/bin/setup-vault.sh" "$VAULT" >/dev/null
grep -q '"custom":true' "$VAULT/.obsidian/app.json"
grep -q '"customGraph":true' "$VAULT/.obsidian/graph.json"
grep -q '"customAppearance":true' "$VAULT/.obsidian/appearance.json"
first_hash=$(shasum -a 256 "$VAULT/.obsidian/app.json" | awk '{print $1}')
bash "$ROOT/bin/setup-vault.sh" "$VAULT" >/dev/null
second_hash=$(shasum -a 256 "$VAULT/.obsidian/app.json" | awk '{print $1}')
[[ "$first_hash" == "$second_hash" ]]

out=$(bash "$ROOT/bin/setup-vault.sh" --reset-obsidian "$VAULT")
backup=$(printf '%s\n' "$out" | awk '/^backup: / {sub(/^backup: /, ""); print; exit}')
[[ -d "$backup" ]]
grep -q '"custom":true' "$backup/app.json"
cmp -s "$ROOT/.obsidian/app.json" "$VAULT/.obsidian/app.json"
cmp -s "$ROOT/.obsidian/graph.json" "$VAULT/.obsidian/graph.json"
cmp -s "$ROOT/.obsidian/appearance.json" "$VAULT/.obsidian/appearance.json"
python3 - "$ROOT/config/obsidian-excalidraw-main.lock.json" \
  "$ROOT/.obsidian/plugins/obsidian-excalidraw-plugin/manifest.json" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
manifest = json.load(open(sys.argv[2], encoding="utf-8"))
assert lock["project"] == "zsviczian/obsidian-excalidraw-plugin"
assert lock["version"] == manifest["version"]
assert f"/releases/download/{lock['version']}/main.js" in lock["asset"]["url"]
PY

# Hermetic Excalidraw artifact tests: fake curl, fixture lock, no network.
GOOD_MAIN="$TMP/pinned-main.js"
BAD_MAIN="$TMP/bad-main.js"
printf 'pinned excalidraw main\n' > "$GOOD_MAIN"
printf 'tampered download\n' > "$BAD_MAIN"
GOOD_SHA=$(shasum -a 256 "$GOOD_MAIN" | awk '{print $1}')
LOCK="$TMP/excalidraw.lock.json"
cat > "$LOCK" <<EOF
{
  "version": "2.22.0",
  "asset": {
    "url": "https://example.invalid/excalidraw/2.22.0/main.js",
    "sha256": "$GOOD_SHA"
  }
}
EOF

FAKE_BIN="$TMP/fake-bin"
FAKE_CURL_LOG="$TMP/fake-curl.log"
mkdir -p "$FAKE_BIN"
: > "$FAKE_CURL_LOG"
cat > "$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$out" ]
printf 'called\n' >> "$FAKE_CURL_LOG"
cp "$FAKE_CURL_SOURCE" "$out"
EOF
chmod +x "$FAKE_BIN/curl"

make_plugin_vault() {
  local target="$1"
  mkdir -p "$target/.obsidian/plugins/obsidian-excalidraw-plugin"
  cat > "$target/.obsidian/plugins/obsidian-excalidraw-plugin/manifest.json" <<'EOF'
{"id":"obsidian-excalidraw-plugin","name":"Excalidraw","version":"2.22.0"}
EOF
}

MISSING="$TMP/missing-main"
make_plugin_vault "$MISSING"
PATH="$FAKE_BIN:$PATH" FAKE_CURL_SOURCE="$GOOD_MAIN" FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  EXCALIDRAW_LOCK_FILE="$LOCK" bash "$ROOT/bin/setup-vault.sh" "$MISSING" >/dev/null
cmp -s "$GOOD_MAIN" "$MISSING/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"

BAD_DOWNLOAD="$TMP/bad-download"
make_plugin_vault "$BAD_DOWNLOAD"
set +e
PATH="$FAKE_BIN:$PATH" FAKE_CURL_SOURCE="$BAD_MAIN" FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  EXCALIDRAW_LOCK_FILE="$LOCK" bash "$ROOT/bin/setup-vault.sh" "$BAD_DOWNLOAD" \
  > "$TMP/bad-download.out" 2>&1
bad_rc=$?
set -e
[[ "$bad_rc" != 0 ]]
[[ ! -e "$BAD_DOWNLOAD/.obsidian/plugins/obsidian-excalidraw-plugin/main.js" ]]
[[ -z "$(find "$BAD_DOWNLOAD" -name '*.tmp.*' -print -quit)" ]]
grep -q 'checksum mismatch' "$TMP/bad-download.out"

VERSION_MISMATCH="$TMP/version-mismatch"
make_plugin_vault "$VERSION_MISMATCH"
printf '%s\n' '{"id":"obsidian-excalidraw-plugin","name":"Excalidraw","version":"9.9.9"}' \
  > "$VERSION_MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/manifest.json"
calls_before=$(wc -l < "$FAKE_CURL_LOG" | tr -d '[:space:]')
PATH="$FAKE_BIN:$PATH" FAKE_CURL_SOURCE="$GOOD_MAIN" FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  EXCALIDRAW_LOCK_FILE="$LOCK" bash "$ROOT/bin/setup-vault.sh" "$VERSION_MISMATCH" \
  > "$TMP/version-mismatch.out" 2>&1
calls_after=$(wc -l < "$FAKE_CURL_LOG" | tr -d '[:space:]')
[[ "$calls_before" == "$calls_after" ]]
[[ ! -e "$VERSION_MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js" ]]
grep -q 'skipping install so the vault bootstrap can continue' "$TMP/version-mismatch.out"
set +e
PATH="$FAKE_BIN:$PATH" FAKE_CURL_SOURCE="$GOOD_MAIN" FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  EXCALIDRAW_LOCK_FILE="$LOCK" bash "$ROOT/bin/setup-vault.sh" --repair-excalidraw "$VERSION_MISMATCH" \
  > "$TMP/version-mismatch-repair.out" 2>&1
version_repair_rc=$?
set -e
[[ "$version_repair_rc" != 0 ]]
grep -q 'does not match pinned main.js version' "$TMP/version-mismatch-repair.out"

MISMATCH="$TMP/existing-mismatch"
make_plugin_vault "$MISMATCH"
CUSTOM_MAIN="$TMP/custom-main.js"
printf 'user custom excalidraw build\n' > "$CUSTOM_MAIN"
cp "$CUSTOM_MAIN" "$MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"
calls_before=$(wc -l < "$FAKE_CURL_LOG" | tr -d '[:space:]')
PATH="$FAKE_BIN:$PATH" FAKE_CURL_SOURCE="$GOOD_MAIN" FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  EXCALIDRAW_LOCK_FILE="$LOCK" bash "$ROOT/bin/setup-vault.sh" "$MISMATCH" \
  > "$TMP/preserve.out" 2>&1
calls_after=$(wc -l < "$FAKE_CURL_LOG" | tr -d '[:space:]')
[[ "$calls_before" == "$calls_after" ]]
cmp -s "$CUSTOM_MAIN" "$MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"
grep -q 'preserving existing Excalidraw main.js checksum' "$TMP/preserve.out"
grep -q -- '--repair-excalidraw' "$TMP/preserve.out"

PATH="$FAKE_BIN:$PATH" FAKE_CURL_SOURCE="$GOOD_MAIN" FAKE_CURL_LOG="$FAKE_CURL_LOG" \
  EXCALIDRAW_LOCK_FILE="$LOCK" bash "$ROOT/bin/setup-vault.sh" --repair-excalidraw "$MISMATCH" \
  > "$TMP/repair.out" 2>&1
cmp -s "$GOOD_MAIN" "$MISMATCH/.obsidian/plugins/obsidian-excalidraw-plugin/main.js"
backup_main=$(find "$MISMATCH/.obsidian-backups" -type f \
  -path '*/plugins/obsidian-excalidraw-plugin/main.js' -print -quit)
[[ -n "$backup_main" ]]
cmp -s "$CUSTOM_MAIN" "$backup_main"
grep -q 'installed: Excalidraw 2.22.0' "$TMP/repair.out"
[[ -z "$(find "$MISMATCH" -name '*.tmp.*' -print -quit)" ]]

echo "Passed: setup-vault preservation, reset, and pinned Excalidraw repair"
