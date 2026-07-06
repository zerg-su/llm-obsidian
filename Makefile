# llm-obsidian Makefile
# Test runner entry points for DragonScale and vault tooling.

.PHONY: test test-address test-tiling test-boundary test-vault test-stop-hook test-bm25 test-bench test-router test-review-dispatch test-gateway test-codex-adapter test-dcg-assets bench-retrieval setup-dragonscale clean-test-state help

help:
	@echo "llm-obsidian developer targets:"
	@echo "  make test              Run all vault + retrieval + hook tests"
	@echo "  make test-address     scripts/allocate-address.sh tests (shell)"
	@echo "  make test-tiling      scripts/tiling-check.py tests (python, no ollama required)"
	@echo "  make test-boundary    scripts/boundary-score.py tests (python, no prereqs)"
	@echo "  make test-vault       vault-write/validate/reindex regression suite (shell)"
	@echo "  make test-stop-hook   stop.sh flock + opt-out + latency suite (shell, sandbox git repo)"
	@echo "  make test-bm25        bm25-index.py + hybrid fusion tests (python, no ollama)"
	@echo "  make test-bench       retrieval-bench metrics/degradation tests (python, no ollama)"
	@echo "  make test-router      skill-router prompt matching suite (shell)"
	@echo "  make test-review-dispatch review-dispatch mode plumbing tests"
	@echo "  make test-gateway     MCP gateway config invariants (shell, offline)"
	@echo "  make test-codex-adapter Codex plugin packaging generator tests"
	@echo "  make test-dcg-assets  dcg config/hooks and Codex limit helper checks"
	@echo "  make bench-retrieval  LIVE retrieval quality benchmark (requires ollama)"
	@echo "  make setup-dragonscale Run bin/setup-dragonscale.sh against this vault"
	@echo "  make clean-test-state Remove runtime lockfiles and tiling cache"

test: test-address test-tiling test-boundary test-vault test-stop-hook test-bm25 test-bench test-router test-review-dispatch test-gateway test-codex-adapter test-dcg-assets
	@echo ""
	@echo "All tests passed."

test-address:
	@echo "=== test_allocate_address.sh ==="
	@bash tests/test_allocate_address.sh

test-tiling:
	@echo "=== test_tiling_check.py ==="
	@python3 tests/test_tiling_check.py

test-boundary:
	@echo "=== test_boundary_score.py ==="
	@python3 tests/test_boundary_score.py

test-vault:
	@echo "=== test_vault_scripts.sh ==="
	@bash tests/test_vault_scripts.sh

test-stop-hook:
	@echo "=== test_stop_hook.sh ==="
	@bash tests/test_stop_hook.sh

test-bm25:
	@echo "=== test_bm25_index.py ==="
	@python3 tests/test_bm25_index.py

test-bench:
	@echo "=== test_retrieval_bench.py ==="
	@python3 tests/test_retrieval_bench.py

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

bench-retrieval:
	@python3 scripts/retrieval-bench.py --verbose

setup-dragonscale:
	@bash bin/setup-dragonscale.sh

clean-test-state:
	@rm -f .vault-meta/.address.lock .vault-meta/.tiling.lock .vault-meta/tiling-cache.json .vault-meta/tiling-cache.*.tmp
	@echo "Runtime lockfiles and tiling cache removed."
