#!/usr/bin/env bash
# Regression suite for the MCP gateway management layer:
#   scripts/mcp-gateway/mcp-gateway.sh  — CLI, doctor pre-flight
#   scripts/mcp-gateway/smoke.py        — handshake, --routes, --init-rabbit
#   scripts/mcp-gateway/update-pins.py  — npx spec parsing, tools.json model, --sync
#
# Fully offline and deterministic: fake MCP HTTP server on localhost instead
# of the live gateway, a stub `uv` binary instead of the real one, live repo
# configs touched READ-ONLY. Sandbox in mktemp; the live gateway is never touched.
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
  grep -qF -- "$3" "$2" && bad "$1" "unexpected pattern present: $3" || ok "$1"
}

OUT="$SANDBOX/out.txt"

# ---------- A. синтаксис ----------
echo "A. syntax"
bash -n "$GW/mcp-gateway.sh" 2>"$OUT"; expect_exit "A1 mcp-gateway.sh bash -n" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/smoke.py').read())" 2>"$OUT"; expect_exit "A2 smoke.py parses" "$?" 0
python3 -c "import ast; ast.parse(open('$GW/update-pins.py').read())" 2>"$OUT"; expect_exit "A3 update-pins.py parses" "$?" 0

# ---------- B. update-pins: чистые функции ----------
echo "B. update-pins pure functions"
python3 - "$GW" >"$OUT" 2>&1 <<'PYEOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("update_pins", f"{sys.argv[1]}/update-pins.py")
up = importlib.util.module_from_spec(spec); spec.loader.exec_module(up)
assert up.split_spec("pkg@1.2.3") == ("pkg", "1.2.3")
assert up.split_spec("@zereight/mcp-gitlab@2.1.29") == ("@zereight/mcp-gitlab", "2.1.29")
assert up.split_spec("@zereight/mcp-gitlab") == ("@zereight/mcp-gitlab", None)
assert up.split_spec("pkg") == ("pkg", None)
assert up.find_npx_spec_index(["-y", "argocd-mcp@0.8.0", "stdio"]) == 1
assert up.find_npx_spec_index(["pkg"]) == 0
assert up.find_npx_spec_index(["-p", "helper", "pkg@1.0"]) == 2
assert up.find_npx_spec_index(["-y"]) is None
print("PURE_OK")
PYEOF
expect_grep "B1 split_spec + find_npx_spec_index" "$OUT" "PURE_OK"

# ---------- C. инварианты живых конфигов (read-only) ----------
echo "C. repo config invariants"
python3 - "$GW" >"$OUT" 2>&1 <<'PYEOF'
import json, os, re, sys
gw = sys.argv[1]
cfg = json.load(open(f"{gw}/config.json"))
tools = {k: v for k, v in json.load(open(f"{gw}/tools.json")).items() if not k.startswith("_")}
entrypoints = {m["entrypoint"] for m in tools.values()}
allowed_local = entrypoints | {"mcp-victoriametrics"}  # Go-бинарь вне tools.json
for name, s in cfg["mcpServers"].items():
    assert ("url" in s) != ("command" in s), f"{name}: ровно одно из url/command"
    cmd = s.get("command", "")
    assert cmd != "uvx", f"{name}: uvx-обёрток больше нет"
    if cmd.startswith("/") and "/.local/bin/" in cmd:
        assert os.path.basename(cmd) in allowed_local, f"{name}: бинарь {cmd} не из tools.json"
for pkg, m in tools.items():
    assert re.fullmatch(r"\d+[\w.]*", m["version"]), f"{pkg}: кривая версия {m['version']}"
    assert m["entrypoint"], f"{pkg}: пустой entrypoint"
    used = any(os.path.basename(s.get("command", "")) == m["entrypoint"] for s in cfg["mcpServers"].values())
    assert used, f"{pkg}: entrypoint {m['entrypoint']} не используется ни одним ребёнком"
print("INVARIANTS_OK")
PYEOF
expect_grep "C1 config/tools.json consistency" "$OUT" "INVARIANTS_OK"

# ---------- D. update-pins --sync со стабом uv ----------
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

# ---------- E. update-pins: фильтр по имени ----------
echo "E. update-pins name filter"
python3 "$GW/update-pins.py" "$SANDBOX/gwdir/config.json" --check no-such-pkg >"$OUT" 2>&1
expect_exit "E1 unknown name -> non-zero" "$?" 1
expect_grep "E2 unknown name message" "$OUT" "unknown name(s): no-such-pkg"

# ---------- F. smoke.py против фейкового MCP-сервера ----------
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
expect_exit "F1 --routes exit 1 при DOWN" "$?" 1
expect_grep "F2 routes summary" "$OUT" "routes: 1/2 active, 1 DOWN"
expect_grep "F3 DOWN marker" "$OUT" "!bad"
cat > "$SANDBOX/gwdir/routes-ok.json" <<'EOF'
{"mcpServers": {"good": {"command": "/x/good", "args": []}}}
EOF
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes-ok.json" "$PORT" --routes >"$OUT" 2>&1
expect_exit "F4 --routes exit 0 когда все живы" "$?" 0
expect_grep "F5 all-active summary" "$OUT" "routes: 1/1 active"
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes.json" "$PORT" good,bad >"$OUT" 2>&1
expect_exit "F6 handshake exit 1 (bad падает)" "$?" 1
expect_grep "F7 handshake OK line" "$OUT" "OK   good: 2 tools"
expect_grep "F8 handshake FAIL line" "$OUT" "FAIL bad"
python3 "$GW/smoke.py" "$SANDBOX/gwdir/routes.json" "$PORT" nonexistent >"$OUT" 2>&1
expect_exit "F9 unknown server -> non-zero" "$?" 1
expect_grep "F10 unknown server message" "$OUT" "unknown server: nonexistent"

# ---------- G. smoke.py --init-rabbit ----------
echo "G. smoke.py --init-rabbit"
cat > "$SANDBOX/gwdir/rabbit.json" <<'EOF'
{"mcpServers": {"rabbitmq": {"command": "/x/amq", "args": []}, "other": {"command": "/x/o", "args": []}}}
EOF
env -u RABBITMQ_MCP_TEST_HOST python3 "$GW/smoke.py" "$SANDBOX/gwdir/rabbit.json" "$PORT" --init-rabbit >"$OUT" 2>&1
expect_exit "G1 без кредов -> no-op exit 0" "$?" 0
expect_grep "G2 skip message" "$OUT" "не заданы в secrets.env — пропускаю"
RABBITMQ_MCP_TEST_HOST=h.example RABBITMQ_MCP_TEST_USER=u \
  python3 "$GW/smoke.py" "$SANDBOX/gwdir/rabbit.json" "$PORT" --init-rabbit >"$OUT" 2>&1
expect_exit "G3 HOST без PW -> no-op exit 0" "$?" 0
expect_grep "G4 warn про неполные креды" "$OUT" "нет _USER/_PW — пропускаю"
RABBITMQ_MCP_TEST_HOST=h.example RABBITMQ_MCP_TEST_USER=u RABBITMQ_MCP_TEST_PW=p RABBITMQ_MCP_DEFAULT=TEST \
  python3 "$GW/smoke.py" "$SANDBOX/gwdir/rabbit.json" "$PORT" --init-rabbit >"$OUT" 2>&1
expect_exit "G5 init против фейка exit 0" "$?" 0
expect_grep "G6 init OK line" "$OUT" "init-rabbit: OK rabbitmq -> h.example"

# ---------- H. doctor в песочнице ----------
echo "H. doctor sandbox"
mkdir -p "$SANDBOX/home" "$SANDBOX/gwcopy"
cp "$GW/mcp-gateway.sh" "$GW/smoke.py" "$GW/update-pins.py" "$SANDBOX/gwcopy/"
cat > "$SANDBOX/gwcopy/tools.json" <<'EOF'
{"plain-pkg": {"version": "1.0.0", "with": [], "python": null, "entrypoint": "plain-pkg"}}
EOF
cat > "$SANDBOX/gwcopy/config.json" <<'EOF'
{"mcpServers": {
  "plain": {"command": "/x/.local/bin/plain-pkg", "args": [], "env": {"AWS_PROFILE": "test-prof", "TOKEN": "${TEST_TOKEN}"}}
}}
EOF
printf 'clusters:\n  t:\n    opensearch_password: "${TEST_OS_PW}"\n' > "$SANDBOX/gwcopy/clusters.yml.tpl"
cat > "$SANDBOX/bin/uv" <<EOF
#!/usr/bin/env bash
[ "\$1 \$2" = "tool list" ] && cat "$SANDBOX/uv-list.txt" 2>/dev/null
exit 0
EOF
: > "$SANDBOX/uv-list.txt"
HOME="$SANDBOX/home" PATH="$SANDBOX/bin:$PATH" bash "$SANDBOX/gwcopy/mcp-gateway.sh" doctor >"$OUT" 2>&1
expect_exit "H1 голая машина -> exit 1" "$?" 1
expect_grep "H2 missing binary" "$OUT" "FAIL binary mcp-proxy"
expect_grep "H3 missing uv tool" "$OUT" "FAIL uv tool plain-pkg==1.0.0"
expect_grep "H4 missing secrets" "$OUT" "FAIL secrets file"
expect_grep "H5 missing aws profile" "$OUT" "FAIL aws profile test-prof"
expect_grep "H6 missing plist" "$OUT" "FAIL launchd plist games.whalekit.mcp-gateway"
# теперь укомплектованная машина
mkdir -p "$SANDBOX/home/.local/bin" "$SANDBOX/home/.config/mcp-gateway" "$SANDBOX/home/.aws" "$SANDBOX/home/Library/LaunchAgents"
for b in mcp-proxy mcp-grafana mcp-victoriametrics; do
  printf '#!/bin/sh\n' > "$SANDBOX/home/.local/bin/$b"; chmod +x "$SANDBOX/home/.local/bin/$b"
done
echo "plain-pkg v1.0.0" > "$SANDBOX/uv-list.txt"
printf 'TEST_TOKEN=x\nTEST_OS_PW=y\nRABBITMQ_MCP_T_HOST=h\n' > "$SANDBOX/home/.config/mcp-gateway/secrets.env"
printf '[profile test-prof]\nregion = us-east-1\n' > "$SANDBOX/home/.aws/config"
touch "$SANDBOX/home/Library/LaunchAgents/games.whalekit.mcp-gateway.plist" \
      "$SANDBOX/home/Library/LaunchAgents/games.whalekit.mcp-grafana.plist"
HOME="$SANDBOX/home" PATH="$SANDBOX/bin:$PATH" bash "$SANDBOX/gwcopy/mcp-gateway.sh" doctor >"$OUT" 2>&1
expect_exit "H7 укомплектованная -> exit 0" "$?" 0
expect_grep "H8 all OK footer" "$OUT" "--- all OK"

# ---------- summary ----------
echo
echo "=== $pass passed, $fail failed ==="
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
