# Wiki, retrieval и долговременная память

## Для кого и результат

Для пользователя, который сохраняет решения и потом должен их найти. Результат
— одна canonical page с provenance/address/wikilinks, доступная через sparse
retrieval и, при наличии Ollama, hybrid retrieval.

## Предварительные условия

- работа из корня vault и доступный Python;
- текущая session identity из `scripts/current-session-id.sh`;
- agent writes проходят только через skill/`vault-write.py` transaction;
- `.vault-meta/` считается derived state.

## Пример

Сначала найдите возможный дубликат:

```bash
python3 scripts/retrieve.py "решение о lifecycle review" --top 5 --json
```

Если canonical page нет, попросите `/save` или `$llm-obsidian:save`. Skill
подбирает type/folder/title, выделяет DragonScale address, создаёт frontmatter,
сессию и wikilinks, затем передаёт один JSON transaction writer'у. Для ручной
автоматизации сначала изучите `python3 scripts/vault-write.py --help`; не
редактируйте `wiki/log.md` или `wiki/hot.md` напрямую.

## Ожидаемый результат и проверка

```bash
python3 scripts/validate-vault.py --summary
python3 scripts/retrieve.py "lifecycle review" --top 5 --json
```

Validation подтверждает frontmatter, caps и plan lifecycle. Retrieval возвращает
наиболее релевантные H2/H3 sections. Без local embeddings sparse path остаётся
полностью работоспособным; hybrid — enhancement, а не скрытый prerequisite.

## Ошибки и восстановление

- Near-duplicate найден: merge/update canonical page с optimistic old hash.
- Writer сообщает hash conflict: перечитайте page и сформируйте новую
  transaction; не перезаписывайте чужую правку.
- Dense unavailable: используйте sparse и выполните optional setup позже.
- Index stale: запустите documented reindex/Stop path; derived files не чинятся
  вручную.
- Validation блокирует commit: исправьте точную страницу и повторите gate.

## Источники истины

- [`AGENTS.md`](../../AGENTS.md), Core rules и Write path.
- [`scripts/vault-write.py`](../../scripts/vault-write.py).
- [`scripts/retrieve.py`](../../scripts/retrieve.py).
- [`docs/dragonscale-guide.md`](../dragonscale-guide.md).
