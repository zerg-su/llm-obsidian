#!/usr/bin/env bash
# Stop-hook: regenerate vault indexes, auto-commit, suggest wiki-fold every 64 log entries.
#
# Phases:
#   1. Detect WIKI_DIRTY (changes in wiki/).
#   2. Regenerate .vault-meta/ indexes via scripts/reindex.py.
#   3. Stage + auto-commit wiki/ .raw/ .vault-meta/ (skip if nothing staged).
#   4. If WIKI_DIRTY → emit hint to update wiki/hot.md.
#   5. wiki-fold suggestion: if current log entry count crossed a 64-multiple
#      since last recorded fold position, emit hint.
#
# Per план item 23: wiki-fold auto-trigger каждые 64 записи log.md.
# Per план item 24: Stop-hook guard skip git add if no staged changes (built-in via `||`).

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

VAULT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
LOG_FILE="$VAULT_ROOT/wiki/log.md"
COUNTER_FILE="$VAULT_ROOT/.vault-meta/last-fold-count.txt"
FOLD_INTERVAL=64

# Opt-out gate: touch .vault-meta/auto-commit.disabled to suspend reindex +
# auto-commit (shared checkouts, manual-commit workflows). rm to re-enable.
if [ -f "$VAULT_ROOT/.vault-meta/auto-commit.disabled" ]; then
  echo "AUTO_COMMIT_DISABLED: .vault-meta/auto-commit.disabled present — reindex/auto-commit skipped. Remove the file to re-enable."
  exit 0
fi

# Latency telemetry: per-phase seconds -> .vault-meta/stop-hook-latency.jsonl
# (gitignored). A model swap or cold cache turns Phase 2d into a minutes-long
# full re-embed that used to stall turn-wrap silently; now it leaves a trace
# and a STOP_HOOK_SLOW warning.
LAT_FILE="$VAULT_ROOT/.vault-meta/stop-hook-latency.jsonl"
SLOW_THRESHOLD_S=30
T_START=$(date +%s)
T_REINDEX=0; T_BACKUP=0; T_BM25=0; T_DENSE=0; T_COMMIT=0

# Phase 1: Detect dirty
WIKI_DIRTY=""
if [ -d "$VAULT_ROOT/.git" ]; then
  WIKI_DIRTY=$(git -C "$VAULT_ROOT" status --porcelain wiki/ 2>/dev/null | head -1)
fi

# Phase 1b: cross-session lock. Parallel Claude sessions (dispatch/reap splits)
# hitting Stop simultaneously used to race: git index.lock silently dropped the
# losing commit, and two reindex runs interleaved half-written .vault-meta files.
# Serialize Phases 2-3 under flock; the loser skips, next Stop catches up.
STOP_LOCK="$VAULT_ROOT/.vault-meta/.stop-hook.lock"
mkdir -p "$VAULT_ROOT/.vault-meta" 2>/dev/null
FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
# macOS: util-linux is keg-only; hook env may miss its PATH entry
[ -z "$FLOCK_BIN" ] && [ -x /opt/homebrew/opt/util-linux/bin/flock ] && FLOCK_BIN=/opt/homebrew/opt/util-linux/bin/flock
HAVE_LOCK=1
if [ -n "$FLOCK_BIN" ]; then
  exec 9>"$STOP_LOCK"
  "$FLOCK_BIN" -w 2 9 || HAVE_LOCK=0
fi
# No flock binary at all -> degrade to old unlocked behaviour (hook never fails).

if [ "$HAVE_LOCK" = "1" ]; then
  # Phase 2: Reindex (incl. per-folder _index.md AUTO-INDEX blocks)
  t=$(date +%s)
  if [ -x "$VAULT_ROOT/scripts/reindex.py" ]; then
    python3 "$VAULT_ROOT/scripts/reindex.py" --quiet --folder-indexes 2>/dev/null || true
  fi
  T_REINDEX=$(( $(date +%s) - t ))

  # Phase 2b: sanitized memory backup into the repo (see scripts/memory-backup.py)
  t=$(date +%s)
  if [ -f "$VAULT_ROOT/scripts/memory-backup.py" ]; then
    python3 "$VAULT_ROOT/scripts/memory-backup.py" >/dev/null 2>&1 || true
  fi
  T_BACKUP=$(( $(date +%s) - t ))

  # Phase 2c: sparse BM25 index over wiki pages (semantic-search --hybrid).
  # Full rebuild is sub-second; gated on WIKI_DIRTY so a no-change Stop pays nothing.
  t=$(date +%s)
  if [ -n "$WIKI_DIRTY" ] && [ -x "$VAULT_ROOT/scripts/bm25-index.py" ]; then
    python3 "$VAULT_ROOT/scripts/bm25-index.py" build --quiet 2>/dev/null || true
  fi
  T_BM25=$(( $(date +%s) - t ))

  # Phase 2d: dense embeddings refresh (hash-incremental — embeds only changed
  # pages; instant exit 10 when ollama is down, swallowed by || true).
  t=$(date +%s)
  if [ -n "$WIKI_DIRTY" ] && [ -x "$VAULT_ROOT/scripts/tiling-check.py" ]; then
    python3 "$VAULT_ROOT/scripts/tiling-check.py" --refresh-only 2>/dev/null || true
  fi
  T_DENSE=$(( $(date +%s) - t ))

  # Phase 3: Auto-commit (subject names changed pages, not just a timestamp)
  t=$(date +%s)
  if [ -d "$VAULT_ROOT/.git" ]; then
    git -C "$VAULT_ROOT" add wiki/ .raw/ .vault-meta/ .claude-memory/ 2>/dev/null
    if ! git -C "$VAULT_ROOT" diff --cached --quiet 2>/dev/null; then
      changed=$(git -C "$VAULT_ROOT" diff --cached --name-only 2>/dev/null \
        | sed -e 's|^wiki/||' -e 's|\.md$||' | grep -v '^\.vault-meta/' | head -3 | paste -sd ', ' -)
      n_changed=$(git -C "$VAULT_ROOT" diff --cached --name-only 2>/dev/null | grep -vc '^\.vault-meta/')
      [ "$n_changed" -gt 3 ] 2>/dev/null && changed="$changed +$((n_changed - 3))"
      [ -z "$changed" ] && changed="indexes"
      git -C "$VAULT_ROOT" commit -m "wiki: $changed ($(date '+%Y-%m-%d %H:%M'))" 2>/dev/null || true
    fi
  fi
  T_COMMIT=$(( $(date +%s) - t ))
else
  echo "STOP_LOCK_BUSY: another session holds the reindex/auto-commit lock — this Stop skipped commit; the next Stop will reindex and commit the changes."
fi

# Phase 3b: rotate oversized jsonl logs (>1MB, keeps one archive generation)
for jl in "$VAULT_ROOT/.vault-meta/router-hits.jsonl" "$VAULT_ROOT/.vault-meta/command-log.jsonl" "$LAT_FILE"; do
  if [ -f "$jl" ] && [ "$(wc -c < "$jl")" -gt 1048576 ]; then
    mv "$jl" "$jl.1" 2>/dev/null || true
  fi
done

# Phase 3c: deterministic cap validation (non-blocking WARN hints)
if [ -f "$VAULT_ROOT/scripts/validate-vault.py" ]; then
  python3 "$VAULT_ROOT/scripts/validate-vault.py" --summary 2>/dev/null || true
fi

# Phase 4: WIKI_DIRTY hint
if [ -n "$WIKI_DIRTY" ]; then
  echo 'WIKI_CHANGED: Wiki pages were modified this session. Please update wiki/hot.md: prepend ONE short bullet to Recent Changes and evict oldest bullets beyond 15. Hard caps: 800 words total, frontmatter updated: holds the date ONLY (never append content there). It is a cache, not a journal — full history lives in log.md.'
fi

# Phase 5: wiki-fold trigger suggestion.
# READ-ONLY here: the counter is updated ONLY by /wiki-fold when a fold actually
# runs. (The old behaviour advanced the counter on showing the hint, so one
# ignored hint silenced folding for another 64 entries — that is how folding
# died between Jun 10 and Jul 1.) The hint repeats every turn while lag >= 64.
if [ -f "$LOG_FILE" ]; then
  current_count=$(grep -c '^## \[' "$LOG_FILE" 2>/dev/null || echo 0)
  last_count=0
  if [ -f "$COUNTER_FILE" ]; then
    last_count=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
  fi
  # Sanity: counter is integer
  if ! [[ "$last_count" =~ ^[0-9]+$ ]]; then
    last_count=0
  fi
  if [ "$current_count" -ge "$((last_count + FOLD_INTERVAL))" ]; then
    echo "WIKI_FOLD_SUGGEST: wiki/log.md прирос на $((current_count - last_count)) entries since last fold (current=$current_count, last=$last_count). Suggested: run /wiki-fold to roll up older entries into wiki/folds/."
  fi
fi

# Phase 6: latency record + slow warning
T_TOTAL=$(( $(date +%s) - T_START ))
printf '{"ts":"%s","total_s":%d,"reindex_s":%d,"backup_s":%d,"bm25_s":%d,"dense_s":%d,"commit_s":%d,"lock":%d,"wiki_dirty":%d}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$T_TOTAL" "$T_REINDEX" "$T_BACKUP" "$T_BM25" "$T_DENSE" "$T_COMMIT" \
  "$HAVE_LOCK" "$([ -n "$WIKI_DIRTY" ] && echo 1 || echo 0)" >> "$LAT_FILE" 2>/dev/null || true
if [ "$T_TOTAL" -ge "$SLOW_THRESHOLD_S" ]; then
  echo "STOP_HOOK_SLOW: turn-wrap took ${T_TOTAL}s (reindex=${T_REINDEX}s backup=${T_BACKUP}s bm25=${T_BM25}s dense=${T_DENSE}s commit=${T_COMMIT}s). Usual suspect is Phase 2d full re-embed (cold/invalidated tiling cache); history in .vault-meta/stop-hook-latency.jsonl."
fi

exit 0
