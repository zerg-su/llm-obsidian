# Cookbook: типовые комбинации

## Для кого и результат

Для пользователя, уже знающего ментальную модель. Результат — выбор короткого
поддерживаемого маршрута без ручной оркестрации harness и без расширения
разрешений.

## Предварительные условия

- работа из правильного vault/task root;
- явный outcome и допустимые effects;
- нужные skills видны текущему runtime;
- recipes с network/write/release выполняются только при соответствующем scope.

## Пример

1. **Сохранить решение:** `wiki-query` на дубликат → `save` → validate/retrieve.
2. **Разобрать неясную feature:** `clarify` → `design` → `implementation-plan`.
3. **Реализовать change:** approved plan → `tdd` → focused/full tests → `review`.
4. **Исправить defect:** `debug` reproduction/root cause → authorized `tdd` fix.
5. **Долгая task:** `save-plan` → `dispatch` → `review` → coordinator `reap`.
6. **Найти прошлую работу:** `find-session` → `wiki-query` для canonical facts.
7. **Импортировать PDF:** `wiki-ingest` → validate → query по целевому разделу.
8. **Исследовать текущий вопрос:** protected `research`; `unsafe-research` только явно.
9. **Подготовить ответ:** `wiki-query` → `draft`; пользователь отправляет сам.
10. **Собрать EOD:** `daily`; для датированного будущего действия — `journal`.
11. **Разобрать inbox:** `backlog list` → promote в `save`/plan или drop.
12. **Обслужить vault:** `wiki-lint` → intentional fixes → dry-run `wiki-fold`.
13. **Сделать runbook:** `distill-runbook` из sanitized command log → human review.
14. **Документировать проект:** `implementation-plan` → `tdd` по page slices →
    `make test-docs` → existing-registry PipelineSpec compile → `review`.
15. **Расширить Obsidian view:** `obsidian-markdown` + `obsidian-bases`; `canvas`
    только по явному visual request.
16. **Ускорить крупную цель:** `implementation-plan` по независимым ownership →
    отдельный `save-plan` и `dispatch` на каждую часть → отдельный `reap` →
    approved integration plan. Полный сценарий: [параллельные задачи](parallel-tasks.md).
17. **Проверить одной моделью:** explicit single-model Deep route из
    [руководства по review](review.md); fallback задаётся явно, а не угадывается.
18. **Восстановиться после callback failure:** `harness-cli.py status` →
    `inspect <operation-id>` → поддерживаемый `diagnose`/`reconcile`; таблица
    решений — в [troubleshooting](troubleshooting.md).
19. **Обновить установленную систему:** preflight → backup → apply → validate →
    rollback при несоответствии; точная процедура — в
    [upgrading-and-releasing](upgrading-and-releasing.md).
20. **Собрать release candidate:** cheap gates → full gates → exact-HEAD review
    → owner-authorized publish; tag или release никогда не выводятся из зелёного
    теста автоматически.

### Runnable recipe: одна обычная длительная задача

```text
/save-plan сохрани утверждённый план изменения отчёта
/dispatch wiki/plans/report-change.md как task report-change
```

Ожидаемый результат: dispatch возвращает отдельные `task_id`, worktree,
operation и cmux surface. После terminal callback координатор запускает `reap`;
готовность проверяется по typed summary и exact-HEAD review, а не по закрытому
окну.

### Runnable recipe: одна цель, три параллельные задачи

```text
Разрежь цель на планы report-core, report-cli и report-guide с непересекающимся
ownership и отдельными evidence. Сохрани и проверь каждый план.

/dispatch wiki/plans/report-core.md как task report-core
/dispatch wiki/plans/report-cli.md как task report-cli
/dispatch wiki/plans/report-guide.md как task report-guide
```

Ожидаемый результат: три независимых worktree выполняют три полных pipeline.
После их отдельного review/reap создаётся четвёртый integration plan с exact
child HEAD и общим test packet. Детали и recovery — в
[руководстве по параллельным задачам](parallel-tasks.md).

### Runnable recipe: документационный проект

```text
Составь implementation-plan по page slices, укажи источники истины и docs gate.
Реализуй утверждённый план через TDD, выполни make test-docs и review результата.
```

Ожидаемый результат: обязательные страницы достижимы из index, примеры
проверяются детерминированно, PipelineSpec компилируется, а terminal review
связан с точными байтами документации.

## Ожидаемый результат и проверка

Каждый recipe заканчивается observable artifact или typed lifecycle state.
Проверка выбирается по владельцу: vault validation/retrieval для wiki, tests и
diff для product, harness status/archive для task, citations для research. Не
считайте вызов skill или созданный файл completion без результата.

## Ошибки и восстановление

- Skill не подходит exact intent: выберите более узкий skill или ручной
  repository contract; router — hint.
- Recipe требует отдельный task, но cmux отсутствует: не имитируйте dispatch.
- Ingest/research source недостоверен: сохраните provenance и reject claim.
- TDD/review обнаружили новый public boundary: остановите recipe и эскалируйте.
- Reap ещё не разрешён: task остаётся resumable; не закрывайте surface вручную.

## Источники истины

- [`skills.md`](skills.md).
- [`mental-model.md`](mental-model.md).
- [`review.md`](review.md).
- [`operations.md`](operations.md).
- Каталог [`skills/`](../../skills/).
