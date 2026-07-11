#!/usr/bin/env bash
# Regression suite for the deterministic vault layer:
#   scripts/vault-write.py        — caps, log prepend, plan_close, atomicity
#   scripts/parse-wiki-summary.py — Wiki Summary block parser
#   scripts/validate-vault.py     — plans / questions / frontmatter-sessions checks
#
# Runs against a SANDBOX copy under mktemp — the live vault is never touched
# (REPO_ROOT in the scripts resolves from their own location, so copies in
# the sandbox operate on sandbox fixtures).
#
# Run from repo root: ./tests/test_vault_scripts.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d /tmp/vault-scripts-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

mkdir -p "$SANDBOX/scripts" "$SANDBOX/wiki/plans" "$SANDBOX/wiki/questions" "$SANDBOX/.vault-meta"
cp "$REPO_ROOT/scripts/vault-write.py" \
   "$REPO_ROOT/scripts/plan_lifecycle.py" \
   "$REPO_ROOT/scripts/pipeline_events.py" \
   "$REPO_ROOT/scripts/validate-vault.py" \
   "$REPO_ROOT/scripts/reindex.py" \
   "$REPO_ROOT/scripts/vault_schema.py" \
   "$REPO_ROOT/scripts/tag-search.py" "$SANDBOX/scripts/"

VW="$SANDBOX/scripts/vault-write.py"
VV="$SANDBOX/scripts/validate-vault.py"
TS="$SANDBOX/scripts/tag-search.py"
PW="$REPO_ROOT/scripts/parse-wiki-summary.py"   # stateless — live copy is fine
TODAY="$(date +%Y-%m-%d)"

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

# ---------- fixtures ----------

write_hot() {
cat > "$SANDBOX/wiki/hot.md" <<'EOF'
---
type: meta
title: "Hot Cache"
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
sessions: []
---

# Hot Cache

## Last Updated

Nothing yet.

## Key Recent Facts

- seed fact

## Recent Changes

- 2026-01-01: [[Seed]] — seed bullet

## Active Threads

- **Open**: seed thread
EOF
}

write_log() {
cat > "$SANDBOX/wiki/log.md" <<'EOF'
---
type: meta
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
---

# Log

## [2026-01-01] seed | Seed

- seed entry
EOF
}

# full-frontmatter plan fixture: $1=filename $2=status $3=created $4=sessions-yaml $5=body
write_plan() {
  { printf -- '---\ntype: plan\ntitle: "%s"\nstatus: %s\ncreated: %s\nupdated: %s\ntags: [plan]\n' \
      "$1" "$2" "$3" "$3"
    printf '%s\n' "$4"
    printf -- '---\n\n# %s\n\n%s\n' "$1" "$5"
  } > "$SANDBOX/wiki/plans/$1.md"
}

write_hot; write_log

# ---------- vault-write: log + hot caps ----------
echo "== vault-write: log + hot caps =="

echo '{"log_entry": "## ['"$TODAY"'] test | Prepend\n\n- new entry body"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-log-prepend" "$?" 0
head -15 "$SANDBOX/wiki/log.md" > "$SANDBOX/.head"
expect_grep "vw-log-prepend-order" "$SANDBOX/.head" "test | Prepend"

echo '{"session":"public-template-v2","log_entry":"## ['"$TODAY"'] test | Provenance\n\n- session-aware entry","hot_narrative":"Session-aware narrative."}' | "$VW" >/dev/null 2>&1
expect_exit "vw-writer-owned-session" "$?" 0
expect_grep "vw-log-session-recorded" "$SANDBOX/wiki/log.md" '  - "public-template-v2"'
expect_grep "vw-hot-session-recorded" "$SANDBOX/wiki/hot.md" '  - "public-template-v2"'

echo '{"log_entry": "no heading format"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-log-bad-format" "$?" 2

echo '{"log_entry": "## ['"$TODAY"'] missing separator\n\n- body"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-log-missing-pipe" "$?" 2

echo '{"log_entry": "## ['"$TODAY"' 12:34] test-op | Timestamp accepted\n\n- body"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-log-timestamp" "$?" 0

hot_hash=$("$VW" --sha256 wiki/hot.md)
python3 - "$SANDBOX/wiki/hot.md" "$hot_hash" <<'PY' | "$VW" >/dev/null 2>"$SANDBOX/.err"
import json, sys
print(json.dumps({"pages": [{"op": "update", "path": "wiki/hot.md", "expected_sha256": sys.argv[2], "content": open(sys.argv[1]).read()}]}))
PY
expect_exit "vw-pages-hot-rejected" "$?" 3
expect_grep "vw-pages-hot-guidance" "$SANDBOX/.err" "dedicated hot_*"

log_hash=$("$VW" --sha256 wiki/log.md)
python3 - "$SANDBOX/wiki/log.md" "$log_hash" <<'PY' | "$VW" >/dev/null 2>"$SANDBOX/.err"
import json, sys
print(json.dumps({"pages": [{"op": "update", "path": "wiki/log.md", "expected_sha256": sys.argv[2], "content": open(sys.argv[1]).read()}]}))
PY
expect_exit "vw-pages-log-rejected" "$?" 3
expect_grep "vw-pages-log-guidance" "$SANDBOX/.err" "dedicated log_entry"

long_bullet=$(printf 'длинное-описание-%.0s' {1..20})
echo '{"hot_bullet": "2026-01-02: [[Structurally Important Page]] — '"$long_bullet"' (`c-000047`)"}' | "$VW" >/dev/null 2>"$SANDBOX/.err"
expect_exit "vw-bullet-truncate-exit" "$?" 0
expect_grep "vw-bullet-truncate-warn" "$SANDBOX/.err" "truncated"
recent=$(sed -n '/## Recent Changes/,/## Active Threads/p' "$SANDBOX/wiki/hot.md" | grep '^- ' | head -n 1)
[[ ${#recent} -le 160 ]] && ok "vw-bullet-truncate-cap" || bad "vw-bullet-truncate-cap" "got ${#recent} chars"
[[ "$recent" == *'[[Structurally Important Page]]'* ]] && ok "vw-bullet-link-preserved" || bad "vw-bullet-link-preserved" "$recent"
[[ "$recent" == *'c-000047'* && "$recent" != *'c-00004…'* ]] && ok "vw-bullet-address-preserved" || bad "vw-bullet-address-preserved" "$recent"

echo '{"hot_bullet": "2026-01-02: [[Missing Address]] — invalid"}' | "$VW" >/dev/null 2>"$SANDBOX/.err"
expect_exit "vw-bullet-missing-address-rejected" "$?" 3
expect_grep "vw-bullet-missing-address-guidance" "$SANDBOX/.err" "c-NNNNNN"

json_out=$(echo '{"schema_version":1,"request_id":"test.hot.001","hot_bullet":"2026-01-02: [[JSON Contract]] — dry (`c-000047`)"}' | "$VW" --dry-run --output json 2>"$SANDBOX/.err")
python3 - "$json_out" <<'PY'
import json, sys
d=json.loads(sys.argv[1])
assert d["schema_version"] == 1
assert d["transaction_id"] == "test.hot.001" == d["request_id"]
assert d["status"] == "dry-run"
assert d["written_paths"] == ["wiki/hot.md"]
PY
expect_exit "vw-json-success-contract" "$?" 0

json_out=$(echo '{"schema_version":2,"request_id":"bad.version","hot_bullet":"2026-01-02: [[JSON Contract]] — bad (`c-000047`)"}' | "$VW" --output json 2>/dev/null)
expect_exit "vw-json-version-exit" "$?" 3
python3 - "$json_out" <<'PY'
import json, sys
d=json.loads(sys.argv[1]); assert d["status"] == "error"
assert d["error"]["category"] == "invalid_request"
assert d["error"]["retryable"] is False
PY
expect_exit "vw-json-error-contract" "$?" 0

for i in $(seq 1 16); do
  printf -v addr 'c-%06d' "$i"
  echo '{"hot_bullet": "2026-01-02: [[P'"$i"']] — bullet '"$i"' (`'"$addr"'`)"}' | "$VW" >/dev/null 2>"$SANDBOX/.err"
done
expect_grep "vw-rc-evict-warn" "$SANDBOX/.err" "evicted"
n_bullets=$(sed -n '/## Recent Changes/,/## Active Threads/p' "$SANDBOX/wiki/hot.md" | grep -c '^- ')
[[ "$n_bullets" == "15" ]] && ok "vw-rc-evict-count" || bad "vw-rc-evict-count" "got $n_bullets bullets (want 15)"

adds='"- t1","- t2","- t3","- t4","- t5","- t6","- t7","- t8"'
echo '{"hot_threads": {"add": ['"$adds"']}}' | "$VW" >/dev/null 2>&1
expect_exit "vw-threads-cap" "$?" 2
n_threads=$(sed -n '/## Active Threads/,$p' "$SANDBOX/wiki/hot.md" | grep -c '^- ')
[[ "$n_threads" == "1" ]] && ok "vw-threads-cap-unchanged" || bad "vw-threads-cap-unchanged" "got $n_threads threads (want 1, nothing written)"

narrative=$(printf 'word %.0s' {1..130})
echo '{"hot_narrative": "'"$narrative"'"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-narrative-cap" "$?" 2

echo '{}' | "$VW" >/dev/null 2>&1
expect_exit "vw-empty-payload" "$?" 3

echo '{"index_line": "- x"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-index-line-rejected" "$?" 3

# ---------- vault-write: transactional pages / manifest / recovery ----------
echo "== vault-write: transactional mutations =="

cat > "$SANDBOX/.page.md" <<EOF
---
type: concept
title: "Transaction Page"
address: c-000001
status: developing
created: $TODAY
updated: $TODAY
tags: [test]
sessions: []
---

# Transaction Page
EOF
python3 -c 'import json,sys; print(json.dumps({"actor":"test","pages":[{"op":"create","path":"wiki/concepts/Transaction Page.md","content":open(sys.argv[1]).read()}],"log_entry":"## ['"$TODAY"'] test | Transaction create\n\n- created"}))' "$SANDBOX/.page.md" \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-page-create" "$?" 0
[[ -f "$SANDBOX/wiki/concepts/Transaction Page.md" ]] && ok "vw-page-created" || bad "vw-page-created" "file missing"
expect_grep "vw-page-log-same-transaction" "$SANDBOX/wiki/log.md" "Transaction create"

cp "$SANDBOX/wiki/log.md" "$SANDBOX/.log-before-conflict"
python3 -c 'import json,sys; print(json.dumps({"pages":[{"op":"create","path":"wiki/concepts/Transaction Page.md","content":open(sys.argv[1]).read()}],"log_entry":"## ['"$TODAY"'] test | MustNotLand"}))' "$SANDBOX/.page.md" \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-create-conflict" "$?" 4
cmp -s "$SANDBOX/wiki/log.md" "$SANDBOX/.log-before-conflict" && ok "vw-conflict-no-partial-log" || bad "vw-conflict-no-partial-log" "log changed"

sed 's/# Transaction Page/# Transaction Page Updated/' "$SANDBOX/.page.md" > "$SANDBOX/.page-updated.md"
python3 -c 'import json,sys; print(json.dumps({"pages":[{"op":"update","path":"wiki/concepts/Transaction Page.md","expected_sha256":"0"*64,"content":open(sys.argv[1]).read()}]}))' "$SANDBOX/.page-updated.md" \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-update-stale-sha" "$?" 4
hash=$(shasum -a 256 "$SANDBOX/wiki/concepts/Transaction Page.md" | awk '{print $1}')
python3 -c 'import json,sys; print(json.dumps({"pages":[{"op":"update","path":"wiki/concepts/Transaction Page.md","expected_sha256":sys.argv[2],"content":open(sys.argv[1]).read()}]}))' "$SANDBOX/.page-updated.md" "$hash" \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-update-matching-sha" "$?" 0
expect_grep "vw-update-landed" "$SANDBOX/wiki/concepts/Transaction Page.md" "Page Updated"

hash=$(shasum -a 256 "$SANDBOX/wiki/concepts/Transaction Page.md" | awk '{print $1}')
python3 -c 'import json,sys; print(json.dumps({"moves":[{"from":"wiki/concepts/Transaction Page.md","to":"wiki/concepts/Transaction Page Renamed.md","expected_sha256":sys.argv[1]}]}))' "$hash" \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-page-move" "$?" 0
[[ ! -e "$SANDBOX/wiki/concepts/Transaction Page.md" && -f "$SANDBOX/wiki/concepts/Transaction Page Renamed.md" ]] \
  && ok "vw-page-moved" || bad "vw-page-moved" "source/target state is wrong"
echo '{"moves":[{"from":"wiki/concepts/Transaction Page Renamed.md","to":"wiki/concepts/Stale Move.md","expected_sha256":"0000000000000000000000000000000000000000000000000000000000000000"}]}' \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-page-move-stale-sha" "$?" 4

cat > "$SANDBOX/.source.md" <<EOF
---
type: source
title: "Provenance Source"
address: c-000003
status: developing
created: $TODAY
updated: $TODAY
tags: [source]
sessions: []
---

# Provenance Source
EOF
python3 -c 'import json,sys; print(json.dumps({"pages":[{"op":"create","path":"wiki/sources/Provenance Source.md","content":open(sys.argv[1]).read()}]}))' "$SANDBOX/.source.md" \
  | "$VW" --dry-run >/dev/null 2>"$SANDBOX/.err"
expect_exit "vw-source-provenance-required" "$?" 3
expect_grep "vw-source-provenance-guidance" "$SANDBOX/.err" "source_class"
python3 - "$SANDBOX/.source.md" <<'PY'
import sys
p=sys.argv[1]; t=open(p).read(); t=t.replace("status: developing", "source_class: official\nverified_at: 2026-07-10\ncontent_sha256: " + "a"*64 + "\nstatus: developing")
open(p,"w").write(t)
PY
python3 -c 'import json,sys; print(json.dumps({"pages":[{"op":"create","path":"wiki/sources/Provenance Source.md","content":open(sys.argv[1]).read()}]}))' "$SANDBOX/.source.md" \
  | "$VW" --dry-run >/dev/null 2>"$SANDBOX/.err"
expect_exit "vw-source-provenance-valid" "$?" 0

mkdir -p "$SANDBOX/.raw"
printf '{"address_map":{},"sources":{}}\n' > "$SANDBOX/.raw/.manifest.json"
manifest_hash=$(shasum -a 256 "$SANDBOX/.raw/.manifest.json" | awk '{print $1}')
python3 -c 'import json,sys; print(json.dumps({"manifest_update":{"path":".raw/.manifest.json","expected_sha256":sys.argv[1],"merge":{"address_map":{"wiki/concepts/Transaction Page.md":"c-000001"}}}}))' "$manifest_hash" \
  | "$VW" >/dev/null 2>&1
expect_exit "vw-manifest-merge" "$?" 0
expect_grep "vw-manifest-merged" "$SANDBOX/.raw/.manifest.json" "Transaction Page.md"

# Exercise the real crash window: the subprocess durably writes a journal and
# then dies before the first destination write. A fresh process must roll the
# complete page + log transaction forward.
cat > "$SANDBOX/.crash-page.md" <<EOF
---
type: concept
title: "Crash Recovery"
address: c-000002
status: developing
created: $TODAY
updated: $TODAY
tags: [test]
sessions: []
---

# Crash Recovery

recovered by journal
EOF
python3 - "$SANDBOX/.crash-page.md" "$SANDBOX/.crash-payload.json" "$TODAY" \
  "$SANDBOX/wiki/concepts/Transaction Page Renamed.md" <<'PY'
import hashlib
import json
import sys

page_file, payload_file, today, move_source = sys.argv[1:]
payload = {
    "actor": "crash-test",
    "pages": [{
        "op": "create",
        "path": "wiki/concepts/Crash Recovery.md",
        "content": open(page_file, encoding="utf-8").read(),
    }],
    "moves": [{
        "from": "wiki/concepts/Transaction Page Renamed.md",
        "to": "wiki/concepts/Transaction Page Recovered.md",
        "expected_sha256": hashlib.sha256(open(move_source, "rb").read()).hexdigest(),
    }],
    "log_entry": f"## [{today}] test | Crash recovery\n\n- recovered transaction",
}
open(payload_file, "w", encoding="utf-8").write(json.dumps(payload))
PY
python3 - "$VW" "$SANDBOX/.crash-payload.json" <<'PY'
import importlib.util
import io
import os
import sys
from pathlib import Path

script, payload_file = map(Path, sys.argv[1:])
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location("vault_write_crash_test", script)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
original_atomic_write = module.atomic_write

def crash_after_journal(path, text):
    if path == module.JOURNAL_FILE:
        return original_atomic_write(path, text)
    os._exit(99)

module.atomic_write = crash_after_journal
sys.stdin = io.StringIO(payload_file.read_text(encoding="utf-8"))
raise SystemExit(module.main([]))
PY
expect_exit "vw-crash-after-journal" "$?" 99
[[ -e "$SANDBOX/.vault-meta/.vault-write-journal.json" ]] && ok "vw-crash-leaves-journal" || bad "vw-crash-leaves-journal" "journal missing"
"$VW" --recover >/dev/null 2>&1
expect_exit "vw-roll-forward-exit" "$?" 0
expect_grep "vw-roll-forward-content" "$SANDBOX/wiki/concepts/Crash Recovery.md" "recovered by journal"
expect_grep "vw-roll-forward-log" "$SANDBOX/wiki/log.md" "test | Crash recovery"
[[ ! -e "$SANDBOX/wiki/concepts/Transaction Page Renamed.md" && -f "$SANDBOX/wiki/concepts/Transaction Page Recovered.md" ]] \
  && ok "vw-roll-forward-move" || bad "vw-roll-forward-move" "move did not roll forward"
[[ ! -e "$SANDBOX/.vault-meta/.vault-write-journal.json" ]] && ok "vw-roll-forward-clears-journal" || bad "vw-roll-forward-clears-journal" "journal remains"
python3 - "$SANDBOX/.vault-meta/pipeline-events.jsonl" > "$SANDBOX/.event-check" <<'PY'
import json, sys
events = [json.loads(line) for line in open(sys.argv[1])]
assert any(e["op"] == "vault-write" and e["actor"] == "test" and "wiki/concepts/Transaction Page.md" in e["paths"] for e in events)
assert any(e["op"] == "vault-recover" and e["counts"]["writes"] == 4 for e in events)
assert all(not ({"prompt", "query", "content", "command", "snippet", "reason"} & set(e)) for e in events)
print("EVENTS_OK")
PY
expect_grep "vw-content-free-events" "$SANDBOX/.event-check" "EVENTS_OK"

# ---------- vault-write: plan_close ----------
echo "== vault-write: plan_close =="

write_plan "pending-plan" "pending" "$TODAY" 'sessions:
  - id: planner-session
    date: 2026-07-01' "Plan body."
write_plan "executed-plan" "executed" "2026-05-01" 'sessions: []' "Old body."
write_plan "marker-plan" "pending" "$TODAY" 'sessions: []' "Marker body."
write_plan "guarded-plan" "pending" "$TODAY" 'sessions: []' "Guarded body."

echo '{"plan_close": {"file": "wiki/hot.md", "result_link": "[[X]]"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-outside-plans" "$?" 2

echo '{"plan_close": {"file": "wiki/plans/nope.md", "result_link": "[[X]]"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-missing-file" "$?" 2

echo '{"plan_close": {"file": "wiki/plans/executed-plan.md", "result_link": "[[X]]"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-not-pending" "$?" 2

echo '{"plan_close": {"file": "wiki/plans/pending-plan.md"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-no-result-link" "$?" 2

echo '{"plan_close": {"file": "wiki/plans/pending-plan.md", "result_link": "[[Result Page]]", "exec_session": "exec-id-42"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-close-ok" "$?" 0
expect_grep "pc-status-executed"  "$SANDBOX/wiki/plans/pending-plan.md" "status: executed"
expect_grep "pc-exec-session"     "$SANDBOX/wiki/plans/pending-plan.md" "- id: exec-id-42"
expect_grep "pc-result-line"      "$SANDBOX/wiki/plans/pending-plan.md" "Результат: [[Result Page]] (reaped $TODAY)"
expect_grep "pc-updated-bumped"   "$SANDBOX/wiki/plans/pending-plan.md" "updated: $TODAY"
expect_grep "pc-planner-kept"     "$SANDBOX/wiki/plans/pending-plan.md" "- id: planner-session"

echo '{"plan_close": {"file": "wiki/plans/pending-plan.md", "result_link": "[[X]]"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-reclose-fails" "$?" 2

echo '{"plan_close": {"file": "wiki/plans/marker-plan.md", "result_link": "[[Y]]", "exec_session": "exec-id-43"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-marker-ok" "$?" 0
expect_grep   "pc-marker-converted" "$SANDBOX/wiki/plans/marker-plan.md" "- id: exec-id-43"
expect_nogrep "pc-marker-no-empty"  "$SANDBOX/wiki/plans/marker-plan.md" "sessions: []"

GUARDED_SHA="$(shasum -a 256 "$SANDBOX/wiki/plans/guarded-plan.md" | awk '{print $1}')"
echo '{"plan_close": {"file": "wiki/plans/guarded-plan.md", "result_link": "[[Guarded]]", "expected_sha256": "0000000000000000000000000000000000000000000000000000000000000000"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-stale-approved-hash" "$?" 4
expect_grep "pc-stale-hash-no-close" "$SANDBOX/wiki/plans/guarded-plan.md" "status: pending"
echo '{"plan_close": {"file": "wiki/plans/guarded-plan.md", "result_link": "[[Guarded]]", "expected_sha256": "'"$GUARDED_SHA"'"}}' | "$VW" >/dev/null 2>&1
expect_exit "pc-approved-hash-close" "$?" 0

# atomicity: valid log_entry + broken plan_close => exit 2, log untouched
echo '{"log_entry": "## ['"$TODAY"'] test | AtomicProbe\n\n- must not land",
      "plan_close": {"file": "wiki/plans/nope.md", "result_link": "[[X]]"}}' | "$VW" >/dev/null 2>&1
expect_exit "vw-atomic-exit" "$?" 2
expect_nogrep "vw-atomic-log-untouched" "$SANDBOX/wiki/log.md" "AtomicProbe"

# ---------- parse-wiki-summary ----------
echo "== parse-wiki-summary =="

out=$(printf '## Wiki Summary\n\ntype: decision\ntitle: Test Title\nsession: abc-123\n\nBody [[Link]].\n' | "$PW" 2>/dev/null)
expect_exit "pws-valid" "$?" 0
printf '%s' "$out" > "$SANDBOX/.json"
expect_grep "pws-valid-json" "$SANDBOX/.json" '"type": "decision"'

printf '## Wiki Summary\n\ntype: wat\ntitle: X\n\nbody\n' | "$PW" >/dev/null 2>&1
expect_exit "pws-bad-type" "$?" 2

printf '## Wiki Summary\n\ntype: session\ntitle: Y\n\nbody\n' | "$PW" >/dev/null 2>"$SANDBOX/.err"
expect_exit "pws-no-session-ok" "$?" 0
expect_grep "pws-no-session-warn" "$SANDBOX/.err" "no session"

printf 'garbage only\n' | "$PW" >/dev/null 2>&1
expect_exit "pws-no-block" "$?" 2

printf '## Wiki Summary\n\ntype: runbook\ntitle: <Note Title>\n\nbody\n' | "$PW" >/dev/null 2>&1
expect_exit "pws-placeholder-title" "$?" 2

out=$(printf '## Wiki Summary\n\ntype: session\ntitle: First\n\nold\n\n## Wiki Summary\n\ntype: decision\ntitle: Second\nsession: s2\n\nnew\n' | "$PW" 2>/dev/null)
printf '%s' "$out" > "$SANDBOX/.json"
expect_grep "pws-last-block-wins" "$SANDBOX/.json" '"title": "Second"'

cat > "$SANDBOX/.task-summary.json" <<'JSON'
{"schema_version":1,"type":"runbook","title":"Typed Summary","session":"exec-1","body":"Steps and evidence."}
JSON
out=$("$PW" --json-file "$SANDBOX/.task-summary.json" 2>"$SANDBOX/.err")
expect_exit "pws-canonical-json" "$?" 0
printf '%s' "$out" > "$SANDBOX/.json"
expect_grep "pws-canonical-version" "$SANDBOX/.json" '"schema_version": 1'
out=$("$PW" --json-file "$SANDBOX/.task-summary.json" --render-markdown 2>"$SANDBOX/.err")
expect_exit "pws-render-markdown" "$?" 0
printf '%s' "$out" > "$SANDBOX/.rendered"
expect_grep "pws-rendered-marker" "$SANDBOX/.rendered" '## Wiki Summary'
expect_grep "pws-rendered-title" "$SANDBOX/.rendered" 'title: Typed Summary'

echo '{"schema_version":2,"type":"session","title":"Bad","session":"x","body":"body"}' > "$SANDBOX/.task-summary.json"
"$PW" --json-file "$SANDBOX/.task-summary.json" >/dev/null 2>"$SANDBOX/.err"
expect_exit "pws-canonical-bad-version" "$?" 2
echo '{"type":"session","title":"Missing Version","session":"x","body":"body"}' > "$SANDBOX/.task-summary.json"
"$PW" --json-file "$SANDBOX/.task-summary.json" >/dev/null 2>"$SANDBOX/.err"
expect_exit "pws-canonical-missing-version" "$?" 2

# ---------- validate-vault: plans / questions / sessions ----------
echo "== validate-vault: plans / questions / sessions =="

write_plan "zoo-plan" "draft" "$TODAY" 'sessions: []' "Zoo."
out=$("$VV" --checks plans 2>&1); rc=$?
expect_exit "vv-plans-zoo-exit" "$rc" 1
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "vv-plans-zoo-msg" "$SANDBOX/.out" "status outside"
rm "$SANDBOX/wiki/plans/zoo-plan.md"

write_plan "noresult-plan" "executed" "$TODAY" 'sessions: []' "No result line."
out=$("$VV" --checks plans 2>&1); rc=$?
expect_exit "vv-plans-noresult-exit" "$rc" 1
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "vv-plans-noresult-msg" "$SANDBOX/.out" "Результат"
rm "$SANDBOX/wiki/plans/noresult-plan.md"

write_plan "stale-plan" "pending" "2026-01-01" 'sessions: []' "Stale."
out=$("$VV" --checks plans 2>&1); rc=$?
expect_exit "vv-plans-stale-warn-exit" "$rc" 0
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "vv-plans-stale-warn-msg" "$SANDBOX/.out" "older than"
rm "$SANDBOX/wiki/plans/stale-plan.md"

out=$("$VV" --checks plans 2>&1); rc=$?
expect_exit "vv-plans-green" "$rc" 0

printf -- '---\ntype: question\nstatus: developing\ncreated: 2026-01-01\nupdated: 2026-01-01\ntags: [q]\nsessions: []\n---\n\n# Q\n' \
  > "$SANDBOX/wiki/questions/badq.md"
out=$("$VV" --checks questions 2>&1); rc=$?
expect_exit "vv-questions-bad-exit" "$rc" 1
sed -i '' 's/^status: developing/status: open/' "$SANDBOX/wiki/questions/badq.md"
out=$("$VV" --checks questions 2>&1); rc=$?
expect_exit "vv-questions-green" "$rc" 0

printf -- '---\ntype: concept\nstatus: draft\ncreated: 2026-01-01\nupdated: 2026-01-01\ntags: [c]\n---\n\n# NoSessions\n' \
  > "$SANDBOX/wiki/nosessions.md"
out=$("$VV" --checks frontmatter 2>&1); rc=$?
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "vv-sessions-warn" "$SANDBOX/.out" "missing sessions"
expect_exit "vv-sessions-warn-not-fail" "$rc" 0
printf -- '---\ntype: concept\nstatus: draft\ncreated: 2026-01-01\nupdated: 2026-01-01\ntags: [c]\nsessions: []\n---\n\n# NoSessions\n' \
  > "$SANDBOX/wiki/nosessions.md"
out=$("$VV" --checks frontmatter 2>&1)
printf '%s' "$out" > "$SANDBOX/.out"
expect_nogrep "vv-sessions-clean" "$SANDBOX/.out" "missing sessions"

# ---------- tag-search: prefilter over tag-index.json ----------

cat > "$SANDBOX/.vault-meta/tag-index.json" <<'EOF'
{
  "opensearch": ["wiki/a.md", "wiki/b.md"],
  "fluent-bit": ["wiki/b.md"],
  "ISM-Policy": ["wiki/c.md"]
}
EOF

out=$(python3 "$TS" "opensearch" 2>&1); rc=$?
expect_exit "ts-exact-exit0" "$rc" 0
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "ts-exact-match" "$SANDBOX/.out" "wiki/a.md"

out=$(python3 "$TS" "opensearch fluent bit" 2>&1)
first=$(printf '%s\n' "$out" | sed -n '2p')
printf '%s' "$first" | grep -qF "wiki/b.md" && ok "ts-multi-tag-ranks-higher" \
  || bad "ts-multi-tag-ranks-higher" "first hit is not b.md: $first"

out=$(python3 "$TS" "fluent bit" 2>&1); rc=$?
expect_exit "ts-bigram-exit0" "$rc" 0
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "ts-hyphen-bigram" "$SANDBOX/.out" "wiki/b.md"

out=$(python3 "$TS" "ism policy" 2>&1)
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "ts-case-insensitive" "$SANDBOX/.out" "wiki/c.md"

out=$(python3 "$TS" "kubernetes" 2>&1); rc=$?
expect_exit "ts-no-match-exit0" "$rc" 0
printf '%s' "$out" > "$SANDBOX/.out"
expect_grep "ts-no-match-msg" "$SANDBOX/.out" "0 matching tags"

out=$(python3 "$TS" 2>&1); rc=$?
expect_exit "ts-usage-exit2" "$rc" 2

echo 'garbage{' > "$SANDBOX/.vault-meta/tag-index.json"
out=$(python3 "$TS" "opensearch" 2>&1); rc=$?
expect_exit "ts-corrupt-exit3" "$rc" 3

rm "$SANDBOX/.vault-meta/tag-index.json"
out=$(python3 "$TS" "opensearch" 2>&1); rc=$?
expect_exit "ts-no-index-exit3" "$rc" 3

# ---------- reindex: atomic writes ----------

mkdir -p "$SANDBOX/wiki/reindex-fixture"
cat > "$SANDBOX/wiki/reindex-fixture/Page.md" <<'EOF'
---
type: concept
title: "Deterministic Listing Date"
address: c-000099
status: developing
created: 2026-02-01
updated: 2026-02-03
tags: [test]
sessions: []
---

# Deterministic Listing Date
EOF
python3 "$SANDBOX/scripts/reindex.py" --quiet --folder-indexes; rc=$?
expect_exit "ri-run-ok" "$rc" 0
[[ -s "$SANDBOX/.vault-meta/index.jsonl" ]] && ok "ri-index-written" || bad "ri-index-written" "index.jsonl missing/empty"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SANDBOX/.vault-meta/tag-index.json" 2>/dev/null \
  && ok "ri-tagindex-valid-json" || bad "ri-tagindex-valid-json" "tag-index.json not valid JSON"
expect_grep "ri-folder-date-from-content" "$SANDBOX/wiki/reindex-fixture/_index.md" "_1 pages, updated 2026-02-03_"
python3 - "$SANDBOX/scripts/reindex.py" "$SANDBOX/wiki/reindex-fixture/_index.md" <<'PY'
import importlib.util
import sys
from pathlib import Path

script, index_file = map(Path, sys.argv[1:])
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location("reindex_clock_test", script)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
pages, _ = module.build_indexes()
before = index_file.read_text(encoding="utf-8")

class FutureClock:
    @staticmethod
    def strftime(_format):
        return "2099-12-31"

module.time = FutureClock
written = module.write_folder_indexes(pages)
after = index_file.read_text(encoding="utf-8")
assert written == 0
assert after == before
PY
expect_exit "ri-folder-no-wall-clock-churn" "$?" 0
# atomic_write must never leave *.tmp.* strays (covers vault-write runs above too)
leftovers=$(find "$SANDBOX" -name '*.tmp.*' | wc -l | tr -d ' ')
[[ "$leftovers" == "0" ]] && ok "atomic-no-tmp-leftovers" || bad "atomic-no-tmp-leftovers" "$leftovers tmp files left"

# ---------- summary ----------
printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf '\nFailures:\n'
  for f in "${failures[@]}"; do printf '  - %s\n' "$f"; done
  exit 1
fi
