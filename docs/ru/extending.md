# Расширение: skill, schema, config, pipeline и MCP

## Для кого и результат

Для maintainer'а, добавляющего новую capability. Результат — минимальная
extension surface с backward-compatible contract, generated adapters и тестами,
либо честное решение не добавлять redundant abstraction.

## Предварительные условия

- подтверждён behavioral gap, которого не закрывает существующий skill/primitive;
- authoritative public contract и compatibility boundary;
- approved ownership файлов и budget;
- system `skill-creator` доступен для skill work, repo `improve-skills` — для
  five-pass quality audit.

## Пример

Для нового skill сначала сохраните raw baseline cases и получите fresh-context
failure без подсказки ответа. Инициализируйте минимальную структуру через
`skill-creator`, вынесите conditional details в references, затем примените
`improve-skills`: invocation, hierarchy, steering, pruning и goal preservation.
Проверьте:

```bash
python3 skills/improve-skills/scripts/audit_skills.py --strict --json
make test-instruction-lint
make test-skill-budget
make test-codex-adapter
```

В 2.6.3 кандидат `document-project` был удалён: control уже прошёл 4/4, поэтому
новый skill и увеличенный registry budget не были оправданы. Quality contracts
остались в docs. Тот же принцип действует для schema/config/PipelineSpec/MCP:
сначала gap, затем smallest registered carrier и generated adapter sync.

## Ожидаемый результат и проверка

Новая capability имеет distinct trigger, false-positive tests, bounded tools и
completion evidence. Claude manifest и Codex marketplace генерируются из
одного source; новый thread видит registry. Schema rejected unknown fields;
config имеет example/migration/rollback; pipeline compile не ослабляет ceiling;
MCP secret остаётся вне Git.

## Ошибки и восстановление

- Baseline не воспроизводит failure: удалите candidate и сохраните rejection
  evidence, как сделано для `document-project`.
- Skill budget превышен: не сжимайте чужие descriptions без scope; решение о
  cap — public registry boundary.
- Generated adapter diff неожиданно меняет другие skills: остановитесь и
  диагностируйте source/adapter drift.
- Schema или config требует migration: отдельное owner decision и rollback.
- MCP требует credential/network: пользователь настраивает user-owned secret;
  repository не угадывает значение.

## Источники истины

- [`skills/improve-skills/SKILL.md`](../../skills/improve-skills/SKILL.md).
- [`scripts/codex-adapter.py`](../../scripts/codex-adapter.py).
- [`schemas/pipeline-spec-v1.schema.json`](../../schemas/pipeline-spec-v1.schema.json).
- [`docs/mcp-gateway.md`](../mcp-gateway.md).
- [`docs/acceptance/v2.6.3-document-project-skill-audit.md`](../acceptance/v2.6.3-document-project-skill-audit.md).
