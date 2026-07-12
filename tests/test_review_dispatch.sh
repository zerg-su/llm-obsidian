#!/usr/bin/env bash
# Regression tests for review-dispatch deterministic mode plumbing.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/skills/review-dispatch/scripts/spawn_review.py"
SEND_SCRIPT="$REPO_ROOT/skills/review-send/scripts/send_review.py"
SUPERVISOR="$REPO_ROOT/scripts/cmux_agent_supervisor.py"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/review-dispatch-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0
fail=0
failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s - %s\n' "$1" "$2"; }

expect_eq() {
  local name="$1" got="$2" want="$3"
  [[ "$got" == "$want" ]] && ok "$name" || bad "$name" "got $got (want $want)"
}

write_fixture() {
  local dir="$1"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" config user.email test@example.com
  git -C "$dir" config user.name test
  printf 'base\n' > "$dir/file.txt"
  git -C "$dir" add file.txt
  git -C "$dir" commit -qm init
  printf 'changed\n' > "$dir/file.txt"
  cat > "$dir/.task-meta.json" <<'JSON'
{"task_name":"review-dispatch-test","base_branch":"HEAD","branch":"task/review-dispatch-test","executor_runtime":"codex","model":"gpt-5.6-sol"}
JSON
  printf '# Task: review-dispatch-test\n' > "$dir/.task-prompt.md"
  printf '00000000-0000-0000-0000-000000000001\n' > "$dir/.task-cmux-surface"
}

json_get() {
  local file="$1" expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get(sys.argv[2], ""))
PY
}

path_resolve() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).resolve())
PY
}

argv_has() {
  local file="$1"
  shift
  python3 - "$file" "$@" <<'PY'
import json
import sys

argv = json.load(open(sys.argv[1], encoding="utf-8"))["argv"]
needle = sys.argv[2:]
raise SystemExit(0 if any(argv[i:i + len(needle)] == needle for i in range(len(argv) + 1)) else 1)
PY
}

echo "== review-dispatch mode plumbing =="

LIGHT="$SANDBOX/light"
write_fixture "$LIGHT"
"$SCRIPT" start --light --no-spawn --worktree "$LIGHT" --vault-root "$REPO_ROOT" >"$SANDBOX/light.out" 2>"$SANDBOX/light.err"
expect_eq "start-light-exit" "$?" 0
expect_eq "start-light-meta" "$(json_get "$LIGHT/.review-meta.json" review_mode)" "light"
grep -q 'Review mode: `light`' "$LIGHT/.review-prompt.md" && ok "start-light-prompt" || bad "start-light-prompt" "missing light marker"
grep -q 'top 5 actionable findings' "$LIGHT/.review-prompt.md" && ok "start-light-instructions" || bad "start-light-instructions" "missing light instructions"
LIGHT_SPEC="$LIGHT/.review-agent-command.json"
argv_has "$LIGHT_SPEC" --permission-mode dontAsk && ok "claude-unattended-mode" || bad "claude-unattended-mode" "dontAsk mode missing"
argv_has "$LIGHT_SPEC" --tools Read,Glob,Grep,Write,Bash && ok "claude-tool-surface" || bad "claude-tool-surface" "restricted tools missing"
grep -q -- "Bash(python3 \*send_review.py submit \*)" "$LIGHT_SPEC" && ok "claude-callback-allowed" || bad "claude-callback-allowed" "typed callback allow rule missing"
grep -qF -- 'Bash(python3 tests/test_*.py)' "$LIGHT_SPEC" && ok "claude-python-tests-allowed" || bad "claude-python-tests-allowed" "bounded Python test rule missing"
grep -qF -- 'Bash(bash tests/test_*.sh)' "$LIGHT_SPEC" && ok "claude-shell-tests-allowed" || bad "claude-shell-tests-allowed" "bounded shell test rule missing"
grep -qF -- '`python3 tests/test_document_normalize.py 2>&1 | tail -50`' "$LIGHT/.review-prompt.md" && ok "claude-test-shell-composition-denied" || bad "claude-test-shell-composition-denied" "prompt does not explain bounded test form"
grep -qF -- 'Edit(./.review-outbox.json)' "$LIGHT_SPEC" && ok "claude-outbox-only-write" || bad "claude-outbox-only-write" "cwd-anchored outbox Edit rule missing"
grep -q -- '--input-file.*/.review-outbox.json' "$LIGHT/.review-prompt.md" && ok "claude-outbox-transport" || bad "claude-outbox-transport" "outbox callback missing"
grep -qF -- 'Do not prefix them with `git -C`' "$LIGHT/.review-prompt.md" && ok "claude-cwd-git-prompt" || bad "claude-cwd-git-prompt" "cwd-relative git guidance missing"
if grep -qF -- "git -C $LIGHT ..." "$LIGHT/.review-prompt.md"; then bad "claude-no-git-c-escape" "Claude prompt requests a denied git -C command"; else ok "claude-no-git-c-escape"; fi
grep -q -- 'cmux_agent_supervisor.py run' "$SANDBOX/light.out" && ok "review-supervisor-wrapper" || bad "review-supervisor-wrapper" "short supervisor command missing"
grep -q -- '--kind reviewer' "$SANDBOX/light.out" && ok "review-watchdog-kind" || bad "review-watchdog-kind" "reviewer routing missing"
"$SUPERVISOR" validate --worktree "$LIGHT" --kind reviewer --surface 00000000-0000-0000-0000-000000000000 >/dev/null 2>"$SANDBOX/supervisor.err"
expect_eq "review-supervisor-valid" "$?" 0
python3 - "$LIGHT_SPEC" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["argv"][data["argv"].index("dontAsk")] = "auto"
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
"$SUPERVISOR" validate --worktree "$LIGHT" --kind reviewer --surface 00000000-0000-0000-0000-000000000000 >/dev/null 2>"$SANDBOX/supervisor-tamper.err"
[[ $? -ne 0 ]] && ok "review-supervisor-rejects-tamper" || bad "review-supervisor-rejects-tamper" "writable reviewer spec accepted"
python3 - "$LIGHT_SPEC" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["argv"][data["argv"].index("auto")] = "dontAsk"
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
[[ $(wc -c < "$SANDBOX/light.out") -lt 1000 ]] && ok "review-command-bounded" || bad "review-command-bounded" "cmux command is unexpectedly long"
argv_has "$LIGHT_SPEC" --effort medium && ok "claude-medium-effort" || bad "claude-medium-effort" "medium effort missing"
argv_has "$LIGHT_SPEC" --model opus && ok "claude-opus-default" || bad "claude-opus-default" "default Claude reviewer is not opus"
expect_eq "claude-opus-meta" "$(json_get "$LIGHT/.review-meta.json" reviewer_model)" "opus"
if argv_has "$LIGHT_SPEC" --permission-mode plan; then bad "claude-no-plan" "plan mode present"; else ok "claude-no-plan"; fi
if argv_has "$LIGHT_SPEC" --permission-mode auto; then bad "claude-no-auto" "auto mode present"; else ok "claude-no-auto"; fi
if grep -q -- 'bash -lc\|WATCHDOG_PID\|REVIEW_RC=' "$SANDBOX/light.out"; then bad "review-shell-portable" "shell wrapper leaked into cmux command"; else ok "review-shell-portable"; fi
grep -q '"schema_version": 1' "$LIGHT/.review-prompt.md" && ok "typed-review-prompt" || bad "typed-review-prompt" "JSON contract missing"

LINK_ROOT="$SANDBOX/linked-root"
LINK_WORKTREE="$SANDBOX/linked-worktree"
mkdir -p "$LINK_ROOT"
git -C "$LINK_ROOT" init -q
git -C "$LINK_ROOT" config user.email test@example.com
git -C "$LINK_ROOT" config user.name test
printf 'base\n' > "$LINK_ROOT/file.txt"
git -C "$LINK_ROOT" add file.txt
git -C "$LINK_ROOT" commit -qm init
git -C "$LINK_ROOT" worktree add -q -b task/linked "$LINK_WORKTREE"
cat > "$LINK_WORKTREE/.task-meta.json" <<'JSON'
{"task_name":"linked-review-test","base_branch":"HEAD","branch":"task/linked","executor_runtime":"codex","model":"gpt-5.6-sol"}
JSON
printf '# Task: linked-review-test\n' > "$LINK_WORKTREE/.task-prompt.md"
printf '00000000-0000-0000-0000-000000000001\n' > "$LINK_WORKTREE/.task-cmux-surface"
"$SCRIPT" start --no-spawn --worktree "$LINK_WORKTREE" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/linked.err"
printf '{}\n' > "$LINK_WORKTREE/.review-relay.json"
printf '{}\n' > "$LINK_WORKTREE/.task-reap-prepared.json"
git -C "$LINK_WORKTREE" check-ignore -q .review-relay.json
relay_ignored=$?
git -C "$LINK_WORKTREE" check-ignore -q .task-reap-prepared.json
prepared_ignored=$?
[[ $relay_ignored -eq 0 && $prepared_ignored -eq 0 ]] && ok "linked-worktree-relay-ignored" || bad "linked-worktree-relay-ignored" "common exclude not updated"

run_id="$(json_get "$LIGHT/.review-meta.json" run_id)"
review_payload=$(python3 - "$run_id" <<'PY'
import json, sys
print(json.dumps({
  "schema_version": 1, "run_id": sys.argv[1], "mode": "light", "verdict": "approve",
  "findings": [], "verification_gaps": ["live model eval not run"],
  "notes_for_executor": [], "residual_risks": []
}))
PY
)
printf '%s' "$review_payload" > "$LIGHT/.review-outbox.json"
printf '{}\n' > "$LIGHT/.review-watchdog.json"
outbox_callback=$("$SEND_SCRIPT" submit --no-send --worktree "$LIGHT" --input-file "$LIGHT/.review-outbox.json" 2>"$SANDBOX/outbox.err")
expect_eq "typed-outbox-submit" "$?" 0
[[ ! -e "$LIGHT/.review-outbox.json" ]] && ok "typed-outbox-removed" || bad "typed-outbox-removed" "outbox remains"
[[ "$outbox_callback" == *"--payload-b64 "* ]] && ok "typed-outbox-callback" || bad "typed-outbox-callback" "callback missing"
[[ "$(grep -o -- '--payload-b64' <<<"$outbox_callback" | wc -l | tr -d ' ')" == 1 ]] && ok "typed-callback-single-payload-flag" || bad "typed-callback-single-payload-flag" "payload flag duplicated"
[[ "$outbox_callback" == "Cross-model review callback"* ]] && ok "runtime-neutral-callback" || bad "runtime-neutral-callback" "callback still depends on a slash command"
callback=$(printf '%s' "$review_payload" | "$SEND_SCRIPT" submit --no-send --worktree "$LIGHT" 2>"$SANDBOX/submit.err")
expect_eq "typed-submit-exit" "$?" 0
token="${callback##*--payload-b64 }"
"$SCRIPT" receive --worktree "$LIGHT" --payload-b64 "$token" >/dev/null 2>"$SANDBOX/receive.err"
expect_eq "typed-receive-exit" "$?" 0
expect_eq "typed-json-verdict" "$(json_get "$LIGHT/.task-review.json" verdict)" "approve"
grep -q '^Verdict: approve$' "$LIGHT/.task-review.md" && ok "typed-markdown-render" || bad "typed-markdown-render" "derived markdown missing"
python3 - "$SEND_SCRIPT" <<'PY'
import importlib.util
import subprocess
import sys

spec = importlib.util.spec_from_file_location("review_send_delay_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
events = []

def fake_run(args, cwd=None):
    events.append(("run", args))
    return subprocess.CompletedProcess(args, 0, "", "")

module.run = fake_run
module.time.sleep = lambda seconds: events.append(("sleep", seconds))
module.send_to_surface("surface:1", "callback")
assert events[0][1][1] == "send"
assert events[1] == ("sleep", module.CMUX_PASTE_SETTLE_SECONDS)
assert events[2][1][1] == "send-key" and events[2][1][-1] == "Enter"
assert module.CMUX_PASTE_SETTLE_SECONDS >= 0.1
PY
expect_eq "callback-paste-settle" "$?" 0
python3 - "$SCRIPT" <<'PY'
import importlib.util
import subprocess
import sys

spec = importlib.util.spec_from_file_location("review_dispatch_delay_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
events = []

def fake_run(args, cwd=None):
    events.append(("run", args))
    return subprocess.CompletedProcess(args, 0, "", "")

module.run = fake_run
module.time.sleep = lambda seconds: events.append(("sleep", seconds))
module.send_to_surface("surface:1", "verify callback")
assert events[0][1][1] == "send"
assert events[1] == ("sleep", module.CMUX_PASTE_SETTLE_SECONDS)
assert events[2][1][1] == "send-key" and events[2][1][-1] == "Enter"
PY
expect_eq "review-dispatch-paste-settle" "$?" 0

cat > "$LIGHT/.task-review.md" <<'MD'
# Cross-Model Review: review-dispatch-test

Verdict: approve

## Findings

Findings: none
MD
cat > "$LIGHT/.task-review-resolution.md" <<'MD'
# Review Resolution

No findings applied.
MD
"$SCRIPT" verify --no-send --worktree "$LIGHT" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/verify.err"
expect_eq "verify-light-exit" "$?" 0
expect_eq "verify-light-preserved" "$(json_get "$LIGHT/.review-meta.json" review_mode)" "light"
expect_eq "verify-file-reference" "$(json_get "$LIGHT/.review-meta.json" send_mode)" "file-reference"
grep -q 'Review mode: `light`' "$LIGHT/.review-prompt-verify.md" && ok "verify-light-prompt" || bad "verify-light-prompt" "missing light marker"

FULL="$SANDBOX/full"
write_fixture "$FULL"
"$SCRIPT" start --no-spawn --worktree "$FULL" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/full.err"
expect_eq "start-full-exit" "$?" 0
expect_eq "start-full-default" "$(json_get "$FULL/.review-meta.json" review_mode)" "full"
grep -q 'Review mode: `full`' "$FULL/.review-prompt.md" && ok "start-full-prompt" || bad "start-full-prompt" "missing full marker"

FABLE="$SANDBOX/fable"
write_fixture "$FABLE"
"$SCRIPT" start --no-spawn --model fable --worktree "$FABLE" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/fable.err"
expect_eq "fable-opt-in-start" "$?" 0
argv_has "$FABLE/.review-agent-command.json" --model fable && ok "fable-opt-in-argv" || bad "fable-opt-in-argv" "explicit Fable model was not preserved"
expect_eq "fable-opt-in-meta" "$(json_get "$FABLE/.review-meta.json" reviewer_model)" "fable"

CODEX_REVIEW="$SANDBOX/codex-review"
write_fixture "$CODEX_REVIEW"
python3 - "$CODEX_REVIEW/.task-meta.json" <<'PY'
import json, sys
p=sys.argv[1]; data=json.load(open(p)); data["executor_runtime"]="claude"
open(p,"w").write(json.dumps(data)+"\n")
PY
"$SCRIPT" start --no-spawn --worktree "$CODEX_REVIEW" --vault-root "$REPO_ROOT" >"$SANDBOX/codex.out" 2>"$SANDBOX/codex.err"
expect_eq "codex-review-start" "$?" 0
CODEX_SPEC="$CODEX_REVIEW/.review-agent-command.json"
CODEX_RUNTIME="$(json_get "$CODEX_REVIEW/.review-meta.json" review_runtime_dir)"
CODEX_REVIEW_RESOLVED="$(path_resolve "$CODEX_REVIEW")"
argv_has "$CODEX_SPEC" -s workspace-write && ok "codex-scratch-write-mode" || bad "codex-scratch-write-mode" "workspace-write missing"
argv_has "$CODEX_SPEC" --model gpt-5.6-sol && ok "codex-sol-default" || bad "codex-sol-default" "default Codex reviewer is not gpt-5.6-sol"
expect_eq "codex-sol-meta" "$(json_get "$CODEX_REVIEW/.review-meta.json" reviewer_model)" "gpt-5.6-sol"
argv_has "$CODEX_SPEC" --disable hooks && ok "codex-review-hooks-disabled" || bad "codex-review-hooks-disabled" "hooks remain enabled"
argv_has "$CODEX_SPEC" --cd "$CODEX_RUNTIME" && ok "codex-scratch-cwd" || bad "codex-scratch-cwd" "isolated cwd missing"
[[ -d "$CODEX_RUNTIME" && "$CODEX_RUNTIME" != "$CODEX_REVIEW"/* ]] && ok "codex-scratch-outside-product" || bad "codex-scratch-outside-product" "runtime overlaps product"
python3 - "$CODEX_SPEC" "$CODEX_RUNTIME" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if data["env"].get("TMPDIR") == sys.argv[2] else 1)
PY
[[ $? -eq 0 ]] && ok "codex-scratch-env" || bad "codex-scratch-env" "TMPDIR is not pinned to scratch"
if grep -q -- '--add-dir' "$CODEX_SPEC"; then bad "codex-no-additional-write-root" "reviewer requests --add-dir"; else ok "codex-no-additional-write-root"; fi
grep -q 'web_search=' "$CODEX_SPEC" && ok "codex-network-disabled" || bad "codex-network-disabled" "web search override missing"
grep -qF -- "$CODEX_RUNTIME/.review-outbox.json" "$CODEX_REVIEW/.review-prompt.md" && ok "codex-relay-outbox-prompt" || bad "codex-relay-outbox-prompt" "relay outbox missing"
grep -q 'Do not run `review-send`' "$CODEX_REVIEW/.review-prompt.md" && ok "codex-no-socket-prompt" || bad "codex-no-socket-prompt" "socket boundary missing"
grep -qF -- "git -C $CODEX_REVIEW_RESOLVED ..." "$CODEX_REVIEW/.review-prompt.md" && ok "codex-worktree-git-prompt" || bad "codex-worktree-git-prompt" "scratch reviewer lacks worktree git guidance"
"$SUPERVISOR" validate --worktree "$CODEX_REVIEW" --kind reviewer --surface 00000000-0000-0000-0000-000000000000 >/dev/null 2>"$SANDBOX/codex-supervisor.err"
expect_eq "codex-review-supervisor-valid" "$?" 0
python3 - "$CODEX_SPEC" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["argv"].extend(["--add-dir", "/tmp"])
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
"$SUPERVISOR" validate --worktree "$CODEX_REVIEW" --kind reviewer --surface 00000000-0000-0000-0000-000000000000 >/dev/null 2>"$SANDBOX/codex-add-dir.err"
[[ $? -ne 0 ]] && ok "codex-review-rejects-add-dir" || bad "codex-review-rejects-add-dir" "additional writable root accepted"
python3 - "$CODEX_SPEC" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
index = data["argv"].index("--add-dir")
del data["argv"][index:index + 2]
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
ARBITRARY_RUNTIME="$SANDBOX/arbitrary-owner-only"
mkdir -m 700 "$ARBITRARY_RUNTIME"
python3 - "$CODEX_SPEC" "$CODEX_REVIEW/.review-meta.json" "$ARBITRARY_RUNTIME" <<'PY'
import json, sys
spec_path, meta_path, runtime = sys.argv[1:]
spec = json.load(open(spec_path, encoding="utf-8"))
meta = json.load(open(meta_path, encoding="utf-8"))
spec["argv"][spec["argv"].index("--cd") + 1] = runtime
spec["env"]["TMPDIR"] = runtime
meta["review_runtime_dir"] = runtime
open(spec_path, "w", encoding="utf-8").write(json.dumps(spec) + "\n")
open(meta_path, "w", encoding="utf-8").write(json.dumps(meta) + "\n")
PY
"$SUPERVISOR" validate --worktree "$CODEX_REVIEW" --kind reviewer --surface 00000000-0000-0000-0000-000000000000 >/dev/null 2>"$SANDBOX/codex-runtime.err"
[[ $? -ne 0 ]] && ok "codex-review-rejects-arbitrary-runtime" || bad "codex-review-rejects-arbitrary-runtime" "arbitrary owner directory accepted"
python3 - "$CODEX_SPEC" "$CODEX_REVIEW/.review-meta.json" "$CODEX_RUNTIME" <<'PY'
import json, sys
spec_path, meta_path, runtime = sys.argv[1:]
spec = json.load(open(spec_path, encoding="utf-8"))
meta = json.load(open(meta_path, encoding="utf-8"))
spec["argv"][spec["argv"].index("--cd") + 1] = runtime
spec["env"]["TMPDIR"] = runtime
meta["review_runtime_dir"] = runtime
open(spec_path, "w", encoding="utf-8").write(json.dumps(spec) + "\n")
open(meta_path, "w", encoding="utf-8").write(json.dumps(meta) + "\n")
PY
python3 - "$SCRIPT" "$CODEX_REVIEW" "$REPO_ROOT" "$SANDBOX" <<'PY'
import importlib.util
import json
import os
import pathlib
import sys

spec = importlib.util.spec_from_file_location("review_dispatch_codex_home_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2])
vault = pathlib.Path(sys.argv[3])
sandbox = pathlib.Path(sys.argv[4])
real_home = sandbox / "real-codex-home"
real_home.mkdir()
linked_home = sandbox / "linked-codex-home"
os.symlink(real_home, linked_home)
module.launch_command(
    worktree, vault, "codex", "gpt-5.6-sol", str(linked_home), "", ".review-prompt.md",
    "00000000-0000-0000-0000-000000000000", "", pathlib.Path(sys.argv[2]).parent,
)
data = json.loads((worktree / ".review-agent-command.json").read_text(encoding="utf-8"))
assert data["env"]["CODEX_HOME"] == str(real_home.resolve())
PY
expect_eq "codex-review-home-resolved" "$?" 0

python3 - "$SUPERVISOR" "$CODEX_REVIEW" "$CODEX_RUNTIME" "$REPO_ROOT" <<'PY'
import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(sys.argv[4]) / "scripts"))
spec = importlib.util.spec_from_file_location("review_relay_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2])
runtime = pathlib.Path(sys.argv[3])
outbox = runtime / module.REVIEW_OUTBOX_FILE
outbox.write_text('{"schema_version":1}\n', encoding="utf-8")
calls = []

def success(args, **kwargs):
    calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 0, "ok", "")

assert module.relay_review_outbox_once(worktree, runtime, success)
assert not outbox.exists()
state = json.loads((worktree / module.REVIEW_RELAY_FILE).read_text(encoding="utf-8"))
assert state["sent_count"] == 1 and state["failure_count"] == 0
assert calls[0][1]["input"] == '{"schema_version":1}\n'

outbox.write_text('{"schema_version":2}\n', encoding="utf-8")
fail_calls = []

def fail(args, **kwargs):
    fail_calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 3, "", "invalid")

assert not module.relay_review_outbox_once(worktree, runtime, fail)
assert not module.relay_review_outbox_once(worktree, runtime, fail)
assert outbox.exists() and len(fail_calls) == 1
state = json.loads((worktree / module.REVIEW_RELAY_FILE).read_text(encoding="utf-8"))
assert state["sent_count"] == 1 and state["failure_count"] == 1
PY
expect_eq "codex-review-relay" "$?" 0

AUTO="$SANDBOX/unattended"
write_fixture "$AUTO"
printf '# approved\n' > "$AUTO/approved-plan.md"
python3 - "$AUTO/.task-meta.json" "$AUTO/approved-plan.md" <<'PY'
import hashlib, json, sys
meta_path, plan_path = sys.argv[1:]
data = json.load(open(meta_path))
data.update({
    "version": 2,
    "origin_session": "origin",
    "plan_file": plan_path,
    "approved_plan_sha256": hashlib.sha256(open(plan_path, "rb").read()).hexdigest(),
    "interaction_policy": "unattended",
    "review_policy": {
        "mode": "light", "max_verify_iterations": 2,
        "auto_resolve_severities": ["warning", "nit"],
        "escalate_severities": ["blocking"],
    },
    "reap_policy": {
        "mode": "final", "auto_file": True,
        "allowed_types": ["session"], "title": "Review result",
    },
    "surface_policy": {"auto_close": True},
    "watchdog_policy": {
        "enabled": True, "poll_seconds": 30,
        "warn_after_seconds": 900, "alert_after_seconds": 1200
    },
    "forbidden_actions": [
        "push", "deploy", "publish", "delete-worktree", "delete-branch", "expand-scope"
    ],
})
open(meta_path, "w").write(json.dumps(data) + "\n")
PY
"$SCRIPT" start --no-spawn --worktree "$AUTO" --vault-root "$REPO_ROOT" >"$SANDBOX/auto.out" 2>"$SANDBOX/auto.err"
expect_eq "unattended-start" "$?" 0
expect_eq "unattended-configured-mode" "$(json_get "$AUTO/.review-meta.json" review_mode)" "light"
"$SCRIPT" finish --no-send --worktree "$AUTO" >"$SANDBOX/auto-finish.out" 2>"$SANDBOX/auto-finish.err"
expect_eq "unattended-finish-dry" "$?" 0
grep -q 'arm close=true' "$SANDBOX/auto-finish.out" && ok "unattended-close-armed" || bad "unattended-close-armed" "close policy not detected"
"$SCRIPT" finish --worktree "$AUTO" >"$SANDBOX/auto-premature.out" 2>"$SANDBOX/auto-premature.err"
expect_eq "unattended-premature-finish" "$?" 1
grep -q 'requires a received approve callback' "$SANDBOX/auto-premature.err" && ok "unattended-finish-gate" || bad "unattended-finish-gate" "approve gate missing"

BAD="$SANDBOX/bad"
write_fixture "$BAD"
"$SCRIPT" start --light --no-spawn --worktree "$BAD" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/bad-start.err"
python3 - "$BAD/.review-meta.json" <<'PY'
import json
import sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["review_mode"] = "sideways"
open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
PY
"$SCRIPT" verify --no-send --worktree "$BAD" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/bad.err"
expect_eq "invalid-mode-exit" "$?" 1
grep -q "review mode must be full or light, got 'sideways'" "$SANDBOX/bad.err" && ok "invalid-mode-message" || bad "invalid-mode-message" "missing value-oriented error"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf 'Failures:\n'
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
