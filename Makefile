# llm-obsidian Makefile
# Test runner entry points for DragonScale and vault tooling.

.DEFAULT_GOAL := help

.PHONY: test-harness test-harness-coverage test-code-quality code-quality-audit test-model-routing test-session-preflight test-model-literal-lint test-upgrade-preflight test-task-sessions test-docs

test-harness:
	@echo "=== harness contracts and replay regressions ==="
	@python3 tests/harness/test_contracts.py
	@python3 tests/harness/test_contract_boundaries.py
	@python3 tests/harness/test_contract_state_edge_matrix.py
	@python3 tests/harness/test_continuation_delivery.py
	@python3 tests/harness/test_pipelines.py
	@python3 tests/harness/test_custom_pipelines.py
	@python3 tests/harness/test_custom_sequence.py
	@python3 tests/harness/test_custom_runtime.py
	@CUSTOM_RUNTIME_PROFILE=fix python3 tests/harness/test_custom_runtime.py
	@python3 tests/test_pipeline_verification_resubmit.py
	@python3 tests/harness/test_liveness.py
	@python3 tests/harness/test_pipeline_builtins.py
	@python3 tests/harness/test_regressions.py
	@python3 tests/harness/test_store.py
	@python3 tests/harness/test_adapters.py
	@python3 tests/harness/test_provider_events.py
	@python3 tests/harness/test_ephemeral_provider_conformance.py
	@python3 tests/harness/test_delivery_boundary.py
	@python3 tests/harness/test_callbacks.py
	@python3 tests/harness/test_callback_submit_recovery.py
	@python3 tests/harness/test_callback_submit_recovery_runtime.py
	@python3 tests/harness/test_harness_control_plane.py
	@python3 tests/harness/test_release_transition_matrix.py
	@python3 tests/harness/test_context_verification.py
	@python3 tests/harness/test_workflows.py
	@python3 tests/harness/test_engineering_fix_workflow.py
	@python3 tests/harness/test_research_vertical.py
	@python3 tests/harness/test_research_notifier.py
	@python3 tests/harness/test_engineering_skills.py
	@python3 tests/harness/test_review_inspect.py
	@python3 tests/harness/test_plan_review_facade.py
	@python3 tests/harness/test_review_resolution.py
	@python3 tests/harness/test_review_resolution_bundle.py
	@python3 tests/harness/test_review_delta_packet.py
	@python3 tests/harness/test_review_telemetry.py
	@python3 tests/harness/test_review_transport.py
	@python3 tests/harness/test_review_program.py
	@python3 tests/harness/test_review_topology.py
	@python3 tests/harness/test_review_vertical.py
	@python3 tests/harness/test_review_gate.py
	@python3 tests/harness/test_runtime_sessions.py
	@python3 tests/harness/test_runtime_task_summary.py
	@python3 tests/harness/test_runtime_research.py
	@python3 tests/harness/test_review_finalization.py
	@python3 tests/harness/test_task_review_mechanism_recovery.py
	@python3 tests/harness/test_task_review_flow_units.py
	@python3 tests/harness/test_task_escalation_records.py
	@python3 tests/harness/test_task_session_store_io.py
	@python3 tests/harness/test_dispatch_runtime.py
	@python3 tests/harness/test_runtime_inventory.py
	@python3 tests/harness/test_release_blocker_runtime.py
	@python3 tests/harness/test_status_segment.py
	@python3 tests/harness/test_diagnostics.py
	@python3 tests/harness/test_suite_registration.py

test-harness-coverage:
	@echo "=== hermetic harness statement-line coverage ==="
	@python3 tests/test_harness_coverage_audit.py
	@python3 scripts/harness-coverage-audit.py

test-code-quality:
	@echo "=== code quality audit unit contracts ==="
	@python3 tests/test_code_quality_audit.py
	@python3 scripts/code-quality-audit.py --baseline config/code-quality-baseline.json

code-quality-audit: test-code-quality
	@python3 scripts/code-quality-audit.py

test-task-sessions:
	@echo "=== test_task_sessions.py ==="
	@python3 tests/test_task_sessions.py

test-model-routing:
	@echo "=== test_model_routing.py ==="
	@python3 tests/test_model_routing.py

test-session-preflight:
	@echo "=== test_session_preflight.py ==="
	@python3 tests/test_session_preflight.py

test-model-literal-lint:
	@echo "=== model-literal-lint.py ==="
	@python3 scripts/model-literal-lint.py

test-upgrade-preflight:
	@echo "=== test_upgrade_preflight.py ==="
	@python3 tests/test_upgrade_preflight.py

.PHONY: test eval-smoke eval-live eval-regression paired-eval-verify acceptance-check acceptance-live acceptance-live-restart retrieval-experiment test-release-acceptance test-live-acceptance-runner test-agent-evals test-paired-evals test-daily-pipeline test-session-map test-claude-subscription test-journal-write test-agenda test-dense-worker test-document-normalize test-documents test-research-isolation test-runtime-hooks test-command-evidence test-runtime-detection test-skill-workstreams test-skill-budget test-improve-skills test-outcome-contract test-contract-schemas test-task-lifecycle test-instruction-lint test-ci-workflow test-mcp-schema-lock test-address test-schema test-tiling test-boundary test-vault test-vault-link-repair test-plan-capture test-stop-hook test-memory-backup test-setup-vault test-pipeline-events test-pipeline-stats test-review-callback-evidence test-custom-pipeline-report test-bm25 test-retrieve test-bench test-retrieval-experiment test-fold test-router test-gateway test-codex-adapter test-dcg-assets test-with-timeout bench-retrieval setup-dragonscale clean-test-state help

help:
	@echo "llm-obsidian developer targets:"
	@echo "  make test              Run all vault + retrieval + hook tests"
	@echo "  make test-harness-coverage  Audit statement-line coverage and ratchet critical floors"
	@echo "  make test-docs          Validate the Russian handbook, examples, inventory, and PipelineSpec"
	@echo "  make eval-smoke        Validate and grade checked-in agent eval fixtures"
	@echo "  make eval-live         Run opt-in live evals (EVAL_RUNNER='command')"
	@echo "  make eval-regression   Smoke + live retrieval quality gate"
	@echo "  make paired-eval-verify Verify frozen 2.6 paired plans and fixture bytes"
	@echo "  make acceptance-check  Validate the bounded four-cell harness release contract"
	@echo "  make acceptance-live   Run/resume exactly four provider-backed harness cells"
	@echo "  make acceptance-live-restart  Discard the matching four-cell checkpoint"
	@echo "  make retrieval-experiment compare contextual/reranker flags without enabling"
	@echo "  make test-research-isolation protected fetch/synthesis boundary tests"
	@echo "  make test-document-normalize hermetic document routing/cache/fallback tests"
	@echo "  make test-documents     live Docling ru/en PDF/Office/offline acceptance"
	@echo "  make test-runtime-hooks Claude/Codex hook wire parity tests"
	@echo "  make test-command-evidence typed command capture/ingestion tests"
	@echo "  make test-runtime-detection legacy + three-way runtime detection tests"
	@echo "  make test-skill-workstreams 2.6 engineering skill behavior contracts"
	@echo "  make test-session-map Claude/Codex daily session grouping tests"
	@echo "  make test-agenda      deterministic daily carry-over and report tests"
	@echo "  make test-skill-budget enforce Codex initial skill registry budget"
	@echo "  make test-contract-schemas executable/published contract parity"
	@echo "  make test-task-lifecycle unattended contract + cmux close lifecycle"
	@echo "  make test-instruction-lint canonical skill/runtime instruction checks"
	@echo "  make test-ci-workflow shallow-checkout whitespace gate invariants"
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
	@echo "  make test-pipeline-stats  evidence-bounded skill usage reporting tests"
	@echo "  make test-review-callback-evidence review callback validity evidence + prompt/validator agreement"
	@echo "  make test-bm25        bm25-index.py + hybrid fusion tests (python, no ollama)"
	@echo "  make test-retrieve    section chunking, ranking, and dense degradation tests"
	@echo "  make test-bench       retrieval-bench metrics/degradation tests (python, no ollama)"
	@echo "  make test-fold        deterministic counter-free log fold tests"
	@echo "  make test-router      skill-router prompt matching suite (shell)"
	@echo "  make test-gateway     MCP gateway config invariants (shell, offline)"
	@echo "  make test-codex-adapter Codex plugin packaging generator tests"
	@echo "  make test-dcg-assets  dcg config/hooks and Codex limit helper checks"
	@echo "  make test-with-timeout portable shell timeout helper tests"
	@echo "  make bench-retrieval  LIVE retrieval quality benchmark (requires ollama)"
	@echo "  make setup-dragonscale Run bin/setup-dragonscale.sh against this vault"
	@echo "  make clean-test-state Remove runtime lockfiles and tiling cache"

test: test-harness test-code-quality test-docs test-task-sessions test-model-routing test-session-preflight test-model-literal-lint test-upgrade-preflight test-release-acceptance test-live-acceptance-runner test-pipeline-runners test-agent-evals test-paired-evals test-daily-pipeline test-session-map test-claude-subscription test-journal-write test-agenda test-dense-worker test-document-normalize test-research-isolation test-runtime-hooks test-command-evidence test-runtime-detection test-skill-workstreams test-skill-budget test-improve-skills test-outcome-contract test-contract-schemas test-task-lifecycle test-instruction-lint test-ci-workflow test-mcp-schema-lock test-address test-schema test-tiling test-boundary test-vault test-vault-link-repair test-plan-capture test-stop-hook test-memory-backup test-setup-vault test-pipeline-events test-pipeline-stats test-review-callback-evidence test-custom-pipeline-report test-bm25 test-retrieve test-bench test-retrieval-experiment test-fold test-router test-gateway test-codex-adapter test-dcg-assets test-with-timeout
	@echo ""
	@echo "All tests passed."

test-docs:
	@echo "=== test_russian_documentation.py ==="
	@python3 tests/test_russian_documentation.py

eval-smoke:
	@python3 scripts/agent-evals.py smoke

eval-live:
	@test -n "$(EVAL_RUNNER)" || { echo "EVAL_RUNNER is required" >&2; exit 2; }
	@python3 scripts/agent-evals.py live --runner "$(EVAL_RUNNER)" --trials "$${EVAL_TRIALS:-3}" --report .vault-meta/evals/latest-live.json

eval-regression: eval-smoke bench-retrieval

paired-eval-verify:
	@python3 scripts/paired-evals.py verify

acceptance-check:
	@python3 scripts/code-quality-audit.py
	@python3 scripts/release-acceptance.py check

acceptance-live:
	@python3 scripts/claude-subscription-check.py
	@python3 scripts/live-acceptance-runner.py run --timeout "$${ACCEPTANCE_CELL_TIMEOUT:-1200}" --report .vault-meta/acceptance/latest-live.json

acceptance-live-restart:
	@python3 scripts/claude-subscription-check.py
	@python3 scripts/live-acceptance-runner.py run --restart --timeout "$${ACCEPTANCE_CELL_TIMEOUT:-1200}" --report .vault-meta/acceptance/latest-live.json

test-release-acceptance:
	@echo "=== test_release_acceptance.py ==="
	@python3 tests/test_release_acceptance.py

test-live-acceptance-runner:
	@echo "=== test_live_acceptance_runner.py ==="
	@python3 tests/test_live_acceptance_runner.py
	@echo "=== test_live_acceptance_surface_cleanup.py ==="
	@python3 tests/test_live_acceptance_surface_cleanup.py

test-pipeline-runners:
	@echo "=== deterministic dispatch/reap runners ==="
	@python3 tests/test_dispatch_resolver.py
	@python3 tests/test_dispatch_runner.py
	@python3 tests/test_pipeline_step_submit.py
	@python3 tests/test_reap_runner.py
	@python3 tests/test_queue_session_exit.py

retrieval-experiment:
	@python3 scripts/retrieval-experiment.py

test-agent-evals:
	@echo "=== test_agent_evals.py ==="
	@python3 tests/test_agent_evals.py
	@python3 tests/test_engineering_eval_runner.py

test-paired-evals:
	@echo "=== test_paired_evals.py ==="
	@python3 tests/test_paired_evals.py

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

test-command-evidence:
	@echo "=== test_command_evidence.py ==="
	@python3 tests/test_command_evidence.py

test-runtime-detection:
	@echo "=== test_detect_runtime.sh ==="
	@bash tests/test_detect_runtime.sh

test-skill-workstreams:
	@echo "=== test_skill_workstream_a.py ==="
	@python3 tests/test_skill_workstream_a.py
	@echo "=== test_skill_workstream_b.py ==="
	@python3 tests/test_skill_workstream_b.py
	@echo "=== test_workstream_c_review_reap.py ==="
	@python3 tests/test_workstream_c_review_reap.py
	@echo "=== test_engineering_quality_skills.py ==="
	@python3 tests/test_engineering_quality_skills.py

test-skill-budget:
	@echo "=== test_skill_budget.py ==="
	@python3 tests/test_skill_budget.py

test-improve-skills:
	@echo "=== test_improve_skills.py ==="
	@python3 tests/test_improve_skills.py

test-outcome-contract:
	@echo "=== test_outcome_contract.py ==="
	@python3 tests/test_outcome_contract.py

test-contract-schemas:
	@echo "=== test_contract_schemas.py ==="
	@python3 tests/test_contract_schemas.py

test-task-lifecycle:
	@echo "=== test_task_lifecycle.py ==="
	@python3 tests/test_task_lifecycle.py

test-instruction-lint:
	@echo "=== test_instruction_lint.py ==="
	@python3 tests/test_instruction_lint.py

test-ci-workflow:
	@echo "=== test_ci_workflow.py ==="
	@python3 tests/test_ci_workflow.py

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

test-vault-link-repair:
	@echo "=== test_vault_link_repair.py ==="
	@python3 tests/test_vault_link_repair.py

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

test-pipeline-stats:
	@echo "=== test_pipeline_stats.py ==="
	@python3 tests/test_pipeline_stats.py

test-review-callback-evidence:
	@echo "=== test_review_callback_evidence.py ==="
	@python3 tests/test_review_callback_evidence.py

test-custom-pipeline-report:
	@echo "=== test_custom_pipeline_report.py ==="
	@python3 tests/test_custom_pipeline_report.py

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
