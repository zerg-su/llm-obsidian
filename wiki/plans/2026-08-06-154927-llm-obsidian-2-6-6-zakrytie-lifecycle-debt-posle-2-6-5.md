---
type: plan
title: "LLM Obsidian 2.6.6 — закрытие lifecycle debt после 2.6.5"
address: c-000127
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-06
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-2-6-5-coordinator"
status: pending
created: 2026-08-06
updated: 2026-08-06
tags:
  - plan
  - manual-save
  - llm-obsidian
  - v2-6-6
  - harness
  - lifecycle
  - technical-debt
---

# LLM Obsidian 2.6.6 — закрытие lifecycle debt после 2.6.5

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Закрыть подтверждённые Codex/Opus расхождения между заявленным lifecycle 2.6.5 и реально достижимыми production-путями, удаляя incident-specific сложность эволюционно и не переписывая Harness.",
  "desired_outcome": "Review/finalization использует один generic exact-HEAD контур: historical и changed-HEAD attempts доступны только для inspect, archive и signal-safe cleanup; новый HEAD резервирует новый bounded cycle и запускает новый attempt. Bounded reviews реально исполняются через выбранный provider-neutral execution profile, поздние циклы получают свежую typed availability независимой модели, а reviewer transitions определяются durable ProviderEvent, а не временем или экраном. Split/join связывает ownership с запечатанным base commit. Durable recovery пишет через публичные CAS-интерфейсы, а временные one-incident authorization compilers и hard-coded identities удалены из production. Release подтверждается bounded simulator coverage и exact-HEAD evidence без бесконечного evidence-descendant цикла.",
  "success_evidence": [
    {
      "evidence_id": "E1-generic-authority",
      "observable": "В production scripts отсутствуют hard-coded operation UUID, callback digest, operator-local /private/tmp path и разбор английского decision prose как authorization grammar; одно typed schema описывает bounded recovery grant, а incident records остаются только immutable acceptance evidence или fixtures."
    },
    {
      "evidence_id": "E2-no-cross-head-resume",
      "observable": "V3, pre-activation V4 и exact-attempt review gates при changed HEAD возвращают typed legacy-cross-head-resume-disabled или terminal archived disposition с нулём provider effects; новый exact attempt не пишет awaiting_resolution, continuation, rebind, checkpoint resurrection или rearm marker."
    },
    {
      "evidence_id": "E3-production-execution-profile",
      "observable": "Frozen execution profile проходит через ReviewOperationRequest и production launcher: bounded ephemeral review использует зарегистрированный Claude-print либо Codex-exec adapter, выполняет auth/billing preflight, публикует schema-valid result и закрывается без cmux/workspace; interactive profile создаёт workspace только при явном выборе."
    },
    {
      "evidence_id": "E4-event-only-review-recovery",
      "observable": "Только exact interactive turn-stopped без результата может разрешить один same-HEAD submit-only effect. Deadline, stable screen, repaint, dead/unknown process, process-exited и event-gap приводят только к recheck или terminal attention; reviewer restart budget равен нулю."
    },
    {
      "evidence_id": "E5-adaptive-independent-route",
      "observable": "Перед атомарной резервацией циклов 4–5 production path получает свежую typed availability independent route, сохраняет её в ledger и запускает вторую модель только при permitted+available; stale, unavailable и unknown дают объяснимый single-model fallback, explicit single-model имеет приоритет."
    },
    {
      "evidence_id": "E6-sealed-split-base",
      "observable": "Split dispatch до worktree effect разрешает base ref в exact commit, сохраняет SHA в manifest/launch evidence и Join требует его ancestry к child HEAD; движение исходной ветки и unrelated child history не меняют proof и fail closed."
    },
    {
      "evidence_id": "E7-public-durable-writes",
      "observable": "Review/recovery modules не вызывают OperationStore._write, _operation_path, LivenessController._write или _locked; public optimistic-CAS methods централизованно проверяют revision, identity, state и durable-boundary invariants."
    },
    {
      "evidence_id": "E8-bounded-completeness",
      "observable": "Детерминированный simulator генерирует bounded state/interleaving matrix для exact-HEAD review, callback publication/ingestion, crash/restart, cleanup, five-cycle budget, provider events, availability и Split base binding; certificate перечисляет покрытые переходы и честно фиксирует непокрытые external/UI свойства, не становясь вторым orchestration engine."
    },
    {
      "evidence_id": "E9-evidence-and-skill-governance",
      "observable": "Release evidence проверяет candidate exact HEAD либо механически разрешённый evidence-only descendant; semantic изменения tdd/prototype/review skills имеют baseline-to-GREEN improve-skills verdict и skill-creator audit."
    },
    {
      "evidence_id": "E10-bounded-finalization",
      "observable": "Finalization сохраняет максимум пять terminal attempts: шестая резервация имеет нулевой model/session/ledger effect; после третьего неуспешного одно-модельного цикла циклы 4–5 используют независимую модель только по E5, а пятый неуспех завершает lineage вместо нового compatibility repair loop."
    },
    {
      "evidence_id": "E11-release-proof",
      "observable": "На одном clean candidate проходят focused regressions, simulator certificate, both-adapter production integration, full tests, honest coverage, 4 370+ transition matrix, acceptance, vault, instruction/skill audit, Codex/MCP sync и diff checks; live bounded dogfood не оставляет owned sessions/resources, а review findings имеют явный fixed/deferred disposition."
    }
  ],
  "non_goals": [
    "Не переписывать Harness, OperationStore, Pipeline DSL или runtime session architecture с нуля и не добавлять второй scheduler, event store или orchestration engine.",
    "Не переносить в 2.6.6 Project Spaces, autonomous decomposition/replanning и workspace-local project orchestration из плана 2.7.",
    "Не сохранять historical cross-HEAD provider continuity ради совместимости; совместимость ограничена read-only inspect/archive и signal-safe cleanup.",
    "Не превращать simulator в копию production reducer и не заявлять исчерпывающее доказательство внешних CLI, cmux UI, OS scheduler, сети или provider billing.",
    "Не добавлять новые decision-prose parsers, UUID/digest literals, recovery markers или time-based positive transitions для прохождения одного исторического инцидента.",
    "Не менять public PipelineSpec v1 несовместимо и не мигрировать historical records in place.",
    "Не запускать дорогой Full review до GREEN focused integration prototype и bounded simulator gate.",
    "Не выполнять push, tag, publish или установку релиза без отдельного пользовательского решения."
  ]
}
```

## 1. Основание и release boundary

2.6.5 выпускается как минимальная стабилизация: его механические release gates зелёные, но независимые Codex и Opus reviews обнаружили расхождения между полным исходным Outcome Contract и активными production-путями. Эти расхождения не маскируются как «готово», но и не расширяют 2.6.5 ещё одним многодневным compatibility loop. Настоящий план является каноничным backlog следующей версии для всех подтверждённых findings, сознательно отложенных из 2.6.5.

Главный принцип 2.6.6 — evolutionary deletion before addition. Мы не строим новый Harness. Сначала выводим одноразовые incident-specific механизмы из active authority, затем соединяем уже существующие generic компоненты с production path и только после этого усиливаем simulator/evidence.

## 2. Полный реестр перенесённых findings

Дубликаты двух моделей объединены по корневой причине, но каждый исходный ID сохранён, чтобы ни одно замечание не потерялось.

| Root class | Исходные findings | Severity | Что подтверждено | Диспозиция 2.6.6 |
|---|---|---:|---|---|
| F1. Active cross-HEAD compatibility | `OI.E3.cross-head-rearm`, `intent-e3-legacy-cross-head-provider-effect`, `intent-nongoal-cross-head-rearm-added` | critical / important | Legacy V3/V4 path способен продолжить moved-HEAD review с provider effect; добавленный `review_drive_rearm` специально работает на mixed reviewed/resolved HEAD и не резервирует новый cycle | Удалить active resume/rearm; historical path только terminal inspect/archive/cleanup; changed HEAD создаёт новый ledger cycle и attempt |
| F2. Ephemeral policy не подключена | `OI.E4b.ephemeral-unwired`, `ENG-EPHEMERAL-INTEGRATION`, `eng-ephemeral-profile-unwired` | critical / important | `execution=ephemeral` валидируется, но production review всегда идёт через interactive `runtime.start`/cmux; adapter registry фактически вызывается только tests | Пронести execution profile через request/attempt identity и подключить registry к production launcher после zero-effect preflight |
| F3. Time/screen остаются authority | `OI.E6.time-screen-authority` | critical | idle/stable screen и elapsed time разрешают submit recovery, а dead reviewer может получить restart | Оставить positive authority только за ProviderEvent; timer только recheck/attention; restart budget reviewer = 0 |
| F4. Incident-pinned production authorization | `eng-incident-pinned-authorization` | important | Тысячи строк production-кода содержат UUID/digest/path одного инцидента и компилируют разрешение из точной английской прозы | Заменить typed grant schema либо удалить исчерпанные paths; incident identity хранить только в acceptance/fixtures |
| F5. Независимая модель недостижима | `ENG-INDEPENDENT-AVAILABILITY` | important | Production reservation передаёт `availability=None`, поэтому cycles 4–5 не могут добавить permitted independent route | Получать fresh availability из adapter/capability source до reservation; сохранить evidence и проверить available/unavailable/stale/unknown |
| F6. Exact attempt сохраняет legacy authority | `ENG-LEGACY-RESOLUTION-AUTHORITY` | important | Terminal changes-requested exact attempt дополнительно пишет mutable `awaiting_resolution` | Оставить findings только в immutable terminal receipt; resolution происходит вне attempt, следующий HEAD — новый cycle |
| F7. Split base mutable | `ENG-SPLIT-BASE-BINDING` | important | Join вычисляет diff от `base_branch`, которая может сдвинуться после dispatch | Seal exact base SHA до effect, persist in manifest/launch, ancestry-check и immutable diff |
| F8. Private durable writers | `eng-store-private-writers` | minor | Несколько recovery modules обходят public `OperationStore.save/transition` и дублируют invariants | Добавить один узкий public CAS transaction seam и перевести callers; запретить private writes тестом topology/AST |
| F9. Skill governance gap | `intent-e9-tdd-skill-verdict-missing` | minor | Семантическое prototype-first правило в `tdd` не имеет отдельного baseline/final verdict record | Выполнить improve-skills + skill-creator audit и сохранить пяти-проходный verdict; не менять skill ad hoc |
| F10. Evidence subject drift | `intent-e14-gate-not-run-at-reviewed-head`, `eng-receipt-subject-is-parent-commit` | minor | Полный receipt относится к parent candidate, а reviewed descendant содержит evidence/docs; корректность выводится из docs-only diff | Определить механически проверяемую candidate/evidence binding и короткий exact-descendant docs gate без бесконечной цепи receipts |
| F11. Outcome overclaim | `OI.E14.outcome-proof-invalid` | important | Зеленые команды 2.6.5 не доказывают contradicted E3/E4b/E6/E9; live dogfood пропустил review | 2.6.6 readiness строится по отдельным outcome observables; каждое review finding получает fixed/deferred/accepted decision |

## 3. Целевая минимальная архитектура

### 3.1. Один attempt — один exact HEAD

Active review path имеет одну writable truth: immutable attempt identity плюс один terminal result. Mutable gate допустим только как исполняемая проекция до terminal. После `approved`, `changes-requested`, `blocked` или `attention-required` attempt не rearm'ится. Если код изменился, finalization coordinator атомарно резервирует следующий номер 1–5 и создаёт новый attempt.

Historical V3/pre-activation records не преобразуются и не продолжаются. Public recovery facade может только:

1. проверить exact identity;
2. сохранить immutable evidence;
3. выполнить signal-safe cleanup лишь при доказанном ownership;
4. terminalize record typed disposition;
5. вернуть управление новому cycle без provider replay.

### 3.2. Generic typed authorization

Coordinator decision больше не является программой на естественном языке. Durable grant содержит schema-validated поля: `grant_kind`, `subject_operation_id`, `subject_head_sha`, `allowed_effect`, `max_uses`, `expires_on_state`, `required_zero_effects`, `evidence_digests`. Human decision text остаётся объяснением, но production policy не сравнивает prefixes/substrings и не извлекает callback IDs regex'ом.

Grant consumption идемпотентен и происходит через public store transaction. Завершённые одноразовые compatibility predicates удаляются, а их bytes/digests остаются в `docs/acceptance/` или `tests/fixtures/` только как воспроизводимое evidence.

### 3.3. Production execution profiles

`ReviewOperationRequest` и attempt identity получают provider-neutral `execution_profile`. Для `ephemeral` production launcher обязан:

1. выбрать adapter из registry;
2. выполнить typed auth/billing preflight до model effect;
3. запустить bounded process без cmux/workspace;
4. принять schema-valid `result-published`;
5. durable зафиксировать `process-exited/resource-closed`;
6. terminalize attempt.

`interactive` остаётся для пользовательски видимой или продолжаемой сессии. Оно не является fallback для неуспешного ephemeral вызова без отдельной compiled policy.

### 3.4. Event-only transitions

Screen digest и elapsed time остаются диагностикой. Они не могут разрешить Enter, submit, restart, result acceptance или progress. Единственное разрешённое submit recovery: exact interactive `turn-stopped` event без результата, тот же HEAD/session/generation/effect boundary, максимум один submit-only effect. Все неизвестные либо terminal process состояния → attention.

### 3.5. Bounded completeness, не второй Harness

Simulator вызывает те же production reducer/policy functions через deterministic fake adapters. Он не хранит параллельный state machine. Certificate строится из конечного набора declared dimensions, pairwise/boundary generation и обязательных named incident traces. Он доказывает покрытие выбранной модели, но явно перечисляет excluded external properties.

## 4. План реализации: owned TDD slices

### Slice A — frozen baseline и deletion budget

- `files/responsibility`: `docs/acceptance/v2.6.6-deferred-findings-baseline.md`, `config/harness-audit-manifest.json`, новый machine-readable finding ledger under `docs/acceptance/`; одна причина — зафиксировать исходный active surface и exact review IDs.
- `consumes`: таблица findings этого плана, exact 2.6.5 review callbacks и current module map.
- `produces`: baseline с production lines/modules, active call graph и deletion target; все 11 root classes имеют owner и evidence ID.
- `failing evidence`: audit показывает hard-coded incident identities/prose parsing/private writers и активные legacy calls.
- `minimal green`: только ledger/baseline и deterministic detector tests; production ещё не меняется.
- `refactor seam`: отсутствует.
- `verification`: finding-ledger schema, module inventory, `git diff --check`; покрывает подготовку E1, E2, E7, E11.

### Slice B — typed grant и удаление incident-specific authority

- `files/responsibility`: `scripts/task_escalation.py`, generic decision/grant contract module, `scripts/task_review_provenance_contract.py`, `scripts/task_review_drift_contract.py`, `scripts/task_review_authorization_boundary.py` и их focused tests; причина — одна generic authorization boundary.
- `consumes`: Slice A ledger и current immutable decision records.
- `produces`: typed bounded grant, exact identity/digest validation, max-use consumption и read-only historical evidence loader; one-shot production constants удалены.
- `failing evidence`: authorization требует одного из известных UUID/path/prose fragments; semantically equal prose не компилируется, malformed typed grant fail closed.
- `minimal green`: additive typed record fields/read model и один validator; compatibility reader только для уже сохранённых records, без нового active effect.
- `refactor seam`: удалить exhausted one-shot modules/predicates после переноса evidence; отрицательный production diff обязателен.
- `verification`: schema/authorization chain/idempotency/unknown-field/expiry tests; grep/AST detector hard-coded identities; E1.

### Slice C — public store transaction seam

- `files/responsibility`: `scripts/harness/store.py`, liveness owner module и callers в review recovery/rearm/post-fresh paths; одна причина — singular durable-write ownership.
- `consumes`: generic typed grant из B и существующие revision/state invariants.
- `produces`: public locked CAS methods для operation+liveness updates с expected revision/state/identity и durable-boundary observation.
- `failing evidence`: external modules вызывают private writers; stale revision либо identity mutation обходят owner API.
- `minimal green`: один узкий transaction interface, затем механическая миграция exact callers.
- `refactor seam`: удалить duplicated local revision/digest checks только после behaviour parity.
- `verification`: concurrent stale-write, crash-before/after-commit, idempotent replay, AST no-private-writer; E7.

### Slice D — terminal exact-HEAD review и historical fail-closed

- `files/responsibility`: `scripts/task_review_flow.py`, `scripts/task_review_resolution_flow.py`, `scripts/harness/review_drive_rearm.py`, `scripts/harness/workflows/review_gate_attempt.py`, finalization ledger integration и focused lifecycle tests; причина — единый active exact-HEAD lifecycle.
- `consumes`: public transaction seam C, typed grant B, existing ReviewAttempt/FinalizationLedger.
- `produces`: moved HEAD closes/archives old attempt and reserves a new cycle; V3/pre-activation gates never start provider; exact terminal findings live only in immutable receipt.
- `failing evidence`: end-to-end V3 awaiting-resolution at moved HEAD emits provider effect; exact changes-requested writes `awaiting_resolution`; rearm resets deadline without ledger reservation.
- `minimal green`: block executable legacy branches and route changed HEAD through existing next-cycle reservation; no new recovery state.
- `refactor seam`: delete now-unreachable rearm/continuation/checkpoint code and scenario expectations; preserve inspect/archive/cleanup adapters.
- `verification`: zero-effect legacy matrix, changed-HEAD new-cycle test, sixth-cycle zero-effect, crash/idempotency cleanup; E2, E10.

### Slice E — production ephemeral execution

- `files/responsibility`: `scripts/harness/workflows/review.py`, review request/contracts, `scripts/harness/runtime_provider.py`, `scripts/harness/ephemeral_provider.py`, Claude/Codex adapters и integration tests; причина — сделать уже объявленный profile реально исполняемым.
- `consumes`: existing adapter registry/conformance contract, exact attempt D, provider-neutral policy.
- `produces`: production ephemeral route with preflight/result/exit/close and zero cmux; explicit interactive route unchanged.
- `failing evidence`: a bounded review with `execution=ephemeral` calls `RuntimeSessionManager.start` and creates workspace; registry spy has zero calls.
- `minimal green`: branch only at review runtime port; reuse existing adapter registry and ProcessAdapter, no new orchestrator.
- `refactor seam`: unify duplicated result normalization only while both real adapter conformance suites remain green.
- `verification`: prototype first against disposable real Claude/Codex command, then fake-adapter production integration, zero-workspace assertion, auth ambiguous fail-before-effect, schema/event-gap tests; E3.
- `parallelism`: после C может идти параллельно D только если request contract frozen и файлы review.py/task_review_flow.py не пересекаются; иначе sequential.

### Slice F — event-only liveness and delivery

- `files/responsibility`: `scripts/harness/callback_submit_recovery.py`, `runtime_worker_liveness.py`, `liveness.py`, provider-event reducer tests; причина — убрать time/screen positive authority.
- `consumes`: ProviderEvent contract и execution profiles E.
- `produces`: one submit-only turn-stopped rule, reviewer restart ceiling zero, deadline/screen/process anomalies → terminal attention.
- `failing evidence`: stable-screen+elapsed-time without turn-stopped sends input; dead reviewer restarts.
- `minimal green`: remove positive branches and compile reviewer-specific zero restart policy.
- `refactor seam`: delete unused screen-based counters/markers from review path, сохранив UI telemetry read-only.
- `verification`: generated event/retry matrix, real adapter event translation prototype, zero-provider-effect assertions; E4.

### Slice G — fresh availability for cycles 4–5

- `files/responsibility`: `scripts/task_review_finalization_attempt.py`, finalization policy/ledger and provider capability source tests; причина — make late independent route reachable.
- `consumes`: execution adapter capability/preflight E and frozen five-cycle policy.
- `produces`: timestamped typed AvailabilityEvidence persisted with cycle reservation.
- `failing evidence`: cycle 4 production entry always compiles `availability-unknown` despite available registered provider.
- `minimal green`: read one fresh provider-neutral availability value before reservation and pass it through existing compiler.
- `refactor seam`: no background polling/cache; one bounded check per reservation.
- `verification`: available/unavailable/stale/unknown/permission-denied, explicit single-model, atomic reservation race; E5, E10.
- `parallelism`: может идти параллельно F после E при непересекающихся owned files.

### Slice H — seal Split base identity

- `files/responsibility`: Split manifest/dispatch contracts, `scripts/harness/split_evidence.py`, workspace preparation and join tests; причина — immutable ownership boundary.
- `consumes`: validated Split manifest before worktree/provider effect.
- `produces`: exact base SHA in manifest, launch and terminal evidence; ancestry requirement at Join.
- `failing evidence`: branch advances after launch and changes computed owned delta; unrelated child history can be compared.
- `minimal green`: `rev-parse --verify <base>^{commit}` once before launch, persist SHA, diff only sealed SHA.
- `refactor seam`: keep human branch name diagnostic-only; do not add branch watcher.
- `verification`: moving branch, deleted branch, unrelated history, exact ancestor, restart/reload; E6.
- `parallelism`: может исполняться параллельно D–G, так как ownership ограничен Split modules.

### Slice I — simulator bounded-completeness certificate

- `files/responsibility`: existing lifecycle simulator generator/oracle/certificate modules, scenario fixtures and release acceptance profile; причина — проверять root classes до дорогих live reviews.
- `consumes`: production seams B–H and declared finite dimensions.
- `produces`: reproducible certificate with generator/version/seed, transition tuple hashes, covered boundary classes, named historical traces and explicit exclusions.
- `failing evidence`: mutation operators (drop callback, reorder close/result, stale generation, changed HEAD, stale availability, moved Split base, crash between prepare/commit) survive suite or certificate claims uncovered dimensions.
- `minimal green`: extend existing simulator to call production reducers/adapters; no parallel state machine.
- `refactor seam`: remove duplicate simulator-only policy; every transition delegates to production function.
- `verification`: deterministic rerun hash, mutation score threshold for declared classes, pairwise/boundary coverage, no external/model effect; E8.

### Slice J — governance and finite evidence binding

- `files/responsibility`: improve-skills verdict artifacts, relevant skills only through skill-creator workflow, `scripts/verification_receipt.py`, release acceptance/readiness docs; причина — close E9/E10 evidence gaps without recursive receipts.
- `consumes`: exact candidate from B–I, current receipt model and prior tdd semantic change.
- `produces`: audited skill verdict plus two-phase evidence rule: immutable product candidate receipt and mechanically restricted evidence-only descendant verified by a non-self-publishing docs gate.
- `failing evidence`: tdd has no baseline/final verdict; unrestricted descendant passes; receipt staging observes itself as dirty.
- `minimal green`: add missing governance record and validator for allowlisted evidence-only paths/digests; no new release state machine.
- `refactor seam`: one receipt authority and one descendant binding format.
- `verification`: tampered descendant, product-code descendant, missing log/digest, exact docs-only descendant, five-pass skill verdict; E9.

### Slice K — Stability Gate and release candidate

- `files/responsibility`: no product ownership; only generated immutable evidence and 2.6.6 release/readiness docs.
- `consumes`: integrated clean candidate and all E1–E10 artifacts.
- `produces`: one bounded RC disposition and terminal resource inventory.
- `failing evidence`: any finding lacks fixed/deferred mapping, simulator certificate mismatch, active session remains owned, or candidate differs from verified tree outside allowed evidence paths.
- `minimal green`: run exact gates once; a failure returns to owning slice, not to a generic compatibility repair loop.
- `refactor seam`: none.
- `verification`: focused suites; simulator; both-adapter production integration; full tests; honest coverage; transition matrix; acceptance/vault/instruction/skill audits; Codex/MCP sync; diff/clean; one live bounded single-model dogfood and second model only when typed available; E11.

## 5. Dependency graph and parallel execution

```text
A baseline
  ↓
B typed authority → C public transaction
                    ├─ D exact-HEAD lifecycle ─┐
                    ├─ E execution profiles ─ F event-only recovery ─ G availability
                    └─ H sealed Split base ────────────────────────────┤
                                                                      ↓
                                                              I simulator
                                                                      ↓
                                                              J governance/evidence
                                                                      ↓
                                                              K release gate
```

Параллельность разрешена только после freeze interfaces. D и H независимы. F и G зависят от E, но могут исполняться параллельно после frozen provider event/capability contracts. Shared files (`Makefile`, audit manifest, release acceptance, skill baselines) принадлежат Join/K, а не отдельным workstreams.

## 6. Stop rules и budget

1. Для неизвестного CLI/cmux/provider механизма сначала disposable prototype с реальным минимальным вызовом и нулевым product mutation; только GREEN механизм переносится в RED production regression.
2. Каждая slice получает максимум два mechanism-repair retries на один и тот же failure class; повторяющиеся compatibility failures возвращаются к design boundary вместо нового literal/compiler tail.
3. Финальный review/fix budget — максимум пять terminal cycles. Циклы 1–3 используют primary route. На 4–5 добавляется independent route только по fresh typed availability и разрешению. После пятого неуспеха status `finalization-budget-exhausted` и release не публикуется.
4. Security, permission, unknown external effect, destructive action и ambiguous ownership останавливаются раньше budget и требуют отдельного решения.
5. Full review запускается только после GREEN focused production integration, simulator certificate и exact clean candidate. Ранние Full reviews не используются как отладочный цикл.

## 7. Rollback и compatibility

- Все DSL additions additive и имеют code-owned ceilings; unknown fields fail closed.
- Historical operation/review records не переписываются. Их loaders read-only; active resume удалён.
- Ephemeral profile можно отключить compiled policy feature flag до model effect и явно использовать interactive route; silent fallback запрещён.
- Typed grant rollout начинается с dual-read/typed-write только для существующих immutable records, затем prose authority удаляется из active path. Не создаётся постоянная двойная writable truth.
- Split exact base SHA сохраняется рядом с diagnostic branch name; старые manifests без SHA не dispatch'ятся повторно и доступны inspect-only.

## 8. Definition of done

- Все 11 root classes F1–F11 имеют fixed evidence либо явно отдельный user-approved defer; ни один исходный finding ID не исчез.
- Production diff по active recovery/authorization contour отрицательный по сравнению с 2.6.5 baseline; исключения объяснены module map.
- Нет hard-coded incident identity/prose authorization и private durable writes вне owning modules.
- Bounded production review реально проходит ephemeral route без cmux, а interactive route остаётся явным.
- Changed HEAD никогда не rearm'ит старый reviewer и потребляет новый bounded cycle.
- Timer/screen не создают положительных provider effects.
- Cycle 4 reachable with available independent provider; sixth cycle zero-effect.
- Split proof выдерживает движение branch ref и rejects unrelated history.
- Simulator certificate покрывает declared boundary/interleaving classes и честно перечисляет exclusions.
- Exact candidate и evidence binding проверены без recursive self-observation.
- Все owned operations/sessions terminal и resource-free; push/tag/publish выполняются только после отдельного решения пользователя.

