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
#   - allocate-address.sh failure → file written without address (lint will flag)
#   - No git interaction — vault picks up via next session's autocommit/manual commit

set -u

VAULT="${CLAUDE_PROJECT_DIR:-$PWD}"
PLANS_DIR="$VAULT/wiki/plans"
ALLOC="$VAULT/scripts/allocate-address.sh"

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

# Atomic write via temp file in same dir.
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

  # Append a log entry pointing back, so `wiki-fold` and friends see it.
  log_file="$VAULT/wiki/log.md"
  if [ -f "$log_file" ]; then
    # Insert under header (line ~24), atomic via temp.
    log_tmp="$log_file.tmp.$$"
    awk -v ts="$date_today" -v slug="$slug" -v session="$session" -v addr="$addr" '
      BEGIN { inserted = 0 }
      /^## \[/ && inserted == 0 {
        addr_str = (addr == "") ? "no-address" : addr
        print "## [" ts "] save-plan | " slug " (auto-captured ExitPlanMode)"
        print ""
        print "- session: " session
        print "- address: " addr_str
        print ""
        inserted = 1
      }
      { print }
    ' "$log_file" > "$log_tmp" 2>/dev/null && mv "$log_tmp" "$log_file" 2>/dev/null || rm -f "$log_tmp"
  fi
} > "$tmp" && mv "$tmp" "$target"

exit 0
