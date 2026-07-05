#!/usr/bin/env bash
# migrate-claude-plans.sh — one-shot migration of ~/.claude/plans/*.md → wiki/plans/
#
# Modes:
#   ./migrate-claude-plans.sh             # dry-run, prints table only
#   ./migrate-claude-plans.sh --apply     # actually writes files
#
# For each plan file in ~/.claude/plans/:
#   1. Pick a fingerprint line (longest distinctive content)
#   2. grep -lF across all session JSONLs in ~/.claude/projects/*/*.jsonl
#   3. Resolve session_id, source_cwd:
#        - unique match  → session = basename(jsonl), cwd from JSONL record
#        - ambiguous     → pick earliest jsonl by mtime, tag accordingly
#        - no match      → session = unknown-<stem>, tag accordingly
#   4. Derive title (first H1/H2 or first non-empty line) and latin slug
#   5. Allocate DragonScale address (only in --apply mode)
#   6. Write wiki/plans/<file-mtime-date>-<HHMMSS>-<slug>.md with full frontmatter
#
# Source plans are NOT deleted.

set -u

VAULT="$HOME/Projects/Obsidian/claude-obsidian"
PLANS_SRC="$HOME/.claude/plans"
PLANS_DST="$VAULT/wiki/plans"
PROJECTS_DIR="$HOME/.claude/projects"
ALLOC="$VAULT/scripts/allocate-address.sh"

MODE="${1:-dry-run}"
case "$MODE" in
  --apply|apply) APPLY=1 ;;
  *)             APPLY=0 ;;
esac

[ -d "$VAULT" ] || { echo "ERR: vault not found at $VAULT" >&2; exit 2; }
[ -d "$PLANS_SRC" ] || { echo "ERR: source plans dir not found at $PLANS_SRC" >&2; exit 2; }
mkdir -p "$PLANS_DST"

transliterate() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
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
    | cut -c1-60
}

# Pick a single distinctive fingerprint line from the plan file.
pick_fingerprint() {
  local f="$1"
  awk '
    /^[A-Za-zА-Яа-я0-9`"\x27].{60,}/ {
      if (length($0) > maxlen) { maxlen = length($0); best = $0 }
    }
    END { if (best != "") print best }
  ' "$f"
}

# Extract title from first H1/H2 or first non-empty line
pick_title() {
  awk '
    NF {
      sub(/^#+[[:space:]]*/, "")
      print
      exit
    }
  ' "$1"
}

# For a matched session JSONL, pick the first non-null .cwd from records.
resolve_cwd_from_jsonl() {
  local jsonl="$1"
  jq -r 'select(.cwd != null and .cwd != "") | .cwd' "$jsonl" 2>/dev/null | head -1
}

printf "%-50s %-12s %-38s %-50s %s\n" "ORIGINAL" "QUALITY" "SESSION" "CWD" "TARGET FILENAME"
printf "%-50s %-12s %-38s %-50s %s\n" \
  "--------------------------------------------------" \
  "------------" \
  "--------------------------------------" \
  "--------------------------------------------------" \
  "----------------------------------------"

total=0
clean=0
ambig=0
nomatch=0
written=0
skipped=0

shopt -s nullglob 2>/dev/null || true
for src in "$PLANS_SRC"/*.md; do
  total=$((total+1))
  stem=$(basename "$src" .md)

  mtime_epoch=$(stat -f '%m' "$src" 2>/dev/null || stat -c '%Y' "$src" 2>/dev/null)
  date_str=$(date -r "$mtime_epoch" '+%Y-%m-%d')
  time_str=$(date -r "$mtime_epoch" '+%H%M%S')

  fp=$(pick_fingerprint "$src")
  if [ -z "$fp" ]; then
    quality="no-match"
    session="unknown-$stem"
    cwd=""
  else
    matches=$(grep -lF "$fp" "$PROJECTS_DIR"/*/*.jsonl 2>/dev/null || true)
    mcount=$(printf '%s' "$matches" | grep -c '\.jsonl$' || true)
    if [ "$mcount" -eq 0 ]; then
      quality="no-match"
      session="unknown-$stem"
      cwd=""
    elif [ "$mcount" -eq 1 ]; then
      quality="unique"
      jsonl="$matches"
      session=$(basename "$jsonl" .jsonl)
      cwd=$(resolve_cwd_from_jsonl "$jsonl")
      clean=$((clean+1))
    else
      quality="ambig-$mcount"
      jsonl=$(printf '%s\n' "$matches" | xargs -I{} stat -f '%m %N' {} 2>/dev/null | sort -n | head -1 | awk '{print $2}')
      session=$(basename "$jsonl" .jsonl)
      cwd=$(resolve_cwd_from_jsonl "$jsonl")
      ambig=$((ambig+1))
    fi
  fi
  [ "$quality" = "no-match" ] && nomatch=$((nomatch+1))

  title=$(pick_title "$src")
  [ -z "$title" ] && title="Untitled plan"
  slug=$(transliterate "$title")
  [ -z "$slug" ] && slug="untitled-plan"

  target_name="${date_str}-${time_str}-${slug}.md"
  target="$PLANS_DST/$target_name"

  n=1
  base_target_name="$target_name"
  while [ -e "$target" ]; do
    target_name="${base_target_name%.md}-${n}.md"
    target="$PLANS_DST/$target_name"
    n=$((n+1))
  done

  cwd_short=$(echo "${cwd:-}" | sed "s|$HOME|~|")
  session_short="${session:0:36}"
  printf "%-50s %-12s %-38s %-50s %s\n" \
    "$stem" "$quality" "$session_short" "$cwd_short" "$target_name"

  if [ "$APPLY" -eq 1 ]; then
    addr=""
    if [ -x "$ALLOC" ]; then
      addr=$("$ALLOC" 2>/dev/null || echo "")
    fi

    body=$(cat "$src")
    yaml_title=${title//\"/\\\"}
    yaml_cwd=${cwd:-}
    yaml_cwd=${yaml_cwd//\"/\\\"}

    extra_tag=""
    case "$quality" in
      ambig-*)  extra_tag="  - migrated-ambiguous-session" ;;
      no-match) extra_tag="  - migrated-no-session" ;;
    esac
    subagent_tag=""
    case "$stem" in
      *agent-*) subagent_tag="  - subagent-plan" ;;
    esac

    tmp="$target.tmp.$$"
    {
      echo "---"
      echo "type: plan"
      echo "title: \"$yaml_title\""
      [ -n "$addr" ] && echo "address: $addr"
      echo "session_id: $session"
      [ -n "$cwd" ] && echo "source_cwd: \"$yaml_cwd\""
      echo "status: executed"
      echo "created: $date_str"
      echo "updated: $(date '+%Y-%m-%d')"
      echo "migrated_from: \"~/.claude/plans/$stem.md\""
      echo "tags:"
      echo "  - plan"
      echo "  - migrated"
      [ -n "$extra_tag" ] && printf '%s\n' "$extra_tag"
      [ -n "$subagent_tag" ] && printf '%s\n' "$subagent_tag"
      echo "---"
      echo
      printf '%s\n' "$body"
    } > "$tmp" && mv "$tmp" "$target" && written=$((written+1)) || skipped=$((skipped+1))
  fi
done

echo
echo "Summary:"
echo "  total plans:  $total"
echo "  unique:       $clean"
echo "  ambiguous:    $ambig (picked earliest jsonl)"
echo "  no-match:     $nomatch (session_id = unknown-<stem>)"
if [ "$APPLY" -eq 1 ]; then
  echo "  written:      $written"
  echo "  skipped:      $skipped"
else
  echo
  echo "DRY-RUN. Re-run with --apply to actually write files."
fi
