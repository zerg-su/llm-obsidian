---
type: concept
title: "DragonScale Memory"
address: c-000001
complexity: advanced
domain: knowledge-management
aliases:
  - "DragonScale"
  - "DragonScale Architecture"
  - "Fractal Memory"
created: 2026-04-23
updated: 2026-04-26
tags:
  - concept
  - knowledge-management
  - memory
  - architecture
  - fractal
status: shipped
related:
  - "[[LLM Wiki Pattern]]"
  - "[[Compounding Knowledge]]"
  - "[[Hot Cache]]"
  - "[[concepts/_index]]"
sources:
---

# DragonScale Memory

Memory-layer дизайн для LLM-wiki вольтов, вдохновлённый кривой Хейтуэя (Heighway dragon curve). Четыре механизма (fold operator, deterministic page addresses, semantic tiling, boundary-first autoresearch) дают LLM-поддерживаемой вики principled-способ расти, компактировать, оставаться cohrent. Кривая дракона — design-justification device, не reasoning-архитектура.

> **Status: v0.4 2026-04-24.** Все четыре механизма shipped как opt-in features. Phase 0 (spec) + Phase 1 (wiki-fold скилл, dry-run verified) + Phase 2 (address MVP) + Phase 3 (semantic tiling) + Phase 3.5/3.6 (hardening) + Phase 4 (boundary-first autoresearch). См. Review History для прогрессии.

---

## Scope

DragonScale это **memory architecture**: управляет тем как вики растёт, компактируется, адресует свои страницы, проверяет дубликаты. Это **не search, planning, или reasoning-алгоритм.** Agent-reasoning использует существующие паттерны (Tree of Thoughts с BFS/DFS/beam search; Yao et al. 2023).

**Honest disclaimer**: memory-layer choices никогда не нейтральны по отношению к reasoning. Что вики выводит на поверхность и в каком порядке формирует то что видит модель. Long-context performance position-sensitive (Liu et al. 2023, *Lost in the Middle*), и premise MemGPT в том что paging-policy влияет на task success (Packer et al. 2023). Один из четырёх механизмов ниже (boundary-first autoresearch) явно пересекает agenda-control; включён намеренно и помечен как такой.

---

## Базовая аналогия

Четыре свойства dragon-curve мапятся на memory-system паттерны, валидированные в смежных областях. Слово — *аналог*, не *тождество*.

| Свойство dragon curve | Memory-аналог | Сила аналогии |
|---|---|---|
| Paper-folding рекурсия: `D_{n+1} = D_n · R · swap(reverse(D_n))` | Hierarchical rollup / materialized summary с экспоненциальным fanout | Слабая. Разделяет экспоненциальную batch-структуру, не compaction-семантику. |
| Turn derivable из bits of `n` (regular paperfolding sequence, OEIS A014577) | Deterministic page addresses как организационная конвенция (MVP это creation-order counter, не настоящий content-hash) | Слабая. Deterministic addressing полезен независимо от dragon. |
| Tiling / no self-intersection | Canonical-home coverage: один концепт, одна страница | Средняя. Dedup-lint enforces механически. |
| Boundary dim ≈ 1.523627 vs interior dim 2 | Agent-attention взвешен в сторону frontier-страниц | Эстетическая. Само число fractal-dimension не делает load-bearing работу. |

Кривая полезна для решения *какие knobs затягивать и почему*, не как math-доказательство что какой-либо механизм оптимален.

---

## Mechanism 1 — Fold Operator

После batch-а ингестов, прогнать fold: produce meta-страницу summary'ящую batch, link children обратно, обновить index. Folds стэкуются: после достаточного числа level-`k` folds накопится, level-`k+1` fold даёт super-summary.

Это **hierarchical rollup**, loosely-similar к LSM-tree compaction но с важными отличиями.

**Что разделяет с LSM compaction:**
- Exponential batch fanout через levels (как fixed level-size ratio LevelDB, обычно 10× per level в leveled-mode)
- Periodic consolidation вместо per-write работы

**Что NE наследует от LSM:**
- Нет sorted-key семантики (страницы имеют семантическую, не key-ordered идентичность)
- Нет SSTable/memtable distinction, нет tombstones, нет Bloom-filters
- Нет write-amplification арифметики; нет read-path acceleration
- **Folds аддитивны**: дети остаются на месте. LSM compaction перезаписывает и удаляет. DragonScale-fold ближе к materialized view чем к compaction.

**Trigger options:**
- `2^k` entry count (k=4 ⇒ каждые 16 log-записей). Просто реализовать; straightforward level-математика; игнорирует размер страницы и novelty.
- **Adaptive trigger (предпочтительный для production)**: token-budget (например, fold когда unfolded-batch превышает N токенов), novelty-score (среднее embedding-distance от существующих summary), или staleness-age (last fold > T дней). Phase 1 реализует entry-count для MVP; adaptive-triggers это follow-up.

**Invariants:**
- Idempotent на одном range (re-running это no-op).
- Reversible (children остаются; fold аддитивен).
- Level-bounded: с entry-count trigger `2^k`, fold-depth максимум `⌈log₂(N)⌉` над leaf-страницами. Derived, не empirical.

---

## Mechanism 2 — Deterministic Page Addresses

Каждая новая страница получает stable `address` поле во frontmatter. Phase 2 MVP использует простой creation-order counter:

```yaml
address: c-000042
```

Формат: `c-<6-digit-counter>`. `c-` означает "creation-order counter." Zero-padded.

**Future extension** (задокументировано, не зашиплено в Phase 2):
- Fold-relative path: `f1.2/c-000042` когда folds существуют, где `f1.2` энкодит fold-tree lineage.
- Content hash suffix: `c-000042:h7f3c2` когда hash-rotation policy решена.

**Что Phase 2 MVP даёт:**
- Uniqueness: counter монотонно увеличивается; адреса удалённых страниц retired, никогда не reused.
- Stability: никогда не меняется через content-edits.
- Determinism: derivable из counter-state в `.vault-meta/address-counter.txt`.
- Ordering: сохраняет creation-sequence.

**Что это NE даёт (renamed "content-addressable paths" был misleading в v0.1):**
- **Нет content-addressability в MVP.** Phase 2 address это sequence-counter, не content-hash. Переименование с "content-addressable paths" на "deterministic page addresses" более честно про то что фактически шипится.
- **Нет prompt-cache benefit** (уже исправлено в v0.1 → v0.2). Per Anthropic docs, cache-hits требуют byte-identical prefixes; address-поле во frontmatter помогает только если frontmatter сам внутри cached-блока И остаётся byte-identical. Stable prefixes, не addresses, drive cache-hits.

**Phase 2 exclusions** (всё deferred):
- Backfill legacy pre-Phase-2 страниц (будет использовать `l-` префикс со своим counter'ом).
- Fold-ancestry bit prefix (требует committed folds от future fold-of-folds скилла).
- Content hash suffix (rotation-policy unresolved; см. limitations).

**Implementation** (Phase 2, shipped):
- `scripts/allocate-address.sh`: flock-guarded атомарный allocator. Все counter reads/writes идут через этот скрипт; прямой Write/Edit на `.vault-meta/address-counter.txt` запрещён (зафайерил бы PostToolUse-хук).
- `skills/wiki-ingest/SKILL.md` → Address Assignment секция: opt-in feature-detection; делегирует allocation хелперу; записывает path-to-address mapping в `.raw/.manifest.json` `address_map` для re-ingest стабильности.
- `skills/wiki-lint/SKILL.md` → Address Validation секция: format check, uniqueness check, counter-drift check, address-map consistency check.

**Lint severity model** (матчит `skills/wiki-lint/SKILL.md` Address Validation поведение):
- Post-rollout страницы (frontmatter `created:` >= 2026-04-23, или любая страница новокреатед после DragonScale adoption) без адреса это **errors**. Это silent-regression guard.
- Legacy страницы (`created:` < 2026-04-23) без адресов это **informational**. Опциональный `.vault-meta/legacy-pages.txt` манифест может grandfather страницы чьи `created:` метаданные неправильные или отсутствуют.
- Meta-страницы (`_index.md`, `index.md`, `log.md`, `hot.md`, etc.) и fold-страницы исключены полностью.

---

## Mechanism 3 — Semantic Tiling Lint

Tiling-property говорит что один концепт должен жить в одной canonical-странице. Enforce embedding-based dedup-чеком в `wiki-lint`.

**Procedure (calibrated, не guess):**
1. Compute embeddings для каждой страницы. Default-модель: local `nomic-embed-text` через ollama на `http://127.0.0.1:11434`. Стоимость: только local hardware time (нет API fees). Скрипт поддерживает remote-override под `--allow-remote-ollama`; remote-эндпоинты могут incur provider API fees.
2. Compute pairwise cosine similarities для всех page-pairs.
3. **Calibration** (one-time, до first-use): label 50-100 in-vault page-pairs как duplicate/near/distinct; найти thresholds оптимизирующие target precision для каждой полосы.
4. **Default bands** (используются до calibration, потом refined):
   - `≥ 0.90` — near-duplicate, lint error
   - `0.80 – 0.90` — review bucket, lint warning
   - `< 0.80` — distinct, no flag
5. Никогда auto-merge. Output review-list.

**Почему не fixed 0.85?** v0.1 использовала 0.85 без обоснования. Опубликованные thresholds в embeddings-литературе span wide-range (Sentence Transformers `community_detection` defaults на 0.75; Quora-duplicate calibrations land around 0.77–0.83; sparse-model defaults differ again). Thresholds зависят от модели, корпуса, objective, поэтому calibration требуется.

---

## Mechanism 4 — Boundary-First Autoresearch

> **Status: shipped (Phase 4, opt-in)** на 2026-04-24. Implementation: `scripts/boundary-score.py`. Integration: `skills/autoresearch/SKILL.md` Topic Selection секция B. Tests: `tests/test_boundary_score.py`.

Boundary-страницы (high out-degree relative to in-degree, recency-weighted) это frontier вольта. `/autoresearch` invoked без topic'а читает top-5 boundary-страниц и предлагает их как research-кандидаты; пользователь выбирает одну (или вводит free-text topic, или declines all и fallback в original ask-user mode).

**Formula (exact)**:

```
out_degree(p) = count of distinct filename-stem wikilinks в body of p резолвящихся в scoreable-страницы
in_degree(p)  = count of distinct scoreable-страниц чьё body содержит wikilink на p
recency_weight(p) = exp(-days_since_updated / 30)      # no floor; old pages approach 0
boundary_score(p) = (out_degree - in_degree) * recency_weight
```

**Link resolution**: filename-stem only. `[[Foo]]` резолвится в `Foo.md` где угодно в вольте. Aliases declared через frontmatter `aliases:` НЕ парсятся. Folder-qualified links (например `[[notes/Foo]]`) резолвятся через stem alone. Это матчит default-поведение Obsidian для unique-filenames но не имплементит full alias-resolution.

**Scoreable** = любая страница NE excluded любым из:
- frontmatter `type: meta` или `type: fold`
- filename в `{_index.md, index.md, log.md, hot.md, overview.md, dashboard.md, Wiki Map.md, getting-started.md}`
- path-prefix в `wiki/folds/` или `wiki/meta/`
- symlinks или paths чей resolved-target escapes vault-root (rejected at scan-time)

**Code-block filtering**: triple-backtick И triple-tilde fenced-code-blocks skipped, с CommonMark-like length tracking чтобы longer-opening fence не закрывался shorter-inner fence. Indented code-blocks (4+ spaces) NE filtered потому что Obsidian bullet-lists commonly использует 4-space indentation и содержит реальные wikilinks. См. `scripts/boundary-score.py:RECENCY_HALFLIFE_DAYS` единственная tunable-константа.

**Honest labeling**: этот механизм это **agenda control**, не pure-память. Он формирует что агент исследует следующим. Включён в DragonScale потому что direct-consequence dragon-curve boundary-аналогии и потому что naturally-pairs с folds (свежеfolded-страницы имеют low out-degree; frontier-страницы pre-fold). Но "memory only, not reasoning" framing не покрывает это. Пользователи которые хотят strict memory-layer subset должны omit этот механизм (просто не invoke `/autoresearch` без topic'а, или не setup `scripts/boundary-score.py`).

**Что NE включено**:
- Нет auto-triggering. `/autoresearch` всё ещё user-invoked.
- Нет persistent boundary-score cache. Scoring O(N * avg_links) и runs на каждом invocation из fresh wiki/-state.
- Нет интеграции с folds или addresses. Pure graph-analysis на wikilink-графе.
- Нет automatic topic-selection без user-confirmation. Helper представляет choices; пользователь выбирает.

---

## Operational Policies (требуются до implementation)

Adversarial review флагнул эти gaps в v0.1. Каждое должно быть decided до того как corresponding-фаза шипится.

| Policy | Phase 0 position | Decision point |
|---|---|---|
| **Retention / GC** | Нет automatic deletion. Страницы permanent. | Revisit если вольт превысит ~5000 страниц. |
| **Tombstones** | Нет. Удалённые страницы removed через git revert. | Revisit если delete-events станут common. |
| **Versioning** | Полагается на git history, не in-vault versioning. | Address-hash rotation policy doubles как coarse version-signal. |
| **Conflict resolution для contradictory folds** | Meta-страница должна quote оба источника с explicit "conflict" callout. Нет automatic resolution. | Phase 1 spec required. |
| **Concurrency / atomicity** | Single-writer assumption (одна Claude-сессия за раз). PostToolUse auto-commit serializes. | Multi-writer case deferred. |
| **Provenance для meta-страниц** | Каждая fold-страница должна включать frontmatter listing children и fold level. | Phase 1 must enforce. |
| **Access control** | Out of scope. Это single-user vault. | Revisit только если shared. |

---

## Mapping в Claude-Obsidian

| Mechanism | Status | New | Extends |
|---|---|---|---|
| Fold operator | shipped (Phase 1, dry-run verified) | `skills/wiki-fold/` | reads `log.md`, writes `wiki/folds/`, updates `index.md` on commit |
| Address anchors | shipped (Phase 2, opt-in) | `scripts/allocate-address.sh`, новое frontmatter-поле | `wiki-ingest` (assignment), `wiki-lint` (validation) |
| Semantic tiling | shipped (Phase 2/3, opt-in) | `scripts/tiling-check.py`, `.vault-meta/tiling-thresholds.json` | `wiki-lint` с banded-thresholds, calibration-procedure documented |
| Boundary-first | shipped (Phase 4, opt-in) | `scripts/boundary-score.py`, `tests/test_boundary_score.py` | `skills/autoresearch/SKILL.md` Topic Selection секция B; `commands/autoresearch.md` no-topic path |

Существующая иерархия hot → index → domain → page уже implement self-similarity через scales. Это единственное dragon-curve-свойство, которое этот вольт имел до DragonScale.

---

## Почему это поверх альтернатив

| Pattern | Что даёт | Что DragonScale добавляет |
|---|---|---|
| MemGPT virtual context (two-tier paging) | Main context ↔ external context swap | Больше двух levels; explicit fold-triggers; dedup-lint |
| Pure LSM compaction | Exponential write-path throughput | Semantic-layer механизмы (tiling, boundary); additive-rollups вместо destructive-merges |
| Ad-hoc `/save` | Human-triggered filing | Rule-based fold cadence |
| Vector-only RAG | Retrieval | Canonical-home structure; lineage-addresses |

DragonScale composes паттерны валидированные в смежных системах: LSM *batching* (databases), MemGPT *paging* (agents), Anthropic *cache ordering* (prompt engineering), embedding *dedup* (knowledge graphs).

---

## Известные ограничения (v0.3)

- **Не валидировано на масштабе.** Все четыре механизма theoretical; ни один не tested на multi-thousand-page вольте.
- **Fold cadence это knob, не theorem.** `k=4` это starting guess. Adaptive-triggers вероятно лучше.
- **Address stability unsolved.** Hash-rotation на edits known issue; deferred.
- **Boundary-first crosses scope.** Включено с warning, не quietly.
- **Calibration load.** Tiling требует one-time labeling pass; без неё applied только defaults.

---

## Primary Sources

Verified против primary-sources на 2026-04-23. **Scope of tagging**: специфические numeric values, formulas, named patterns ниже tagged **[sourced]** когда directly citable, **[derived]** когда derivable из sourced material, или **[conjecture]** когда based на reasoning без specific source. **Не tagged** (и читатели должны treat as interpretive synthesis): framing-предложения в body такие как "composes patterns validated," "self-similarity already exists," design-rationale tying четыре механизма вместе. Это editorial, не source-backed.

**Dragon curve math [sourced]**
- Boundary dimension `2·log₂(λ)` где `λ³ − λ² − 2 = 0`, дающее 1.523627086: [Dragon curve, Wikipedia](https://en.wikipedia.org/wiki/Dragon_curve)
- Paper-folding construction и OEIS A014577: [Regular paperfolding sequence, Wikipedia](https://en.wikipedia.org/wiki/Regular_paperfolding_sequence); [OEIS A014577](https://oeis.org/A014577)
- Tiling и rep-tiles: [Wolfram Demonstrations: Tiling Dragons and Rep-tiles of Order Two](https://demonstrations.wolfram.com/TilingDragonsAndRepTilesOfOrderTwo/)

**LSM trees [sourced]**
- Level size ratios и compaction semantics: [RocksDB Compaction wiki](https://github.com/facebook/rocksdb/wiki/Compaction), [RocksDB Tuning Guide](https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide), [How to Grow an LSM-tree? (2025)](https://arxiv.org/abs/2504.17178)
- LevelDB 10× level ratio: referenced в arXiv-paper выше. Treat as *typical*, не required.

**LLM memory architectures [sourced]**
- OS-inspired paging: [MemGPT: Towards LLMs as Operating Systems (Packer et al. 2023)](https://arxiv.org/abs/2310.08560)
- Position sensitivity: [Lost in the Middle (Liu et al. 2023)](https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00638/119630/Lost-in-the-Middle-How-Language-Models-Use-Long)
- Note-based agentic memory: [A-Mem (2025)](https://arxiv.org/abs/2502.12110)

**Prompt caching [sourced]**
- Byte-identical prefix-требование, breakpoint-механика, TTL-options: [Anthropic Prompt Caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

**Embedding thresholds [sourced]**
- Sentence Transformers defaults и calibration-examples: [Sentence Transformers util](https://sbert.net/docs/package_reference/util.html), [SBERT evaluation docs](https://sbert.net/docs/package_reference/sentence_transformer/evaluation.html)

**Reasoning search (out of scope, cited только to justify scope-boundary) [sourced]**
- [Tree of Thoughts (Yao et al. 2023)](https://arxiv.org/abs/2305.10601)

**Items marked [conjecture] in this doc:**
- `k=4`/`k=5` starting-value для fold-cadence (нужен empirical-tuning)
- `~30s` full-vault embedding-pass time (нужно measurement)
- `boundary_score` formula exact-weighting (plausible-starting-form; не valid against retrieval-metrics)

**Items marked [derived]:**
- `⌈log₂(N)⌉` fold-depth-bound (trivially-derivable из entry-count-trigger)
- Default tiling-bands `{≥0.90, 0.80-0.90, <0.80}` до calibration (interpolated из cited-ranges в Sentence Transformers examples; не optimal by construction)

---

## Review History

**v0.1 (2026-04-23, initial draft)** — written после verification-pass против Wikipedia, arXiv, Anthropic-docs. Четыре механизма proposed.

**v0.4 (2026-04-24, Phase 4 shipped)** — Mechanism 4 (boundary-first autoresearch) implemented as `scripts/boundary-score.py` с `tests/test_boundary_score.py` covering parsing, recency-weight, wikilink-extraction (с fence-length + tilde + indented-block tests), graph-construction (self-loop/unresolved/meta-target exclusion), symlink-rejection, CLI-surface (`--top`, `--page`, `--json`). Integrated в `skills/autoresearch/SKILL.md` как opt-in Topic Selection mode с explicit helper-failure fallback. Spec's "NOT IMPLEMENTED" marker removed; exact scoring-formula (no recency floor), filename-stem-only resolution-disclosure, scope, "what is NOT included" секция added. Phase 3.6 pre-Phase-4 hardening shipped concurrently (5 fixes: `--report` path-confinement, rollout-baseline, AGENTS.md consistency, wiki-ingest .raw contradiction, install-guide version).

**v0.3 (2026-04-23, Phase 2 alignment)** — Mechanism 2 rewritten to match actual Phase 2 MVP shipped в `wiki-ingest` и `wiki-lint`. Renamed from "Content-Addressable Paths" to "Deterministic Page Addresses" (MVP это creation-order counter, не content-hash). Documented extension-path для fold-ancestry bits и content-hash suffix, оба explicitly deferred.

**v0.2 (2026-04-23, post-adversarial review)** — после `codex exec` adversarial-review. Все 7 critiques accepted:

1. *LSM "structurally identical"* → weakened к "loosely analogous to hierarchical rollup"; non-inherited properties listed explicitly.
2. *Prompt cache address benefit* → removed strong claim; narrowed к organizational convention.
3. *0.85 threshold* → replaced с calibration-procedure и banded defaults.
4. *2^k cadence* → justified как implementation convenience; adaptive-trigger flagged as preferred для production.
5. *Scope-boundary contradiction* → acknowledged; boundary-first explicitly labeled как agenda-control.
6. *Missing production mechanisms* → added Operational Policies секция (retention, versioning, conflict-resolution, concurrency, provenance).
7. *Unverified claims* → tagged specific numeric-values, formulas, named-patterns как [sourced], [derived], or [conjecture]. Editorial-synthesis в body explicitly flagged как not tagged (см. scope-note под Primary Sources).

---

## Связи

См. [[LLM Wiki Pattern]] broader-паттерн который это extends.
См. [[Compounding Knowledge]] почему persistent-state precondition для DragonScale.
См. [[Hot Cache]] существующий 500-словный session-context, который level-0 manual fold.
См. [[Andrej Karpathy]] intellectual-lineage.
