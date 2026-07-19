#!/usr/bin/env python3
"""Hermetic checks for read-only dispatch candidate resolution."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dispatch_resolver", ROOT / "scripts/dispatch-resolver.py")
assert spec and spec.loader
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)

failures: list[str] = []


def check(name: str, value: bool) -> None:
    print(("ok" if value else "not ok") + " - " + name)
    if not value:
        failures.append(name)


with tempfile.TemporaryDirectory(prefix="dispatch-resolver-test.") as raw:
    tmp = Path(raw)
    vault = tmp / "vault"
    projects = tmp / "projects"
    repo = projects / "demo"
    (vault / "wiki/repos").mkdir(parents=True)
    (vault / "wiki/plans").mkdir(parents=True)
    (vault / "wiki/decisions").mkdir(parents=True)
    (repo / ".git").mkdir(parents=True)
    (vault / "wiki/repos/Demo.md").write_text(f"---\npath: {repo}\n---\n", encoding="utf-8")
    (vault / "wiki/repos/Demo body only.md").write_text(f"# Not metadata\npath: {projects / 'wrong'}\n", encoding="utf-8")
    (vault / "wiki/plans/2026-demo.md").write_text(
        "---\nstatus: pending\nsession_id: session-one\n---\n", encoding="utf-8"
    )
    (vault / "wiki/decisions/Dispatch safety.md").write_text(
        "# Dispatch safety\nUse an anchored worktree runner for demo.\n", encoding="utf-8"
    )
    result = resolver.resolve_request({
        "schema_version": 1,
        "vault_root": str(vault),
        "projects_root": str(projects),
        "repo_name": "demo",
        "description": "Improve dispatch worktree safety",
        "session_id": "session-one",
    })
    check("unique repo and plan resolve", result["status"] == "resolved")
    check("duplicate repo sources deduplicate by path", len(result["repo_candidates"]) == 1)
    check("body path text is never treated as repo metadata", result["repo_candidates"][0]["path"] == str(repo.resolve()))
    check("current-session plan is selected", len(result["plan_candidates"]) == 1)
    check("context is ranked deterministically", result["context_candidates"][0]["title"] == "Dispatch safety")
    check("missing canonical retriever is an explicit sparse degradation", result["context_retrieval"]["degraded"] is True)
    (projects / "other/demo/.git").mkdir(parents=True)
    ambiguous = resolver.resolve_request({
        "schema_version": 1,
        "vault_root": str(vault),
        "projects_root": str(projects),
        "repo_name": "demo",
        "description": "Improve dispatch worktree safety",
        "session_id": "session-one",
    })
    check("ambiguous repo fails closed", ambiguous["status"] == "needs-selection" and "repo-ambiguous" in ambiguous["blockers"])

if failures:
    raise SystemExit(f"{len(failures)} dispatch resolver test(s) failed")
print("All dispatch resolver tests passed.")
