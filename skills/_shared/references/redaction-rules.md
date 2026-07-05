# Redaction rules — shared

Used by any skill that publishes content outside the personal vault (issue trackers, shared docs, draft replies). Single source of truth для cleanup перед публикацией наружу personal vault.

---

## 1. No slang / разговорные обороты

| Bad | Good |
|---|---|
| пофиксить | решить / устранить |
| лажа / косяк | проблема / дефект |
| замутить | реализовать |
| погуглить | найти в документации |
| чуть-чуть подправить | внести правку |
| тыкнуть | вызвать / запустить |
| наговнокодить | реализовать (черновик) |
| вкатить | внедрить / выкатить |
| отвалилось | перестало работать |
| прикрутить | подключить / интегрировать |

Применяется ВО ВСЕХ text fields где это попадает в issue tracker / shared docs / external comms.

## 2. No personal vault refs / private URLs / secrets

- `[[Home Server]]` → `home server` (plain text, без wikilink).
- `[[2026-05-18-...]]` → выкусить полностью или заменить на нейтральное «связанный incident» (без ссылки).
- `c-NNNNNN` адреса — выкусить.
- `wiki/<path>` — выкусить.
- Personal session-IDs / `${CLAUDE_CODE_SESSION_ID}` — выкусить.
- Credentials / tokens / API keys — выкусить ВСЕГДА, заменить на `<REDACTED>`.
- Internal hostnames / private IPs (`nas.local`, `10.0.0.x`) — заменить на нейтральное описание («storage host») если аудитория их не знает.
- Personal names — только с согласия и когда важно для context'а.

Наружу ссылка только на public URL'ы (например `github.com/<user>/<repo>`) или на знакомые читателю имена сервисов как plain text.

## 3. No TD-XX / R-XX / personal tracking markers

User часто использует личные tracking-маркеры `TD-1`, `R-3`, `task-meta`, «handoff в вики», «финальная сверка» — это internal vault жаргон. Выкусить полностью.

## 4. No personal tooling references

| Bad | Good |
|---|---|
| `aws-vault exec ...` | `(локально с тем же AWS-credential)` |
| `direnv allow` | (drop) |
| `fish shell ...` | (drop, neutral terms) |
| личные shell-скрипты | «вручную» / «локально» |
| `claude code` / `mcp__*` tool names | (drop — это user-environment) |

User runs commands himself ([[feedback_user_runs_infra_commands]]) — в комм-стиле говорим «запустить локально», не «выполнить через aws-vault».

## 5. No process trivia / commit hashes / YAML внутрь prose

В issue description / shared doc / draft reply — НЕ:
- commit hashes (`abc1234`)
- YAML / JSON inline (отдельно code-block — OK)
- «handoff», «промежуточная сверка», «промоут», «диспатч» — vault-process слова
- внутренние file paths `~/Projects/...`

Если ссылка на репо / file нужна — full https URL.

## 6. No target dates если user не дал

«target 2026-05-30», «к концу спринта», «до пятницы» — выкусить если user не явно сказал. User сам разберётся когда.

## 7. ASCII-only в constrained external fields

Некоторые внешние системы server-side отбрасывают non-ASCII (например description-поля cloud-ресурсов с regex `[\t\n\r\x20-\x7e\xa1-\xff]*`). Если текст попадает в такой context — translate description в ASCII / English.

(Отдельные системы дополнительно запрещают `()`, `,`, `;`, `'`, `"`, `[]` в tag/label values — проверять ограничения целевой системы до записи.)

## 8. Familiar-terms KEEP / accept-list

Слова, которые аудитория уже знает, оставляем как есть (примеры — заменить на свои):

- устоявшиеся project codenames (`blog`, `home-lab`)
- имена окружений (`prod`, `staging`, `dev`)
- public identifiers (усто́явшиеся имена сервисов: `nginx`, `postgres`, `argocd` etc)

## 9. Language

- Проза — на языке целевого канала (team convention); идентификаторы / имена сервисов — на английском.
- Если целевая аудитория англоязычная — весь текст english.

## 10. Output format

При invocation skill'а делать **diff-like preview redaction**:

```
=== Redactions applied ===
- "[[Home Server]]" → "home server" (4 occurrences)
- "пофиксить лажу" → "устранить проблему" (1 occurrence)
- "target 2026-05-30" → removed (user did not request a date)
- "TD-3" → removed (personal tracking marker)
- "token=ghp_abc123..." → "<REDACTED>" (credential)
- "10.0.0.42 (nas.local)" → "the storage host" (internal hostname)
```

Это позволяет user'у видеть что было изменено и поправить если что-то лишнего срезано.

---

## Anti-pattern: don't redact too aggressively

Если user явно использует slang в качестве commentary («да, эту лажу нужно пофиксить срочно» в свободной речи) — это для context, не для финального ticket text. Но если user диктует description чётко — мы преобразуем. Граница: то что попадает в `summary` / `description` / `comment` / `MR body` — redaction-pass. Раз message-text от user к Claude — это input, не output.
