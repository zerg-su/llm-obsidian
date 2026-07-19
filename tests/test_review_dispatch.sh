#!/usr/bin/env bash
# Regression tests for review-dispatch deterministic mode plumbing.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/skills/review-dispatch/scripts/spawn_review.py"
SEND_SCRIPT="$REPO_ROOT/skills/review-send/scripts/send_review.py"
SUPERVISOR="$REPO_ROOT/scripts/cmux_agent_supervisor.py"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/review-dispatch-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

# Keep this suite hermetic on clean CI runners. The production supervisor
# requires both agent runtimes and cmux to be resolvable from its pinned PATH,
# while --no-spawn tests only inspect the generated command specs. Provide
# owner-only no-op executables instead of depending on developer-installed
# Claude/Codex/cmux binaries, and isolate the configured ~/.codex path.
TEST_HOME="$SANDBOX/home"
TEST_RUNTIME_BIN="$SANDBOX/runtime-bin"
mkdir -m 700 -p "$TEST_HOME/.codex" "$TEST_RUNTIME_BIN"
for command in claude codex cmux; do
  printf '#!/bin/sh\nexit 0\n' > "$TEST_RUNTIME_BIN/$command"
  chmod 700 "$TEST_RUNTIME_BIN/$command"
done
export HOME="$TEST_HOME"
export PATH="$TEST_RUNTIME_BIN:$PATH"

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
  printf '# Task: review-dispatch-test\n\n## Task description\n\nImplement a small, bounded task.\n' > "$dir/.task-prompt.md"
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
LIGHT_VAULT="$SANDBOX/light-vault"
write_fixture "$LIGHT"
LIGHT_RESOLVED="$(cd "$LIGHT" && pwd -P)"
mkdir -p "$LIGHT_VAULT/wiki" "$LIGHT_VAULT/scripts" "$LIGHT_VAULT/.codex" \
  "$LIGHT_VAULT/skills/review-dispatch/scripts"
printf '# fixture\n' > "$LIGHT_VAULT/scripts/vault-write.py"
printf '# fixture\n' > "$LIGHT_VAULT/skills/review-dispatch/scripts/archive_review.py"
printf '[codex_dispatch]\nclaude_review_effort = "high"\n' > "$LIGHT_VAULT/.codex/dispatch-env.toml"
python3 - "$LIGHT/.task-meta.json" "$LIGHT_VAULT" <<'PY'
import json, sys
path, vault = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
data["vault_root"] = vault
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
printf '# stale resolution\n' > "$LIGHT/.task-review-resolution.md"
printf '# stale verify\n' > "$LIGHT/.task-review-verify.md"
"$SCRIPT" start --light --no-spawn --worktree "$LIGHT" --vault-root "$LIGHT_VAULT" >"$SANDBOX/light.out" 2>"$SANDBOX/light.err"
expect_eq "start-light-exit" "$?" 0
[[ ! -e "$LIGHT/.task-review-resolution.md" && ! -e "$LIGHT/.task-review-verify.md" ]] && ok "start-clears-stale-round-artifacts" || bad "start-clears-stale-round-artifacts" "old resolution/verify survived"
expect_eq "start-light-meta" "$(json_get "$LIGHT/.review-meta.json" review_mode)" "light"
expect_eq "start-supervised-receive" "$(json_get "$LIGHT/.review-meta.json" callback_transport)" "supervised-receive-v1"
expect_eq "start-review-id-stable" "$(json_get "$LIGHT/.review-meta.json" review_id)" "$(json_get "$LIGHT/.review-meta.json" run_id)"
[[ -f "$LIGHT/.review-history.json" ]] && ok "start-review-history" || bad "start-review-history" "history file missing"
python3 - "$LIGHT/.review-history.json" <<'PY'
import json, sys
history = json.load(open(sys.argv[1], encoding="utf-8"))
request = history.get("request", {})
assert request.get("review_mode") == "light"
assert "Implement a small, bounded task" in request.get("description", "")
PY
[[ $? -eq 0 ]] && ok "start-review-request-snapshot" || bad "start-review-request-snapshot" "request not retained"
grep -q 'Review mode: `light`' "$LIGHT/.review-prompt.md" && ok "start-light-prompt" || bad "start-light-prompt" "missing light marker"
grep -q 'top 5 actionable findings' "$LIGHT/.review-prompt.md" && ok "start-light-instructions" || bad "start-light-instructions" "missing light instructions"
LIGHT_SPEC="$LIGHT/.review-agent-command.json"
argv_has "$LIGHT_SPEC" --permission-mode dontAsk && ok "claude-unattended-mode" || bad "claude-unattended-mode" "dontAsk mode missing"
python3 - "$LIGHT_SPEC" <<'PY'
import json, os, sys
env = json.load(open(sys.argv[1], encoding="utf-8"))["env"]
assert set(env) == {"PATH"}
assert env["PATH"] and all(os.path.isabs(item) for item in env["PATH"].split(os.pathsep))
PY
[[ $? -eq 0 ]] && ok "claude-review-trusted-path" || bad "claude-review-trusted-path" "reviewer PATH is not pinned"
argv_has "$LIGHT_SPEC" --tools Read,Glob,Grep,Write,Bash && ok "claude-tool-surface" || bad "claude-tool-surface" "restricted tools missing"
python3 - "$LIGHT_SPEC" "$LIGHT/.review-meta.json" <<'PY'
import json, sys
argv = json.load(open(sys.argv[1], encoding="utf-8"))["argv"]
meta = json.load(open(sys.argv[2], encoding="utf-8"))
assert meta["submission_command"].startswith("supervisor relay watches ")
assert not any("send_review.py" in item for item in argv)
assert not any(item.startswith("Bash(git ") and "*" in item for item in argv)
PY
[[ $? -eq 0 ]] && ok "claude-supervised-callback" || bad "claude-supervised-callback" "Claude reviewer received an operation-specific callback permission"
grep -qF -- 'Bash(python3 tests/test_*.py)' "$LIGHT_SPEC" && ok "claude-python-tests-allowed" || bad "claude-python-tests-allowed" "bounded Python test rule missing"
grep -qF -- "Bash(python3 $LIGHT_RESOLVED/tests/test_*.py)" "$LIGHT_SPEC" && ok "claude-v3-absolute-python-tests-allowed" || bad "claude-v3-absolute-python-tests-allowed" "exact-worktree Python test rule missing"
grep -qF -- "Bash(bash $LIGHT_RESOLVED/tests/test_*.sh)" "$LIGHT_SPEC" && ok "claude-v3-absolute-shell-tests-allowed" || bad "claude-v3-absolute-shell-tests-allowed" "exact-worktree shell test rule missing"
if grep -qF -- 'Bash(python3 */' "$LIGHT_SPEC" || grep -qF -- 'Bash(bash */' "$LIGHT_SPEC"; then bad "claude-no-interpreter-adjacent-wildcard" "interpreter can consume wildcard-matched arguments"; else ok "claude-no-interpreter-adjacent-wildcard"; fi
grep -qF -- 'Bash(bash tests/test_*.sh)' "$LIGHT_SPEC" && ok "claude-shell-tests-allowed" || bad "claude-shell-tests-allowed" "bounded shell test rule missing"
grep -qF -- 'Bash(bash scripts/dcg-test-suite.sh)' "$LIGHT_SPEC" && ok "claude-dcg-smoke-allowed" || bad "claude-dcg-smoke-allowed" "exact DCG smoke rule missing"
grep -qF -- '`bash scripts/dcg-test-suite.sh`' "$LIGHT/.review-prompt.md" && ok "claude-dcg-smoke-advertised" || bad "claude-dcg-smoke-advertised" "DCG smoke command missing from prompt"
grep -qF -- '`python3 tests/test_document_normalize.py 2>&1 | tail -50`' "$LIGHT/.review-prompt.md" && ok "claude-test-shell-composition-denied" || bad "claude-test-shell-composition-denied" "prompt does not explain bounded test form"
grep -qF -- 'Edit(./.review-outbox.json)' "$LIGHT_SPEC" && ok "claude-outbox-only-write" || bad "claude-outbox-only-write" "cwd-anchored outbox Edit rule missing"
grep -qF -- 'supervisor relay watches' "$LIGHT/.review-prompt.md" && ok "claude-outbox-transport" || bad "claude-outbox-transport" "supervised outbox callback missing"
grep -q 'Do not run `review-send`' "$LIGHT/.review-prompt.md" && ok "claude-no-callback-command" || bad "claude-no-callback-command" "Claude reviewer is still told to invoke the callback command"
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
python3 - "$LIGHT_SPEC" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["env"]["PATH"] = "/tmp:" + data["env"]["PATH"]
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
"$SUPERVISOR" validate --worktree "$LIGHT" --kind reviewer --surface 00000000-0000-0000-0000-000000000000 >/dev/null 2>"$SANDBOX/supervisor-path.err"
[[ $? -ne 0 ]] && ok "review-supervisor-rejects-path-tamper" || bad "review-supervisor-rejects-path-tamper" "reviewer PATH drift accepted"
python3 - "$SUPERVISOR" "$LIGHT_SPEC" <<'PY'
import importlib.util, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).resolve().parent))
spec = importlib.util.spec_from_file_location("supervisor_path_restore", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
path = pathlib.Path(sys.argv[2])
data = json.loads(path.read_text(encoding="utf-8"))
data["env"]["PATH"] = module.trusted_runtime_path()
path.write_text(json.dumps(data) + "\n", encoding="utf-8")
PY
[[ $(wc -c < "$SANDBOX/light.out") -lt 1000 ]] && ok "review-command-bounded" || bad "review-command-bounded" "cmux command is unexpectedly long"
argv_has "$LIGHT_SPEC" --effort high && ok "claude-high-effort" || bad "claude-high-effort" "high effort missing"
argv_has "$LIGHT_SPEC" --model fable && ok "claude-fable-default" || bad "claude-fable-default" "default Claude reviewer is not fable"
expect_eq "claude-fable-meta" "$(json_get "$LIGHT/.review-meta.json" reviewer_model)" "fable"
if argv_has "$LIGHT_SPEC" --permission-mode plan; then bad "claude-no-plan" "plan mode present"; else ok "claude-no-plan"; fi
if argv_has "$LIGHT_SPEC" --permission-mode auto; then bad "claude-no-auto" "auto mode present"; else ok "claude-no-auto"; fi
if grep -q -- 'bash -lc\|WATCHDOG_PID\|REVIEW_RC=' "$SANDBOX/light.out"; then bad "review-shell-portable" "shell wrapper leaked into cmux command"; else ok "review-shell-portable"; fi
grep -q '"schema_version": 1' "$LIGHT/.review-prompt.md" && ok "typed-review-prompt" || bad "typed-review-prompt" "JSON contract missing"

# A coordinator review may use only the canonical vault's sanctioned generated
# scratch hierarchy, even though that hierarchy is inside the reviewed checkout.
python3 - "$SUPERVISOR" "$SANDBOX" <<'PY'
import importlib.util, pathlib, shutil, sys, uuid

real_supervisor, sandbox = (pathlib.Path(p).resolve() for p in sys.argv[1:3])
vault = sandbox / "canonical-vault"
(vault / "scripts").mkdir(parents=True)
copy = vault / "scripts" / "cmux_agent_supervisor.py"
shutil.copy2(real_supervisor, copy)
sys.path.insert(0, str(real_supervisor.parent))
spec = importlib.util.spec_from_file_location("supervisor_fixture_mod", copy)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
surface = str(uuid.uuid4()).upper()

def check(label, worktree, runtime, expected):
    meta = {"review_runtime_dir": str(runtime), "review_surface": surface}
    try:
        mod.validated_review_runtime(worktree, meta)
        actual = True
    except mod.SupervisorError:
        actual = False
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")

root = vault / ".vault-meta" / "review-runtimes"
root.mkdir(parents=True, mode=0o700)
scratch = root / f"llm-review-test-{uuid.uuid4().hex[:8]}"
scratch.mkdir(mode=0o700)
external = sandbox / "task-worktree"
external.mkdir()
check("canonical", vault, scratch, True)
check("external-task", external, scratch, True)
(scratch / "stale").write_text("x", encoding="utf-8")
check("nonempty", vault, scratch, False)
(scratch / "stale").unlink()
scratch.chmod(0o750)
check("permissions", vault, scratch, False)
scratch.chmod(0o700)
rogue = vault / f"llm-review-rogue-{uuid.uuid4().hex[:8]}"
rogue.mkdir(mode=0o700)
check("rogue-location", vault, rogue, False)
PY
[[ $? -eq 0 ]] && ok "review-runtime-coordinator-matrix" || bad "review-runtime-coordinator-matrix" "coordinator runtime validation failed"

probe=".vault-meta/review-runtimes/llm-review-probe/.review-outbox.json"
if git -C "$REPO_ROOT" check-ignore -q "$probe" && git -C "$REPO_ROOT" check-ignore -q "$probe.tmp"; then
  ok "review-runtimes-gitignored"
else
  bad "review-runtimes-gitignored" "review scratch hierarchy is not ignored"
fi

# Reviewer profile precedence excludes the executor's full-MCP profile. If a
# dedicated readonly profile is absent, omit --profile instead of reviving the
# schema-overflow failure through a fallback.
python3 - "$SCRIPT" "$SANDBOX" <<'PY'
import importlib.util, json, pathlib, sys

script, sandbox = (pathlib.Path(p).resolve() for p in sys.argv[1:3])
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location("spawn_review_mod", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
base = sandbox / "profile-fixture"
vault = base / "vault"
(vault / ".claude-plugin").mkdir(parents=True)
(vault / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "llm-obsidian"}), encoding="utf-8")
worktree = base / "worktree"
worktree.mkdir(parents=True)
home_with = base / "codex-with"
home_with.mkdir(parents=True)
(home_with / "llm-obsidian-reviewer-readonly.config.toml").write_text("", encoding="utf-8")
home_without = base / "codex-without"
home_without.mkdir(parents=True)

def profile(meta, runtime="codex"):
    return mod.resolve_review_env(worktree, vault, meta, runtime)["profile"]

assert profile({"codex_home": str(home_with), "reviewer_profile": "custom", "codex_profile": "executor"}) == "custom"
assert profile({"codex_home": str(home_with), "codex_profile": "executor"}) == "llm-obsidian-reviewer-readonly"
assert profile({"codex_home": str(home_without), "codex_profile": "executor"}) == ""
assert profile({"codex_home": str(home_with), "codex_profile": "executor"}, "claude") == ""
PY
[[ $? -eq 0 ]] && ok "reviewer-profile-precedence" || bad "reviewer-profile-precedence" "reviewer profile isolation failed"

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
printf '{}\n' > "$LINK_WORKTREE/.review-callback.json"
printf '{}\n' > "$LINK_WORKTREE/.task-reap-prepared.json"
git -C "$LINK_WORKTREE" check-ignore -q .review-relay.json
relay_ignored=$?
git -C "$LINK_WORKTREE" check-ignore -q .review-callback.json
callback_ignored=$?
git -C "$LINK_WORKTREE" check-ignore -q .task-reap-prepared.json
prepared_ignored=$?
[[ $relay_ignored -eq 0 && $callback_ignored -eq 0 && $prepared_ignored -eq 0 ]] && ok "linked-worktree-relay-ignored" || bad "linked-worktree-relay-ignored" "common exclude not updated"
for artifact in .review-history.json .review-archive.json .review-archive-request.json; do
  printf '{}\n' > "$LINK_WORKTREE/$artifact"
  git -C "$LINK_WORKTREE" check-ignore -q "$artifact" || bad "linked-worktree-$artifact-ignored" "common exclude not updated"
done
ok "linked-worktree-review-archive-ignored"

SELF_VAULT="$SANDBOX/self-vault"
SELF_TASK="$SANDBOX/self-task"
mkdir -p "$SELF_VAULT/wiki/plans" "$SELF_VAULT/scripts" \
  "$SELF_VAULT/skills/review-dispatch/scripts"
printf '# approved\n' > "$SELF_VAULT/wiki/plans/self-plan.md"
printf '# fixture\n' > "$SELF_VAULT/scripts/vault-write.py"
printf '# fixture\n' > "$SELF_VAULT/skills/review-dispatch/scripts/archive_review.py"
write_fixture "$SELF_TASK"
python3 - "$SELF_TASK/.task-meta.json" "$SELF_VAULT/wiki/plans/self-plan.md" <<'PY'
import json, sys
path, plan = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
data["plan_file"] = plan
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
"$SCRIPT" start --no-spawn --worktree "$SELF_TASK" >"$SANDBOX/self-start.out" 2>"$SANDBOX/self-start.err"
expect_eq "plan-derived-vault-start" "$?" 0
expect_eq "plan-derived-vault-root" "$(json_get "$SELF_TASK/.review-meta.json" vault_root)" "$(path_resolve "$SELF_VAULT")"
(cd "$SELF_TASK" && "$SCRIPT" archive --dry-run --worktree "$SELF_TASK") >"$SANDBOX/self-archive.out" 2>"$SANDBOX/self-archive.err"
expect_eq "plan-derived-task-archive" "$?" 0
grep -q '"status": "deferred"' "$SANDBOX/self-archive.out" && ok "plan-derived-task-defers" || bad "plan-derived-task-defers" "task context attempted a coordinator write"

python3 - "$SCRIPT" "$LINK_WORKTREE" <<'PY'
import importlib.util
import os
import pathlib
import sys

spec = importlib.util.spec_from_file_location("review_dispatch_self_vault_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2]).resolve()
os.chdir(worktree)
result = module.archive_or_defer(
    worktree,
    {"vault_root": str(worktree), "review_id": "self-vault"},
    dry_run=True,
)
assert result["status"] == "deferred"
PY
expect_eq "worktree-never-self-archives" "$?" 0

python3 - "$SCRIPT" "$LINK_WORKTREE" "$REPO_ROOT" <<'PY'
import importlib.util
import os
import pathlib
import sys

spec = importlib.util.spec_from_file_location("review_dispatch_coordinator_cwd_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2]).resolve()
vault = pathlib.Path(sys.argv[3]).resolve()
os.chdir(vault)
result = module.archive_or_defer(
    worktree,
    {"vault_root": str(vault), "review_id": "linked-from-coordinator-cwd"},
    dry_run=True,
)
assert result["status"] == "deferred"
PY
expect_eq "linked-task-defers-even-from-coordinator-cwd" "$?" 0

COORDINATOR="$SANDBOX/coordinator"
write_fixture "$COORDINATOR"
mkdir -p "$COORDINATOR/wiki" "$COORDINATOR/scripts" \
  "$COORDINATOR/skills/review-dispatch/scripts"
printf '# fixture\n' > "$COORDINATOR/scripts/vault-write.py"
cat > "$COORDINATOR/skills/review-dispatch/scripts/archive_review.py" <<'PY'
import json
print(json.dumps({"schema_version": 1, "status": "dry-run", "review_id": "fixture"}))
PY
"$SCRIPT" start --coordinator-review --no-spawn --worktree "$COORDINATOR" \
  --vault-root "$COORDINATOR" >"$SANDBOX/coordinator-start.out" 2>"$SANDBOX/coordinator-start.err"
expect_eq "coordinator-review-start" "$?" 0
expect_eq "coordinator-review-mode" "$(json_get "$COORDINATOR/.review-meta.json" archive_mode)" "coordinator"
(cd "$COORDINATOR" && "$SCRIPT" archive --dry-run --worktree "$COORDINATOR") \
  >"$SANDBOX/coordinator-archive.out" 2>"$SANDBOX/coordinator-archive.err"
expect_eq "coordinator-review-archive" "$?" 0
grep -q '"status": "dry-run"' "$SANDBOX/coordinator-archive.out" && ok "coordinator-review-archives-directly" || bad "coordinator-review-archives-directly" "primary checkout deferred"
LINK_COORDINATOR="$SANDBOX/linked-coordinator"
git -C "$LINK_ROOT" worktree add -q -b task/linked-coordinator "$LINK_COORDINATOR"
cat > "$LINK_COORDINATOR/.task-meta.json" <<'JSON'
{"task_name":"linked-coordinator-test","base_branch":"HEAD","branch":"task/linked-coordinator","executor_runtime":"codex","model":"gpt-5.6-sol"}
JSON
printf '# Task: linked-coordinator-test\n' > "$LINK_COORDINATOR/.task-prompt.md"
printf '00000000-0000-0000-0000-000000000001\n' > "$LINK_COORDINATOR/.task-cmux-surface"
"$SCRIPT" start --coordinator-review --no-spawn --worktree "$LINK_COORDINATOR" \
  --vault-root "$REPO_ROOT" >"$SANDBOX/linked-coordinator.out" 2>"$SANDBOX/linked-coordinator.err"
[[ $? -ne 0 ]] && ok "linked-worktree-coordinator-mode-rejected" || bad "linked-worktree-coordinator-mode-rejected" "linked task accepted coordinator mode"
grep -q 'worktree and vault root to be identical' "$SANDBOX/linked-coordinator.err" && ok "linked-worktree-coordinator-rejection-reason" || bad "linked-worktree-coordinator-rejection-reason" "wrong rejection path"
python3 - "$SCRIPT" "$LINK_COORDINATOR" <<'PY'
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location("review_dispatch_forged_coordinator_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2]).resolve()
result = module.archive_or_defer(
    worktree,
    {
        "archive_mode": "coordinator",
        "vault_root": str(worktree),
        "review_id": "forged-linked-coordinator",
    },
    dry_run=True,
)
assert result["status"] == "deferred"
assert not module.is_primary_coordinator_review(
    worktree,
    worktree,
    {"archive_mode": "coordinator"},
)
PY
expect_eq "linked-worktree-forged-archive-mode-defers" "$?" 0

python3 - "$LIGHT/.review-meta.json" "$LIGHT" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["vault_root"] = sys.argv[2]
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
run_id="$(json_get "$LIGHT/.review-meta.json" run_id)"
review_payload=$(python3 - "$run_id" <<'PY'
import json, sys
print(json.dumps({
  "schema_version": 1, "run_id": sys.argv[1], "mode": "light", "verdict": "approve",
  "findings": [], "verification_gaps": ["x" * 1800],
  "notes_for_executor": [], "residual_risks": []
}))
PY
)
printf '%s' "$review_payload" > "$LIGHT/.review-outbox.json"
printf '{}\n' > "$LIGHT/.review-watchdog.json"
outbox_callback=$("$SEND_SCRIPT" submit --no-send --worktree "$LIGHT" --input-file "$LIGHT/.review-outbox.json" 2>"$SANDBOX/outbox.err")
expect_eq "typed-outbox-submit" "$?" 0
light_callback_path="$(path_resolve "$LIGHT")/.review-callback.json"
[[ ! -e "$LIGHT/.review-outbox.json" ]] && ok "typed-outbox-removed" || bad "typed-outbox-removed" "outbox remains"
[[ -f "$LIGHT/.review-callback.json" ]] && ok "typed-callback-relay-written" || bad "typed-callback-relay-written" "relay missing"
python3 - "$LIGHT/.review-callback.json" <<'PY'
import stat, sys
raise SystemExit(0 if stat.S_IMODE(__import__("os").stat(sys.argv[1]).st_mode) == 0o600 else 1)
PY
[[ $? -eq 0 ]] && ok "typed-callback-relay-private" || bad "typed-callback-relay-private" "relay mode is not 600"
[[ "$outbox_callback" == *"--relay-file $light_callback_path"* ]] && ok "typed-outbox-callback" || bad "typed-outbox-callback" "short relay callback missing"
if [[ "$outbox_callback" == *"--payload-b64"* ]]; then bad "typed-callback-no-inline-payload" "large payload remains inline"; else ok "typed-callback-no-inline-payload"; fi
[[ ${#outbox_callback} -lt 1000 ]] && ok "typed-callback-bounded" || bad "typed-callback-bounded" "callback is unexpectedly long"
[[ "$outbox_callback" == "Cross-model review callback"* ]] && ok "runtime-neutral-callback" || bad "runtime-neutral-callback" "callback still depends on a slash command"
callback=$(printf '%s' "$review_payload" | "$SEND_SCRIPT" submit --no-send --worktree "$LIGHT" 2>"$SANDBOX/submit.err")
expect_eq "typed-submit-exit" "$?" 0
[[ "$callback" == *"--relay-file $light_callback_path"* ]] && ok "typed-stdin-short-callback" || bad "typed-stdin-short-callback" "stdin transport did not publish relay"
"$SCRIPT" receive --worktree "$LIGHT" --relay-file "$LIGHT/.review-callback.json" >/dev/null 2>"$SANDBOX/receive.err"
expect_eq "typed-receive-exit" "$?" 0
expect_eq "receive-canonical-vault-root" "$(json_get "$LIGHT/.review-meta.json" vault_root)" "$(path_resolve "$LIGHT_VAULT")"
[[ ! -e "$LIGHT/.review-callback.json" ]] && ok "typed-receive-removes-relay" || bad "typed-receive-removes-relay" "received relay remains"
expect_eq "typed-json-verdict" "$(json_get "$LIGHT/.task-review.json" verdict)" "approve"
python3 - "$LIGHT/.review-history.json" "$run_id" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["review_id"] == sys.argv[2]
assert len(data["rounds"]) == 1
assert data["rounds"][0]["review"]["run_id"] == sys.argv[2]
PY
[[ $? -eq 0 ]] && ok "receive-records-review-history" || bad "receive-records-review-history" "round not retained"
grep -q '^Verdict: approve$' "$LIGHT/.task-review.md" && ok "typed-markdown-render" || bad "typed-markdown-render" "derived markdown missing"
python3 - "$LIGHT_VAULT/.vault-meta/pipeline-events.jsonl" <<'PY'
import json, sys
events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
matches = [event for event in events if event.get("op") == "review-round"]
assert len(matches) == 1
assert matches[0]["counts"]["valid_callbacks"] == 1
assert matches[0]["counts"]["verdict_approve"] == 1
assert matches[0]["counts"]["action_interactive"] == 1
PY
[[ $? -eq 0 ]] && ok "review-callback-telemetry" || bad "review-callback-telemetry" "typed lifecycle event missing"

python3 - "$SCRIPT" "$LIGHT" <<'PY'
import contextlib, importlib.util, io, json, pathlib, sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("review_drive_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2])
meta_path = worktree / ".review-meta.json"
original = meta_path.read_text(encoding="utf-8")
calls = []

module.configure_existing_review_state = lambda _ns, root, _meta: root
module.cmd_finish = lambda ns: calls.append(("finish", ns.operation_dir)) or 0
module.cmd_verify = lambda ns: calls.append(("verify", ns.operation_dir)) or 0

try:
    meta = json.loads(original)
    meta.update(status="review_received", recommended_action="approve")
    meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        assert module.cmd_drive(SimpleNamespace(
            worktree=str(worktree), operation_dir="", apply_action=True
        )) == 0
    assert calls == [("finish", str(worktree.resolve()))]

    calls.clear()
    meta["recommended_action"] = "resolve"
    meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    (worktree / ".task-review-resolution.md").write_text("# Applied\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        assert module.cmd_drive(SimpleNamespace(
            worktree=str(worktree), operation_dir="", apply_action=True
        )) == 0
    assert calls == [("verify", str(worktree.resolve()))]

    calls.clear()
    meta["recommended_action"] = "escalate"
    meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    try:
        module.cmd_drive(SimpleNamespace(
            worktree=str(worktree), operation_dir="", apply_action=True
        ))
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("escalation was automatically applied")
    assert not calls
finally:
    meta_path.write_text(original, encoding="utf-8")
    (worktree / ".task-review-resolution.md").unlink(missing_ok=True)
PY
[[ $? -eq 0 ]] && ok "review-drive-safe-actions" || bad "review-drive-safe-actions" "drive did not preserve approve/resolve/escalate boundaries"

LEGACY="$SANDBOX/legacy-payload"
write_fixture "$LEGACY"
"$SCRIPT" start --light --no-spawn --worktree "$LEGACY" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/legacy-start.err"
legacy_run_id="$(json_get "$LEGACY/.review-meta.json" run_id)"
legacy_token=$(PYTHONPATH="$REPO_ROOT/scripts" python3 - "$legacy_run_id" <<'PY'
import sys
from review_contract import encode_review
print(encode_review({
  "schema_version": 1, "run_id": sys.argv[1], "mode": "light", "verdict": "approve",
  "findings": [], "verification_gaps": [], "notes_for_executor": [], "residual_risks": []
}))
PY
)
"$SCRIPT" receive --worktree "$LEGACY" --payload-b64 "$legacy_token" >/dev/null 2>"$SANDBOX/legacy-receive.err"
expect_eq "legacy-payload-receive" "$?" 0
expect_eq "legacy-payload-verdict" "$(json_get "$LEGACY/.task-review.json" verdict)" "approve"

printf '%s\n' "$review_payload" > "$LIGHT/not-the-relay.json"
"$SCRIPT" receive --worktree "$LIGHT" --relay-file "$LIGHT/not-the-relay.json" >/dev/null 2>"$SANDBOX/wrong-relay.err"
expect_eq "wrong-relay-path-rejected" "$?" 3
grep -q 'relay file must be the regular file' "$SANDBOX/wrong-relay.err" && ok "wrong-relay-path-message" || bad "wrong-relay-path-message" "exact-path failure missing"
rm -f "$LIGHT/not-the-relay.json"
printf '{invalid\n' > "$LIGHT/.review-callback.json"
"$SCRIPT" receive --worktree "$LIGHT" --relay-file "$LIGHT/.review-callback.json" >/dev/null 2>"$SANDBOX/invalid-relay.err"
expect_eq "invalid-relay-rejected" "$?" 3
[[ -f "$LIGHT/.review-callback.json" ]] && ok "invalid-relay-retained" || bad "invalid-relay-retained" "failed callback was destroyed"
rm -f "$LIGHT/.review-callback.json"

SEND_FAIL="$SANDBOX/send-failure"
write_fixture "$SEND_FAIL"
"$SCRIPT" start --light --no-spawn --worktree "$SEND_FAIL" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/send-failure-start.err"
python3 - "$SEND_SCRIPT" "$SEND_FAIL" <<'PY'
import importlib.util, json, pathlib, sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("review_send_failure_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
worktree = pathlib.Path(sys.argv[2])
meta = json.loads((worktree / ".review-meta.json").read_text(encoding="utf-8"))
outbox = worktree / ".review-outbox.json"
outbox.write_text(json.dumps({
  "schema_version": 1, "run_id": meta["run_id"], "mode": "light", "verdict": "approve",
  "findings": [], "verification_gaps": [], "notes_for_executor": [], "residual_risks": []
}) + "\n", encoding="utf-8")
def fail_send(*_):
    raise SystemExit(1)

module.send_to_surface = fail_send
assert module.cmd_submit(SimpleNamespace(
    worktree=str(worktree), input_file=str(outbox), no_send=False
)) == 0
assert not outbox.exists()
assert not (worktree / module.REVIEW_CALLBACK_FILE).exists()
received = json.loads((worktree / ".review-meta.json").read_text(encoding="utf-8"))
assert received["status"] == "review_received"
assert received["recommended_action"] == "interactive"
assert (worktree / ".task-review.json").is_file()
PY
[[ $? -eq 0 ]] && ok "failed-notify-keeps-received-state" || bad "failed-notify-keeps-received-state" "durable receive was retried after a UI-only notification failure"
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
python3 - "$LIGHT/.task-meta.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data.update({
    "base_branch": "stale/base",
    "branch": "stale/branch",
    "executor_runtime": "claude",
    "model": "stale-model",
    "plan_file": "/tmp/stale-plan.md",
})
open(path, "w", encoding="utf-8").write(json.dumps(data) + "\n")
PY
"$SCRIPT" verify --no-send --worktree "$LIGHT" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/verify.err"
expect_eq "verify-light-exit" "$?" 0
expect_eq "verify-light-preserved" "$(json_get "$LIGHT/.review-meta.json" review_mode)" "light"
expect_eq "verify-file-reference" "$(json_get "$LIGHT/.review-meta.json" send_mode)" "file-reference"
expect_eq "verify-review-id-preserved" "$(json_get "$LIGHT/.review-meta.json" review_id)" "$run_id"
python3 - "$LIGHT/.review-history.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["rounds"][0]["resolution"].startswith("# Review Resolution")
PY
[[ $? -eq 0 ]] && ok "verify-snapshots-resolution" || bad "verify-snapshots-resolution" "resolution not retained"
grep -qF -- '- Base branch: `HEAD`' "$LIGHT/.review-prompt-verify.md" && ok "verify-stable-base-branch" || bad "verify-stable-base-branch" "task metadata overrode review base"
grep -qF -- '- Task branch: `task/review-dispatch-test`' "$LIGHT/.review-prompt-verify.md" && ok "verify-stable-task-branch" || bad "verify-stable-task-branch" "task metadata overrode review branch"
grep -qF -- '- Executor: `codex` `gpt-5.6-sol`' "$LIGHT/.review-prompt-verify.md" && ok "verify-stable-executor" || bad "verify-stable-executor" "task metadata overrode executor context"
grep -qF -- '- Plan file: `none`' "$LIGHT/.review-prompt-verify.md" && ok "verify-stable-plan" || bad "verify-stable-plan" "stale task plan leaked"
(cd "$LIGHT" && "$SCRIPT" archive --dry-run --worktree "$LIGHT") >"$SANDBOX/archive-dry.out" 2>"$SANDBOX/archive-dry.err"
expect_eq "task-archive-deferred-dry" "$?" 0
grep -q '"status": "deferred"' "$SANDBOX/archive-dry.out" && ok "task-archive-defers-to-reap" || bad "task-archive-defers-to-reap" "task attempted a vault write"
"$SCRIPT" start --no-spawn --worktree "$LIGHT" --vault-root "$REPO_ROOT" >"$SANDBOX/restart.out" 2>"$SANDBOX/restart.err"
expect_eq "unarchived-cycle-restart-blocked" "$?" 1
grep -q 'previous review history is not archived' "$SANDBOX/restart.err" && ok "unarchived-cycle-preserved" || bad "unarchived-cycle-preserved" "prior cycle could be overwritten"
grep -q 'Review mode: `light`' "$LIGHT/.review-prompt-verify.md" && ok "verify-light-prompt" || bad "verify-light-prompt" "missing light marker"

FULL="$SANDBOX/full"
write_fixture "$FULL"
"$SCRIPT" start --no-spawn --worktree "$FULL" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/full.err"
expect_eq "start-full-exit" "$?" 0
expect_eq "start-full-default" "$(json_get "$FULL/.review-meta.json" review_mode)" "full"
grep -q 'Review mode: `full`' "$FULL/.review-prompt.md" && ok "start-full-prompt" || bad "start-full-prompt" "missing full marker"

FINDINGS="$SANDBOX/findings-without-resolution"
write_fixture "$FINDINGS"
"$SCRIPT" start --no-spawn --worktree "$FINDINGS" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/findings-start.err"
findings_run_id="$(json_get "$FINDINGS/.review-meta.json" run_id)"
python3 - "$FINDINGS/.review-callback.json" "$findings_run_id" <<'PY'
import json, os, sys
path, run_id = sys.argv[1:]
payload = {
  "schema_version": 1, "run_id": run_id, "mode": "full", "verdict": "changes-requested",
  "findings": [{
    "severity": "warning", "file": "file.txt", "line": 1,
    "title": "fixture finding", "evidence": "fixture evidence", "recommendation": "fix it"
  }],
  "verification_gaps": [], "notes_for_executor": [], "residual_risks": []
}
open(path, "w", encoding="utf-8").write(json.dumps(payload) + "\n")
os.chmod(path, 0o600)
PY
"$SCRIPT" receive --worktree "$FINDINGS" --relay-file "$FINDINGS/.review-callback.json" >/dev/null 2>"$SANDBOX/findings-receive.err"
expect_eq "findings-receive" "$?" 0
"$SCRIPT" verify --no-send --worktree "$FINDINGS" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/findings-verify.err"
expect_eq "findings-require-resolution" "$?" 1
grep -q 'latest review has findings; write .task-review-resolution.md before verify' "$SANDBOX/findings-verify.err" && ok "findings-resolution-message" || bad "findings-resolution-message" "missing fail-closed guidance"

OPUS="$SANDBOX/opus"
write_fixture "$OPUS"
python3 - "$OPUS/.task-meta.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data.update({"claude_review_model": "opus", "claude_review_effort": "xhigh"})
open(path, "w").write(json.dumps(data) + "\n")
PY
"$SCRIPT" start --no-spawn --worktree "$OPUS" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/opus.err"
expect_eq "claude-opt-in-start" "$?" 0
argv_has "$OPUS/.review-agent-command.json" --model opus && ok "claude-opt-in-model" || bad "claude-opt-in-model" "explicit Claude model was not preserved"
argv_has "$OPUS/.review-agent-command.json" --effort xhigh && ok "claude-opt-in-effort" || bad "claude-opt-in-effort" "explicit Claude effort was not preserved"
expect_eq "claude-opt-in-meta" "$(json_get "$OPUS/.review-meta.json" reviewer_model)" "opus"

CODEX_REVIEW="$SANDBOX/codex-review"
write_fixture "$CODEX_REVIEW"
python3 - "$CODEX_REVIEW/.task-meta.json" <<'PY'
import json, sys
p=sys.argv[1]; data=json.load(open(p)); data["executor_runtime"]="claude"
open(p,"w").write(json.dumps(data)+"\n")
PY

SAME_MODEL="$SANDBOX/same-model-review"
write_fixture "$SAME_MODEL"
"$SCRIPT" start --same-model --no-spawn --worktree "$SAME_MODEL" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/same-model.err"
expect_eq "same-model-start" "$?" 0
expect_eq "same-model-runtime" "$(json_get "$SAME_MODEL/.review-meta.json" reviewer_runtime)" "codex"
expect_eq "same-model-model" "$(json_get "$SAME_MODEL/.review-meta.json" reviewer_model)" "gpt-5.6-sol"
expect_eq "same-model-effort" "$(json_get "$SAME_MODEL/.review-meta.json" reviewer_effort)" "high"
argv_has "$SAME_MODEL/.review-agent-command.json" --model gpt-5.6-sol && ok "same-model-argv" || bad "same-model-argv" "current model not pinned"

SAME_MODEL_BAD="$SANDBOX/same-model-bad"
write_fixture "$SAME_MODEL_BAD"
"$SCRIPT" start --same-model --model forbidden --no-spawn --worktree "$SAME_MODEL_BAD" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/same-model-bad.err"
expect_eq "same-model-conflict-rejected" "$?" 1
grep -q -- '--same-model cannot be combined' "$SANDBOX/same-model-bad.err" && ok "same-model-conflict-message" || bad "same-model-conflict-message" "conflict not explained"

CROSS_PROVIDER="$SANDBOX/cross-provider-review"
write_fixture "$CROSS_PROVIDER"
"$SCRIPT" start --reviewer-runtime codex --model fable --no-spawn --worktree "$CROSS_PROVIDER" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/cross-provider.err"
expect_eq "cross-provider-model-rejected" "$?" 1
grep -q -- "registered for claude, not codex" "$SANDBOX/cross-provider.err" && ok "cross-provider-model-message" || bad "cross-provider-model-message" "provider mismatch not explained"

"$SCRIPT" start --no-spawn --worktree "$CODEX_REVIEW" --vault-root "$REPO_ROOT" >"$SANDBOX/codex.out" 2>"$SANDBOX/codex.err"
expect_eq "codex-review-start" "$?" 0
CODEX_SPEC="$CODEX_REVIEW/.review-agent-command.json"
CODEX_RUNTIME="$(json_get "$CODEX_REVIEW/.review-meta.json" review_runtime_dir)"
CODEX_REVIEW_RESOLVED="$(path_resolve "$CODEX_REVIEW")"
argv_has "$CODEX_SPEC" -s workspace-write && ok "codex-scratch-write-mode" || bad "codex-scratch-write-mode" "workspace-write missing"
argv_has "$CODEX_SPEC" --model gpt-5.6-sol && ok "codex-sol-default" || bad "codex-sol-default" "default Codex reviewer is not gpt-5.6-sol"
argv_has "$CODEX_SPEC" -c 'service_tier="default"' && ok "codex-review-default-service" || bad "codex-review-default-service" "automated reviewer inherited Fast service"
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
grep -q 'web_search=' "$CODEX_SPEC" && ok "codex-external-network-disabled" || bad "codex-external-network-disabled" "web search override missing"
python3 - "$CODEX_SPEC" <<'PY'
import json, os, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
argv = data["argv"]
configs = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-c"]
assert 'features.network_proxy.domains={ "localhost" = "allow", "127.0.0.1" = "allow", "::1" = "allow" }' in configs
assert "features.network_proxy.unix_sockets={}" in configs
assert "features.network_proxy.allow_local_binding=true" in configs
assert "PATH" in data["env"] and data["env"]["PATH"]
assert all(os.path.isabs(item) for item in data["env"]["PATH"].split(os.pathsep))
PY
[[ $? -eq 0 ]] && ok "codex-review-loopback-toolchain" || bad "codex-review-loopback-toolchain" "exact loopback/toolchain policy missing"
argv_has "$CODEX_SPEC" -c 'model_reasoning_effort="high"' && ok "codex-high-effort-default" || bad "codex-high-effort-default" "high effort default missing"
expect_eq "codex-high-effort-meta" "$(json_get "$CODEX_REVIEW/.review-meta.json" reviewer_effort)" "high"

CODEX_EFFORT="$SANDBOX/codex-effort"
write_fixture "$CODEX_EFFORT"
python3 - "$CODEX_EFFORT/.task-meta.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["executor_runtime"] = "claude"
open(path, "w").write(json.dumps(data) + "\n")
PY
"$SCRIPT" start --no-spawn --effort max --worktree "$CODEX_EFFORT" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/codex-effort.err"
expect_eq "codex-effort-start" "$?" 0
CODEX_EFFORT_SPEC="$CODEX_EFFORT/.review-agent-command.json"
argv_has "$CODEX_EFFORT_SPEC" -c 'model_reasoning_effort="max"' && ok "codex-effort-argv" || bad "codex-effort-argv" "explicit effort missing"
python3 - "$CODEX_EFFORT_SPEC" <<'PY'
import json, sys
argv = json.load(open(sys.argv[1], encoding="utf-8"))["argv"]
raise SystemExit(0 if argv.index("--model") < argv.index('model_reasoning_effort="max"') else 1)
PY
[[ $? -eq 0 ]] && ok "codex-effort-after-model" || bad "codex-effort-after-model" "effort must follow --model"
expect_eq "codex-effort-meta" "$(json_get "$CODEX_EFFORT/.review-meta.json" reviewer_effort)" "max"

CODEX_EFFORT_META="$SANDBOX/codex-effort-meta"
write_fixture "$CODEX_EFFORT_META"
python3 - "$CODEX_EFFORT_META/.task-meta.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data.update({"executor_runtime": "claude", "codex_review_effort": "xhigh"})
open(path, "w").write(json.dumps(data) + "\n")
PY
"$SCRIPT" start --no-spawn --worktree "$CODEX_EFFORT_META" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/codex-effort-meta.err"
expect_eq "codex-effort-meta-start" "$?" 0
argv_has "$CODEX_EFFORT_META/.review-agent-command.json" -c 'model_reasoning_effort="xhigh"' && ok "codex-effort-from-task-meta" || bad "codex-effort-from-task-meta" "metadata effort missing"

CODEX_EFFORT_BAD="$SANDBOX/codex-effort-bad"
write_fixture "$CODEX_EFFORT_BAD"
python3 - "$CODEX_EFFORT_BAD/.task-meta.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data.update({"executor_runtime": "claude", "codex_review_effort": "turbo"})
open(path, "w").write(json.dumps(data) + "\n")
PY
"$SCRIPT" start --no-spawn --worktree "$CODEX_EFFORT_BAD" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/codex-effort-bad.err"
expect_eq "codex-effort-invalid-rejected" "$?" 1
grep -q 'Codex reviewer effort must be one of' "$SANDBOX/codex-effort-bad.err" && ok "codex-effort-invalid-message" || bad "codex-effort-invalid-message" "invalid effort not rejected"
grep -qF -- "$CODEX_RUNTIME/.review-outbox.json" "$CODEX_REVIEW/.review-prompt.md" && ok "codex-relay-outbox-prompt" || bad "codex-relay-outbox-prompt" "relay outbox missing"
grep -q 'Do not run `review-send`' "$CODEX_REVIEW/.review-prompt.md" && ok "codex-no-socket-prompt" || bad "codex-no-socket-prompt" "socket boundary missing"
grep -qF -- "git -C $CODEX_REVIEW_RESOLVED status --porcelain=v1" "$CODEX_REVIEW/.review-prompt.md" && ok "codex-worktree-git-prompt" || bad "codex-worktree-git-prompt" "scratch reviewer lacks exact worktree git guidance"
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
assert module.reviewer_uses_supervised_relay("claude")
assert module.reviewer_uses_supervised_relay("codex")
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
