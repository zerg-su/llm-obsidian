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
   "$REPO_ROOT/scripts/validate-vault.py" \
   "$REPO_ROOT/scripts/reindex.py" \
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

echo '{"log_entry": "no heading format"}' | "$VW" >/dev/null 2>&1
expect_exit "vw-log-bad-format" "$?" 2

long_bullet=$(printf 'x%.0s' {1..200})
echo '{"hot_bullet": "'"$long_bullet"'"}' | "$VW" >/dev/null 2>"$SANDBOX/.err"
expect_exit "vw-bullet-truncate-exit" "$?" 0
expect_grep "vw-bullet-truncate-warn" "$SANDBOX/.err" "truncated"

for i in $(seq 1 16); do
  echo '{"hot_bullet": "2026-01-02: [[P'"$i"']] — bullet '"$i"'"}' | "$VW" >/dev/null 2>"$SANDBOX/.err"
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

# ---------- vault-write: plan_close ----------
echo "== vault-write: plan_close =="

write_plan "pending-plan" "pending" "$TODAY" 'sessions:
  - id: planner-session
    date: 2026-07-01' "Plan body."
write_plan "executed-plan" "executed" "2026-05-01" 'sessions: []' "Old body."
write_plan "marker-plan" "pending" "$TODAY" 'sessions: []' "Marker body."

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

python3 "$SANDBOX/scripts/reindex.py" --quiet; rc=$?
expect_exit "ri-run-ok" "$rc" 0
[[ -s "$SANDBOX/.vault-meta/index.jsonl" ]] && ok "ri-index-written" || bad "ri-index-written" "index.jsonl missing/empty"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SANDBOX/.vault-meta/tag-index.json" 2>/dev/null \
  && ok "ri-tagindex-valid-json" || bad "ri-tagindex-valid-json" "tag-index.json not valid JSON"
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
