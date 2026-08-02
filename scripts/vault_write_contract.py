#!/usr/bin/env python3
"""Shared error, digest, and repository-path contract for vault writes."""

from __future__ import annotations

import hashlib
from pathlib import Path


class CapViolation(Exception):
    """A rendered vault-owned surface would violate a hard invariant."""


class PayloadError(ValueError):
    """The requested mutation is structurally invalid."""


class ConflictError(ValueError):
    """Optimistic state no longer matches the caller's expected identity."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_repo_path(
    repo_root: Path, rel: str, *, prefix: str | None = None
) -> Path:
    """Resolve one mutation path while preserving the repository boundary."""

    if not rel or Path(rel).is_absolute():
        raise PayloadError(f"path must be repository-relative (got {rel!r})")
    path = (repo_root / rel).resolve()
    if repo_root.resolve() not in path.parents:
        raise PayloadError(f"path escapes repository: {rel!r}")
    if prefix:
        allowed = (repo_root / prefix.rstrip("/")).resolve()
        if not rel.startswith(prefix) or allowed not in path.parents:
            raise PayloadError(f"path must stay inside {prefix!r}: {rel!r}")
    return path
