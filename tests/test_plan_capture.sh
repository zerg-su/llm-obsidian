#!/usr/bin/env bash
# Hermetic proof that ExitPlanMode page + log use the transactional writer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d /tmp/plan-capture-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/.claude/hooks" "$TMP/scripts" "$TMP/wiki" "$TMP/.vault-meta"
cp "$ROOT/.claude/hooks/plan-capture.sh" "$TMP/.claude/hooks/"
cp "$ROOT/scripts/allocate-address.sh" "$ROOT/scripts/allocate-address.py" \
   "$ROOT/scripts/vault-write.py" "$ROOT/scripts/vault_schema.py" \
   "$ROOT/scripts/plan_lifecycle.py" \
   "$ROOT/scripts/pipeline_events.py" "$TMP/scripts/"
chmod +x "$TMP/.claude/hooks/plan-capture.sh" "$TMP/scripts/"*.py "$TMP/scripts/"*.sh

cat > "$TMP/wiki/log.md" <<'EOF'
---
type: meta
title: "Log"
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
sessions: []
---

# Log

## [2026-01-01] seed | Seed

Seed.
EOF

payload='{"tool_name":"ExitPlanMode","permission_decision":"allow","tool_input":{"plan":"# Transactional Capture\n\n1. Do the thing."},"session_id":"session-test","cwd":"/tmp/project"}'
printf '%s' "$payload" | CLAUDE_PROJECT_DIR="$TMP" LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 \
  "$TMP/.claude/hooks/plan-capture.sh"

plan_count=$(find "$TMP/wiki/plans" -name '*.md' | wc -l | tr -d '[:space:]')
[[ "$plan_count" == 1 ]] || { echo "FAIL expected one plan, got $plan_count"; exit 1; }
plan=$(find "$TMP/wiki/plans" -name '*.md' -print -quit)
grep -qF 'sessions:' "$plan"
grep -qF 'id: session-test' "$plan"
grep -qF 'save-plan | transactional-capture' "$TMP/wiki/log.md"
[[ ! -e "$TMP/.vault-meta/.vault-write-journal.json" ]]

denied='{"tool_name":"ExitPlanMode","permission_decision":"deny","tool_input":{"plan":"# Must Not Land"},"session_id":"session-test"}'
printf '%s' "$denied" | CLAUDE_PROJECT_DIR="$TMP" LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 \
  "$TMP/.claude/hooks/plan-capture.sh"
after=$(find "$TMP/wiki/plans" -name '*.md' | wc -l | tr -d '[:space:]')
[[ "$after" == 1 ]] || { echo "FAIL denied plan landed"; exit 1; }

# Writer failures must remain non-blocking while becoming safely diagnosable.
counter_before=$(cat "$TMP/.vault-meta/address-counter.txt")
cp "$TMP/wiki/log.md" "$TMP/log-before-failure.md"
mv "$TMP/scripts/vault-write.py" "$TMP/scripts/vault-write.real.py"
cat > "$TMP/scripts/vault-write.py" <<'EOF'
#!/usr/bin/env bash
exit 4
EOF
chmod +x "$TMP/scripts/vault-write.py"

failed='{"tool_name":"ExitPlanMode","permission_decision":"allow","tool_input":{"plan":"# Private Failure Body\n\nThis text must not enter diagnostics."},"session_id":"session-failure","cwd":"/tmp/private-project"}'
printf '%s' "$failed" | CLAUDE_PROJECT_DIR="$TMP" LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 \
  "$TMP/.claude/hooks/plan-capture.sh" 2> "$TMP/failure.stderr"
failure_rc=$?
[[ "$failure_rc" == 0 ]] || { echo "FAIL hook propagated writer failure: $failure_rc"; exit 1; }
failed_count=$(find "$TMP/wiki/plans" -name '*.md' | wc -l | tr -d '[:space:]')
[[ "$failed_count" == 1 ]] || { echo "FAIL failed transaction created a page"; exit 1; }
cmp -s "$TMP/wiki/log.md" "$TMP/log-before-failure.md" || { echo "FAIL failed transaction changed log"; exit 1; }
counter_after=$(cat "$TMP/.vault-meta/address-counter.txt")
[[ "$counter_after" == $((counter_before + 1)) ]] || { echo "FAIL expected monotonic reserved address"; exit 1; }
grep -qF 'PLAN_CAPTURE_FAILED: writer exit 4 (conflict)' "$TMP/failure.stderr"
grep -qF 'exit_code=4' "$TMP/.vault-meta/plan-capture-last-error.log"
grep -qF 'category=conflict' "$TMP/.vault-meta/plan-capture-last-error.log"
if grep -qF 'Private Failure Body' "$TMP/.vault-meta/plan-capture-last-error.log"; then
  echo "FAIL marker leaked plan content"
  exit 1
fi

python3 - "$TMP/.vault-meta/pipeline-events.jsonl" <<'PY'
import json
import sys

events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
event = next(e for e in reversed(events) if e["op"] == "plan-capture")
assert event["status"] == "error"
assert event["actor"] == "plan-capture-hook"
assert event["counts"] == {"exit_code": 4}
assert len(event["paths"]) == 1 and event["paths"][0].startswith("wiki/plans/")
assert not ({"prompt", "query", "content", "command", "snippet", "reason"} & set(event))
PY

# A later successful capture clears the last-error marker.
mv "$TMP/scripts/vault-write.real.py" "$TMP/scripts/vault-write.py"
recovered='{"tool_name":"ExitPlanMode","permission_decision":"allow","tool_input":{"plan":"# Capture Recovered\n\n1. Continue."},"session_id":"session-recovered"}'
printf '%s' "$recovered" | CLAUDE_PROJECT_DIR="$TMP" LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 \
  "$TMP/.claude/hooks/plan-capture.sh"
[[ ! -e "$TMP/.vault-meta/plan-capture-last-error.log" ]] || { echo "FAIL success did not clear marker"; exit 1; }
final_count=$(find "$TMP/wiki/plans" -name '*.md' | wc -l | tr -d '[:space:]')
[[ "$final_count" == 2 ]] || { echo "FAIL recovery capture did not land"; exit 1; }

echo "Passed: transactional plan capture"
