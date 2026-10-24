#!/usr/bin/env python3
"""Reap removes only completed owned checkouts and supports durable replay."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("reap_runner", ROOT / "scripts/reap-runner.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from harness.contracts import OwnedResources, RuntimeRoute
from harness.git_ops import GitAdapter
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.workflows.dispatch import DispatchRequest, run_dispatch


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReapCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reap-worktree-cleanup.")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.vault, self.repo, self.worktree = base / "vault", base / "repo", base / "worker"
        (self.vault / "wiki").mkdir(parents=True)
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "code.py").write_text("value = 1\n")
        (self.repo / ".gitignore").write_text("*.pyc\n.env\n")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        GitAdapter(self.repo).create_worktree(self.worktree, "task/test", "main")
        self.head = self.git("rev-parse", "task/test")
        self.task = str(uuid.uuid4())
        self.session = "origin-session"
        self.meta = {
            "version": 3, "task_id": self.task, "task_name": "test",
            "project_id": str(uuid.uuid4()), "interaction_policy": "unattended",
            "origin_session": self.session, "vault_root": str(self.vault),
            "target_repo": str(self.repo), "worktree": str(self.worktree),
            "branch": "task/test", "routing": {"session": {"runtime": "codex"}},
            "reap_policy": {"mode": "final"},
        }
        self.write(".task-meta.json", self.meta)
        self.summary = {"schema_version": 1, "type": "session", "title": "Result",
                        "session": "executor", "body": "Completed the task."}
        self.write(".task-summary.json", self.summary)
        self.result_path = self.vault / "wiki/Result.md"
        self.result = {"schema_version": 1, "status": "complete", "result_path": str(self.result_path),
                       "result_link": "[[Result]]", "plan_close_status": "closed", "warnings": [], "duration_ms": 1}
        self.store = OperationStore(self.vault / ".vault-meta/harness")
        run_dispatch(DispatchRequest(self.task, self.task, "b" * 64, "packets/task/manifest.json",
                     RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)),
                     self.store, launch=lambda _: {"status": "launched"}, persist_result=lambda *_: None)
        OperationSupervisor(self.store, self.task, self.task).bind_resources(
            OwnedResources("22222222-2222-4222-8222-222222222222", 4242, 4343))
        self.effects = []
        fixture = self
        class Runtime:
            def request_exit(self, owner, operation):
                fixture.effects.append("exit")
                s = OperationSupervisor(fixture.store, owner, operation)
                return SimpleNamespace(action="exit-requested", record=s.transition("exiting"))
            def cleanup(self, owner, operation):
                fixture.effects.append("runtime-cleanup")
                s = OperationSupervisor(fixture.store, owner, operation)
                s.bind_resources(OwnedResources())
                return SimpleNamespace(action="cleaned", record=s.transition("complete"))
        self.runtime = Runtime()
        for name in ("validate_handoff", "validate_summary_wikilinks", "approved_plan_state", "authorize_review", "launch_bound_facade_dashboard"):
            p = patch.object(runner, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(runner, "_finalize_reap", side_effect=self.finalize)
        p.start()
        self.addCleanup(p.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def write(self, name, value):
        (self.worktree / name).write_text(json.dumps(value) + "\n")

    def finalize(self, *_):
        self.effects.append("file-result")
        self.result_path.write_text("# Result\nCompleted.\n")
        self.write(".task-reap-prepared.json", self.result)
        self.write(".task-reap-complete.json", {
            "version": 1, "task_name": "test", "current_session": self.session,
            "vault_root": str(self.vault), "result_path": str(self.result_path),
            "meta_sha256": digest(self.worktree / ".task-meta.json"),
            "summary_sha256": digest(self.worktree / ".task-summary.json"),
            "result_sha256": digest(self.result_path), "validated": True,
            "task_session_status": "archived", "plan_close_status": self.result["plan_close_status"],
        })
        return dict(self.result)

    def reap(self, session=None):
        return runner.apply_reap(self.vault, self.worktree, session or self.session, runtime_manager=self.runtime)

    def test_completed_owned_checkout_is_removed_and_replay_is_zero_effect(self):
        result = self.reap()
        self.assertFalse(self.worktree.exists(), "completed owned worktree was leaked")
        self.assertEqual(result["worktree_cleanup"]["status"], "removed")
        self.assertEqual(self.git("rev-parse", "task/test"), self.head)
        self.assertNotIn(str(self.worktree), self.git("worktree", "list", "--porcelain"))
        self.assertTrue(self.result_path.is_file())
        self.assertEqual(self.effects, ["file-result", "exit", "runtime-cleanup"])
        replay = self.reap()
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["result_path"], str(self.result_path))
        self.assertEqual(self.effects, ["file-result", "exit", "runtime-cleanup"])

    def test_dirty_tracked_file_is_retained(self):
        (self.worktree / "code.py").write_text("value = 2\n")
        result = self.reap()
        self.assertEqual((self.worktree / "code.py").read_text(), "value = 2\n")
        self.assertEqual(result["worktree_cleanup"]["status"], "retained")

    def test_unknown_untracked_file_is_retained(self):
        (self.worktree / "unfinished.md").write_text("unique work\n")
        result = self.reap()
        self.assertEqual((self.worktree / "unfinished.md").read_text(), "unique work\n")
        self.assertEqual(result["worktree_cleanup"]["status"], "retained")

    def test_unknown_ignored_file_is_retained(self):
        (self.worktree / ".env").write_text("local configuration\n")
        result = self.reap()
        self.assertTrue((self.worktree / ".env").is_file())
        self.assertEqual(result["worktree_cleanup"]["status"], "retained")

    def test_owned_runtime_artifacts_and_python_cache_do_not_leak_checkout(self):
        (self.worktree / ".task-origin-session").write_text(self.session)
        (self.worktree / "__pycache__").mkdir()
        (self.worktree / "__pycache__/code.cpython-313.pyc").write_bytes(b"cache")
        self.reap()
        self.assertFalse(self.worktree.exists())

    def test_missing_ownership_marker_is_retained(self):
        gitdir = subprocess.check_output(["git", "-C", str(self.worktree), "rev-parse", "--absolute-git-dir"], text=True).strip()
        (Path(gitdir) / "llm-obsidian-harness-owner.json").unlink()
        result = self.reap()
        self.assertTrue(self.worktree.exists())
        self.assertEqual(result["worktree_cleanup"]["status"], "retained")

    def test_provider_cleanup_failure_cannot_remove_worktree(self):
        with patch.object(self.runtime, "cleanup", side_effect=runner.RuntimeSessionError("still active")):
            with self.assertRaises(runner.ReapError):
                self.reap()
        self.assertTrue(self.worktree.exists())

    def test_recreated_path_is_not_deleted_by_completed_replay(self):
        self.reap()
        self.assertFalse(self.worktree.exists())
        self.worktree.mkdir()
        (self.worktree / "new.txt").write_text("new owner")
        with self.assertRaises((runner.ReapError, ValueError, OSError)):
            self.reap()
        self.assertEqual((self.worktree / "new.txt").read_text(), "new owner")

    def test_replay_requires_original_coordinator(self):
        self.reap()
        self.assertFalse(self.worktree.exists())
        with self.assertRaises((runner.ReapError, ValueError)):
            self.reap("different-session")


    def test_current_v4_task_is_removed(self):
        from approved_plan_snapshot import bind_approved_plan_snapshot
        from outcome_contract import extract_from_bytes

        plan = self.vault / "plan.md"
        contract = {"schema_version": 1, "desired_outcome": "Ship cleanup.",
                    "success_evidence": [{"evidence_id": "cleanup", "observable": "Checkout is gone."}],
                    "non_goals": ["Delete branches."]}
        plan.write_text("# Plan\n```json\n" + json.dumps(contract) + "\n```\n")
        snapshot = bind_approved_plan_snapshot({"vault_root": self.vault, "plan_file": plan})
        self.meta.update(version=4, plan_file=str(plan),
                         plan_snapshot_file=str(snapshot["_approved_plan_file"]),
                         approved_plan_sha256=snapshot["_approved_plan_sha256"],
                         outcome_contract_sha256=extract_from_bytes(plan.read_bytes()).sha256)
        self.summary.update(schema_version=2, outcome_disposition="achieved",
                            outcome_evidence_ids=["cleanup"], residual_gap_pointers=[])
        self.write(".task-meta.json", self.meta)
        self.write(".task-summary.json", self.summary)
        self.reap()
        self.assertFalse(self.worktree.exists())

    def test_shared_plan_task_is_removed(self):
        self.meta["reap_policy"]["mode"] = "shared"
        self.write(".task-meta.json", self.meta)
        self.result["plan_close_status"] = "retained"
        self.reap()
        self.assertFalse(self.worktree.exists())

    def test_plan_close_conflict_retains_checkout(self):
        self.result["plan_close_status"] = "conflict"
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertTrue(self.worktree.exists())

    def test_locked_checkout_is_retained(self):
        self.git("worktree", "lock", str(self.worktree))
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertTrue(self.worktree.exists())

    def test_staged_changes_are_retained(self):
        (self.worktree / "code.py").write_text("value = 2\n")
        subprocess.check_call(["git", "-C", str(self.worktree), "add", "code.py"])
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")

    def test_index_hidden_changes_are_retained(self):
        subprocess.check_call(["git", "-C", str(self.worktree), "update-index", "--assume-unchanged", "code.py"])
        (self.worktree / "code.py").write_text("value = 2\n")
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertEqual((self.worktree / "code.py").read_text(), "value = 2\n")

    def test_live_binding_is_retained(self):
        self.write(".task-session-binding.json", {"status": "active"})
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")

    def test_coordinator_cwd_is_retained(self):
        previous = Path.cwd()
        try:
            os.chdir(self.worktree)
            result = self.reap()
        finally:
            os.chdir(previous)
        self.assertEqual(result["worktree_cleanup"]["status"], "retained")

    def test_missing_completion_proof_is_retained(self):
        def incomplete(*args):
            result = self.finalize(*args)
            (self.worktree / ".task-reap-complete.json").unlink()
            return result
        with patch.object(runner, "_finalize_reap", side_effect=incomplete):
            self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertTrue(self.worktree.exists())

    def test_receipt_survives_crash_after_git_removal(self):
        import task_worktree_cleanup as cleanup
        real_write = cleanup._write
        def fail_completed(path, receipt):
            if receipt["status"] == "removed":
                raise OSError("simulated crash")
            real_write(path, receipt)
        with patch.object(cleanup, "_write", side_effect=fail_completed):
            with self.assertRaises(OSError):
                self.reap()
        self.assertFalse(self.worktree.exists())
        effects = list(self.effects)
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "removed")
        self.assertEqual(self.effects, effects)

    def test_git_remove_failure_preserves_all_metadata_and_retries(self):
        from harness.git_ops import GitError
        real_run = GitAdapter._run
        def fail_remove(adapter, args, **kwargs):
            if "remove" in args:
                raise GitError("simulated busy worktree")
            return real_run(adapter, args, **kwargs)
        with patch.object(GitAdapter, "_run", fail_remove):
            self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertTrue((self.worktree / ".task-meta.json").exists())
        effects = list(self.effects)
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "removed")
        self.assertEqual(self.effects, effects)

    def test_unique_committed_work_is_kept_on_branch(self):
        (self.worktree / "code.py").write_text("value = 3\n")
        subprocess.check_call(["git", "-C", str(self.worktree), "commit", "-am", "completed work"], stdout=subprocess.DEVNULL)
        head = self.git("rev-parse", "task/test")
        self.reap()
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.git("rev-parse", "task/test"), head)
        self.assertEqual(self.git("show", "task/test:code.py"), "value = 3")

    def test_detached_checkout_is_retained(self):
        subprocess.check_call(["git", "-C", str(self.worktree), "checkout", "--detach"], stderr=subprocess.DEVNULL)
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")

    def test_runtime_symlink_is_retained(self):
        outside = self.vault / "private.txt"
        outside.write_text("keep me")
        (self.worktree / ".task-origin-session").symlink_to(outside)
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertEqual(outside.read_text(), "keep me")

    def test_clean_paused_cherry_pick_sequence_is_retained(self):
        self.git("checkout", "-b", "sequence")
        (self.repo / "code.py").write_text("value = 2\n")
        self.git("commit", "-am", "first")
        first = self.git("rev-parse", "HEAD")
        (self.repo / "extra.py").write_text("extra = True\n")
        self.git("add", "extra.py")
        self.git("commit", "-m", "second")
        second = self.git("rev-parse", "HEAD")
        def worker(*args, check=True):
            return subprocess.run(["git", "-C", str(self.worktree), *args],
                                  text=True, capture_output=True, check=check)
        (self.worktree / "code.py").write_text("value = 3\n")
        worker("commit", "-am", "conflicting work")
        self.assertNotEqual(worker("cherry-pick", first, second, check=False).returncode, 0)
        (self.worktree / "code.py").write_text("value = 4\n")
        worker("add", "code.py")
        worker("commit", "--no-edit")
        gitdir = Path(worker("rev-parse", "--absolute-git-dir").stdout.strip())
        self.assertFalse((gitdir / "CHERRY_PICK_HEAD").exists())
        todo = gitdir / "sequencer/todo"
        expected = todo.read_bytes()
        self.assertEqual(worker("diff", "HEAD").stdout, "")
        self.assertEqual(self.reap()["worktree_cleanup"]["status"], "retained")
        self.assertEqual(todo.read_bytes(), expected)
        self.assertTrue(self.worktree.exists())

    def test_cli_replays_after_checkout_is_removed(self):
        self.reap()
        output = subprocess.check_output([
            sys.executable, str(ROOT / "scripts/reap-runner.py"),
            "--vault-root", str(self.vault), "--worktree", str(self.worktree),
            "--current-session", self.session], text=True)
        replay = json.loads(output)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["worktree_cleanup"]["status"], "removed")


if __name__ == "__main__":
    unittest.main()
