# Глоссарий

Термины расположены от базовых к зависимым.

| Термин | Определение |
|---|---|
| Vault | Git-каталог с wiki, docs, skills, config и scripts; Obsidian — UI над файлами |
| Wiki page | Canonical Markdown с typed frontmatter, provenance, address и wikilinks |
| DragonScale address | Детерминированный `c-NNNNNN` identifier страницы |
| Sparse retrieval | Unicode/BM25 section search, доступный без embeddings |
| Dense retrieval | Optional local multilingual semantic channel на `bge-m3` |
| Skill | Versioned instruction для intent/reasoning; не permission grant |
| Outcome Contract | Desired outcome, evidence, scope, non-goals и boundaries |
| Plan | Approved steps/ownership/verification, сохраняющие Outcome Contract |
| Task | Изолированная единица работы с stable identity и lifecycle |
| Worktree | Точный writable Git checkout task; source checkout может быть read-only |
| Session | Provider/runtime context, привязанный к task identity |
| cmux surface | Видимая terminal surface; locator, но не источник lifecycle truth |
| Operation | Harness-owned typed workflow instance |
| Callback | Typed сообщение operation, связанное digest и payload identity |
| Reconcile | Fail-closed сверка persisted и callback/terminal state |
| Pipeline | Immutable compiled sequence typed primitives |
| Primitive | Code-owned executable/control building block PipelineSpec |
| Semantic skill | Зарегистрированная reasoning discipline model step |
| Context pointer | ID + content SHA-256 + byte limit, без prompt/path bytes |
| Human gate | Explicit approval control до effects |
| Attention-required | Terminal/pause state при неизвестном ownership или решении владельца |
| Review round | Frozen-HEAD независимая проверка и verify; ещё не task completion |
| Finding | Typed severity/evidence/recommendation/verification gap |
| Reap | Coordinator-owned финальная transaction после approved result/review |
| Protected research | Networked fetch без vault + networkless synthesis validated artifact |
| Unsafe research | Явно разрешённый single-context web flow; не fallback |
| Derived state | Перестраиваемые indexes/caches в `.vault-meta/`; не hand-edit |
| Exact HEAD | Один Git commit, к которому относятся все release evidence |
| Completion proxy | Локальный green/artifact, который не доказывает весь Outcome Contract |

См. [ментальную модель](../mental-model.md), [PipelineSpec](../pipeline-dsl.md)
и [сессии](../sessions-and-tasks.md).
