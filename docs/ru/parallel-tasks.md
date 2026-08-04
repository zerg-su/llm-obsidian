# Параллельные задачи: ручная декомпозиция и сборка результата

## Для кого и результат

Для координатора крупного изменения, которое слишком долго или рискованно
выполнять одним монолитным task. Результат — несколько независимо проверяемых
планов, одновременно работающих в отдельных task worktree, и один управляемый
интеграционный этап.

В 2.6.3 параллелизм находится **между задачами**:

```text
одна цель
  -> несколько approved plans
  -> несколько независимых dispatch task
  -> отдельная проверка каждого результата
  -> один integration plan
  -> общий проверенный HEAD
```

Каждая task внутри остаётся обычным последовательным pipeline. Версия 2.6.3 не
строит автоматического task graph, не вычисляет зависимости и не выполняет
автоматический join. Эти возможности запланированы для 2.7. Сейчас границы и
порядок сборки явно задаёт координатор.

## Предварительные условия

- Сформулирован общий Outcome Contract: что должно стать наблюдаемо истинным,
  какими evidence это доказывается и что остаётся non-goal.
- Найден естественный разрез с непересекающимся ownership файлов или модулей.
- Для каждой части известны входы (`consumes`) и результаты (`produces`).
- Общий интерфейс, schema или формат данных либо уже стабилен, либо выделен в
  отдельную первую последовательную задачу.
- Каждый дочерний план отдельно утверждён до dispatch.
- Доступен cmux: именно harness создаёт видимые session/worktree и владеет их
  lifecycle.

Не запускайте части одновременно только ради скорости. Параллельный разрез
безопасен, когда ответ «да» дан на все вопросы:

1. Можно ли завершить часть без чтения незакоммиченного результата соседа?
2. Есть ли у неё собственный проверяемый результат, а не только «сделать шаг»?
3. Не владеют ли две задачи одним файлом или одной миграцией?
4. Можно ли принять или отклонить результат части независимо?
5. Известно ли, кто и в каком порядке соберёт итоговые HEAD?

Если две части меняют один контракт, сначала стабилизируйте контракт отдельным
планом. Последующие задачи стартуют от нового общего base. Это обычно быстрее,
чем параллельно создать несовместимые реализации и потом разбирать конфликт.

## Пример

### 1. Разрезать цель на продукты, а не на действия

Допустим, общая цель — добавить новый отчёт, CLI и подробное руководство.
Плохой разрез: «одна task пишет код, вторая пишет тесты». Он ломает TDD и
оставляет обе задачи взаимозависимыми.

Рабочий разрез:

| План | Ownership | Consumes | Produces | Независимая проверка |
|---|---|---|---|---|
| `report-core.md` | formatter и его unit tests | утверждённая schema | библиотечный API и fixtures | focused unit suite |
| `report-cli.md` | CLI adapter и CLI tests | стабильная schema/API | команда, help и ошибки | CLI contract tests |
| `report-guide.md` | `docs/ru/` и docs gate | утверждённое CLI | tutorial и runnable examples | `make test-docs` |

Если CLI не может разрабатываться до появления formatter API, есть два
варианта: сначала последовательно выпустить маленький contract slice либо
сделать CLI task зависимой и запускать её после core. Не маскируйте зависимость
словом «параллельно».

### 2. Сохранить отдельные планы

Создайте master-запись о цели и по отдельному plan-файлу на каждую task.
Практический запрос агенту:

```text
Разложи цель «новый отчёт» на независимые планы core, CLI и guide.
Для каждого зафиксируй Outcome Contract, ownership, consumes/produces,
RED/GREEN evidence, non-goals и stop conditions. Общие файлы не дели.
Сохрани каждый план отдельно через save-plan и отдай планы на intent review.
```

В каждом плане должны быть:

- ссылка на общую цель и граница дочернего outcome;
- уникальное имя task и однозначный набор файлов;
- точный base commit или правило, от какого принятого результата стартовать;
- дешёвые focused tests до дорогих integration tests;
- запрещённые эффекты: push, publish, release, чужие файлы;
- handoff: какие exact HEAD, summary и receipts получит интегратор.

Один plan-файл удобнее использовать только для повторяемых sibling-задач с
неизменным контрактом. Тогда при dispatch явно задайте
`reap.plan_mode=shared`: дочерний reap сохранит master plan открытым. Для
разных результатов предпочтительнее отдельные планы и обычный `final`.

### 3. Запустить задачи отдельно

После review каждого плана запустите по одному dispatch. Не объединяйте три
плана в один prompt и не запускайте provider вручную.

Claude Code:

```text
/dispatch wiki/plans/report-core.md как task report-core
/dispatch wiki/plans/report-cli.md как task report-cli
/dispatch wiki/plans/report-guide.md как task report-guide
```

Codex:

```text
$llm-obsidian:dispatch запусти approved plan wiki/plans/report-core.md как task report-core
$llm-obsidian:dispatch запусти approved plan wiki/plans/report-cli.md как task report-cli
$llm-obsidian:dispatch запусти approved plan wiki/plans/report-guide.md как task report-guide
```

Каждый вызов создаёт отдельные `task_id`, branch, worktree, operation и cmux
surface. Дождитесь typed launch result одного вызова, сохраните идентификаторы и
переходите к следующему. Завершение dispatch означает только успешный запуск,
а не готовность результата.

### 4. Наблюдать без ручного управления сессиями

Общий список:

```bash
python3 scripts/harness-cli.py status
python3 scripts/harness-cli.py doctor
```

Точная задача:

```bash
python3 scripts/harness-cli.py inspect <operation-id>
```

Ориентируйтесь на `task_id`, `operation_id`, worktree и typed state, а не на
положение вкладки. Callback возвращает координатору escalation или terminal
handoff. Не polling'уйте интерактивную модель и не прерывайте видимо активную
сессию.

### 5. Принять каждую ветку до общей сборки

Для каждой task отдельно требуется:

1. чистый task worktree и committed exact HEAD;
2. все evidence её Outcome Contract;
3. focused/full gates в пределах плана;
4. terminal review согласно утверждённому профилю;
5. typed Wiki Summary и успешный coordinator-owned reap.

Reap архивирует доказательства задачи, но **не сливает кодовые ветки**. Для
отдельного дочернего плана используйте `final`. Для общего неизменённого master
plan используйте `shared`, а закрывает master только последняя интеграционная
задача.

### 6. Собрать результаты отдельным integration plan

Не делайте join случайной серией merge-команд в координаторской сессии. Создайте
короткий approved integration plan, который перечисляет:

- exact HEAD каждой принятой task;
- порядок интеграции и причину порядка;
- ожидаемые пересечения и владельца каждого конфликта;
- общий integration test packet;
- итоговый intent/implementation/release review;
- stop condition при semantic conflict или устаревшем base.

Пример запроса:

```text
Подготовь integration plan для принятых HEAD core=<oid>, cli=<oid>, guide=<oid>.
Не меняй их результаты. Зафиксируй порядок, conflict ownership, общий test
packet и evidence полной исходной цели. После утверждения запусти отдельную
integration task через dispatch.
```

Обычная разрешённая Git-интеграция выполняется уже внутри этой task. При
существующем точном merge/rebase/cherry-pick конфликте используйте
`resolve-conflict`: он восстанавливает намерение по evidence, но не получает
права менять outcome. После сборки повторите общие тесты и review уже на
комбинированном HEAD — зелёные дочерние ветки не доказывают совместимость.

## Ожидаемый результат и проверка

Успешный параллельный цикл оставляет:

- отдельный approved plan, `task_id`, operation и worktree на каждую часть;
- отсутствие одновременного ownership одного изменяемого файла;
- terminal summary/review/test receipts для каждого exact child HEAD;
- отдельный integration plan с перечисленными входными HEAD;
- один чистый combined HEAD, прошедший integration/full tests и общий review;
- доказательство всех evidence исходного Outcome Contract, а не только суммы
  дочерних checklist.

Контрольный список перед объявлением общей цели достигнутой:

```text
[ ] Все дочерние outcomes приняты, а не просто «задачи завершились».
[ ] Ни одна ветка не интегрирована по имени без exact HEAD.
[ ] Reap и Git integration не перепутаны.
[ ] Общий plan закрыт только после combined-head проверки.
[ ] Результат не требует скрытой ручной правки после reap.
```

## Ошибки и восстановление

- **Две задачи меняют один файл.** Остановите одну task или пересмотрите
  ownership; не позволяйте обеим «аккуратно разрешить потом».
- **Одна задача зависит от незавершённой соседней.** Зафиксируйте зависимость и
  запускайте после принятого HEAD либо выделите сначала contract slice.
- **Одна ветка провалилась.** Остальные независимые task могут завершиться, но
  integration plan не должен притворяться, что общая цель достигнута. Замените
  или перепланируйте только отсутствующий продукт.
- **Base устарел.** Не переносите изменения вслепую. Обновите integration plan и
  повторите затронутые тесты/review на новом combined HEAD.
- **Возник точный Git conflict.** Используйте `resolve-conflict` только в уже
  conflicted worktree и сверяйте intent обеих сторон.
- **Harness показывает `attention-required`.** Сначала read-only `inspect` и
  `doctor`; repo-owned mechanism failure следует failure-repair contract.
- **Закончился лимит модели.** Применяйте только поддерживаемый provider fallback
  из frozen task route; не меняйте модель задним числом у уже принятого effect.
- **Нужен автоматический DAG, очередь зависимостей или deterministic join.** Это
  не скрытая возможность 2.6.3. Сохраните потребность для Project Spaces и Task
  Orchestration 2.7, а текущую работу ведите явными планами.

## Источники истины

- [`skills/implementation-plan/SKILL.md`](../../skills/implementation-plan/SKILL.md).
- [`skills/dispatch/SKILL.md`](../../skills/dispatch/SKILL.md).
- [`skills/reap/SKILL.md`](../../skills/reap/SKILL.md).
- [`skills/resolve-conflict/SKILL.md`](../../skills/resolve-conflict/SKILL.md).
- [`docs/task-sessions.md`](../task-sessions.md).
- [`docs/unattended-pipeline-operations.md`](../unattended-pipeline-operations.md).
- [`docs/skill-references/failure-repair-contract.md`](../skill-references/failure-repair-contract.md).
