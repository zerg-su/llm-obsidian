# PipelineSpec v1: полный справочник

Каноническая форма задана
[`schemas/pipeline-spec-v1.schema.json`](../../schemas/pipeline-spec-v1.schema.json).
JSON строго закрыт: неизвестные или отсутствующие поля отвергаются.

## Top-level fields

| Поле | Контракт |
|---|---|
| `schema_version` | Ровно `1` |
| `spec_id`, `version` | Bounded identifier и semver |
| `intent`, `task_profile` | Семантическая цель; profile выбирает code-owned baseline |
| `baseline_pipeline` | Должен совпасть с deterministic selector |
| `route_alias` | Только разрешённый alias, обычно `executor-default` |
| `required_capabilities` | Подмножество policy ceiling и preflight facts |
| `input_schema`, `output_schema` | Сквозная typed boundary |
| `steps` | 1–8 зарегистрированных primitives |
| `transitions` | 1–16 typed edges с bounded traversal |
| `controls` | До двух registered control primitives |
| `budget` | Attempts, restart, seconds, tokens; только внутри ceiling |
| `completion_policy` | `attention` или `autonomous` |
| `requested_permissions` | Декларация enforceable permissions внутри ceiling |
| `requested_side_effects` | Декларация effects внутри ceiling |
| `context_pointers` | ID, SHA-256 содержимого и byte limit; без path/body |
| `verification_checks` | Только code-owned names, не shell strings |
| `review_mode` | `simple`, `deep`, `full` или `skip` |
| `human_gates` | Содержит `initial-approval` |
| `terminal_outcomes` | Непустой набор allowed terminal IDs |

## Step, transition и control

Step содержит `step_id`, `primitive_id`, `primitive_version`, input/output
schemas, `session_mode` и `semantic_skills`. Output одного forward step должен
совпадать с input следующего. Разрешённые session modes:
`parent-child`, `worktree`, `verification`, `review`.

Transition содержит `from_step`, `outcome`, target step или
`terminal:<outcome>` и `max_traversals` 1–3. Backward edge требует
`bounded_loop`; terminal transition не запускает скрытый reap.

Control содержит только `primitive_id` и version. В v1 registered controls —
`human_gate` и `bounded_loop`.

## Registry версии 2.6.3

- Primitives: `model_step`, `verify`, `review`, `human_gate`, `bounded_loop`.
- Semantic skills: `debug`, `dispatch`, `review`, `tdd`.
- Named checks: `diff-check`, `harness-tests`, `instruction-lint`,
  `model-routing`, `vault-validation`.

Documentation-specific skill/check намеренно не добавлены: evaluation не
доказал semantic gap. [`document-project-v1.json`](../../examples/pipelines/document-project-v1.json)
компилируется на существующем registry и вызывает `make test-docs` как
отдельный release gate, а не как новый runtime check carrier.

## Fail-closed примеры

Compiler отвергает arbitrary command, неизвестные keys/checks/skills, route или
authority expansion, reserved `outcome-contract` pointer, graph bombs,
unbounded backward edges, превышение budget и built-in-equivalent custom spec.
Freeze связывает definition hash и approval-card hash; replay revalidates оба.
