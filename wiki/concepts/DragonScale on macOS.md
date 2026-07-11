---
type: concept
address: c-000005
title: "DragonScale on macOS"
created: 2026-04-26
updated: 2026-07-11
tags:
  - dragonscale
  - macos
  - compatibility
  - install
status: developing
related:
  - "[[DragonScale Memory]]"
  - "[[index]]"
sources: []
sessions:
  - public-template-v2
---

# DragonScale on macOS

DragonScale (опциональная memory-layer надстройка `llm-obsidian` из четырёх механизмов) содержит три неявных предположения о GNU-окружении, которые ломаются на чистой macOS-установке. У каждого есть маленький идемпотентный фикс. Никаких прав root, никакой миграции с системного Python.

## Три места, где `bin/setup-dragonscale.sh` ломается на свежем Mac

### 1. Нет `flock`

`scripts/allocate-address.sh` использует `flock` для межпроцессной блокировки счётчика адресов. macOS по умолчанию `flock` не поставляет. Скрипт падает так:

```
./scripts/allocate-address.sh: line 36: flock: command not found
ERR: could not acquire address allocator lock within 5s
```

Это полностью блокирует Mechanism 2 (Deterministic Addresses) и частично ломает lint-хелперы, которые от него зависят.

**Фикс.** Поставить через Homebrew. Формула `util-linux` помечена как keg-only (Apple поставляет свои `getopt`, `cal` и т.д.), поэтому бинарники не появляются в `PATH` автоматически:

```bash
brew install util-linux
# в ~/.zshrc:
export PATH="/opt/homebrew/opt/util-linux/bin:$PATH"
export PATH="/opt/homebrew/opt/util-linux/sbin:$PATH"
# fish (идемпотентно и идиоматично):
fish_add_path -g /opt/homebrew/opt/util-linux/bin /opt/homebrew/opt/util-linux/sbin
```

Открыть новый шелл. `which flock` должно отдавать `/opt/homebrew/opt/util-linux/bin/flock`. Компиляторные envvar'ы (`LDFLAGS`, `CPPFLAGS`, `PKG_CONFIG_PATH`), которые показывает `brew info util-linux`, для DragonScale-скриптов не нужны: важен только `flock` в `PATH`.

### 2. Системный Python 3.9 не поддерживает PEP 604

`scripts/tiling-check.py` и `scripts/boundary-score.py` содержат аннотации вида:

```python
report_path: Path | None
date_str: str | None
```

PEP 604 (`X | Y` как тип-объединение во время выполнения) требует Python 3.10+. macOS Command Line Tools поставляют Python 3.9.6 по умолчанию, и эта же строка падает с:

```
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
```

Это ломает Mechanism 3 (Semantic Tiling Lint) и Mechanism 4 (Boundary-First Autoresearch).

**Фикс.** Добавить `from __future__ import annotations` в начало каждого скрипта. PEP 563 делает все аннотации lazy-строками, и PEP 604-синтаксис становится валидным на Python 3.7+ без рантайм-стоимости. Две однострочные правки в `scripts/tiling-check.py` и `scripts/boundary-score.py`. Логика не меняется, тесты на Python 3.9.6 проходят как есть.

Это правильный фикс, а не апгрейд Python: (а) не трогает пользовательское окружение, (б) на Linux ведёт себя идентично, (в) канонический Python-идиом для back-port'а современных аннотаций.

### 3. BSD `wc -l` пишет с ведущими пробелами

`tests/test_allocate_address.sh` проверяет конкурентную аллокацию адресов так:

```bash
UNIQ=$(sort -u concurrent.txt | wc -l)
TOTAL=$(wc -l < concurrent.txt)
assert_eq "10 concurrent allocs: unique count" "10" "$UNIQ"
```

GNU `wc -l` на Linux выводит `10`. BSD `wc -l` на macOS выводит `      10` (выравнивание по правому краю до 8 колонок). Сравнение строк падает:

```
FAIL 10 concurrent allocs: unique count: expected '10', got '      10'
```

`make test` становится красным на любой чистой macOS-установке, хотя сам аллокатор работает корректно.

**Фикс.** Срезать пробелы:

```bash
UNIQ=$(sort -u concurrent.txt | wc -l | tr -d '[:space:]')
TOTAL=$(wc -l < concurrent.txt | tr -d '[:space:]')
```

`tr -d '[:space:]'` портабельно работает и на BSD, и на GNU `tr`. Эквивалентная альтернатива: `awk 'END {print NR}' concurrent.txt`.

## Проверка после всех трёх фиксов

```bash
which flock                              # /opt/homebrew/opt/util-linux/bin/flock
./scripts/allocate-address.sh --peek     # выводит счётчик, exit 0
./scripts/tiling-check.py --peek         # JSON-диагностика, exit 10 если ollama не запущен
./scripts/boundary-score.py --top 5      # таблица frontier, exit 0
make test                                # all green
```

После этих трёх правок локальный DragonScale-стек функционально эквивалентен Linux-baseline. Проверено против системного Python `3.9.6`, BSD `wc`, macOS `25.4.1` (Darwin `25.4.0`).

В этом форке плагина все три фикса уже применены: `from __future__ import annotations` в `scripts/tiling-check.py` и `scripts/boundary-score.py`, `tr -d '[:space:]'` в `tests/test_allocate_address.sh`. Установить `util-linux` остаётся вашей задачей (один раз на машину).

## Сопутствующие соображения

- `flock` — единственная DragonScale-зависимость, которая в `util-linux` keg-only. Компиляторные `LDFLAGS`/`CPPFLAGS`/`PKG_CONFIG_PATH`, которые предлагает Homebrew, тут ни при чём; они нужны только если вы что-то компилируете против util-linux.
- Mechanism 4 (boundary scoring) полностью работает без `ollama`: ему достаточно `python3` и `git`. Mechanism 3 (semantic tiling) требует `ollama` плюс модель `nomic-embed-text`. На macOS: `brew install ollama && ollama serve && ollama pull nomic-embed-text`.
- Хук PostToolUse стейджит только `wiki/`, `.raw/` и `.vault-meta/`. Правки в `scripts/` и `tests/` (где живут macOS-фиксы) не коммитятся автоматически: это ручной коммит, который контролирует пользователь.
