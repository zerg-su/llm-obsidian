# llm-obsidian Makefile
# Test runner entry points for DragonScale and vault tooling.

.PHONY: test eval-smoke eval-live eval-regression retrieval-experiment test-agent-evals test-daily-pipeline test-session-map test-claude-subscription test-journal-write test-agenda test-dense-worker test-document-normalize test-documents test-research-isolation test-runtime-hooks test-runtime-detection test-skill-budget test-contract-schemas test-task-lifecycle test-instruction-lint test-mcp-schema-lock test-address test-schema test-tiling test-boundary test-vault test-plan-capture test-stop-hook test-memory-backup test-setup-vault test-pipeline-events test-bm25 test-retrieve test-bench test-retrieval-experiment test-fold test-router test-review-dispatch test-gateway test-codex-adapter test-dcg-assets test-with-timeout bench-retrieval setup-dragonscale clean-test-state help

help:
	@echo "llm-obsidian developer targets:"
	@echo "  make test              Run all vault + retrieval + hook tests"
	@echo "  make eval-smoke        Validate and grade checked-in agent eval fixtures"
	@echo "  make eval-live         Run opt-in live evals (EVAL_RUNNER='command')"
	@echo "  make eval-regression   Smoke + live retrieval quality gate"
	@echo "  make retrieval-experiment compare contextual/reranker flags without enabling"
	@echo "  make test-research-isolation protected fetch/synthesis boundary tests"
	@echo "  make test-document-normalize hermetic document routing/cache/fallback tests"
	@echo "  make test-documents     live Docling ru/en PDF/Office/offline acceptance"
	@echo "  make test-runtime-hooks Claude/Codex hook wire parity tests"
	@echo "  make test-runtime-detection legacy + three-way runtime detection tests"
	@echo "  make test-session-map Claude/Codex daily session grouping tests"
	@echo "  make test-agenda      deterministic daily carry-over and report tests"
	@echo "  make test-skill-budget enforce Codex initial skill registry budget"
	@echo "  make test-contract-schemas executable/published contract parity"
	@echo "  make test-task-lifecycle unattended contract + cmux close lifecycle"
	@echo "  make test-instruction-lint canonical skill/runtime instruction checks"
	@echo "  make test-mcp-schema-lock offline MCP tool schema drift checks"
	@echo "  make test-address     scripts/allocate-address.sh tests (shell)"
	@echo "  make test-schema      strict frontmatter/link/address schema tests"
	@echo "  make test-tiling      scripts/tiling-check.py tests (python, no ollama required)"
	@echo "  make test-boundary    scripts/boundary-score.py tests (python, no prereqs)"
	@echo "  make test-vault       vault-write/validate/reindex regression suite (shell)"
	@echo "  make test-plan-capture transactional ExitPlanMode capture test"
	@echo "  make test-stop-hook   stop.sh flock + opt-out + latency suite (shell, sandbox git repo)"
	@echo "  make test-memory-backup memory-backup sanitize/check/prune tests"
	@echo "  make test-setup-vault Obsidian config preservation/reset tests"
	@echo "  make test-pipeline-events runtime-neutral content-free telemetry tests"
	@echo "  make test-bm25        bm25-index.py + hybrid fusion tests (python, no ollama)"
	@echo "  make test-retrieve    section chunking, ranking, and dense degradation tests"
	@echo "  make test-bench       retrieval-bench metrics/degradation tests (python, no ollama)"
	@echo "  make test-fold        deterministic counter-free log fold tests"
	@echo "  make test-router      skill-router prompt matching suite (shell)"
	@echo "  make test-review-dispatch review-dispatch mode plumbing tests"
	@echo "  make test-gateway     MCP gateway config invariants (shell, offline)"
	@echo "  make test-codex-adapter Codex plugin packaging generator tests"
	@echo "  make test-dcg-assets  dcg config/hooks and Codex limit helper checks"
	@echo "  make test-with-timeout portable shell timeout helper tests"
	@echo "  make bench-retrieval  LIVE sparse quality gate (local dense channel optional)"
	@echo "  make setup-dragonscale Run bin/setup-dragonscale.sh against this vault"
	@echo "  make clean-test-state Remove runtime lockfiles and tiling cache"

test: test-agent-evals test-daily-pipeline test-session-map test-claude-subscription test-journal-write test-agenda test-dense-worker test-document-normalize test-research-isolation test-runtime-hooks test-runtime-detection test-skill-budget test-contract-schemas test-task-lifecycle test-instruction-lint test-mcp-schema-lock test-address test-schema test-tiling test-boundary test-vault test-plan-capture test-stop-hook test-memory-backup test-setup-vault test-pipeline-events test-bm25 test-retrieve test-bench test-retrieval-experiment test-fold test-router test-review-dispatch test-gateway test-codex-adapter test-dcg-assets test-with-timeout
	@echo ""
	@echo "All tests passed."

eval-smoke:
	@python3 scripts/agent-evals.py smoke

eval-live:
	@test -n "$(EVAL_RUNNER)" || { echo "EVAL_RUNNER is required" >&2; exit 2; }
	@python3 scripts/agent-evals.py live --runner "$(EVAL_RUNNER)" --trials "$${EVAL_TRIALS:-3}" --report .vault-meta/evals/latest-live.json

eval-regression: eval-smoke bench-retrieval

retrieval-experiment:
	@python3 scripts/retrieval-experiment.py

test-agent-evals:
	@echo "=== test_agent_evals.py ==="
	@python3 tests/test_agent_evals.py

test-daily-pipeline:
	@echo "=== test_daily_pipeline.py ==="
	@python3 tests/test_daily_pipeline.py

test-session-map:
	@echo "=== test_session_map.py ==="
	@python3 tests/test_session_map.py

test-claude-subscription:
	@echo "=== test_claude_subscription.py ==="
	@python3 tests/test_claude_subscription.py

test-journal-write:
	@echo "=== test_journal_write.py ==="
	@python3 tests/test_journal_write.py

test-agenda:
	@echo "=== test_agenda.py ==="
	@python3 tests/test_agenda.py

test-dense-worker:
	@echo "=== test_dense_worker.py ==="
	@python3 tests/test_dense_worker.py

test-document-normalize:
	@echo "=== test_document_normalize.py ==="
	@python3 tests/test_document_normalize.py

test-documents:
	@echo "=== test_document_live.py ==="
	@python3 tests/test_document_live.py

test-research-isolation:
	@echo "=== test_research_isolation.py ==="
	@python3 tests/test_research_isolation.py

test-runtime-hooks:
	@echo "=== test_runtime_hooks.py ==="
	@python3 tests/test_runtime_hooks.py

test-runtime-detection:
	@echo "=== test_detect_runtime.sh ==="
	@bash tests/test_detect_runtime.sh

test-skill-budget:
	@echo "=== test_skill_budget.py ==="
	@python3 tests/test_skill_budget.py

test-contract-schemas:
	@echo "=== test_contract_schemas.py ==="
	@python3 tests/test_contract_schemas.py

test-task-lifecycle:
	@echo "=== test_task_lifecycle.py ==="
	@python3 tests/test_task_lifecycle.py

test-instruction-lint:
	@echo "=== test_instruction_lint.py ==="
	@python3 tests/test_instruction_lint.py

test-mcp-schema-lock:
	@echo "=== test_mcp_schema_lock.py ==="
	@python3 tests/test_mcp_schema_lock.py

test-address:
	@echo "=== test_allocate_address.sh ==="
	@bash tests/test_allocate_address.sh

test-schema:
	@echo "=== test_vault_schema.py ==="
	@python3 tests/test_vault_schema.py

test-tiling:
	@echo "=== test_tiling_check.py ==="
	@python3 tests/test_tiling_check.py

test-boundary:
	@echo "=== test_boundary_score.py ==="
	@python3 tests/test_boundary_score.py

test-vault:
	@echo "=== test_vault_scripts.sh ==="
	@bash tests/test_vault_scripts.sh

test-plan-capture:
	@echo "=== test_plan_capture.sh ==="
	@bash tests/test_plan_capture.sh

test-stop-hook:
	@echo "=== test_stop_hook.sh ==="
	@bash tests/test_stop_hook.sh

test-memory-backup:
	@echo "=== test_memory_backup.sh ==="
	@bash tests/test_memory_backup.sh

test-setup-vault:
	@echo "=== test_setup_vault.sh ==="
	@bash tests/test_setup_vault.sh

test-pipeline-events:
	@echo "=== test_pipeline_events.py ==="
	@python3 tests/test_pipeline_events.py

test-bm25:
	@echo "=== test_bm25_index.py ==="
	@python3 tests/test_bm25_index.py

test-retrieve:
	@echo "=== test_retrieve.py ==="
	@python3 tests/test_retrieve.py

test-bench:
	@echo "=== test_retrieval_bench.py ==="
	@python3 tests/test_retrieval_bench.py

test-retrieval-experiment:
	@echo "=== test_retrieval_experiment.py ==="
	@python3 tests/test_retrieval_experiment.py

test-fold:
	@echo "=== test_fold_log.py ==="
	@python3 tests/test_fold_log.py

test-router:
	@echo "=== test_skill_router.sh ==="
	@bash tests/test_skill_router.sh

test-review-dispatch:
	@echo "=== test_review_dispatch.sh ==="
	@bash tests/test_review_dispatch.sh

test-gateway:
	@echo "=== test_mcp_gateway.sh ==="
	@bash tests/test_mcp_gateway.sh

test-codex-adapter:
	@echo "=== test_codex_adapter.sh ==="
	@bash tests/test_codex_adapter.sh

test-dcg-assets:
	@echo "=== test_dcg_assets.sh ==="
	@bash tests/test_dcg_assets.sh

test-with-timeout:
	@echo "=== test_with_timeout.sh ==="
	@bash tests/test_with_timeout.sh

bench-retrieval:
	@python3 scripts/retrieval-bench.py --gate --verbose

setup-dragonscale:
	@bash bin/setup-dragonscale.sh

clean-test-state:
	@rm -f .vault-meta/.address.lock .vault-meta/.tiling.lock .vault-meta/tiling-cache.json .vault-meta/tiling-cache.*.tmp
	@echo "Runtime lockfiles and tiling cache removed."
