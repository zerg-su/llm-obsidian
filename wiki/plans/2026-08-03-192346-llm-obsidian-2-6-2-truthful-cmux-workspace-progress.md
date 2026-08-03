---
type: plan
title: "LLM Obsidian 2.6.2 — truthful cmux workspace progress"
address: c-000097
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-03
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-2-6-2-status"
status: pending
created: 2026-08-03
updated: 2026-08-03
tags:
  - plan
  - manual-save
  - v2-6-2
  - cmux
  - harness
---

# LLM Obsidian 2.6.2 — truthful cmux workspace progress

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Сделать cmux progress bar честным и workspace-scoped, чтобы завершённые или потерявшие точную runtime-liveness исторические операции не выглядели как текущие задачи.",
  "desired_outcome": "В LLM Obsidian 2.6.2 sidebar текущего coordinator workspace показывает только прогресс действительно текущих живых harness programs, немедленно очищается в idle-состоянии и остаётся одинаково корректным для Claude и Codex без изменения operation state, pipeline semantics или cmux ownership authority; единственный legacy-план из docs/plans перенесён в канонический wiki/plans без потери содержания и результата.",
  "success_evidence": [
    {
      "evidence_id": "idle-clears",
      "observable": "При отсутствии текущих живых programs SessionStart/resume и terminal lifecycle publish вызывают workspace-scoped clear-progress; старые terminal owners, stale descendants и missing surfaces не оставляют progress line."
    },
    {
      "evidence_id": "active-is-truthful",
      "observable": "Для одной или нескольких текущих programs того же coordinator origin status показывает только их completed/total, active, awaiting-callback и attention counts; programs другого workspace не смешиваются."
    },
    {
      "evidence_id": "controller-authority",
      "observable": "Terminal top-level dispatch/review/research controller исключает весь program даже при противоречивом nonterminal child; классификация использует code-owned kinds/identity и exact metadata, а не filename или title guesses."
    },
    {
      "evidence_id": "liveness-bound",
      "observable": "Running или awaiting controller с exact cmux surface, отсутствующим в одном bounded live inventory snapshot, не считается живой работой; неизвестность probe не превращается в ложное idle и не мутирует lifecycle."
    },
    {
      "evidence_id": "transition-matrix",
      "observable": "Дешёвая детерминированная unit-матрица покрывает idle, preflight/start, running, waiting, attention, terminal, stale child, missing surface, multiple programs, cross-workspace isolation, corrupt active/inactive history и SessionStart refresh."
    },
    {
      "evidence_id": "provider-parity",
      "observable": "Одинаковая content-free строка и cleanup semantics работают для Claude и Codex; cmux commands используют exact workspace target и documented set-progress/clear-progress interface."
    },
    {
      "evidence_id": "release-ready",
      "observable": "Focused tests, полный make test, acceptance-check, vault/adapters/snapshot/release gates, bounded active-to-idle live cmux smoke и независимый Deep review зелёные на одном exact release HEAD."
    },
    {
      "evidence_id": "ordinary-provider-launch",
      "observable": "Ordinary dispatch/review providers pin an exact trusted env-shebang interpreter before entering a new cmux surface; the regression proves launch works even when the child PATH cannot resolve node."
    },
    {
      "evidence_id": "turn-end-close-preserved",
      "observable": "Pre-integrated commit 80fcab2 keeps turn-end save-and-close reachable for Claude and Codex; its queue-session-exit and runtime-hook regressions pass on the exact 2.6.2 release HEAD."
    },
    {
      "evidence_id": "plan-location-normalized",
      "observable": "Единственный docs/plans/v2.1.1-code-owned-optimization-plan.md сохранён в wiki/plans с валидным plan frontmatter, status executed, DragonScale address и ссылкой на финальный result; пустой отдельный docs/plans namespace больше не существует."
    },
    {
      "evidence_id": "readme-onboarding",
      "observable": "README.md и README.ru.md точно и симметрично объясняют pipeline/DSL, review topology 2.6.1, truthful cmux progress 2.6.2 и короткий путь clarify/grill me → plan → plan review → dispatch → implementation review → reap/close для Claude и Codex."
    }
  ],
  "non_goals": [
    "Не создавать project/task backlog, DAG или task-program scheduler из 2.7.",
    "Не удалять, мигрировать, исправлять или переинтерпретировать исторические OperationStore records.",
    "Не добавлять rate-limit metadata, новый sidebar widget или отдельный UI daemon.",
    "Не менять pipeline DSL, review topology, provider routing, callback, cleanup или lifecycle authority.",
    "Не считать внутренние harness steps пользовательскими project tasks и не обещать task-level progress до 2.7.",
    "Не расширять cmux permissions и не использовать focused workspace/title/index guesses вместо exact identity.",
    "Не проводить широкую реорганизацию docs или wiki и не переписывать исторические review-архивы ради исправления одного legacy plan path."
  ]
}
```

## Проблема и подтверждённая причина

В 2.6.1 `scripts/harness/status_segment.py` выбирает owner как активный, если любой его operation record nonterminal, затем считает всю историю этого owner. На реальном store это превращает завершённый dispatch с одним stale verification child в большой ложный счётчик. Другой реальный owner сохраняет `awaiting-callback`, хотя его exact cmux surface уже отсутствует. `terminal_owner` дополнительно удерживает финальный progress, а SessionStart не пересчитывает status. Обычный `llm-obsidian` и Swarm содержат одинаковую реализацию, поэтому 2.6.2 исправляется сначала в `/Users/zak/Projects/llm-obsidian`. После двух fail-closed preflight запусков release base также получил regression-covered interpreter binding для ordinary dispatch/review providers: parent записывает exact trusted env-shebang interpreter до входа в новую cmux surface. В release base принят и отдельный сфокусированный commit `80fcab2` из `fix/turn-end-commit-close`; релиз обязан сохранить его save-and-close semantics и зелёные регрессии, не смешивая этот код со status selection.

Локальная документация cmux 0.64.20 подтверждает, что `set-progress` и `clear-progress` принадлежат exact workspace; это и остаётся единственным UI transport.

## Архитектурное решение

1. Ввести внутри status module маленькую code-owned классификацию top-level program controllers и derived operations. Она основывается на `OperationSpec.kind` и exact runtime metadata, не на operation-id suffix.
2. Считать owner текущим только при наличии nonterminal top-level controller. Terminal controller является authoritative closure для его program и подавляет противоречивые stale children.
3. Привязать visible program к coordinator origin surface/workspace из exact runtime `session.json`. Workspace-placement child не должен переносить aggregate bar из coordinator workspace в собственный child workspace.
4. Получать один bounded cmux tree inventory snapshot на publish и проверять exact controller surface. `running`/`awaiting-callback` с missing surface исключаются как неживая работа. Probe error возвращает best-effort failure без очистки/перезаписи существующего bar и без lifecycle mutation.
5. Передавать exact trigger owner в runtime notification, чтобы preflight/starting boundary не терялся до появления session metadata; одновременно агрегировать другие доказанно текущие programs того же origin.
6. Удалить terminal-owner fallback, который сохраняет 100% bar после окончания. Последняя terminal transition обязана очистить progress.
7. На coordinator SessionStart/resume/clear/compact выполнить silent best-effort refresh. Task worktree SessionStart не получает coordinator status authority.
8. Сохранить content-free compact label и документировать его как harness-step status. UI не должен читать prompts, paths, model output или user content.

## TDD slices

### Slice 1 — truthful status selection

- `files/responsibility`: `tests/harness/test_status_segment.py` — behavioral matrix; `scripts/harness/status_segment.py` — selection, rendering and exact cmux inventory boundary.
- `consumes`: existing `OperationStore`, `OperationSpec.kind`, terminal states, runtime `session.json`, exact cmux workspace/surface identities.
- `produces`: a pure/injectable current-program snapshot plus workspace-scoped publish/clear behavior.
- `failing evidence`: fixtures where terminal dispatch + stale child and missing reviewer surface currently render nonzero progress; cross-workspace records currently contaminate one aggregate.
- `minimal green`: select only exact current nonterminal controllers and their program records, using one injected live-surface inventory; clear when selection is empty.
- `refactor seam`: isolate pure controller/program selection from cmux transport only after the red matrix is green.
- `focused verification`: `python3 tests/harness/test_status_segment.py`; covers `idle-clears`, `active-is-truthful`, `controller-authority`, `liveness-bound`, `transition-matrix`, `provider-parity`.

### Slice 2 — lifecycle and SessionStart refresh

- `files/responsibility`: `scripts/harness/runtime_session_contracts.py`, `scripts/harness/runtime_sessions.py`, relevant `scripts/harness/runtime_session_*.py` — exact trigger-owner notifier contract; `hooks/run-hook.py` — non-task SessionStart refresh; `tests/harness/test_runtime_sessions.py` and `tests/test_runtime_hooks.py` — observable lifecycle/hook behavior.
- `consumes`: Slice 1 publisher, exact current owner, existing task-context suppression and hook root resolution.
- `produces`: every material lifecycle boundary refreshes the right aggregate; a new/resumed idle coordinator clears stale UI without granting task sessions coordinator authority.
- `failing evidence`: terminal publish currently preserves 100%, and a new idle SessionStart leaves an old progress bar untouched.
- `minimal green`: pass owner identity at existing notify points and invoke silent publisher from coordinator SessionStart only.
- `refactor seam`: no new daemon, store or hook route; keep failures best-effort and content-free.
- `focused verification`: status tests, runtime-session focused tests, `python3 tests/test_runtime_hooks.py`; covers `idle-clears`, `active-is-truthful`, `provider-parity`.

### Slice 3 — canonicalize the singleton legacy plan

- `files/responsibility`: `docs/plans/v2.1.1-code-owned-optimization-plan.md` — единственный legacy source; новая каноническая страница `wiki/plans/` — сохранённое содержание с plan frontmatter; соответствующий vault address/index derived state — только через `scripts/vault-write.py`.
- `consumes`: подтверждённый уникальный v2.1.1 plan, существующие approved plan reviews и финальный результат `[[Cross-model review — v2.1.1 final implementation review — 1bae885ecfdf]]`.
- `produces`: executed plan в общем `wiki/plans/`, содержащий исходный текст и строку `Результат:`; tracked `docs/plans/` после удаления файла отсутствует.
- `failing evidence`: singleton остаётся вне canonical vault plan lifecycle и не имеет обязательных frontmatter/address/result metadata.
- `minimal green`: создать каноническую страницу одной vault-write транзакцией, проверить сохранность body, затем удалить только исходный tracked file; не редактировать архивные review quotes.
- `refactor seam`: не переносить другие docs и не создавать compatibility stub для старого внутреннего path.
- `focused verification`: `python3 scripts/validate-vault.py --summary`, точная проверка отсутствия tracked `docs/plans/` file и наличия исходных ключевых разделов в migrated page; покрывает `plan-location-normalized`.

### Slice 4 — operator contract and 2.6.2 release

- `files/responsibility`: `docs/runtime-capabilities.md` and/or `docs/unattended-pipeline-operations.md` — exact label/idle semantics; `CHANGELOG.md`, `CHANGELOG.ru.md`, `docs/releases/v2.6.2.md`, `README.md`, `README.ru.md`, README release indexes and plugin manifests — patch release identity and commands.
- `consumes`: green implementation, exact documented cmux 0.64.20 command contract и pre-integrated turn-end save-and-close fix `80fcab2` и ordinary-provider interpreter repair `0630477`.
- `produces`: user-readable icon semantics, upgrade/rollback notes, version 2.6.2 metadata, release notes for both bounded fixes, exact release gate instructions and a concise pipeline/skill/session quick-start in both READMEs.
- `failing evidence`: adapter/version checks identify 2.6.1 and docs do not promise idle clearing/current-program scoping.
- `minimal green`: update only release-owned metadata/docs; document clarify/grill me, saved plans, intent plan review, built-in/custom pipeline dispatch, Simple/Deep/Full review, single-model fallback, reap and close symmetrically in both READMEs; regenerate/check the Codex adapter through repository tooling.
- `refactor seam`: no roadmap or 2.7 task semantics in 2.6.2 docs.
- `focused verification`: `python3 tests/test_queue_session_exit.py`, `python3 tests/test_runtime_hooks.py`, `python3 tests/harness/test_runtime_sessions.py`, `python3 tests/harness/test_runtime_research.py`, adapter checks, instruction/docs lint and `git diff --check`; covers `provider-parity`, `ordinary-provider-launch`, `turn-end-close-preserved`, `release-ready`.

## Verification and review

Run fast tests first, then `make test`, `make acceptance-check`, `python3 scripts/validate-vault.py --summary`, `python3 scripts/codex-adapter.py --check`, `scripts/mcp-gateway/mcp-gateway.sh codex-sync --check`, `references/upstream-skills/verify_snapshots.py`, and `git diff --check` on one exact HEAD. Perform one bounded visible cmux smoke that demonstrates `active → awaiting/attention as applicable → terminal idle clear` without fabricating lifecycle records. Review with Deep default; if one provider is unavailable, use the existing explicit single-model Deep topology rather than weakening or blocking the pipeline. Apply material findings, rerun affected deterministic gates, then review the exact release HEAD.

## Rollback and boundaries

The patch changes only a read-only projection and notification timing. Rollback is one release revert: old operation records remain untouched. Any need to mutate old store entries, widen cmux authority, add a background daemon, or introduce 2.7 task-program semantics is a scope fork and stops this patch.
