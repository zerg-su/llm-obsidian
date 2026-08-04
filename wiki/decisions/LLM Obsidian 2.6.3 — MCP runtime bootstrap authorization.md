---
type: decision
title: "LLM Obsidian 2.6.3 — MCP runtime bootstrap authorization"
address: c-000103
status: active
created: 2026-08-04
updated: 2026-08-04
tags:
  - decision
  - llm-obsidian
  - v2-6-3
  - mcp
  - bootstrap
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-04-044240-llm-obsidian-2-6-3-russkaya-tekhnicheskaya-dokumentatsiya|LLM Obsidian 2.6.3 — русская техническая документация]]"
---

# LLM Obsidian 2.6.3 — MCP runtime bootstrap authorization

## Решение

Coordinator decision `70acb04f-5542-4e90-9466-94193b369181` разрешает узкую repository-owned починку bootstrap для проверки 2.6.3. Только default wrapper-вызов `scripts/mcp-gateway/mcp-gateway.sh sync-config --apply` может создать отсутствующий canonical `scripts/mcp-gateway/runtime.env` из точного committed sibling example после строгой проверки формата.

## Зафиксированная идентичность

- task ID: `8596fe76-7baa-4f73-b20c-23f33c0ba120`;
- approved plan SHA-256: `db4037cac1967b0907dbf1b6fd5850eefa2bfc5173080d2aa811a239fb36b8dc`;
- Outcome Contract SHA-256: `2c9728dc7c7fa3bc108ffb6ce5085bb41fcd9ba16310157e76276c6967b5bf5f`;
- mechanism repair commit: `e85ad74`.

## Разрешённое поведение

Создание выполняется существующим atomic writer с mode `0600`. Replay с теми же validated bytes идемпотентен. Право относится только к missing default runtime path и только к `sync-config --apply`.

## Запрещённое расширение

`--check`, print/read-only paths, custom runtime path, environment override, прямой config-sync, symlink target, отсутствующий или невалидный example не получают self-authorization и ничего не пишут. Решение не меняет provider routing, network authority, permissions, pipeline policy, credentials, push, publish, tag или release. Любое отличие fail closed и требует нового решения.

## Evidence

Regression matrix содержит 25 cases: default creation/replay разрешены; check/print, environment/custom/direct paths, missing/invalid/symlink inputs и symlink targets дают zero write. Release/readiness и task summary должны ссылаться на эту запись, а не только на transient escalation marker.
