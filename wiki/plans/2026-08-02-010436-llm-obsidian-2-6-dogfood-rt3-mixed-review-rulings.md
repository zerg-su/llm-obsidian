---
type: plan
title: "LLM Obsidian 2.6 dogfood RT3 — mixed review rulings"
address: c-000081
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-08-02
updated: 2026-08-02
tags:
  - plan
  - manual-save
  - dogfood
  - v2.6
---

# LLM Obsidian 2.6 dogfood RT3 — mixed review rulings

## Outcome Contract

```json
{"schema_version":1,"purpose":"Проверить typed material-finding resolution на настоящем bounded изменении, включая исправление доказанного замечания и аргументированный отказ от scope-expanding рекомендации.","desired_outcome":"Review resolution принимает только identity-bound evidence: минимум один material finding получает applied с доказанным fix delta, а минимум один независимый material finding получает rejected с bounded rationale, после чего verification подтверждает оба rulings без повторного provider effect.","success_evidence":[{"evidence_id":"rt3-applied","observable":"Один reviewer finding исправлен на новом HEAD, связан с тестом и классифицирован verification как addressed."},{"evidence_id":"rt3-rejected","observable":"Один reviewer finding отклонён bounded rationale, которое ссылается на Outcome Contract или non-goal и принимается typed resolution validator."},{"evidence_id":"rt3-identity","observable":"Оба rulings привязаны к исходным finding IDs, review operation, callback и reviewed HEAD; replay чужого или старого ruling отклоняется."},{"evidence_id":"rt3-no-duplicate","observable":"Resolution и same-session verification завершаются без duplicate provider effect и без новой review surface."}],"non_goals":["Ослабление material finding policy.","Автоматическое отклонение findings без executor rationale.","Добавление новой review lane или увеличение verification budget."]}
```

## Контекст

Foundation уже поддерживает `applied`, `rejected` и `out-of-scope`, но real-task gate требует живой mixed-ruling путь. Задача должна использовать небольшое реальное изменение в review resolution tests или materialization code и пройти обычный simple review.

## Работа

1. Выбрать минимальный непокрытый seam в resolution evidence или replay rejection; не менять публичную workflow semantics.
2. Реализовать через `tdd` одно bounded улучшение и отправить exact HEAD на simple review.
3. При получении material findings исправить один доказанный дефект.
4. Одну независимую рекомендацию, которая реально нарушает non-goal или расширяет scope, отклонить typed `rejected` с коротким проверяемым rationale. Не выдумывать finding: если reviewer не дал двух независимых material findings, завершить `partially-achieved` и зафиксировать residual gap.
5. Провести не более одной same-session verification iteration.

## Проверка

- Focused review resolution/replay tests.
- Exact-HEAD review packet и resolution receipt.
- Ноль duplicate effects и ноль дополнительных review surfaces.
- Wiki Summary v2 честно отражает `achieved` или `partially-achieved`.
