#!/usr/bin/env python3
"""Exercise the mutation runner through its CLI against disposable Git projects."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "skills/mutation-testing/scripts/run_mutations.py"


class MutationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mutation-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "project"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("calc.py", "def total(a, b):\n    return a + b\n")
        self.write("verify.py", "from calc import total\nassert total(4, 2) == 6, 'sum contract'\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
        self.plan = {
            "schema_version": 1, "command": [sys.executable, "verify.py"],
            "timeout_seconds": 3,
            "mutations": [{"id": "subtract", "path": "calc.py", "before": "a + b",
                           "after": "a - b", "reason": "wrong arithmetic",
                           "expected_failure": "sum contract"}],
        }

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args])

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def invoke(self, *args):
        plan = self.home / "plan.json"
        plan.write_text(json.dumps(self.plan))
        output = self.home / "evidence"
        argv = [sys.executable, str(RUNNER), "--source", str(self.repo),
                "--plan", str(plan), "--output", str(output), *args]
        return argv, output

    def run_plan(self, *args):
        argv, output = self.invoke(*args)
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        receipt = output / "report.json"
        return result, json.loads(receipt.read_text()) if receipt.exists() else None

    def test_dirty_state_killed_survivor_and_no_original_writes(self):
        self.write("calc.py", "def total(a, b):\n    return a + b + 0\n")
        self.git("add", "calc.py")
        self.write("calc.py", "def total(a, b):\n    return a + b + 0 # working change\n")
        self.write("untracked.txt", "preserve me")
        self.plan["mutations"].append(dict(self.plan["mutations"][0], id="equivalent", before="+ 0", after="- 0"))
        original = (self.repo / "calc.py").read_bytes()
        index = (self.repo / ".git/index").read_bytes()
        status = self.git("status", "--porcelain=v1")
        result, report = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([x["status"] for x in report["runs"]], ["passed", "failed", "passed", "passed"])
        self.assertTrue(report["source_unchanged"])
        self.assertIn("sum contract", (self.home / "evidence/subtract.log").read_text())
        self.assertEqual(original, (self.repo / "calc.py").read_bytes())
        self.assertEqual(index, (self.repo / ".git/index").read_bytes())
        self.assertEqual(status, self.git("status", "--porcelain=v1"))
        self.assertFalse((self.repo / "__pycache__").exists())
        self.assertFalse((self.home / "evidence/work").exists())

    def test_red_baseline_stops_before_mutation(self):
        self.write("verify.py", "assert False, 'preexisting failure'\n")
        result, report = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["status"], "baseline-failed")
        self.assertEqual(len(report["runs"]), 1)
        self.assertTrue(report["source_unchanged"])

    def test_syntax_error_is_not_automatically_a_kill(self):
        self.plan["mutations"][0]["after"] = "a +"
        result, report = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["runs"][1]["status"], "failed")
        self.assertNotIn("killed", json.dumps(report))
        self.assertIn("SyntaxError", (self.home / "evidence/subtract.log").read_text())

    def test_timeout_and_final_green(self):
        self.plan["timeout_seconds"] = 0.15
        self.plan["mutations"][0]["after"] = "__import__('time').sleep(5)"
        result, report = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["runs"][1]["status"], "timeout")
        self.assertEqual(report["runs"][-1]["status"], "passed")

    def test_each_verifier_has_fresh_temporary_files(self):
        self.write("verify.py", "import os\nfrom pathlib import Path\np = Path(os.environ['TMPDIR']) / 'once'\nassert not p.exists(), 'fresh temporary files'\np.touch()\n")
        result, report = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([x["status"] for x in report["runs"]], ["passed", "passed", "passed"])

    def test_deleted_file_untracked_tests_and_ignored_dependency(self):
        self.git("rm", "verify.py")
        self.write("verify_new.py", "from calc import total\nfrom dep.value import N\nassert total(4,2) == N\n")
        self.write(".gitignore", "dep/\n")
        self.write("dep/value.py", "N = 6\n")
        self.plan["command"] = [sys.executable, "verify_new.py"]
        result, report = self.run_plan("--include", "dep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["source_unchanged"])
        self.assertFalse((self.repo / "verify.py").exists())

    def test_shell_config_consumer_without_python_test_framework(self):
        self.write("settings.sh", "LIMIT=3\n")
        self.write("admit.sh", '. ./settings.sh\nif [ "$1" -lt "$LIMIT" ]; then echo admitted; else echo rejected; fi\n')
        self.write("check.sh", 'test "$(sh admit.sh 3)" = rejected || { echo "capacity contract"; exit 1; }\n')
        self.plan["command"] = ["sh", "check.sh"]
        self.plan["mutations"] = [dict(self.plan["mutations"][0], path="settings.sh", before="LIMIT=3", after="LIMIT=4")]
        result, report = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["runs"][1]["status"], "failed")
        self.assertIn("capacity contract", (self.home / "evidence/subtract.log").read_text())

    def test_external_symlink_rejected_without_touching_target(self):
        target = self.home / "outside"
        target.write_text("keep")
        (self.repo / "escape").symlink_to(target)
        result, _ = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(target.read_text(), "keep")

    def test_internal_symlink_is_private(self):
        (self.repo / "alias.py").symlink_to("calc.py")
        result, report = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["source_unchanged"])

    def test_mutation_path_escape_rejected(self):
        self.plan["mutations"][0]["path"] = "../outside"
        result, _ = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("path", result.stderr)

    def test_explicit_unrelated_exclusion_is_recorded(self):
        target = self.home / "outside"
        target.write_text("keep")
        (self.repo / "unrelated").symlink_to(target)
        result, report = self.run_plan("--exclude", "unrelated")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.home / "evidence/source-manifest.json").read_text())
        self.assertEqual(manifest["excludes"], ["unrelated"])
        self.assertNotIn("unrelated", manifest["files"])

    def test_original_drift_is_detected_never_restored(self):
        # Deliberate out-of-copy effect inside this test fixture exercises the guard.
        self.write("verify.py", "from pathlib import Path\nPath(" + repr(str(self.repo / "unrelated")) + ").write_text('concurrent edit')\n")
        result, report = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["status"], "source-drift")
        self.assertFalse(report["source_unchanged"])
        self.assertEqual((self.repo / "unrelated").read_text(), "concurrent edit")

    def test_ambiguous_replacement_rejected_before_verifier(self):
        self.plan["mutations"][0]["before"] = "a"
        result, _ = self.run_plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "evidence/baseline.log").exists())

    def test_output_must_be_new_and_outside_source(self):
        argv, _ = self.invoke()
        argv[-1] = str(self.repo / "evidence")
        result = subprocess.run(argv, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "evidence").exists())

    def test_sigterm_preserves_original_and_records_interruption(self):
        self.write("verify.py", "from pathlib import Path\nPath('started').touch()\nimport time\ntime.sleep(30)\n")
        self.plan["timeout_seconds"] = 60
        original = self.git("status", "--porcelain=v1")
        argv, output = self.invoke()
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (output / "work/started").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((output / "work/started").exists())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["status"], "interrupted")
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(original, self.git("status", "--porcelain=v1"))
        self.assertFalse((output / "work").exists())


if __name__ == "__main__":
    unittest.main()
