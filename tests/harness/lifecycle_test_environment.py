#!/usr/bin/env python3
"""Bounded simulator command environment, preserving the caller's tool choices.

Apple's /usr/bin/git launches the selected developer Git indirectly. The
crash matrix runs thousands of real Git observations, making that repeated
launcher cost significant within its unchanged 60-second budget. Resolve it
once and expose only Git, not the developer directory's Python or other tools.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterator


def developer_git() -> Path | None:
    try:
        result = subprocess.run(
            ["/usr/bin/xcrun", "--find", "git"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


@contextmanager
def command_environment(inherited: dict[str, str]) -> Iterator[dict[str, str]]:
    env = dict(inherited)
    if sys.platform != "darwin" or shutil.which("git", path=env.get("PATH")) != "/usr/bin/git":
        yield env
        return
    native = developer_git()
    if (
        native is None
        or not native.is_absolute()
        or native == Path("/usr/bin/git")
        or not native.is_file()
        or not os.access(native, os.X_OK)
    ):
        yield env
        return
    with tempfile.TemporaryDirectory(prefix="lifecycle-native-git.") as raw:
        Path(raw, "git").symlink_to(native)
        env["PATH"] = raw + os.pathsep + env.get("PATH", os.defpath)
        yield env


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: lifecycle_test_environment.py <command> [args...]")
    with command_environment(dict(os.environ)) as env:
        result = subprocess.run(sys.argv[1:], env=env)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
