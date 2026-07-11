# DragonScale Memory Guide

DragonScale Memory — опциональное расширение `llm-obsidian`. Добавляет conservative-helpers для log rollups, стабильных адресов страниц, lint'а duplicate-страниц, frontier topic-suggestion. Начните с [README](../README.ru.md). Для design-спеки и rationale — [wiki/concepts/DragonScale Memory.md](../wiki/concepts/DragonScale%20Memory.md).

Эта страница близка к shipped-поведению в `v1.6.0`. Объясняет что setup создаёт, что каждый механизм фактически делает, что ему нужно, и как безопасно его отключить без удаления репо.

## Что такое DragonScale

### Scope и opt-in статус

DragonScale — memory-layer расширение для вики. Покрывает rollups, deterministic page-IDs, duplicate-detection, и один opt-in topic-selection путь для `/autoresearch`. Не требуется для базового вольта.

Если вы никогда не запускали `bash bin/setup-dragonscale.sh`, base-инсталл и оригинальное skill-поведение остаются нетронутыми. Репо использует feature-detection чтобы DragonScale оставался опциональным вместо hard-dependency.

Концепт-страница шире чем этот guide. Этот guide — operational. Когда spec и implementation отличаются в детали, предпочитайте shipped-скрипты и скиллы для day-to-day поведения.

### Что зашиплено в 1.6.0

Версия `1.6.0` шипит все четыре DragonScale-механизма как opt-in features:

- Mechanism 1, Fold Operator: `skills/wiki-fold/`
- Mechanism 2, Deterministic Page Addresses: `scripts/allocate-address.sh` плюс `wiki-ingest` и `wiki-lint` интеграция
- Mechanism 3, Semantic Tiling Lint: `scripts/tiling-check.py` плюс `wiki-lint` интеграция
- Mechanism 4, Boundary-First Autoresearch: `scripts/boundary-score.py` плюс `skills/autoresearch/SKILL.md` Topic Selection логика

Используйте `CHANGELOG.md` для release-trail, [docs/install-guide.md](./install-guide.md) для quick-start view, [wiki/concepts/DragonScale Memory.md](../wiki/concepts/DragonScale%20Memory.md) для полного design-контекста.

## Перед включением

### Base-install требования

DragonScale это add-on, не замена base-setup. Сначала сделайте обычный vault-инсталл по [docs/install-guide.md](./install-guide.md).

Минимум:

- clone репо или установить плагин
- запустить `bash bin/setup-vault.sh`
- открыть папку как Obsidian-вольт
- использовать `/wiki` для scaffold или continue setup

Setup-скрипт DragonScale принимает один опциональный аргумент — путь к вольту:

```bash
bash bin/setup-dragonscale.sh
```

```bash
bash bin/setup-dragonscale.sh /path/to/vault
```

Если опустить путь, использует repo-root inferred из `bin/`.

### Universal prerequisite: Python 3

Mechanism 2 и cache-lock Mechanism 3 используют stdlib `fcntl.flock`; внешний бинарник `flock` и пакет `util-linux` не нужны. Публичный `scripts/allocate-address.sh` остаётся стабильной shell-обёрткой над `allocate-address.py`, поэтому вызовы skills не меняются. На Windows `fcntl` недоступен; toolkit ориентирован на macOS/Linux.

### Mechanism 3 дополнительные prerequisites: python3, ollama, bge-m3

Mechanism 3 — единственный механизм с full local embeddings stack'ом. Нужны `python3`, `ollama`, и модель `bge-m3` спулленная в ollama.

Useful checks:

```bash
command -v python3
```

```bash
curl -sS http://127.0.0.1:11434/api/version
```

```bash
ollama pull bge-m3
```

Setup-скрипт ничего из этого не устанавливает. Только проверяет и репортит status. Mechanism 4 требует `python3`, но не ollama. Mechanisms 1 и 2 не требуют ни того, ни другого.

### Что происходит когда optional deps отсутствуют

DragonScale задизайнен fail-closed или no-op cleanly.

Если `python3` отсутствует:

- Mechanism 3 не может работать
- Mechanism 4 не может работать
- Mechanisms 1 и 2 всё ещё работают

Если ollama unreachable, `scripts/tiling-check.py` exits `10`. Если ollama reachable но `bge-m3` не установлена, exits `11`. `wiki-lint` ожидаемо трактует это как skip-conditions для semantic-tiling, не как причину сломать остальной lint-flow.

Если boundary-helper падает, `/autoresearch` fallback'ит к нормальному ask-the-user topic-пути. Не форсит candidate-list и не improvise topic.

Если DragonScale-setup никогда не запускался, `wiki-ingest` и `wiki-lint` сохраняют их non-DragonScale поведение.

## Setup

### Запуск bin/setup-dragonscale.sh

Запустить:

```bash
bash bin/setup-dragonscale.sh
```

Скрипт идемпотентный. Безопасно перезапускать, и не overwrite-ит runtime-файлы которые уже создал.

Перед provisioning state, верифицирует:

- `scripts/allocate-address.sh`
- `scripts/tiling-check.py`
- `skills/wiki-fold/SKILL.md`

Если что-то отсутствует, setup останавливается и говорит переустановить плагин.

Что setup делает:

- делает `scripts/allocate-address.sh` executable
- делает `scripts/tiling-check.py` executable
- создаёт `.vault-meta/` если нужно
- создаёт address, tiling, legacy-baseline state-файлы если отсутствуют
- создаёт `.raw/.manifest.json` если отсутствует
- запускает sanity-checks в конце

Что setup НЕ делает:

- установить ollama
- pull `bge-m3`
- backfill адресов на старых страницах
- запустить fold
- запустить semantic tiling
- переписать существующие wiki-страницы

### Какие файлы и state создаёт

Setup provisions небольшое количество runtime-state.

В `.vault-meta/` создаёт:

- `address-counter.txt`
- `tiling-thresholds.json`
- `legacy-pages.txt`

В `.raw/` создаёт:

- `.manifest.json`

`address-counter.txt` стартует на `1`, так что next-reserved page-address в brand-new вольте будет `c-000001`.

`tiling-thresholds.json` seeded с `error: 0.90`, `review: 0.80`, `calibrated: false`. Это conservative seed-bands, не calibrated truth для вашего вольта.

`legacy-pages.txt` получает rollout marker comment:

```text
# rollout: YYYY-MM-DD
```

`wiki-lint` использует этот baseline для разделения legacy-страниц от post-rollout-страниц для address enforcement.

`.raw/.manifest.json` стартует с пустыми `sources` и `address_map` объектами. Ingest-скилл поддерживает этот файл. Source-документы под `.raw/` остаются immutable.

### Как verify setup

Setup-скрипт уже выполняет sanity-checks, но полезно verify несколько вещей самим.

Check next-address без reserve:

```bash
./scripts/allocate-address.sh --peek
```

Check что runtime-state существует:

```bash
ls -1 .vault-meta
```

Check tiling-readiness без compute embeddings:

```bash
python3 ./scripts/tiling-check.py --peek
```

Check boundary-helper:

```bash
python3 ./scripts/boundary-score.py --top 5
```

Если ваш вольт небольшой или tightly-integrated, boundary-helper может репортить no-positive-score frontier-страниц. Это всё ещё валидный run.

## Mechanism 1: Fold Operator

### Что делает

Fold-оператор — deterministic log-rollup. Каждая operation-log запись получает SHA-256 по canonical text. Helper берёт самые старые `2^k` IDs, которых ещё нет в `entry_ids` существующих `wiki/folds/*.md`.

Fold аддитивен: не удаляет, не двигает и не переписывает children. Extract берётся из первой meaningful строки каждой записи. Fold-operation записи исключены из входа, поэтому flat folds не начинают рекурсивно поглощать собственный audit trail.

Текущий shipped-скилл намеренно narrow. Поддерживает flat-fold над raw log-записями. Hierarchical fold-of-folds поведение остаётся вне scope текущего скилла, даже если concept-spec обсуждает stacked-folds.

Fold-ID содержит boundary hashes:

```text
fold-k{K}-{OLDEST-ID[:12]}-{NEWEST-ID[:12]}-n{COUNT}
```

Это даёт structural-идемпотентность без mutable counter. Удаление fold page намеренно снова делает её entry IDs необработанными.

### Когда использовать

Используйте fold когда log накопил coherent batch работы и хочется checkpoint-страницу проще для скана чем raw run записей.

Типичные кейсы:

- после нескольких ингестов на одну тему
- после burst research-сессий
- до того как long flat `wiki/log.md` станет hard to use

Не трактуйте folds как garbage-collection. Они summary'ят. Не compact'ят через deletion.

Пример команды:

```text
python3 scripts/fold-log.py status --json
python3 scripts/fold-log.py
```

Default batch — `2^6 = 64`. Для явного maintenance-прогона можно передать `--k 3`; partial batch не создаётся.

### Dry-run vs commit-режим

Dry-run — default и stdout-only.

В dry-run режиме:

- ни один файл не записывается
- ни один auto-commit-хук не триггерится
- получаете полностью детерминированный fold-content в terminal-output

В commit-режиме:

- fold-страница записывается в `wiki/folds/`
- `wiki/log.md` получает новую fold-запись

Fold page и log entry land через одну `vault-write.py` transaction; folder index регенерирует Stop.

Пример commit-команды:

```text
python3 scripts/fold-log.py --commit
```

Запустить dry-run сначала. Commit только после явного подтверждения пользователя.

Чтобы отключить Mechanism 1 без uninstall DragonScale, перестать invoke `wiki-fold`. Существующие fold-страницы могут оставаться в вольте, или можно удалить вручную.

## Mechanism 2: Deterministic Page Addresses

### Address-формат и rollout policy

Mechanism 2 назначает stable frontmatter-адреса. Shipped-формат:

```yaml
address: c-000042
```

`c-` означает creation-order counter. Numeric-часть zero-padded до 6 digits. Это не content-hash. Spec явно говорит что shipped-address detrministic и stable, но не content-addressable.

Rollout-baseline `2026-04-23`. После DragonScale-adoption, post-rollout non-meta страницы expected to have addresses. Legacy-страницы exempt пока вы не сделаете deliberate-backfill.

Helper имеет три real modes:

```bash
./scripts/allocate-address.sh
```

```bash
./scripts/allocate-address.sh --peek
```

```bash
./scripts/allocate-address.sh --rebuild
```

Default-mode reserves и печатает next-address. `--peek` read-only. `--rebuild`
поднимает counter минимум до highest observed `c-NNNNNN` + 1, но никогда не
уменьшает уже валидный counter: зарезервированные или удалённые addresses не
переиспользуются.

Пример команды:

```bash
./scripts/allocate-address.sh --peek
```

### Как ingest и lint используют

`wiki-ingest` enables address-assignment только когда `./scripts/allocate-address.sh` executable и `./.vault-meta` существует. Если оба условия true, новые non-meta страницы получают `address:` во frontmatter. Если нет, ingest продолжается без addresses.

`wiki-lint` enables address-validation только когда `./scripts/allocate-address.sh` executable и `./.vault-meta/address-counter.txt` существует. Если эти условия true, lint проверяет address-формат, uniqueness, counter-consistency против `--peek`, missing-addresses на post-rollout страницах, address-map consistency в `.raw/.manifest.json`. `peek` обязан быть больше любого наблюдаемого address; gaps выше `highest + 1` допустимы и репортятся как warning, потому что reservation может пережить неуспешную запись или удаление страницы.

Single-writer rule важен здесь. Allocator сериализует counter через `fcntl`, но не сериализует запись самих страниц. Не запускайте параллельные ингесты из multiple-сессий или sub-агентов которые пишут один и тот же page path.

Одно hard-rule из skill-доков стоит повторить. Никогда не редактировать `.vault-meta/address-counter.txt` напрямую. Mutate только через `scripts/allocate-address.sh`.

Чтобы отключить Mechanism 2 без uninstall:

1. перестать запускать ингесты которые depend от address-assignment
2. удалить `.vault-meta/` если хотите feature-detection turn off
3. перестать использовать `./scripts/allocate-address.sh`

Existing `address:` поля могут оставаться на страницах. Они становятся inert-метаданными если feature disabled.

## Mechanism 3: Semantic Tiling Lint

### Что проверяет

Mechanism 3 — embedding-based duplicate-page detector. Сканирует markdown-файлы под `wiki/` и исключает:

- `wiki/folds/`
- `wiki/meta/`
- common meta-filenames такие как `index.md`, `log.md`, `hot.md`, `overview.md`, `dashboard.md`, `Wiki Map.md`, `getting-started.md`
- файлы с `type: meta`
- файлы с `type: fold`
- symlinks или paths которые escape vault-root

Computes one-embedding на included-страницу, сравнивает pairs по cosine similarity, эмитит candidate-overlap в bands.

Default bands:

- `>= 0.90` как error
- `0.80 - 0.90` как review
- `< 0.80` как pass

Helper никогда auto-merge'ит страницы. Только репортит candidates для review.

Пример команды:

```bash
python3 ./scripts/tiling-check.py --peek
```

Это даёт structured-диагностику без compute embeddings.

### Local embeddings требование

По умолчанию helper доверяет только local ollama-эндпоинту на `http://127.0.0.1:11434`. Remote ollama-эндпоинты требуют explicit override-флаг потому что page-bodies отправляются как embedding-input.

Remote-override пример:

```bash
python3 ./scripts/tiling-check.py --allow-remote-ollama --peek
```

Normal ready-path — local:

1. `python3` установлен
2. ollama reachable на localhost
3. `bge-m3` установлена в ollama

Important exit codes:

- `0` success
- `10` ollama unreachable
- `11` model missing

`wiki-lint` написан трактовать их как skip-conditions.

### Calibration и no-op поведение

Shipped-thresholds — conservative seeds, не calibrated-truth. Skill-доки calls for manual one-time calibration-pass per-vault. Пока вы это не сделаете, expect both false-negatives и false-positives.

Helper также имеет intentional no-op поведение. Если ollama или модели нет, exits с skip-кодом. Не fake'ит результаты.

Useful commands:

```bash
python3 ./scripts/tiling-check.py --peek
```

```bash
python3 ./scripts/tiling-check.py --rebuild-cache
```

```bash
python3 ./scripts/tiling-check.py --report wiki/meta/tiling-report-YYYY-MM-DD.md
```

`--report` real и path-confined к вольту. Используйте когда хотите saved-report. `--peek` когда только хотите readiness и diagnostics.

Чтобы отключить Mechanism 3 без uninstall:

1. перестать запускать `python3 ./scripts/tiling-check.py`
2. перестать использовать semantic-tiling путь в `wiki-lint`
3. не provision'ить ollama или модель если не нужны

Заметьте что `.vault-meta/` shared-gate для Mechanisms 2, 3, 4. Не удаляйте чтобы отключить Mechanism 3 alone — также turn off address-allocation и boundary-first autoresearch. Tiling-cache живёт под `.vault-meta/` но inert когда helper не invoked.

## Mechanism 4: Boundary-First Autoresearch

### Что делает

Mechanism 4 scores frontier-страницы в wiki-graph. Shipped-формула:

```text
boundary_score(p) = (out_degree(p) - in_degree(p)) * recency_weight(p)
```

На практике, high-score страницы point outward к много scoreable-страниц, receive relatively fewer inbound-links, и были обновлены недавно достаточно чтобы оставаться frontier-like.

Helper читает `wiki/**/*.md`, строит wikilink-граф, эмитит ranked-results в stdout или JSON. Намеренно stdout-only. В отличие от tiling-helper'а, не имеет `--report PATH` mode.

Пример команды:

```bash
python3 ./scripts/boundary-score.py --json --top 5
```

Это exact-команда которую autoresearch-скилл использует для candidate-generation.

### Agenda-control caveat

Этот caveat явный в spec и skill-доках.

Это agenda-control, не pure-память.

Mechanism 4 не просто описывает вольт. Влияет на то что агент likely исследует следующим. Это пересекает memory- и planning-границу.

Проект держит это opt-in и labels honestly. Если хотите strict memory-layer subset only, omit этот путь. Не используйте `/autoresearch` без topic, или не setup и не invoke boundary-scorer.

### Как /autoresearch ведёт себя с и без

С Mechanism 4 available, и только когда `/autoresearch` invoked без topic, скилл:

1. checks для `scripts/boundary-score.py`
2. checks для `./.vault-meta`
3. checks для `python3`
4. runs `./scripts/boundary-score.py --json --top 5`
5. presents top frontier-страницы как candidate-topics
6. lets user pick, override free-текстом, или decline

Если helper exits non-zero, returns invalid JSON, или returns empty `results`-array, скилл falls back.

Без Mechanism 4, или после fallback, `/autoresearch` просто спрашивает:

```text
What topic should I research?
```

Helper suggests. Пользователь всё ещё decides.

Чтобы отключить Mechanism 4 без uninstall:

1. перестать запускать `python3 ./scripts/boundary-score.py`
2. использовать `/autoresearch [topic]` с explicit-topic
3. избегать no-topic `/autoresearch` пути если не хотите frontier-suggestions

Заметьте что `.vault-meta/` shared-gate для Mechanisms 2, 3, 4. Не удаляйте чтобы отключить Mechanism 4 alone. Scorer сам по себе read-only и не использует shared-state; disabling = просто не invoke.

## Operational Policies

### Single-writer rule

Allocator `fcntl`-guarded и безопасен для параллельных reservations, но writers всё равно должны использовать optimistic hashes и не менять один page path одновременно.

Ingest-скилл явный здесь. Не запускать параллельные ингесты из multiple Claude-сессий или sub-агентов которые assign addresses.

Safe operating-policy:

- один active-ingest-writer одновременно
- один address-allocator path одновременно
- никаких прямых manual-edits counter-state

Mechanism 1 human-invoked и легко serialize. Mechanism 3 использует lock для cache I/O. Mechanism 4 read-only.

### Feature-detection и graceful fallback

DragonScale задизайнен feature-detected, не assumed.

`wiki-ingest` only assigns addresses когда allocator executable и `.vault-meta/` существует.
`wiki-lint` only validates addresses когда allocator существует и `.vault-meta/address-counter.txt` существует.
`wiki-lint` only runs semantic-tiling когда helper существует и `python3` available, потом интерпретирует readiness из `--peek`.
`autoresearch` only uses boundary-first selection когда helper существует, `.vault-meta/` существует, и `python3` присутствует.

Когда эти conditions не met, репо falls back к earlier-поведению. Это intended operational-posture.

## Troubleshooting

### Address lock busy

Allocator ждёт `.vault-meta/.address.lock` до пяти секунд. Если он возвращает `ERR: could not acquire...`, дождитесь завершения другого writer и повторите вызов. Не обходите lock прямым редактированием `.vault-meta/address-counter.txt`; при подозрении на drift используйте `./scripts/allocate-address.sh --rebuild`.

macOS-specific подсказки: см. [`wiki/concepts/DragonScale on macOS.md`](../wiki/concepts/DragonScale%20on%20macOS.md).

### Missing ollama или model

Это блокирует только Mechanism 3. Не блокирует остальной DragonScale.

Check ollama reachability:

```bash
curl -sS http://127.0.0.1:11434/api/version
```

Check tiling readiness:

```bash
python3 ./scripts/tiling-check.py --peek
```

Если helper exits `10`, ollama не reachable. Если exits `11`, pull model:

```bash
ollama pull bge-m3
```

Потом rerun:

```bash
python3 ./scripts/tiling-check.py --peek
```

Помните что Mechanism 4 не нуждается в ollama. Если хотите только boundary-first autoresearch, `python3` достаточно.

### Safe rollback / disable path

Не нужно uninstall репо чтобы выключить DragonScale. Используйте smallest-rollback который fits то что хотите:

- Mechanism 1: перестать invoke `wiki-fold`. Не использует shared-state.
- Mechanism 2: перестать использовать `./scripts/allocate-address.sh`. Существующие `address:` frontmatter-поля остаются как plain-content.
- Mechanism 3: перестать запускать `python3 ./scripts/tiling-check.py` и перестать invoke semantic-tiling путь в `wiki-lint`. Cache под `.vault-meta/` inert когда не used.
- Mechanism 4: перестать запускать `python3 ./scripts/boundary-score.py` и избегать no-topic `/autoresearch` пути. Scorer read-only; disabling = не invoke.

`.vault-meta/` — shared-gate для Mechanisms 2, 3, 4. Removing disables все три вместе, не только один.

Если хотите выключить DragonScale feature-detection across setup-based mechanisms сразу, удалите `.vault-meta/`:

```bash
rm -rf .vault-meta
```

Потом перестать invoke DragonScale-specific helpers и скиллы. Это leaves ваш normal wiki-content intact. Не удаляет fold-страницы и не strip существующие `address:` поля из frontmatter. Они остаются как plain-content unless вы выберете cleanup-ить вручную.

Если позже хотите DragonScale назад, rerun:

```bash
bash bin/setup-dragonscale.sh
```
