#!/usr/bin/env bash
# Smoke tests for .claude/hooks/skill-router.{sh,py}.
# Run from repo root: ./tests/test_skill_router.sh
#
# Each case: prompt → expected outcome (substring in output, or empty).
# Exits non-zero on first failure.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ROUTER="./.claude/hooks/skill-router.sh"
pass=0
fail=0
failures=()

run_case() {
  local name="$1"
  local prompt="$2"
  local expect="$3"  # substring expected, or "EMPTY" for no output
  local env_prefix="${4:-}"

  local output
  output=$(printf '%s' "{\"prompt\":\"${prompt//\"/\\\"}\"}" | eval "${env_prefix}${ROUTER}" 2>/dev/null)

  if [[ "$expect" == "EMPTY" ]]; then
    if [[ -z "$output" ]]; then
      pass=$((pass + 1))
      printf '  OK   %s\n' "$name"
    else
      fail=$((fail + 1))
      failures+=("$name (expected empty, got: ${output:0:120})")
      printf '  FAIL %s — non-empty output: %s\n' "$name" "${output:0:120}"
    fi
  else
    if printf '%s' "$output" | grep -qF "$expect"; then
      pass=$((pass + 1))
      printf '  OK   %s\n' "$name"
    else
      fail=$((fail + 1))
      failures+=("$name (expected '${expect}', got: ${output:0:200})")
      printf '  FAIL %s — missing %s\n' "$name" "$expect"
    fi
  fi
}

echo "== positive skill matches =="
run_case "save-RU"          'сохрани в вики этот ответ'                               'Skill("save")'
run_case "save-EN"          'save this to the wiki please'                            'Skill("save")'
run_case "close-RU"         'сохрани и закрой сессию'                                 'Skill("close")'
run_case "dispatch-RU"      'запусти параллельную задачу на новый worktree'           'Skill("dispatch")'
run_case "wiki-query-RU"    'что ты знаешь про гибридный поиск'                       'Skill("wiki-query")'
run_case "wiki-query-search" 'поищи в вики страницу про embeddings'                   'Skill("wiki-query")'
run_case "find-session-RU"  'найди похожую сессию про переезд заметок'                'Skill("find-session")'
run_case "find-session-EN"  'was there a similar past task with imports?'             'Skill("find-session")'
run_case "draft-RU"         'сформируй 2 варианта ответа на это письмо'               'Skill("draft")'
run_case "daily-RU"         'собери статус за день'                                   'Skill("daily")'
run_case "journal-today-RU" 'открой дневник'                                          'Skill("journal")'
run_case "journal-plan-RU"  'запиши на завтра проверить бэкапы'                       'Skill("journal")'
run_case "journal-remind-RU" 'напомни в пятницу про сертификаты'                      'Skill("journal")'
run_case "journal-EN"       'journal plan tomorrow check backups'                     'Skill("journal")'
run_case "journal-sessions" 'покажи карту сессий за сегодня'                          'Skill("journal")'
run_case "backlog-RU"       'надо не забыть продлить домен'                           'Skill("backlog")'
run_case "backlog-EN"       'remind me to renew the domain'                           'Skill("backlog")'
run_case "ingest-RU"        'загрузи источник в вики'                                 'Skill("wiki-ingest")'
run_case "learn-RU"         'давай учиться, квиз по модулю'                           'Skill("learn")'
run_case "distill-RU"       'сделай ранбук из сессии'                                 'Skill("distill-runbook")'
run_case "distill-EN"       'distill runbook from this session'                       'Skill("distill-runbook")'

echo "== false-positive resilience =="
run_case "fp-milk"          'купи мне молока'                                         'EMPTY'
run_case "fp-empty"         ''                                                        'EMPTY'
run_case "fp-chto-delat"    'что делать с правами на бакет'                           'EMPTY'
run_case "fp-tormozit"      'почему тормозит ноутбук после обновления'                'EMPTY'
run_case "fp-chto-segodnya" 'что сегодня было интересного'                            'EMPTY'
# journal требует дату: dateless "напомни мне" не должно дёргать journal
# (ловится backlog'ом только в форме "не забыть"/"remind me to")
run_case "fp-save-word"     'какие сейвы есть в этой игре'                            'EMPTY'
run_case "fp-v-wiki"        'у нас в вики есть страница про это?'                     'EMPTY'
run_case "fp-learn-word"    'я узнал много нового сегодня'                            'EMPTY'

echo "== mute env-var =="
run_case "mute-dispatch"    'запусти параллельную задачу'                             'EMPTY' 'SKILL_ROUTER_MUTE=1 '

printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf '\nFailures:\n'
  for f in "${failures[@]}"; do
    printf '  - %s\n' "$f"
  done
  exit 1
fi
