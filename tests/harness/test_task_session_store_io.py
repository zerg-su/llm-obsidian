#!/usr/bin/env python3
"""Real-filesystem containment, permission, and identity matrix for session IO."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_session_store_io as store_io  # noqa: E402
from task_session_contracts import TaskSessionError  # noqa: E402


def expect_error(label: str, expected: str, call: object) -> None:
    try:
        call()  # type: ignore[operator]
    except TaskSessionError as exc:
        assert expected in str(exc), (label, str(exc))
    else:
        raise AssertionError(f"{label}: unsafe operation was accepted")


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], text=True, capture_output=True, check=True
    )


def read_and_directory_matrix(root: Path) -> None:
    state = root / "read" / "state.json"
    state.parent.mkdir()
    state.write_text('{"state":"ready"}\n', encoding="utf-8")
    assert store_io.read_object(state) == {"state": "ready"}
    assert store_io.read_object(root / "missing.json", required=False) == {}
    expect_error(
        "required state",
        "missing state file",
        lambda: store_io.read_object(root / "missing.json"),
    )
    state.write_text("not-json\n", encoding="utf-8")
    expect_error("invalid JSON", "invalid state file", lambda: store_io.read_object(state))
    state.write_text("[]\n", encoding="utf-8")
    expect_error("non-object JSON", "must contain an object", lambda: store_io.read_object(state))
    with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
        expect_error("read permission error", "invalid state file", lambda: store_io.read_object(state))

    created = store_io.ensure_owner_only_dir(root / "created" / "nested")
    assert created.is_dir() and stat.S_IMODE(created.stat().st_mode) == 0o700
    permissive = root / "permissive"
    permissive.mkdir(mode=0o755)
    permissive.chmod(0o755)
    assert store_io.ensure_owner_only_dir(permissive) == permissive
    assert stat.S_IMODE(permissive.stat().st_mode) == 0o700
    regular = root / "regular"
    regular.write_text("file", encoding="utf-8")
    expect_error("directory regular file", "not an owned directory", lambda: store_io.ensure_owner_only_dir(regular))
    link = root / "directory-link"
    link.symlink_to(permissive, target_is_directory=True)
    expect_error("directory symlink", "not an owned directory", lambda: store_io.ensure_owner_only_dir(link))
    with mock.patch.object(store_io.os, "getuid", return_value=os.getuid() + 1):
        expect_error("directory foreign owner", "not owner-only", lambda: store_io.ensure_owner_only_dir(permissive))
    permissive.chmod(0o755)
    with mock.patch.object(Path, "chmod", autospec=True, return_value=None):
        expect_error("chmod verification", "not owner-only", lambda: store_io.ensure_owner_only_dir(permissive))
    permissive.chmod(0o700)


def atomic_and_lock_matrix(root: Path) -> None:
    state = root / "atomic" / "state.json"
    store_io.atomic_write(state, {"revision": 1, "unicode": "да"})
    assert json.loads(state.read_text(encoding="utf-8")) == {"revision": 1, "unicode": "да"}
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
    assert not list(state.parent.glob(f".{state.name}.tmp.*"))
    store_io.atomic_write(state, {"revision": 2})
    assert store_io.read_object(state)["revision"] == 2

    shared = root / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    file_only = shared / "state.json"
    store_io.atomic_write_file_only(file_only, {"safe": True})
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert stat.S_IMODE(file_only.stat().st_mode) == 0o600
    missing = root / "absent" / "state.json"
    expect_error("file-only missing parent", "parent is missing", lambda: store_io.atomic_write_file_only(missing, {}))
    parent_file = root / "parent-file"
    parent_file.write_text("x", encoding="utf-8")
    expect_error("file-only non-directory", "parent is missing", lambda: store_io.atomic_write_file_only(parent_file / "state", {}))
    link = root / "shared-link"
    link.symlink_to(shared, target_is_directory=True)
    expect_error("file-only symlink parent", "parent is missing", lambda: store_io.atomic_write_file_only(link / "state", {}))
    with mock.patch.object(store_io.os, "getuid", return_value=os.getuid() + 1):
        expect_error("file-only foreign parent", "not owned", lambda: store_io.atomic_write_file_only(shared / "other", {}))

    lock = root / "locks" / "session.lock"
    with store_io.file_lock(lock):
        assert lock.is_file()
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    with store_io.file_lock(lock):
        assert lock.read_text(encoding="utf-8") == ""


def containment_matrix(root: Path) -> None:
    vault = root / "vault"
    vault.mkdir()
    inside = vault / "llm-obsidian-fetch-owned"
    inside.mkdir()
    # macOS exposes /var as an alias for /private/var. The scratch path is
    # canonicalized internally; an equally valid non-canonical vault root must
    # still contain it and fail closed.
    assert not store_io.remove_owned_research_scratch(inside, vault)
    assert inside.is_dir()

    wrong_name = root / "ordinary-scratch"
    wrong_name.mkdir()
    assert not store_io.remove_owned_research_scratch(wrong_name, vault)
    regular = root / "llm-obsidian-fetch-file"
    regular.write_text("x", encoding="utf-8")
    assert not store_io.remove_owned_research_scratch(regular, vault)
    missing = root / "llm-obsidian-fetch-missing"
    assert not store_io.remove_owned_research_scratch(missing, vault)
    target = root / "llm-obsidian-fetch-target"
    target.mkdir()
    link = root / "llm-obsidian-fetch-link"
    link.symlink_to(target, target_is_directory=True)
    assert not store_io.remove_owned_research_scratch(link, vault)
    assert target.is_dir()
    foreign = root / "llm-obsidian-synth-foreign"
    foreign.mkdir()
    with mock.patch.object(store_io.os, "getuid", return_value=os.getuid() + 1):
        assert not store_io.remove_owned_research_scratch(foreign, vault)
    for prefix in ("llm-obsidian-fetch-", "llm-obsidian-synth-"):
        owned = root / f"{prefix}{uuid.uuid4()}"
        owned.mkdir()
        (owned / "payload").write_text("data", encoding="utf-8")
        assert store_io.remove_owned_research_scratch(owned, vault)
        assert not owned.exists()


def git_and_project_identity_matrix(root: Path) -> None:
    nonrepo = root / "not-a-repo"
    nonrepo.mkdir()
    expect_error("non-git worktree", "require a Git worktree", lambda: store_io.git_common_dir(nonrepo))

    repo = root / "repo"
    run_git("init", "-q", str(repo))
    common = store_io.git_common_dir(repo)
    assert common == (repo / ".git").resolve()
    with mock.patch.object(store_io.os, "getuid", return_value=os.getuid() + 1):
        expect_error("foreign git common dir", "not owned", lambda: store_io.git_common_dir(repo))

    run_git("-C", str(repo), "-c", "user.name=Coverage", "-c", "user.email=coverage@example.invalid", "commit", "--allow-empty", "-qm", "seed")
    linked = root / "linked"
    run_git("-C", str(repo), "worktree", "add", "-q", "--detach", str(linked))
    assert store_io.git_common_dir(linked) == common

    expect_error("missing project id", "marker is missing", lambda: store_io.project_id_for(repo, create=False))
    project_id = store_io.project_id_for(repo, create=True)
    assert uuid.UUID(project_id) and store_io.project_id_for(linked, create=False) == project_id
    marker = common / "llm-obsidian" / "project-id"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    marker.write_text("not-a-uuid\n", encoding="utf-8")
    expect_error("invalid project id", "project_id", lambda: store_io.project_id_for(repo, create=False))
    marker.write_text(project_id + "\n", encoding="utf-8")
    marker.chmod(0o644)
    expect_error("permissive project marker", "not owner-only", lambda: store_io.project_id_for(repo, create=False))
    marker.chmod(0o600)
    with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
        expect_error("project marker read error", "cannot read project identity", lambda: store_io.project_id_for(repo, create=False))


def lane_matrix() -> None:
    project = str(uuid.uuid4())
    task = str(uuid.uuid4())
    baseline = store_io.lane_id_for(project, task, "normal", "codex", "gpt-5.6-sol")
    assert len(baseline) == 32 and baseline == store_io.lane_id_for(project, task, "normal", "codex", "gpt-5.6-sol")
    assert baseline != store_io.lane_id_for(project, task, "review", "codex", "gpt-5.6-sol")
    assert baseline != store_io.lane_id_for(project, task, "normal", "claude", "gpt-5.6-sol")
    assert baseline != store_io.lane_id_for(project, task, "normal", "codex", "gpt-5.6-terra")
    for label, expected, args in (
        ("project UUID", "project_id", ("bad", task, "normal", "codex", "model")),
        ("task UUID", "task_id", (project, "bad", "normal", "codex", "model")),
        ("domain", "domain must be", (project, task, "other", "codex", "model")),
        ("runtime", "runtime must be", (project, task, "normal", "other", "model")),
        ("blank model", "model is invalid", (project, task, "normal", "codex", " ")),
        ("long model", "model is invalid", (project, task, "normal", "codex", "x" * 201)),
        ("nul model", "model is invalid", (project, task, "normal", "codex", "bad\0model")),
    ):
        expect_error(label, expected, lambda args=args: store_io.lane_id_for(*args))


with tempfile.TemporaryDirectory(prefix="task-session-store-io.") as raw:
    temp_root = Path(raw)
    read_and_directory_matrix(temp_root)
    atomic_and_lock_matrix(temp_root)
    containment_matrix(temp_root)
    git_and_project_identity_matrix(temp_root)
lane_matrix()
print("task session store IO matrix passed")
