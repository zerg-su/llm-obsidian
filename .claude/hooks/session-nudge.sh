#!/usr/bin/env bash
# SessionStart maintenance nudges (matcher: startup only — not resume).
#
# Prints 0..N one-line soft hints about overdue vault maintenance. Tone rule:
# suggestion, never MANDATORY. Cheap checks only — no network beyond a
# localhost probe, no MCP, no python imports beyond stdlib one-liners.
#
# Checks:
#   0. gateway-down  — MCP HTTP gateway (127.0.0.1:9090) не отвечает
#                      (только если гейтвей сконфигурирован на этой машине)
#   1. lint-age      — newest wiki/meta/reports/lint-report-*.md older than 7d
#   2. fold-due      — log entries since last fold >= 64 (same counter as stop.sh)
#   3. tiling-age    — newest tiling-report older than 14d (opt-in mechanism)
#   4. backup-stale  — memory dir newer than .claude-memory/ backup
#   5. skill-of-day  — rotate one underused skill + a concrete use-case
#                      (once per day, marker in .vault-meta/skill-nudge-day.txt)
#   6. assist-usage  — wiki-query router hints без единого retrieval-assist
#                      вызова за 7д (pipeline-stats --nudge, только jsonl-логи)

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
NOW=$(date +%s)
HINTS=0

hint() { echo "MAINTENANCE_HINT: $1"; HINTS=$((HINTS + 1)); }

newest_mtime() { # glob -> epoch of newest match, 0 if none
  local newest=0 f m
  for f in $1; do
    [ -f "$f" ] || continue
    m=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0)
    [ "$m" -gt "$newest" ] && newest=$m
  done
  echo "$newest"
}

# 0. mcp-gateway alive — only when the gateway is actually configured here
#    (localhost TCP probe; any HTTP answer from the proxy = OK, refused = down)
if [ -f "$HOME/.config/mcp-gateway/secrets.env" ] || [ -f "$VAULT_ROOT/scripts/mcp-gateway/config.json" ]; then
  if ! curl -s -o /dev/null --max-time 1 "http://127.0.0.1:9090/" 2>/dev/null; then
    hint "MCP gateway не отвечает (127.0.0.1:9090) — MCP-серверы недоступны. Запуск: scripts/mcp-gateway/mcp-gateway.sh start (диагноз: status / doctor / logs)."
  fi
fi

# 1. lint-age
lint_m=$(newest_mtime "$VAULT_ROOT/wiki/meta/reports/lint-report-*.md")
if [ "$lint_m" -gt 0 ]; then
  age_d=$(( (NOW - lint_m) / 86400 ))
  [ "$age_d" -ge 7 ] && hint "wiki-lint не гонялся ${age_d} дней — consider /wiki-lint."
fi

# 2. fold-due (same logic as stop.sh, but read-only — counter не трогаем)
LOG_FILE="$VAULT_ROOT/wiki/log.md"
COUNTER_FILE="$VAULT_ROOT/.vault-meta/last-fold-count.txt"
if [ -f "$LOG_FILE" ] && [ -f "$COUNTER_FILE" ]; then
  cur=$(grep -c '^## \[' "$LOG_FILE" 2>/dev/null || echo 0)
  last=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  [ "$cur" -ge $((last + 64)) ] && hint "log.md вырос на $((cur - last)) записей с последнего fold — consider /wiki-fold."
fi

# 3. tiling-age (opt-in: only nudge if a tiling report ever existed)
til_m=$(newest_mtime "$VAULT_ROOT/wiki/meta/reports/tiling-report-*.md")
if [ "$til_m" -gt 0 ]; then
  age_d=$(( (NOW - til_m) / 86400 ))
  [ "$age_d" -ge 14 ] && hint "semantic tiling не гонялся ${age_d} дней (нужен ollama) — consider при следующем lint."
fi

# 4. backup staleness (memory dir vs .claude-memory/)
#    Project dir = vault path with non-alphanumeric chars dashed (Claude Code convention).
PROJ_SLUG=$(printf '%s' "$VAULT_ROOT" | sed 's/[^A-Za-z0-9]/-/g')
MEM_DIR="$HOME/.claude/projects/$PROJ_SLUG/memory"
BK_DIR="$VAULT_ROOT/.claude-memory"
if [ -d "$MEM_DIR" ] && [ -d "$BK_DIR" ]; then
  mem_m=$(newest_mtime "$MEM_DIR/*.md")
  bk_m=$(newest_mtime "$BK_DIR/*.md")
  # >1h drift = stop-hook backup явно не отработал
  [ "$mem_m" -gt $((bk_m + 3600)) ] && hint "backup памяти отстал от memory-директории — проверь scripts/memory-backup.py (Stop-хук)."
fi

# 5. skill-of-day — ротация недоиспользуемых скиллов с конкретным кейсом.
#    Список правится руками — скилл, вошедший в привычку, просто удаляй из массива.
SKILL_TIPS=(
  "/wiki-query — спроси вики прежде чем гуглить: «что ты знаешь про X?» (поиск с цитатами по твоей же базе)"
  "/save — зафиксируй вывод из текущего разговора одной командой; тип и папку выведет сам"
  "/wiki-ingest <путь|URL> — преврати статью, доку или заметку в структурированные wiki-страницы"
  "/autoresearch <тема> — автономный research-цикл: поиск, синтез, файлинг в вики"
  "/journal — план на завтра, напоминание на дату, перенос невыполненного"
  "/backlog add <мысль> — одна строка в capture-инбокс, чтобы не потерять"
  "/find-session — перед похожей задачей: найдёт прошлый разбор среди старых сессий"
  "/distill-runbook — команды этой сессии в copy-paste ранбук (работает и без ИИ)"
  "/wiki-fold — сверни разросшийся log.md в фолд-страницы (DragonScale M1)"
  "/dispatch <задача> — вынеси работу в параллельный split на отдельной модели (требует cmux)"
)
DAY_MARKER="$VAULT_ROOT/.vault-meta/skill-nudge-day.txt"
today_str=$(date +%Y-%m-%d)
if [ "$(cat "$DAY_MARKER" 2>/dev/null)" != "$today_str" ]; then
  idx=$(( $(date +%j | sed 's/^0*//') % ${#SKILL_TIPS[@]} ))
  hint "скилл дня: ${SKILL_TIPS[$idx]}"
  echo "$today_str" > "$DAY_MARKER" 2>/dev/null || true
fi

# 6. retrieval-assist discipline (дёшево: только router-hits + command-log jsonl,
#    транскрипты не сканируются — см. pipeline-stats.py --nudge)
if [ -f "$VAULT_ROOT/scripts/pipeline-stats.py" ]; then
  nudge_line=$(python3 "$VAULT_ROOT/scripts/pipeline-stats.py" --nudge 2>/dev/null || true)
  [ -n "$nudge_line" ] && hint "$nudge_line"
fi

exit 0
