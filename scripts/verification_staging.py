"""Physically external staging for atomic immutable receipt publication."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable


class VerificationStagingError(RuntimeError):
    """Unpublished receipt bytes cannot be isolated from the Git subject."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


class ExternalPublicationStaging:
    """Own an external same-filesystem staging root and atomic publication."""

    def __init__(
        self,
        root: Path,
        *,
        subject_root: Path,
        git_roots: Iterable[Path],
        publication_dir: Path,
    ) -> None:
        requested = root.expanduser()
        if requested.is_symlink():
            raise VerificationStagingError(
                "verification staging root cannot be a symlink"
            )
        parent = requested.parent.resolve(strict=True)
        resolved = (parent / requested.name).resolve()
        subject = subject_root.expanduser().resolve(strict=True)
        publication = publication_dir.expanduser().resolve()
        protected = (subject, *(item.expanduser().resolve(strict=True) for item in git_roots))
        if any(_overlaps(resolved, item) for item in protected) or _overlaps(
            resolved, publication
        ):
            raise VerificationStagingError(
                "verification staging must be physically outside subject and Git roots"
            )
        if not resolved.exists():
            resolved.mkdir(mode=0o700)
            _fsync_directory(parent)
        if (
            resolved.is_symlink()
            or not resolved.is_dir()
            or resolved.stat().st_mode & 0o077
        ):
            raise VerificationStagingError(
                "verification staging root must be owner-only"
            )
        physical = resolved.resolve(strict=True)
        if physical != resolved or any(
            _overlaps(physical, item) for item in protected
        ):
            raise VerificationStagingError(
                "verification staging resolved into a protected Git path"
            )
        if physical.stat().st_dev != publication.parent.stat().st_dev:
            raise VerificationStagingError(
                "verification staging and publication must share a filesystem"
            )
        self.root = physical

    def create(self, attempt_id: str) -> Path:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=self.root)
        )
        staging.chmod(0o700)
        return staging

    def publish(self, staging: Path, publication_dir: Path) -> None:
        staging = staging.resolve(strict=True)
        publication_dir = publication_dir.expanduser().resolve()
        if (
            staging.parent != self.root
            or publication_dir.exists()
            or publication_dir.is_symlink()
        ):
            raise VerificationStagingError(
                "verification publication boundary changed"
            )
        os.replace(staging, publication_dir)
        _fsync_directory(publication_dir.parent)

    def cleanup(self, staging: Path) -> None:
        if staging.parent == self.root:
            shutil.rmtree(staging, ignore_errors=True)
