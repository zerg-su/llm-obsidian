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
bash -n "$REPO_ROOT/bin/setup-clean-machine.sh" 2>"$OUT"; expect_exit "A2 setup-clean-machine.sh bash -n" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/smoke.py').read())" 2>"$OUT"; expect_exit "A3 smoke.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/update-pins.py').read())" 2>"$OUT"; expect_exit "A4 update-pins.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/codex-sync.py').read())" 2>"$OUT"; expect_exit "A5 codex-sync.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/config-sync.py').read())" 2>"$OUT"; expect_exit "A6 config-sync.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/install-proxy.py').read())" 2>"$OUT"; expect_exit "A7 install-proxy.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/schema-lock.py').read())" 2>"$OUT"; expect_exit "A8 schema-lock.py parses" "$?" 0

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
python3 - "$GW/codex-sync.py" >"$OUT" 2>&1 <<'PYEOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("codex_sync", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
profile = "[hooks . state . 'global-hook:stop:0']\ntrusted_hash = 'sha256:local'\n"
global_cfg = '[hooks.state."global-hook:stop:0"]\ntrusted_hash = "sha256:global"\n'
merged = mod.merge_missing_hook_trust(profile, global_cfg)
assert merged.count("trusted_hash") == 1
assert "sha256:local" in merged and "sha256:global" not in merged
print("HOOK_CANONICAL_OK")
PYEOF
expect_grep "B2 equivalent TOML hook headers dedupe" "$OUT" "HOOK_CANONICAL_OK"

# ---------- C. shipped example config invariants (read-only) ----------
echo "C. example config invariants"
python3 - "$GW" "$REPO_ROOT" >"$OUT" 2>&1 <<'PYEOF'
import json, re, sys
gw, root = sys.argv[1], sys.argv[2]
cfg = json.load(open(f"{gw}/config.json.example"))
lock = json.load(open(f"{gw}/mcp-schema.lock.json"))
assert set(lock["servers"]) == set(cfg["mcpServers"]), "schema lock server inventory drift"
assert '"description":' not in json.dumps(lock), "lock must store description hashes only"
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
# The examples and all pinned assets are tied to explicit, reviewable inputs.
port = int(re.search(r"^MCP_GATEWAY_PORT=(\d+)$", open(f"{gw}/runtime.env.example").read(), re.M).group(1))
assert cfg["mcpProxy"]["addr"] == f"127.0.0.1:{port}"
assert cfg["mcpProxy"]["baseURL"] == f"http://127.0.0.1:{port}"
lock = json.load(open(f"{gw}/mcp-proxy.lock.json"))
assert re.fullmatch(r"\d+\.\d+\.\d+", lock["version"])
assert set(lock["assets"]) == {"darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64"}
for key, asset in lock["assets"].items():
    assert f"/v{lock['version']}/" in asset["url"], (key, asset["url"])
    assert "latest" not in asset["url"]
    assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
nudge = open(f"{root}/.claude/hooks/session-nudge.sh").read()
assert "config-sync.py\" --print-port" in nudge
assert 'http://127.0.0.1:9090/' not in nudge
print("INVARIANTS_OK")
PYEOF
expect_grep "C1 example config consistency" "$OUT" "INVARIANTS_OK"

# ---------- C2. config sync + pinned installer (offline) ----------
echo "C2. config sync + pinned installer"
mkdir -p "$SANDBOX/sync-gw" "$SANDBOX/sync-repo"
cp "$GW/config.json.example" "$SANDBOX/sync-gw/config.json.example"
cp "$REPO_ROOT/.mcp.json.example" "$SANDBOX/sync-repo/.mcp.json.example"
printf 'MCP_GATEWAY_PORT=9191\n' > "$SANDBOX/sync-gw/runtime.env"
python3 "$GW/config-sync.py" --gateway-dir "$SANDBOX/sync-gw" --repo-root "$SANDBOX/sync-repo" --check >"$OUT" 2>&1
expect_exit "C2.1 missing generated configs drift" "$?" 1
python3 "$GW/config-sync.py" --gateway-dir "$SANDBOX/sync-gw" --repo-root "$SANDBOX/sync-repo" --apply >"$OUT" 2>&1
expect_exit "C2.2 custom port apply" "$?" 0
expect_grep "C2.3 gateway port materialized" "$SANDBOX/sync-gw/config.json" '"addr": "127.0.0.1:9191"'
expect_grep "C2.4 client port materialized" "$SANDBOX/sync-repo/.mcp.json" "127.0.0.1:9191/context7/mcp"
python3 "$GW/config-sync.py" --gateway-dir "$SANDBOX/sync-gw" --repo-root "$SANDBOX/sync-repo" --check >"$OUT" 2>&1
expect_exit "C2.5 second config check clean" "$?" 0
printf 'MCP_GATEWAY_PORT=9192\n' > "$SANDBOX/sync-gw/runtime.env"
python3 "$GW/config-sync.py" --gateway-dir "$SANDBOX/sync-gw" --repo-root "$SANDBOX/sync-repo" --check >"$OUT" 2>&1
expect_exit "C2.6 runtime change detects JSON drift" "$?" 1
python3 - "$SANDBOX/sync-gw/config.json" "$SANDBOX/sync-repo/.mcp.json" <<'PYEOF'
import json, pathlib, sys
for raw in sys.argv[1:]:
    path = pathlib.Path(raw); value = json.loads(path.read_text())
    value["custom-preserved"] = {"yes": True}
    path.write_text(json.dumps(value))
PYEOF
python3 "$GW/config-sync.py" --gateway-dir "$SANDBOX/sync-gw" --repo-root "$SANDBOX/sync-repo" --apply >"$OUT" 2>&1
expect_exit "C2.7 port update applies" "$?" 0
expect_grep "C2.8 gateway custom fields preserved" "$SANDBOX/sync-gw/config.json" '"custom-preserved"'
expect_grep "C2.9 client custom fields preserved" "$SANDBOX/sync-repo/.mcp.json" '"custom-preserved"'
expect_grep "C2.10 changed port materialized" "$SANDBOX/sync-repo/.mcp.json" "127.0.0.1:9192/context7/mcp"

mkdir -p "$SANDBOX/proxy-fixture"
cat > "$SANDBOX/proxy-fixture/mcp-proxy" <<'EOF'
#!/usr/bin/env sh
echo 'mcp-proxy version 9.9.9'
EOF
chmod +x "$SANDBOX/proxy-fixture/mcp-proxy"
python3 - "$SANDBOX" <<'PYEOF'
import hashlib, json, pathlib, tarfile, sys
root = pathlib.Path(sys.argv[1])
archive = root / "proxy.tar.gz"
binary = root / "proxy-fixture" / "mcp-proxy"
with tarfile.open(archive, "w:gz") as tf:
    tf.add(binary, arcname="release/mcp-proxy")
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
lock = {"version": "9.9.9", "assets": {"linux-amd64": {
    "url": archive.resolve().as_uri(), "sha256": digest}}}
(root / "proxy.lock.json").write_text(json.dumps(lock))
PYEOF
python3 "$GW/install-proxy.py" --lock "$SANDBOX/proxy.lock.json" --dest "$SANDBOX/bin/pinned-proxy" --platform linux-amd64 >"$OUT" 2>&1
expect_exit "C2.11 pinned fixture installs" "$?" 0
python3 "$GW/install-proxy.py" --lock "$SANDBOX/proxy.lock.json" --dest "$SANDBOX/bin/pinned-proxy" --platform linux-amd64 --check >"$OUT" 2>&1
expect_exit "C2.12 provenance marker verifies" "$?" 0
printf '\n# tamper\n' >> "$SANDBOX/bin/pinned-proxy"
python3 "$GW/install-proxy.py" --lock "$SANDBOX/proxy.lock.json" --dest "$SANDBOX/bin/pinned-proxy" --platform linux-amd64 --check >"$OUT" 2>&1
expect_exit "C2.13 binary tamper is detected" "$?" 1
python3 - "$SANDBOX/proxy.lock.json" <<'PYEOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); value = json.loads(p.read_text())
value["assets"]["linux-amd64"]["sha256"] = "0" * 64
p.write_text(json.dumps(value))
PYEOF
rm -f "$SANDBOX/bin/pinned-proxy" "$SANDBOX/bin/pinned-proxy.install.json"
python3 "$GW/install-proxy.py" --lock "$SANDBOX/proxy.lock.json" --dest "$SANDBOX/bin/pinned-proxy" --platform linux-amd64 >"$OUT" 2>&1
expect_exit "C2.14 checksum mismatch blocks install" "$?" 1
[[ ! -e "$SANDBOX/bin/pinned-proxy" ]] && ok "C2.15 failed verification leaves no binary" || bad "C2.15 failed verification leaves no binary" "destination exists"

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
cp "$GW/mcp-gateway.sh" "$GW/smoke.py" "$GW/update-pins.py" "$GW/config-sync.py" "$SANDBOX/gwcopy/"
printf 'MCP_GATEWAY_PORT=9090\n' > "$SANDBOX/gwcopy/runtime.env"
# G0: no config.json -> every command fails with the copy-example hint
HOME="$SANDBOX/home" bash "$SANDBOX/gwcopy/mcp-gateway.sh" doctor >"$OUT" 2>&1
expect_exit "G0 missing config -> exit 1" "$?" 1
expect_grep "G0b copy-example hint" "$OUT" "config.json.example"
cat > "$SANDBOX/gwcopy/tools.json" <<'EOF'
{"plain-pkg": {"version": "1.0.0", "with": [], "python": null, "entrypoint": "plain-pkg"}}
EOF
cat > "$SANDBOX/gwcopy/config.json" <<EOF
{"mcpProxy": {"baseURL": "http://127.0.0.1:9090", "addr": "127.0.0.1:9090"}, "mcpServers": {
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
mkdir -p "$SANDBOX/codex-repo/.codex/profiles" "$SANDBOX/codex-repo/.mcp-profiles" "$SANDBOX/codex-repo/.claude-plugin" "$SANDBOX/codex-home"
cat > "$SANDBOX/codex-repo/.claude-plugin/plugin.json" <<'EOF'
{"name": "llm-obsidian", "version": "1.0.0", "description": "test"}
EOF
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
cat > "$SANDBOX/codex-repo/.codex/profiles/deep.toml" <<'EOF'
model_reasoning_effort = "max"
approval_policy = "on-request"
EOF
cat > "$SANDBOX/codex-home/config.toml" <<'EOF'
model = "gpt-5.5"

[tui]
status_line = ["project-name", "five-hour-limit"]

[mcp_servers.node_repl]
command = "/x/node_repl"

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]

[hooks.state]

[hooks.state."global-hook:stop:0"]
trusted_hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EOF
cat > "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" <<'EOF'
model = "gpt-5.6-sol"
model_reasoning_effort = "high"

# BEGIN LLM-OBSIDIAN CODEX MCP (managed by scripts/mcp-gateway/codex-sync.py)
# Source of truth: .mcp.json or .mcp.json.example plus .mcp-profiles/*.json.
# Secrets stay in ~/.config/mcp-gateway/secrets.env behind the gateway.

[mcp_servers.context7]
command = "npx"

# Simulate Codex persisting user-owned tables before the trailing marker.
[projects."/tmp/codex-repo"]
trust_level = "trusted"

[hooks.state]

[tui]
status_line = ["project-name", "five-hour-limit", "weekly-limit"]
status_line_use_colors = true

# END LLM-OBSIDIAN CODEX MCP
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
expect_grep "H9a global keeps statusline" "$SANDBOX/codex-home/config.toml" 'status_line = ["project-name", "five-hour-limit"]'
expect_grep "H10 default profile has context7" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" "context7/mcp"
expect_grep "H10a profile keeps model" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" 'model = "gpt-5.6-sol"'
expect_grep "H10b profile keeps statusline" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" 'status_line = ["project-name", "five-hour-limit", "weekly-limit"]'
expect_grep "H10c profile keeps hooks" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" '[hooks.state]'
expect_grep "H10c2 profile inherits trusted global hook" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" '[hooks.state."global-hook:stop:0"]'
marker_count=$(grep -c '^# BEGIN LLM-OBSIDIAN CODEX MCP' "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml")
[[ "$marker_count" == 1 ]] && ok "H10d profile has one managed block" || bad "H10d profile has one managed block" "got $marker_count"
expect_grep "H11 extra profile has paper-search" "$SANDBOX/codex-home/llm-obsidian-research.config.toml" "paper-search/mcp"
expect_grep "H11a runtime profile copied" "$SANDBOX/codex-home/llm-obsidian-deep.config.toml" 'model_reasoning_effort = "max"'
printf '\n# unrelated-user-drift\n' >> "$SANDBOX/codex-home/llm-obsidian-deep.config.toml"
python3 - "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" <<'PYEOF'
import re, sys
p=sys.argv[1]
t=open(p).read()
t=re.sub(r'\n\[hooks\.state\."global-hook:stop:0"\]\ntrusted_hash = "sha256:a{64}"\n', '\n', t)
open(p, 'w').write(t)
PYEOF
python3 "$GW/codex-sync.py" --repo-root "$SANDBOX/codex-repo" --codex-home "$SANDBOX/codex-home" \
  --apply --only-profile llm-obsidian-mcp >"$OUT" 2>&1
expect_exit "H11b scoped profile apply" "$?" 0
expect_grep "H11c scoped apply restores inherited hook" "$SANDBOX/codex-home/llm-obsidian-mcp.config.toml" '[hooks.state."global-hook:stop:0"]'
expect_grep "H11d scoped apply preserves unrelated profile" "$SANDBOX/codex-home/llm-obsidian-deep.config.toml" '# unrelated-user-drift'
cp "$SANDBOX/codex-repo/.codex/profiles/deep.toml" "$SANDBOX/codex-home/llm-obsidian-deep.config.toml"
python3 "$GW/codex-sync.py" --repo-root "$SANDBOX/codex-repo" --codex-home "$SANDBOX/codex-home" --check >"$OUT" 2>&1
expect_exit "H12 second --check clean" "$?" 0
expect_grep "H13 no changes message" "$OUT" "codex-sync: no changes"

mkdir -p "$SANDBOX/fork-repo/.codex" "$SANDBOX/fork-repo/.mcp-profiles" "$SANDBOX/fork-repo/.claude-plugin" "$SANDBOX/fork-home"
cp "$SANDBOX/codex-repo/.mcp.json.example" "$SANDBOX/fork-repo/.mcp.json.example"
cp "$SANDBOX/codex-repo/.mcp-profiles/research.json" "$SANDBOX/fork-repo/.mcp-profiles/research.json"
cat > "$SANDBOX/fork-repo/.claude-plugin/plugin.json" <<'EOF'
{"name": "llm-obsidian-custom", "version": "1.0.0", "description": "test fork"}
EOF
python3 "$GW/codex-sync.py" --repo-root "$SANDBOX/fork-repo" --codex-home "$SANDBOX/fork-home" --apply >"$OUT" 2>&1
expect_exit "H14 fork apply exits 0" "$?" 0
expect_grep "H15 fork default profile name" "$SANDBOX/fork-home/llm-obsidian-custom-mcp.config.toml" "context7/mcp"
expect_grep "H16 fork extra profile name" "$SANDBOX/fork-home/llm-obsidian-custom-research.config.toml" "paper-search/mcp"

# ---------- I. clean-machine dry run ----------
echo "I. clean-machine dry run"
mkdir -p "$SANDBOX/check-home"
HOME="$SANDBOX/check-home" MCP_PROXY_BIN="$SANDBOX/check-home/bin/mcp-proxy" \
  bash "$REPO_ROOT/bin/setup-clean-machine.sh" --check --skip-vault >"$OUT" 2>&1
expect_exit "I1 dry run exits 0" "$?" 0
expect_grep "I2 exact proxy version shown" "$OUT" "mcp-proxy 0.43.2"
expect_nogrep "I3 no latest-release lookup" "$OUT" "releases/latest"
[[ ! -e "$SANDBOX/check-home/bin/mcp-proxy" ]] && ok "I4 dry run writes no proxy" || bad "I4 dry run writes no proxy" "binary exists"

mkdir -p "$SANDBOX/bash32-home"
HOME="$SANDBOX/bash32-home" MCP_PROXY_BIN="$SANDBOX/bash32-home/bin/mcp-proxy" \
  /bin/bash "$REPO_ROOT/bin/setup-clean-machine.sh" --check --skip-proxy >"$OUT" 2>&1
expect_exit "I5 stock macOS bash flag-less vault path" "$?" 0
expect_grep "I6 stock macOS bash includes vault setup" "$OUT" "bin/setup-vault.sh"

# ---------- summary ----------
echo
echo "=== $pass passed, $fail failed ==="
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
