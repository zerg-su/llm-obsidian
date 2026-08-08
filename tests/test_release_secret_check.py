#!/usr/bin/env python3
"""Exact-candidate secret-check behavior against a tagged release baseline."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release-secret-check.py"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def commit(root: Path, message: str) -> None:
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", message)


def check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--base", "nearest-tag"],
        check=False,
        capture_output=True,
        text=True,
    )


with tempfile.TemporaryDirectory(prefix="release-secret-check.") as raw:
    repo = Path(raw) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Release Secret Test")
    git(repo, "config", "user.email", "release-secret@example.invalid")
    (repo / "legacy.txt").write_text(
        "token=" + "abcdef123456" + "\n", encoding="utf-8"
    )
    commit(repo, "baseline fixture")
    git(repo, "tag", "v1.0.0")

    (repo / "safe.txt").write_text("safe candidate\n", encoding="utf-8")
    commit(repo, "safe candidate")
    safe = check(repo)
    assert safe.returncode == 0, safe.stderr
    safe_value = json.loads(safe.stdout)
    assert safe_value["status"] == "passed" and safe_value["added_lines"] == 1

    (repo / "safe_assignments.py").write_text(
        "token = raw.strip()\n"
        'token = edge.replace(" ", "-")\n',
        encoding="utf-8",
    )
    commit(repo, "safe credential-named Python references")
    safe_assignments = check(repo)
    assert safe_assignments.returncode == 0, safe_assignments.stdout
    assert json.loads(safe_assignments.stdout)["status"] == "passed"

    (repo / "wrapped_leak.py").write_text(
        "tok" + 'en = load("' + "AKIA" + 'IOSFODNN7EXAMPLE")\n',
        encoding="utf-8",
    )
    commit(repo, "candidate wrapped credential literal")
    wrapped_leak = check(repo)
    assert wrapped_leak.returncode == 1, wrapped_leak.stdout
    assert "aws-access-key" in json.loads(wrapped_leak.stdout)["credential_kinds"]
    git(repo, "rm", "wrapped_leak.py")
    git(repo, "commit", "-q", "-m", "remove wrapped credential literal")

    (repo / "literal_leak.py").write_text(
        'token = "newcandidatevalue123456"\n', encoding="utf-8"
    )
    commit(repo, "candidate Python credential literal")
    python_literal = check(repo)
    assert python_literal.returncode == 1
    assert json.loads(python_literal.stdout)["credential_kinds"] == [
        "credential-assignment"
    ]
    git(repo, "rm", "literal_leak.py")
    git(repo, "commit", "-q", "-m", "remove Python credential literal")

    (repo / "leak.txt").write_text(
        "token=" + "newcandidatevalue123456" + "\n", encoding="utf-8"
    )
    commit(repo, "candidate credential")
    leaked = check(repo)
    assert leaked.returncode == 1
    leaked_value = json.loads(leaked.stdout)
    assert leaked_value["status"] == "blocked"
    assert leaked_value["credential_kinds"] == ["credential-assignment"]

    git(repo, "rm", "-q", "leak.txt")
    git(repo, "commit", "-q", "-m", "remove candidate credential")
    (repo / ".env").write_text("PLACEHOLDER=1\n", encoding="utf-8")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-q", "-m", "candidate secret container")
    container = check(repo)
    assert container.returncode == 1
    container_value = json.loads(container.stdout)
    assert container_value["status"] == "blocked"
    assert container_value["secret_paths"] == [".env"]

print("Release exact-candidate secret check tests passed.")
