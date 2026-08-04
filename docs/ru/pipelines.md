# Pipeline: код владеет механикой

Pipeline — immutable скомпилированный контракт из typed primitives. Model может
предложить данные PipelineSpec, но не shell/Python execution. Compiler сверяет
registry, schemas, graph, budgets, capabilities, permissions, side effects,
context hashes и explicit approval до эффекта.

## Встроенные профили

| Pipeline | Steps | Применение |
|---|---|---|
| `lifecycle/default` | dispatch → review | Обычная approved task без engineering verify step |
| `engineering/change` | TDD model step → verify → review | Ясное продуктовое изменение |
| `engineering/fix` | reproduce → root cause → regression → minimal fix → verify → review | Воспроизводимый defect, bounded retry loop |

Built-ins code-owned и executable только по exact compiled hash. `human_gate` и
`bounded_loop` — control primitives, не вымышленные runtime steps.

## Когда custom оправдан

Custom PipelineSpec допустим только при доказанном semantic gap относительно
детерминированно выбранного built-in. Он не может выбирать более permissive
baseline, raw command, произвольный provider route, неизвестный skill/check,
unbounded loop или filesystem path в context pointer. Перед execution человек
видит approval card с baseline delta и абсолютным ceiling.

## Completion policy и budgets

`attention` возвращает решение координатору на неоднозначности. `autonomous`
может повторять только заранее ограниченные переходы. Attempt, provider restart,
time, token, model calls, context bytes и transition traversals ограничены
code-owned policy; spec может только сузить ceiling.

## Наблюдаемость

Operation ledger и content-free events хранят IDs, относительные paths и
числовые counters, но не prompts, commands или page bodies. Progress line
показывает только live steps текущего coordinator workspace.

Практика: [документационный pipeline](documentation-pipeline.md). Поля:
[PipelineSpec DSL](pipeline-dsl.md). Авторитеты:
[`scripts/harness/pipeline_builtins.py`](../../scripts/harness/pipeline_builtins.py),
[`docs/pipeline-observability.md`](../pipeline-observability.md).
