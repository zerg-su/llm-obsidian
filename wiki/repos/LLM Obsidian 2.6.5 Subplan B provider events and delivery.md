---
type: repo
title: "LLM Obsidian 2.6.5 Subplan B provider events and delivery"
address: c-000124
created: 2026-08-05
updated: 2026-08-05
tags:
  - reap
  - repo
status: active
sessions:
  - "019fab00-3160-7380-8920-4b20183afb76"
executor_runtime: codex
executor_model: "gpt-5.6-sol"
outcome_disposition: achieved
outcome_evidence_ids:
  - B1-event-contract
  - B2-equal-ephemeral
  - B3-zero-blind-replay
  - B4-time-last-close
residual_gap_pointers:
related:
  - "[[Cross-model review — 5b3b36e7-206d-4fb0-940b-65832d14032f — 40c1f82a1995]]"
---

# LLM Obsidian 2.6.5 Subplan B provider events and delivery

Exact HEAD `84ecb3bad198053468f1ceb15f69bbea06510e3a`.

- B1: `ProviderEvent` exposes exactly seven values. Interactive accepts all seven; ephemeral accepts `{provider-started,input-accepted,result-published,process-exited,resource-closed,event-gap}`. Exact owner/operation/run/generation/provider-session/process/workspace/surface/source identity and contiguous cursors reject duplicate, stale, gap, ownership drift, wrong scalar types, and unreachable durable projections.
- B2: Claude print and Codex exec share one `EphemeralRunSpec/Result` and hermetic fake-process conformance matrix. Native-subscription/anchored ChatGPT preflight yields `ready`; negated, mixed, stderr-bearing, API-key, or ambiguous evidence yields `billing-profile-unverified` before model effect. Fixed commands, minimal allowlisted environments, isolated Codex home, and secret-bearing fixtures prove zero credential/output leakage and no fallback. No live paid/API-key call ran; live activation remains the Join gate named in the review resolution.
- B3: delivery table: pre-accept failure -> one same-key retry; reserved/accepted/ambiguous -> wait with zero input replay; first interactive `turn-stopped` -> one submit-only effect; second Stop, event gap, or resultless exit/close -> attention; schema-valid result -> close. Concurrent, crash-reload, and corruption tests linearize one effect and fail closed.
- B4: screen change -> recheck/wait only; deadline -> attention only; exact PID/supervisor/workspace/surface disappearance -> one fsynced owner-only `resource-closed` receipt. Runtime cleanup persists it before clearing `OwnedResources`, while incomplete historical identity cleans without fabricated events. Join owns atomic activation and removal of the legacy continuation call graph.

Review resolution: applied all three owned engineering findings at this HEAD; classified the two central-wiring findings out-of-scope with a durable pointer to the approved Join plan. Verification: `make test-harness`; `make test-model-routing`; `python3 scripts/runtime-harness-lint.py`; code-quality audit (0 blockers, 0 owned hotspots); `git diff --check`. Product worktree clean.

Review archive: [[Cross-model review — 5b3b36e7-206d-4fb0-940b-65832d14032f — 40c1f82a1995]]

## Outcome

Outcome disposition: `achieved`

Outcome evidence IDs: `B1-event-contract`, `B2-equal-ephemeral`, `B3-zero-blind-replay`, `B4-time-last-close`

Residual gaps:
- none
