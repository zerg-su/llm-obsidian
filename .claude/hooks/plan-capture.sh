#!/usr/bin/env bash
# Plan capture hook: files every approved ExitPlanMode plan into wiki/plans/.
# Registered by the plugin (hooks/hooks.json, PostToolUse matcher ExitPlanMode),
# so it fires only for sessions running inside this vault. To capture plans
# from OTHER projects into this vault, copy this script to ~/.claude/hooks/,
# hardcode VAULT below and register it in ~/.claude/settings.json instead.
#
# Behavior:
#   - Reads Claude Code hook JSON from stdin
#   - Acts only when tool_name == ExitPlanMode (matcher should already filter, but defensive)
#   - Writes wiki/plans/<YYYY-MM-DD-HHMMSS>-<slug>.md with frontmatter:
#       type, title, address (DragonScale), session_id, transcript, source_cwd,
#       status: pending, created, tags
#   - Body is the raw plan markdown verbatim
#
# Robustness:
#   - set -u, never breaks Claude (exit 0 always at end)
#   - vault missing → silent no-op
#   - allocate-address.sh failure → writer rejects post-rollout pages and the
#     safe last-error marker explains the failed capture
#   - writer failure → stderr summary + content-free telemetry; hook still exits 0
#   - No git interaction — vault picks up via next session's autocommit/manual commit

set -u

if [ "${LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS:-}" != "1" ]; then
  # Skip in Codex — the Claude hook layer is a no-op there. Shared detector with a
  # self-contained env fallback so a missing script never disables the guard.
  _dr="$(dirname -- "$0")/../../scripts/detect-runtime.sh"
  if [ -x "$_dr" ]; then
    [ "$("$_dr")" = codex ] && exit 0
  elif [ -n "${CODEX_THREAD_ID:-}${CODEX_CI:-}${CODEX_MANAGED_BY_NPM:-}" ]; then
    exit 0
  fi
fi

VAULT="${CLAUDE_PROJECT_DIR:-$PWD}"
PLANS_DIR="$VAULT/wiki/plans"
ALLOC="$VAULT/scripts/allocate-address.sh"
WRITER="$VAULT/scripts/vault-write.py"
META_DIR="$VAULT/.vault-meta"
ERROR_MARKER="$META_DIR/plan-capture-last-error.log"

record_writer_failure() {
  failure_code="$1"
  failure_target="$2"
  failure_session="$3"
  case "$failure_code" in
    1) failure_category="lock-or-io" ;;
    2) failure_category="cap-violation" ;;
    3) failure_category="bad-payload" ;;
    4) failure_category="conflict" ;;
    *) failure_category="unknown" ;;
  esac

  mkdir -p "$META_DIR" 2>/dev/null || true
  marker_tmp="$ERROR_MARKER.tmp.$$"
  {
    printf 'timestamp=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'target=%s\n' "$failure_target"
    printf 'exit_code=%s\n' "$failure_code"
    printf 'category=%s\n' "$failure_category"
  } > "$marker_tmp" 2>/dev/null && mv -f "$marker_tmp" "$ERROR_MARKER" 2>/dev/null
  rm -f "$marker_tmp" 2>/dev/null || true

  # The event schema accepts identifiers, relative vault paths, and numeric
  # counters only. Never forward writer stderr or plan content here.
  PYTHONPATH="$VAULT/scripts${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$VAULT" "$failure_target" "$failure_session" "$failure_code" <<'PY' \
      >/dev/null 2>&1 || true
import sys
from pathlib import Path

from pipeline_events import emit_event

root, target, session, exit_code = sys.argv[1:]
emit_event(
    "plan-capture",
    actor="plan-capture-hook",
    session=session,
    paths=[target],
    counts={"exit_code": int(exit_code)},
    status="error",
    root=Path(root),
)
PY

  printf 'PLAN_CAPTURE_FAILED: writer exit %s (%s); see .vault-meta/plan-capture-last-error.log\n' \
    "$failure_code" "$failure_category" >&2
}

# Not a vault (no wiki/) → silent exit
[ -d "$VAULT/wiki" ] || exit 0

payload=$(cat 2>/dev/null || true)
[ -z "$payload" ] && exit 0

# Defensive: only handle ExitPlanMode (matcher should restrict, but be safe)
tool=$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null)
[ "$tool" = "ExitPlanMode" ] || exit 0

# Only capture APPROVED plans. ExitPlanMode is itself an approval gate — the tool
# completes successfully only when user clicks "Accept plan". But Claude Code may
# still fire PostToolUse on denials (depends on version). Defensive filter:
#   - if hook payload exposes a permission_decision field, require "allow"
#   - if tool_response is a string containing common denial keywords, skip
#   - if tool_response is an object with .approved == false, skip
decision=$(printf '%s' "$payload" | jq -r '.permission_decision // empty' 2>/dev/null)
case "$decision" in
  ""|"allow"|"accept"|"approved") ;;  # proceed
  *) exit 0 ;;                         # any other explicit decision → skip
esac

resp_kind=$(printf '%s' "$payload" | jq -r '.tool_response | type' 2>/dev/null)
case "$resp_kind" in
  "object")
    # Note: `// empty` treats `false` as missing in jq, so query without fallback.
    if printf '%s' "$payload" | jq -e '.tool_response.approved == false' >/dev/null 2>&1; then
      exit 0
    fi
    ;;
  "string")
    resp_text=$(printf '%s' "$payload" | jq -r '.tool_response' 2>/dev/null | tr '[:upper:]' '[:lower:]')
    case "$resp_text" in
      *denied*|*rejected*|*"keep planning"*|*"plan rejected"*|*"user did not approve"*)
        exit 0
        ;;
    esac
    ;;
esac

plan=$(printf '%s' "$payload" | jq -r '.tool_input.plan // empty' 2>/dev/null)
[ -z "$plan" ] && exit 0

session=$(printf '%s' "$payload" | jq -r '.session_id // "unknown"' 2>/dev/null)
transcript=$(printf '%s' "$payload" | jq -r '.transcript_path // empty' 2>/dev/null)
cwd=$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null)

# Derive slug from first markdown heading or first non-empty line
title_raw=$(printf '%s\n' "$plan" \
  | awk 'NF{ sub(/^#+[[:space:]]*/, ""); print; exit }')
[ -z "$title_raw" ] && title_raw="Untitled plan"

# Transliterate cyrillic → latin (simplified GOST), then slug-clean.
# Multi-char mappings (ё, щ, ю, я, ж, х, ц, ч, ш, й) MUST precede single-char.
slug=$(printf '%s' "$title_raw" \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E '
      s/ё/yo/g; s/щ/shch/g; s/ю/yu/g; s/я/ya/g; s/ж/zh/g; s/х/kh/g;
      s/ц/ts/g; s/ч/ch/g; s/ш/sh/g; s/й/y/g;
      s/а/a/g; s/б/b/g; s/в/v/g; s/г/g/g; s/д/d/g; s/е/e/g;
      s/з/z/g; s/и/i/g; s/к/k/g; s/л/l/g; s/м/m/g; s/н/n/g;
      s/о/o/g; s/п/p/g; s/р/r/g; s/с/s/g; s/т/t/g; s/у/u/g;
      s/ф/f/g; s/ы/y/g; s/э/e/g;
      s/[ъь]//g;
    ' \
  | sed -E 's/[^a-z0-9 ]+/ /g; s/[[:space:]]+/-/g; s/-+/-/g; s/^-+//; s/-+$//' \
  | cut -c1-60)
[ -z "$slug" ] && slug="untitled-plan"

ts=$(date '+%Y-%m-%d-%H%M%S')
date_today=$(date '+%Y-%m-%d')

mkdir -p "$PLANS_DIR" || exit 0
target="$PLANS_DIR/$ts-$slug.md"

# Avoid clobber on the rare same-second collision
n=1
while [ -e "$target" ]; do
  target="$PLANS_DIR/$ts-$slug-$n.md"
  n=$((n+1))
done

addr=""
if [ -x "$ALLOC" ]; then
  addr=$("$ALLOC" 2>/dev/null || echo "")
fi

# Escape title for YAML (double-quotes around it; escape internal ")
yaml_title=${title_raw//\"/\\\"}
yaml_transcript=${transcript//\"/\\\"}
yaml_cwd=${cwd//\"/\\\"}

# Compose the page in a private temp file, then hand page + log to the single
# transactional writer. The hook never mutates wiki files directly.
# Frontmatter format aligns with project convention:
#   - `sessions: [{id, date}]` array (not single `session_id:`)
#   - Preserves legacy `session_id:` field for backwards compatibility
#   - `source_cwd` and `transcript` still recorded for provenance
tmp="$target.tmp.$$"
{
  echo "---"
  echo "type: plan"
  echo "title: \"$yaml_title\""
  [ -n "$addr" ] && echo "address: $addr"
  echo "session_id: $session"
  echo "sessions:"
  echo "  - id: $session"
  echo "    date: $date_today"
  [ -n "$transcript" ] && echo "transcript: \"$yaml_transcript\""
  [ -n "$cwd" ] && echo "source_cwd: \"$yaml_cwd\""
  echo "status: pending"
  echo "created: $date_today"
  echo "updated: $date_today"
  echo "tags:"
  echo "  - plan"
  echo "  - auto-captured"
  echo "---"
  echo
  printf '%s\n' "$plan"
} > "$tmp" || { rm -f "$tmp"; exit 0; }

rel_target=${target#"$VAULT/"}
addr_str=${addr:-no-address}
mkdir -p "$META_DIR" 2>/dev/null || true
payload_tmp="$META_DIR/plan-capture-payload.tmp.$$"
writer_status=3
if python3 - "$tmp" "$rel_target" "$date_today" "$slug" "$session" "$addr_str" <<'PY' > "$payload_tmp"
import json
import sys

content_file, target, today, slug, session, address = sys.argv[1:]
payload = {
    "actor": "plan-capture-hook",
    "session": session,
    "pages": [{
        "op": "create",
        "path": target,
        "content": open(content_file, encoding="utf-8").read(),
    }],
    "log_entry": (
        f"## [{today}] save-plan | {slug} (auto-captured ExitPlanMode)\n\n"
        f"- session: {session}\n- address: {address}"
    ),
}
print(json.dumps(payload, ensure_ascii=False))
PY
then
  if [ -x "$WRITER" ]; then
    "$WRITER" --file "$payload_tmp" >/dev/null 2>/dev/null
    writer_status=$?
  else
    writer_status=1
  fi
fi
rm -f "$tmp" "$payload_tmp"

if [ "$writer_status" -eq 0 ]; then
  rm -f "$ERROR_MARKER" 2>/dev/null || true
else
  record_writer_failure "$writer_status" "$rel_target" "$session"
fi

exit 0
