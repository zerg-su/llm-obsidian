import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex-auth-backup.py"


class CodexAuthBackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()
        self.auth = self.home / "auth.json"
        self.auth.write_text(json.dumps({"tokens": {"access_token": "account-a"}}))
        self.auth.chmod(0o600)

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env={**os.environ, "CODEX_HOME": str(self.home)},
            capture_output=True,
            text=True,
        )

    def test_backup_list_restore_preserves_both_accounts(self):
        saved = self.run_script("backup")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        first = next((self.home / "auth-backups").glob("auth-*.json"))
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o700)

        self.auth.write_text(json.dumps({"tokens": {"access_token": "account-b"}}))
        listed = self.run_script("list")
        self.assertEqual(listed.stdout.strip(), first.name)
        restored = self.run_script("restore", first.name)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(json.loads(self.auth.read_text())["tokens"]["access_token"], "account-a")
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o600)
        self.assertEqual(len(list(first.parent.glob("auth-*.json"))), 2)
        self.assertIn("account-b", "".join(path.read_text() for path in first.parent.glob("auth-*.json")))

    def test_restore_rejects_path_traversal(self):
        result = self.run_script("restore", "../auth.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Use a filename from the list command", result.stderr)

    def test_switch_saves_cache_and_clears_only_local_file(self):
        result = self.run_script("switch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.auth.exists())
        self.assertEqual(len(list((self.home / "auth-backups").glob("auth-*.json"))), 1)
        restored = self.run_script("restore", "latest")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(json.loads(self.auth.read_text())["tokens"]["access_token"], "account-a")


if __name__ == "__main__":
    unittest.main()
