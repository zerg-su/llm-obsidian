#!/usr/bin/env bash
# Regression suite for the MCP gateway management layer:
#   scripts/mcp-gateway/mcp-gateway.sh  — CLI, doctor pre-flight, config gating
#   scripts/mcp-gateway/smoke.py        — handshake, --routes
#   scripts/mcp-gateway/update-pins.py  — npx spec parsing, tools.json model, --sync
#
# Fully offline and deterministic: fake MCP HTTP server on localhost instead
# of the live gateway, a stub `uv` binary instead of the real one, shipped
# example configs touched READ-ONLY. Sandbox in mktemp; a live gateway (if
# any) is never touched.
#
# Run from repo root: ./tests/test_mcp_gateway.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GW="$REPO_ROOT/scripts/mcp-gateway"
SANDBOX="$(mktemp -d /tmp/mcp-gateway-test.XXXXXX)"
FAKE_PID=""
trap '[ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null; rm -rf "$SANDBOX"' EXIT

pass=0; fail=0; failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s — %s\n' "$1" "$2"; }

expect_exit() { # name got want
  [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "exit $2 (want $3)"
}
expect_grep() { # name file pattern
  grep -qF -- "$3" "$2" && ok "$1" || bad "$1" "pattern not found: $3"
}
expect_nogrep() { # name file pattern
  grep -qF -- "$3" "$2" && bad "$1" "unexpected pattern found: $3" || ok "$1"
}

OUT="$SANDBOX/out.txt"

# ---------- A. syntax ----------
echo "A. syntax"
bash -n "$GW/mcp-gateway.sh" 2>"$OUT"; expect_exit "A1 mcp-gateway.sh bash -n" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/smoke.py').read())" 2>"$OUT"; expect_exit "A2 smoke.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/update-pins.py').read())" 2>"$OUT"; expect_exit "A3 update-pins.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/codex-sync.py').read())" 2>"$OUT"; expect_exit "A4 codex-sync.py parses" "$?" 0

# ---------- B. update-pins pure functions ----------
echo "B. update-pins pure functions"
python3 - "$GW" >"$OUT" 2>&1 <<'PYEOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("update_pins", f"{sys.argv[1]}/update-pins.py")
up = importlib.util.module_from_spec(spec); spec.loader.exec_module(up)
assert up.split_spec("pkg@1.2.3") == ("pkg", "1.2.3")
assert up.split_spec("@scope/pkg@2.1.29") == ("@scope/pkg", "2.1.29")
assert up.split_spec("@scope/pkg") == ("@scope/pkg", None)
assert up.split_spec("pkg") == ("pkg", None)
assert up.find_npx_spec_index(["-y", "some-mcp@0.8.0", "stdio"]) == 1
assert up.find_npx_spec_index(["pkg"]) == 0
assert up.find_npx_spec_index(["-p", "helper", "pkg@1.0"]) == 2
assert up.find_npx_spec_index(["-y"]) is None
print("PURE_OK")
PYEOF
expect_grep "B1 split_spec + find_npx_spec_index" "$OUT" "PURE_OK"

# ---------- C. shipped example config invariants (read-only) ----------
echo "C. example config invariants"
python3 - "$GW" "$REPO_ROOT" >"$OUT" 2>&1 <<'PYEOF'
import json, re, sys
gw, root = sys.argv[1], sys.argv[2]
cfg = json.load(open(f"{gw}/config.json.example"))
tools = {k: v for k, v in json.load(open(f"{gw}/tools.json")).items() if not k.startswith("_")}
for name, s in cfg["mcpServers"].items():
    assert ("url" in s) != ("command" in s), f"{name}: exactly one of url/command"
    assert s.get("command", "") != "uvx", f"{name}: no uvx wrappers"
# every ${VAR} in the example must be documented in secrets.env.example
needed = set(re.findall(r"\$\{(\w+)\}", open(f"{gw}/config.json.example").read()))
documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", open(f"{gw}/secrets.env.example").read(), re.M))
missing = needed - documented
assert not missing, f"secrets.env.example missing: {missing}"
# client example must point every server at the gateway route of the same name
client = json.load(open(f"{root}/.mcp.json.example"))
for name, entry in client["mcpServers"].items():
    assert entry["type"] == "http", f"{name}: client entries are http pointers"
    assert entry["url"].endswith(f"/{name}/mcp"), f"{name}: route path must match server name"
    assert name in cfg["mcpServers"], f"{name}: client entry has no gateway child"
print("INVARIANTS_OK")
PYEOF
expect_grep "C1 example config consistency" "$OUT" "INVARIANTS_OK"

# ---------- D. update-pins --sync (stub uv) ----------
echo "D. update-pins --sync (stub uv)"
mkdir -p "$SANDBOX/bin" "$SANDBOX/gwdir"
cat > "$SANDBOX/bin/uv" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$SANDBOX/uv-calls.log"
EOF
chmod +x "$SANDBOX/bin/uv"
cat > "$SANDBOX/gwdir/tools.json" <<'EOF'
{
  "_comment": "fixture",
  "plain-pkg": {"version": "1.0.0", "with": [], "python": null, "entrypoint": "plain-pkg"},
  "fancy-pkg": {"version": "2.0.0", "with": ["dep<3"], "python": "3.11", "entrypoint": "fancy-pkg"}
}
EOF
cat > "$SANDBOX/gwdir/config.json" <<'EOF'
{"mcpServers": {"plain": {"command": "/x/.local/bin/plain-pkg", "args": []}}}
EOF
PATH="$SANDBOX/bin:$PATH" python3 "$GW/update-pins.py" "$SANDBOX/gwdir/config.json" --sync >"$OUT" 2>&1
expect_exit "D1 --sync exits 0" "$?" 0
expect_grep "D2 plain install cmd" "$SANDBOX/uv-calls.log" "tool install plain-pkg==1.0.0 --force"
expect_grep "D3 constraints preserved" "$SANDBOX/uv-calls.log" "tool install fancy-pkg==2.0.0 --force --with dep<3 --python 3.11"

# ---------- E. update-pins name filter ----------
echo "E. update-pins name filter"
python3 "$GW/update-pins.py" "$SANDBOX/gwdir/config.json" --check no-such-pkg >"$OUT" 2>&1
expect_exit "E1 unknown name -> non-zero" "$?" 1
expect_grep "E2 unknown name message" "$OUT" "unknown name(s): no-such-pkg"

# ---------- F. smoke.py vs fake MCP server ----------
echo "F. smoke.py vs fake MCP server"
cat > "$SANDBOX/fake_mcp.py" <<'EOF'
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/bad/"):
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "tools": [{"name": "a"}, {"name": "b"}],
            "content": [{"type": "text", "text": "successfully connected"}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
EOF
python3 "$SANDBOX/fake_mcp.py" > "$SANDBOX/port.txt" &
FAKE_PID=$!
disown
for _ in $(seq 1 50); do [ -s "$SANDBOX/port.txt" ] && break; sleep 0.1; done
PORT="$(cat "$SANDBOX/port.txt")"
cat > "$SANDBOX/gwdir/routes.json" <<'EOF'
{"mcpServers": {"good": {"command": "/x/good", "args": []}, "bad": {"command": "/x/bad", "args": []}}}
EOF
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes.json" "$PORT" --routes >"$OUT" 2>&1
expect_exit "F1 --routes exit 1 with a DOWN child" "$?" 1
expect_grep "F2 routes summary" "$OUT" "routes: 1/2 active, 1 DOWN"
expect_grep "F3 DOWN marker" "$OUT" "!bad"
cat > "$SANDBOX/gwdir/routes-ok.json" <<'EOF'
{"mcpServers": {"good": {"command": "/x/good", "args": []}}}
EOF
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes-ok.json" "$PORT" --routes >"$OUT" 2>&1
expect_exit "F4 --routes exit 0 when all alive" "$?" 0
expect_grep "F5 all-active summary" "$OUT" "routes: 1/1 active"
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes.json" "$PORT" good,bad >"$OUT" 2>&1
expect_exit "F6 handshake exit 1 (bad fails)" "$?" 1
expect_grep "F7 handshake OK line" "$OUT" "OK   good: 2 tools"
expect_grep "F8 handshake FAIL line" "$OUT" "FAIL bad"
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes.json" "$PORT" nonexistent >"$OUT" 2>&1
expect_exit "F9 unknown server -> non-zero" "$?" 1
expect_grep "F10 unknown server message" "$OUT" "unknown server: nonexistent"

# ---------- G. config gating + doctor sandbox ----------
echo "G. config gating + doctor sandbox"
mkdir -p "$SANDBOX/home" "$SANDBOX/gwcopy"
cp "$GW/mcp-gateway.sh" "$GW/smoke.py" "$GW/update-pins.py" "$SANDBOX/gwcopy/"
# G0: no config.json -> every command fails with the copy-example hint
HOME="$SANDBOX/home" bash "$SANDBOX/gwcopy/mcp-gateway.sh" doctor >"$OUT" 2>&1
expect_exit "G0 missing config -> exit 1" "$?" 1
expect_grep "G0b copy-example hint" "$OUT" "config.json.example"
cat > "$SANDBOX/gwcopy/tools.json" <<'EOF'
{"plain-pkg": {"version": "1.0.0", "with": [], "python": null, "entrypoint": "plain-pkg"}}
EOF
cat > "$SANDBOX/gwcopy/config.json" <<EOF
{"mcpServers": {
  "plain": {"command": "$SANDBOX/home/.local/bin/plain-pkg", "args": [], "env": {"AWS_PROFILE": "test-prof", "TOKEN": "\${TEST_TOKEN}"}},
  "hosted": {"url": "https://example.com/mcp", "transportType": "streamable-http"}
}}
EOF
cat > "$SANDBOX/bin/uv" <<EOF
#!/usr/bin/env bash
[ "\$1 \$2" = "tool list" ] && cat "$SANDBOX/uv-list.txt" 2>/dev/null
exit 0
EOF
: > "$SANDBOX/uv-list.txt"
HOME="$SANDBOX/home" PATH="$SANDBOX/bin:$PATH" bash "$SANDBOX/gwcopy/mcp-gateway.sh" doctor >"$OUT" 2>&1
expect_exit "G1 bare machine -> exit 1" "$?" 1
expect_grep "G2 missing proxy binary" "$OUT" "FAIL binary mcp-proxy"
expect_grep "G3 missing child binary" "$OUT" "FAIL child binary for plain"
expect_grep "G4 missing uv tool" "$OUT" "FAIL uv tool plain-pkg==1.0.0"
expect_grep "G5 missing secrets" "$OUT" "FAIL secrets file"
expect_grep "G6 missing aws profile" "$OUT" "FAIL aws profile test-prof"
expect_grep "G7 missing plist" "$OUT" "FAIL launchd plist io.llm-obsidian.mcp-gateway"
# now a fully provisioned machine
mkdir -p "$SANDBOX/home/.local/bin" "$SANDBOX/home/.config/mcp-gateway" "$SANDBOX/home/.aws" "$SANDBOX/home/Library/LaunchAgents"
printf '#!/bin/sh\n' > "$SANDBOX/home/.local/bin/mcp-proxy"; chmod +x "$SANDBOX/home/.local/bin/mcp-proxy"
printf '#!/bin/sh\n' > "$SANDBOX/home/.local/bin/plain-pkg"; chmod +x "$SANDBOX/home/.local/bin/plain-pkg"
echo "plain-pkg v1.0.0" > "$SANDBOX/uv-list.txt"
printf 'TEST_TOKEN=x\n' > "$SANDBOX/home/.config/mcp-gateway/secrets.env"
printf '[profile test-prof]\nregion = us-east-1\n' > "$SANDBOX/home/.aws/config"
touch "$SANDBOX/home/Library/LaunchAgents/io.llm-obsidian.mcp-gateway.plist"
HOME="$SANDBOX/home" PATH="$SANDBOX/bin:$PATH" bash "$SANDBOX/gwcopy/mcp-gateway.sh" doctor >"$OUT" 2>&1
expect_exit "G8 provisioned -> exit 0" "$?" 0
expect_grep "G9 all OK footer" "$OUT" "--- all OK"

# ---------- H. codex-sync sandbox ----------
echo "H. codex-sync sandbox"
mkdir -p "$SANDBOX/codex-repo/.codex" "$SANDBOX/codex-repo/.mcp-profiles" "$SANDBOX/codex-home"
cat > "$SANDBOX/codex-repo/.mcp.json.example" <<'EOF'
{"mcpServers": {
  "context7": {"type": "http", "url": "http://127.0.0.1:9090/context7/mcp"}
}}
EOF
cat > "$SANDBOX/codex-repo/.mcp-profiles/research.json" <<'EOF'
{"mcpServers": {
  "paper-search": {"type": "http", "url": "http://127.0.0.1:9090/paper-search/mcp"}
}}
EOF
cat > "$SANDBOX/codex-repo/.codex/config.toml" <<'EOF'
[tui]
status_line = ["project-name"]

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]

[mcp_servers.paper-search]
command = "uvx"
args = ["paper-search-mcp"]
EOF
cat > "$SANDBOX/codex-home/config.toml" <<'EOF'
model = "gpt-5.5"

[mcp_servers.node_repl]
command = "/x/node_repl"

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
EOF
chmod 600 "$SANDBOX/codex-home/config.toml"
python3 "$GW/codex-sync.py" --repo-root "$SANDBOX/codex-repo" --codex-home "$SANDBOX/codex-home" --check >"$OUT" 2>&1
expect_exit "H1 --check reports drift" "$?" 1
expect_grep "H2 check mentions repo config" "$OUT" "$SANDBOX/codex-repo/.codex/config.toml"
python3 "$GW/codex-sync.py" --repo-root "$SANDBOX/codex-repo" --codex-home "$SANDBOX/codex-home" --apply >"$OUT" 2>&1
expect_exit "H3 --apply exits 0" "$?" 0
expect_grep "H4 backup reported" "$OUT" "backup dir:"
python3 - "$SANDBOX/codex-home" "$SANDBOX/codex-repo/.codex" >"$OUT" 2>&1 <<'PYEOF'
import pathlib, stat, sys
home = pathlib.Path(sys.argv[1])
repo_codex = pathlib.Path(sys.argv[2])
backups = sorted((home / "backups").glob("llm-obsidian-codex-sync-*"))
assert backups, "no backup dir"
assert stat.S_IMODE(backups[-1].stat().st_mode) == 0o700
assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600
assert not list(home.glob("*.config.toml.tmp.*"))
assert not list(repo_codex.glob("*.config.toml.tmp.*"))
print("BACKUP_MODE_OK")
PYEOF
expect_grep "H5 backup dir private and atomic cleanup" "$OUT" "BACKUP_MODE_OK"
expect_grep "H6 repo has gateway context7" "$SANDBOX/codex-repo/.codex/config.toml" "url = \"http://127.0.0.1:9090/context7/mcp\""
expect_nogrep "H7 repo default excludes profile server" "$SANDBOX/codex-repo/.codex/config.toml" "paper-search"
expect_grep "H8 global keeps node_repl" "$SANDBOX/codex-home/config.toml" "[mcp_servers.node_repl]"
expect_nogrep "H9 global removes legacy context7" "$SANDBOX/codex-home/config.toml" "@upstash/context7-mcp"
expect_grep "H10 default profile has context7" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" "context7/mcp"
expect_grep "H11 extra profile has paper-search" "$SANDBOX/codex-home/llm-obsidian-research.config.toml" "paper-search/mcp"
python3 "$GW/codex-sync.py" --repo-root "$SANDBOX/codex-repo" --codex-home "$SANDBOX/codex-home" --check >"$OUT" 2>&1
expect_exit "H12 second --check clean" "$?" 0
expect_grep "H13 no changes message" "$OUT" "codex-sync: no changes"

# ---------- summary ----------
echo
echo "=== $pass passed, $fail failed ==="
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
