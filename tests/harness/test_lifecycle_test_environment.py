#!/usr/bin/env python3
"""The simulator selects native Apple Git without changing other tools."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_test_environment import command_environment


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


def output(name: str, env: dict[str, str]) -> str:
    return subprocess.check_output([name], env=env, text=True).strip()


with tempfile.TemporaryDirectory(prefix="lifecycle-test-env.") as raw:
    root = Path(raw)
    selected = root / "selected"
    developer = root / "developer tools"
    selected.mkdir()
    developer.mkdir()
    executable(selected / "git", "echo selected-git")
    executable(selected / "python3", "echo selected-python")
    executable(developer / "git", "echo native-git")
    executable(developer / "python3", "echo unwanted-python")
    inherited = {**os.environ, "PATH": str(selected), "KEEP_ME": "yes"}
    before = dict(inherited)
    # The discovery port represents xcrun, while real child processes prove
    # which executables and environment the caller receives.
    with patch("lifecycle_test_environment.sys.platform", "darwin"), patch(
        "lifecycle_test_environment.shutil.which", return_value="/usr/bin/git"
    ), patch(
        "lifecycle_test_environment.developer_git", return_value=developer / "git"
    ):
        with command_environment(inherited) as env:
            temporary_bin = Path(env["PATH"].split(os.pathsep)[0])
            assert output("git", env) == "native-git"
            assert output("python3", env) == "selected-python"
            assert env["KEEP_ME"] == "yes"
        assert not temporary_bin.exists()
    assert inherited == before
    print("OK native Git selected; Python/environment preserved; temporary link removed")

    for platform, selected_git in (
        ("linux", "/usr/bin/git"),
        ("darwin", str(selected / "git")),
        ("darwin", None),
    ):
        with patch("lifecycle_test_environment.sys.platform", platform), patch(
            "lifecycle_test_environment.shutil.which", return_value=selected_git
        ), patch("lifecycle_test_environment.developer_git") as discovery:
            with command_environment(inherited) as env:
                assert env == inherited
                assert output("git", env) == "selected-git"
            discovery.assert_not_called()
    print("OK other platforms and explicitly selected Git remain unchanged")

    for native in (None, Path("/usr/bin/git"), root / "missing"):
        with patch("lifecycle_test_environment.sys.platform", "darwin"), patch(
            "lifecycle_test_environment.shutil.which", return_value="/usr/bin/git"
        ), patch("lifecycle_test_environment.developer_git", return_value=native):
            with command_environment(inherited) as env:
                assert env == inherited
                assert output("git", env) == "selected-git"
    print("OK unavailable developer Git preserves the original environment")

launcher = Path(__file__).with_name("lifecycle_test_environment.py")
for exit_code in (0, 7):
    result = subprocess.run(
        [
            sys.executable, str(launcher), sys.executable, "-c",
            "import sys; print(sys.argv[1]); raise SystemExit(int(sys.argv[2]))",
            "argument with spaces", str(exit_code),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == exit_code, result
    assert result.stdout.strip() == "argument with spaces", result
print("OK command argv and failing exit status propagate through the CLI")

print("All lifecycle test-environment tests passed.")
