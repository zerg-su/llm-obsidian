"""Atomic local diagnostics for failed immutable verification attempts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence


class VerificationDiagnosticError(RuntimeError):
    """A diagnostic attempt identity or storage boundary is invalid."""


def canonical_attempt_id(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise VerificationDiagnosticError(
            "verification attempt identity is invalid"
        ) from exc
    if value != normalized:
        raise VerificationDiagnosticError(
            "verification attempt identity must be canonical"
        )
    return normalized


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FailureDiagnosticStore:
    """Publish one owner-only diagnostic bundle per exact failed attempt."""

    def __init__(
        self,
        root: Path,
        *,
        attempt_id: str,
        subject_root: Path,
        publication_dir: Path,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.attempt_id = canonical_attempt_id(attempt_id)
        subject_root = subject_root.expanduser().resolve()
        publication_dir = publication_dir.expanduser().resolve()
        if (
            self.root == subject_root
            or subject_root in self.root.parents
            or self.root == publication_dir
            or self.root in publication_dir.parents
            or publication_dir in self.root.parents
            or self.root.is_symlink()
        ):
            raise VerificationDiagnosticError(
                "verification diagnostics must stay outside subject and publication"
            )
        if not self.root.exists():
            if not self.root.parent.is_dir() or self.root.parent.is_symlink():
                raise VerificationDiagnosticError(
                    "verification diagnostic parent is unavailable"
                )
            self.root.mkdir(mode=0o700)
            _fsync_directory(self.root.parent)
        if (
            not self.root.is_dir()
            or self.root.stat().st_mode & 0o077
        ):
            raise VerificationDiagnosticError(
                "verification diagnostic root must be owner-only"
            )
        self.target = self.root / self.attempt_id
        if self.target.exists() or self.target.is_symlink():
            raise VerificationDiagnosticError(
                "verification attempt identity already has diagnostics"
            )

    def publish(
        self,
        *,
        subject_head_sha: str,
        subject_tree_sha: str,
        profile: str,
        profile_sha256: str,
        attempt_started_at: str,
        command_index: int,
        command_count: int,
        command_id: str,
        command_argv: Sequence[str],
        command_text: str,
        command_started_at: str,
        command_finished_at: str,
        exit_code: int,
        failure_kind: str,
        output_path: Path,
        runner: str,
        runner_sha256: str,
    ) -> Path:
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{self.attempt_id}.", dir=self.root)
        )
        temporary.chmod(0o700)
        try:
            log_path = temporary / "command.log"
            with output_path.open("rb") as source, log_path.open("wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            log_path.chmod(0o600)
            raw = log_path.read_bytes()
            receipt: Mapping[str, object] = {
                "schema_version": 1,
                "type": "diagnostic-only",
                "evidence_disposition": "not-verification-evidence",
                "status": "failed",
                "attempt_id": self.attempt_id,
                "attempt_started_at": attempt_started_at,
                "subject_head_sha": subject_head_sha,
                "subject_tree_sha": subject_tree_sha,
                "profile": profile,
                "profile_sha256": profile_sha256,
                "command_index": command_index,
                "command_count": command_count,
                "command_id": command_id,
                "command_argv": list(command_argv),
                "command_text": command_text,
                "command_started_at": command_started_at,
                "command_finished_at": command_finished_at,
                "exit_code": exit_code,
                "failure_kind": failure_kind,
                "stdout_stderr_pointer": "command.log",
                "stdout_stderr_sha256": hashlib.sha256(raw).hexdigest(),
                "stdout_stderr_bytes": len(raw),
                "runner": runner,
                "runner_sha256": runner_sha256,
            }
            receipt_path = temporary / "diagnostic-receipt.json"
            with receipt_path.open("w", encoding="utf-8") as handle:
                json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            receipt_path.chmod(0o600)
            _fsync_directory(temporary)
            lock_path = self.root / ".diagnostics.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.chmod(lock_path, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                if self.target.exists() or self.target.is_symlink():
                    raise VerificationDiagnosticError(
                        "verification attempt diagnostics already exist"
                    )
                os.replace(temporary, self.target)
                _fsync_directory(self.root)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            return self.target / "diagnostic-receipt.json"
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
