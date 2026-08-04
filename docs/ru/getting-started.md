# Установка и первый долговременный результат

## Для кого и результат

Для пользователя macOS, знакомого с Git и терминалом. В конце репозиторий
открыт как Obsidian vault, Claude Code или Codex видит skills, а одна заметка
сохранена и проходит vault validation.

## Предварительные условия

- macOS, Xcode Command Line Tools, Git и Python 3.9+;
- Obsidian и хотя бы один официальный CLI: Claude Code или Codex CLI;
- право создать локальный каталог и установить зависимости bootstrap;
- network нужен только для clone/install; runtime вольта остаётся local-first.

Перед bootstrap прочитайте вывод `--help`, если машина уже настроена:

```bash
bash bin/setup-clean-machine.sh --help
```

## Пример

Clone и bootstrap меняют локальную машину и могут скачивать зависимости:

```bash
git clone https://github.com/zerg-su/llm-obsidian ~/Projects/llm-obsidian
cd ~/Projects/llm-obsidian
bash bin/setup-clean-machine.sh
```

Откройте этот каталог как vault. Для Claude добавьте local marketplace/plugin
через plugin UI. Для Codex синхронизируйте repo-local marketplace:

```bash
python3 scripts/codex-adapter.py --apply
scripts/mcp-gateway/mcp-gateway.sh codex-sync --apply
codex plugin marketplace add "$(pwd)"
codex plugin add llm-obsidian@llm-obsidian-codex
```

Начните новый thread после установки plugin. Запустите `claude` или `codex` из
корня и попросите: «Сохрани заметку “Первый результат” с одним проверяемым
фактом». В Codex можно явно вызвать `$llm-obsidian:save`; в Claude — `/save`.

## Ожидаемый результат и проверка

Bootstrap сохраняет существующие Obsidian settings/secrets, создаёт managed
assets и печатает точные repair steps для недоступных optional-компонентов.
После сохранения новая wiki-страница имеет frontmatter, session provenance и
DragonScale address. Проверка не требует сети:

```bash
python3 scripts/validate-vault.py --summary
python3 scripts/retrieve.py "Первый результат" --top 5 --json
```

Validation должна завершиться без ошибок; retrieval должен вернуть созданную
страницу или её раздел после reindex/Stop pipeline.

## Ошибки и восстановление

- Команда `codex` или `claude` не найдена: остановитесь и установите официальный
  CLI по его документации; repository bootstrap не выдаёт provider access.
- Codex не видит новый skill: проверьте plugin list и начните новый thread;
  существующий thread сохраняет стартовый registry.
- Bootstrap сообщает optional dependency: используйте напечатанную точную
  repair-команду или повторите с документированным skip-флагом; не редактируйте
  generated metadata вручную.
- Vault validation красный: не обходите Stop hook. Сначала выполните read-only
  validation, исправьте названный файл и повторите проверку.

## Источники истины

- [`README.ru.md`](../../README.ru.md), раздел «Быстрый старт».
- [`AGENTS.md`](../../AGENTS.md), skills discovery и write path.
- [`bin/setup-clean-machine.sh`](../../bin/setup-clean-machine.sh).
- [`scripts/codex-adapter.py`](../../scripts/codex-adapter.py).
