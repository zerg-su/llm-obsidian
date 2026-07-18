#!/usr/bin/env python3
"""Hermetic tests for durable cross-model review history."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "review-dispatch" / "scripts" / "archive_review.py"
SPEC = importlib.util.spec_from_file_location("review_archive", SCRIPT)
assert SPEC and SPEC.loader
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)
vault_schema = importlib.import_module("vault_schema")


def review(run_id: str, verdict: str, findings: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "mode": "full",
        "verdict": verdict,
        "findings": findings,
        "verification_gaps": ["Live cmux close was not exercised"] if verdict != "approve" else [],
        "notes_for_executor": ["Keep the regression test"],
        "residual_risks": [],
    }


class ReviewArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.worktree = base / "worktree"
        self.vault = base / "vault"
        self.worktree.mkdir()
        (self.vault / "wiki" / "meta" / "reviews").mkdir(parents=True)
        (self.vault / "wiki" / "plans").mkdir(parents=True)
        self.plan = self.vault / "wiki" / "plans" / "Durable review history.md"
        self.plan.write_text("plan\n", encoding="utf-8")
        (self.worktree / ".task-meta.json").write_text(
            json.dumps(
                {
                    "task_name": "durable-review-history",
                    "origin_session": "origin-session",
                    "executor_runtime": "codex",
                    "plan_file": str(self.plan),
                }
            ),
            encoding="utf-8",
        )
        (self.worktree / ".task-prompt.md").write_text(
            "# Task: durable-review-history\n\n"
            "## Task description\n\n"
            "Keep every validated review round and explain why the review ran.\n\n"
            "## Wiki context (pre-loaded)\n\nInternal orchestration details.\n",
            encoding="utf-8",
        )
        (self.worktree / ".review-meta.json").write_text(
            json.dumps(
                {
                    "version": 5,
                    "review_id": "review-cycle-1",
                    "task_name": "durable-review-history",
                    "started_at": "2026-07-13T01:02:03Z",
                    "updated_at": "2026-07-13T02:03:04Z",
                    "reviewer_runtime": "claude",
                    "reviewer_model": "opus",
                    "reviewer_effort": "max",
                    "review_mode": "full",
                    "executor_runtime": "codex",
                }
            ),
            encoding="utf-8",
        )
        initial = review(
            "review-cycle-1",
            "changes-requested",
            [
                {
                    "severity": "warning",
                    "file": "skills/review-dispatch/scripts/archive_review.py",
                    "line": 42,
                    "title": "Archive update is not idempotent",
                    "evidence": "The second write duplicates the operation log entry.",
                    "recommendation": "Use a stable review identifier and optimistic update.",
                }
            ],
        )
        final = review("round-2", "approve", [])
        (self.worktree / ".review-history.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "review_id": "review-cycle-1",
                    "task_name": "durable-review-history",
                    "request": {
                        "description": "Keep every validated review round and explain why the review ran.",
                        "base_branch": "main",
                        "branch": "feature/review-history",
                        "review_mode": "full",
                    },
                    "rounds": [
                        {
                            "iteration": 1,
                            "phase": "initial-review",
                            "received_at": "2026-07-13T01:30:00Z",
                            "review": initial,
                            "resolution": "Applied: stable identity and optimistic hash were added.",
                        },
                        {
                            "iteration": 2,
                            "phase": "verify-fixes",
                            "received_at": "2026-07-13T02:00:00Z",
                            "review": final,
                            "resolution": None,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.payloads: list[dict[str, object]] = []
        self.allocations = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def allocate(self, _vault: Path) -> str:
        self.allocations += 1
        return "c-123456"

    def write(self, vault: Path, payload: dict[str, object]) -> None:
        self.payloads.append(payload)
        page = payload["pages"][0]  # type: ignore[index]
        target = vault / str(page["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(page["content"]), encoding="utf-8")

    def run_archive(self, *, dry_run: bool = False) -> dict[str, object]:
        return archive.archive_review(
            self.worktree,
            self.vault,
            session="archive-session",
            allocate=self.allocate,
            write=self.write,
            dry_run=dry_run,
        )

    def test_archives_all_rounds_and_is_idempotent(self) -> None:
        first = self.run_archive()
        self.assertEqual(first["status"], "archived")
        self.assertEqual(first["rounds"], 2)
        self.assertEqual(first["verdict"], "approve")
        self.assertTrue(str(first["path"]).startswith("wiki/meta/reviews/"))
        page = (self.vault / str(first["path"])).read_text(encoding="utf-8")
        self.assertIn("type: review", page)
        self.assertIn('review_id: "review-cycle-1"', page)
        self.assertIn("## Round 1 — changes-requested", page)
        self.assertIn("## Round 2 — approve", page)
        self.assertIn("Applied: stable identity", page)
        self.assertIn("[[Durable review history]]", page)
        self.assertIn("## Review request", page)
        self.assertIn("Original task request", page)
        self.assertIn("explain why the review ran", page)
        self.assertNotIn("Internal orchestration details", page)
        self.assertNotIn("payload-b64", page)
        self.assertNotIn(str(self.worktree), page)
        self.assertEqual(self.allocations, 1)
        self.assertIn("log_entry", self.payloads[0])

        second = self.run_archive()
        self.assertEqual(second["status"], "already-current")
        self.assertEqual(len(self.payloads), 1)
        marker = json.loads((self.worktree / ".review-archive.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["review_id"], "review-cycle-1")
        self.assertEqual(marker["wikilink"], first["wikilink"])

    def test_update_uses_optimistic_hash_without_new_log_entry(self) -> None:
        first = self.run_archive()
        history = json.loads((self.worktree / ".review-history.json").read_text(encoding="utf-8"))
        history["rounds"][1]["review"]["notes_for_executor"] = ["Verified after a focused rerun"]
        (self.worktree / ".review-history.json").write_text(json.dumps(history), encoding="utf-8")
        second = self.run_archive()
        self.assertEqual(second["status"], "archived")
        self.assertEqual(len(self.payloads), 2)
        update = self.payloads[1]
        self.assertNotIn("log_entry", update)
        page = update["pages"][0]  # type: ignore[index]
        self.assertEqual(page["op"], "update")
        self.assertIn("expected_sha256", page)
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(self.allocations, 1)

    def test_dry_run_has_no_side_effects(self) -> None:
        result = self.run_archive(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(self.allocations, 0)
        self.assertEqual(self.payloads, [])
        self.assertFalse((self.worktree / ".review-archive.json").exists())

    def test_operation_scoped_archive_keeps_worktree_clean(self) -> None:
        state_dir = Path(self.tmp.name) / "operation"
        state_dir.mkdir()
        for name in (".review-meta.json", ".review-history.json"):
            shutil.move(self.worktree / name, state_dir / name)
        meta = json.loads((state_dir / ".review-meta.json").read_text(encoding="utf-8"))
        meta["worktree"] = str(self.worktree)
        meta["review_id"] = "operation-review-id"
        (state_dir / ".review-meta.json").write_text(json.dumps(meta), encoding="utf-8")
        history = json.loads((state_dir / ".review-history.json").read_text(encoding="utf-8"))
        history["review_id"] = "operation-review-id"
        (state_dir / ".review-history.json").write_text(json.dumps(history), encoding="utf-8")
        result = archive.archive_review(
            self.worktree,
            self.vault,
            state_dir=state_dir,
            session="archive-session",
            allocate=self.allocate,
            write=self.write,
        )
        self.assertEqual(result["status"], "archived")
        self.assertTrue((state_dir / ".review-archive.json").is_file())
        self.assertFalse((self.worktree / ".review-archive.json").exists())

    def test_free_text_cannot_create_spurious_wikilinks(self) -> None:
        history_path = self.worktree / ".review-history.json"
        history = json.loads(history_path.read_text(encoding="utf-8"))
        history["request"]["description"] = "Inspect [[Request Page]] before release."
        finding = history["rounds"][0]["review"]["findings"][0]
        finding["evidence"] = "See [[Some Page]] and the class [[:alpha:]]."
        finding["recommendation"] = "Do not link [[Recommendation Page]]."
        history["rounds"][0]["resolution"] = "Applied after checking [[Resolution Page]]."
        history["rounds"][0]["review"]["verification_gaps"] = [
            "The [[:alpha:]] case was not exercised live."
        ]
        history["rounds"][0]["review"]["residual_risks"] = [
            "A reviewer may mention [[Risk Page]]."
        ]
        history["rounds"][0]["review"]["notes_for_executor"] = [
            "Keep [[Notes Page]] as plain prose."
        ]
        history_path.write_text(json.dumps(history), encoding="utf-8")

        result = self.run_archive()
        page = (self.vault / str(result["path"])).read_text(encoding="utf-8")

        self.assertIn(r"\[\[Some Page\]\]", page)
        self.assertIn(r"\[\[:alpha:\]\]", page)
        links = list(vault_schema.iter_wikilinks(page))
        self.assertTrue(links)
        self.assertEqual(set(links), {"Durable review history"})

    def test_rejects_invalid_history(self) -> None:
        history = json.loads((self.worktree / ".review-history.json").read_text(encoding="utf-8"))
        history["rounds"][0]["review"]["findings"][0]["file"] = "/private/secret.txt"
        (self.worktree / ".review-history.json").write_text(json.dumps(history), encoding="utf-8")
        with self.assertRaises(archive.ArchiveError):
            self.run_archive()

    def test_unicode_filename_stays_below_filesystem_limit(self) -> None:
        history = json.loads((self.worktree / ".review-history.json").read_text(encoding="utf-8"))
        history["task_name"] = "проверка-" * 100
        (self.worktree / ".review-history.json").write_text(json.dumps(history), encoding="utf-8")
        result = self.run_archive()
        self.assertLessEqual(len(Path(str(result["path"])).name.encode("utf-8")), 255)

    def test_legacy_artifacts_are_backfilled(self) -> None:
        (self.worktree / ".review-history.json").unlink()
        (self.worktree / ".task-prompt.md").write_text(
            "# Task: legacy-review\n\nReview the legacy implementation.\n\n"
            "## Constraints\n\nPreserve every human-authored scope section.\n",
            encoding="utf-8",
        )
        initial = review("legacy-initial", "changes-requested", [])
        final = review("legacy-verify", "approve", [])
        (self.worktree / ".task-review.json").write_text(json.dumps(initial), encoding="utf-8")
        (self.worktree / ".task-review-verify.json").write_text(json.dumps(final), encoding="utf-8")
        (self.worktree / ".task-review-resolution.md").write_text("Applied legacy finding.\n", encoding="utf-8")
        result = self.run_archive()
        page = (self.vault / str(result["path"])).read_text(encoding="utf-8")
        self.assertEqual(result["review_id"], "legacy-initial")
        self.assertIn("Applied legacy finding", page)
        self.assertIn("## Round 2 — approve", page)
        self.assertIn("Review the legacy implementation", page)
        self.assertIn("Preserve every human-authored scope section", page)


if __name__ == "__main__":
    unittest.main()
