# Русский технический справочник LLM Obsidian 2.6.3

Этот handbook ведёт от чистой машины до проверенного изменения проекта. Он
объясняет поддерживаемый контракт версии 2.6.3; при расхождении источником
истины остаются указанные repository-файлы, schema, CLI help и тесты.

## Маршруты чтения

- Новый пользователь: [быстрый старт](getting-started.md) → [ментальная модель](mental-model.md) → [первый проект](first-project.md).
- Архитектор нового проекта: [Architecture Workflow v1](../architecture-workflow-v1.ru.md)
  ведёт от clarify и Vision через Design/Spec/Contract к Work Item DAG,
  implementation plan и Harness на полном примере Neon Snake. Этот маршрут
  описывает поведение 2.8.0+ и дополняет handbook 2.6.3.
- Постоянный пользователь и оператор: [skills](skills.md) → [wiki и память](wiki-memory.md) → [операции](operations.md) → [устранение неполадок](troubleshooting.md).
- Координатор крупной работы: [планирование](planning.md) → [ручная декомпозиция и параллельные задачи](parallel-tasks.md) → [сессии и задачи](sessions-and-tasks.md).
- Автор pipeline: [pipeline](pipelines.md) → [PipelineSpec DSL](pipeline-dsl.md) → [документационный pipeline](documentation-pipeline.md).
- Разработчик и maintainer: [разработка](development.md) → [тестирование](testing.md) → [расширение](extending.md) → [обновление и релиз](upgrading-and-releasing.md).

## Полное содержание

- [Architecture Workflow v1: от идеи до проверенной реализации](../architecture-workflow-v1.ru.md)
- [Установка и первый результат](getting-started.md)
- [Ментальная модель](mental-model.md)
- [Первый проект от идеи до reap](first-project.md)
- [Полный каталог skills](skills.md)
- [Планирование](planning.md)
- [Ручная декомпозиция и параллельные задачи](parallel-tasks.md)
- [Сессии и задачи](sessions-and-tasks.md)
- [Review](review.md)
- [Pipeline и встроенные профили](pipelines.md)
- [PipelineSpec v1: справочник DSL](pipeline-dsl.md)
- [Pipeline для документации](documentation-pipeline.md)
- [Wiki, retrieval и долговременная память](wiki-memory.md)
- [Документы и защищённый research](documents-and-research.md)
- [Эксплуатация](operations.md)
- [Разработка](development.md)
- [Тестирование](testing.md)
- [Расширение проекта](extending.md)
- [Upgrade, rollback и release candidate](upgrading-and-releasing.md)
- [Устранение неполадок](troubleshooting.md)
- [Практические рецепты](cookbook.md)
- [Справочник команд](reference/commands.md)
- [Справочник конфигурации](reference/configuration.md)
- [Глоссарий](reference/glossary.md)

## Граница версии

Страницы описывают 2.6.3. Они не предоставляют разрешение на network, push,
publish, deploy, tag или release. Операционные правила принадлежат
[`AGENTS.md`](../../AGENTS.md) и [`CLAUDE.md`](../../CLAUDE.md); handbook помогает
применять их, но не переопределяет.
