#!/usr/bin/env python3
"""Durable journal, roll-forward recovery, and atomic commit for vault-write."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from vault_write_contract import (
    ConflictError,
    PayloadError,
    safe_repo_path,
    sha256_text,
)


AtomicWriter = Callable[[Path, str], None]


def atomic_write(path: Path, text: str) -> None:
    """Replace one file atomically after flushing its same-directory temp file."""

    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class TransactionJournal:
    """Own one journal's durable write, commit, and roll-forward lifecycle."""

    def __init__(
        self, repo_root: Path, journal_file: Path, atomic_writer: AtomicWriter
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.journal_file = journal_file.resolve()
        self.atomic_writer = atomic_writer

    def write(
        self,
        writes: list[tuple[Path, str]],
        deletes: list[tuple[Path, str]] | None = None,
    ) -> None:
        deletes = deletes or []
        payload = {
            "version": 2 if deletes else 1,
            "entries": [
                {
                    **({"op": "write"} if deletes else {}),
                    "path": str(path.relative_to(self.repo_root)),
                    "sha256": sha256_text(content),
                    "content": content,
                }
                for path, content in writes
            ]
            + [
                {
                    "op": "delete",
                    "path": str(path.relative_to(self.repo_root)),
                    "sha256": expected,
                }
                for path, expected in deletes
            ],
        }
        self.atomic_writer(
            self.journal_file,
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def commit(
        self,
        writes: list[tuple[Path, str]],
        deletes: list[tuple[Path, str]] | None = None,
    ) -> None:
        """Journal first, then apply each effect in recoverable roll-forward order."""

        deletes = deletes or []
        self.write(writes, deletes)
        for path, text in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.atomic_writer(path, text)
        for path, expected in deletes:
            actual = (
                sha256_text(path.read_text(encoding="utf-8"))
                if path.is_file()
                else None
            )
            if actual != expected:
                raise ConflictError(
                    "delete conflict during transaction: "
                    f"{path.relative_to(self.repo_root)} is {actual}, expected {expected}"
                )
            path.unlink()
        self.journal_file.unlink()

    def recover(self) -> int:
        if not self.journal_file.exists():
            return 0
        try:
            journal = json.loads(self.journal_file.read_text(encoding="utf-8"))
            if not isinstance(journal, dict):
                raise PayloadError("journal root must be an object")
            entries = journal.get("entries")
            version = journal.get("version")
            if version not in {1, 2} or not isinstance(entries, list):
                raise PayloadError("unsupported or corrupt journal")
            recovered = 0
            for entry in entries:
                recovered += self._recover_entry(version, entry)
            self.journal_file.unlink()
            return recovered
        except (OSError, json.JSONDecodeError, PayloadError) as exc:
            raise OSError(f"cannot recover transaction journal: {exc}") from exc

    def _recover_entry(self, version: int, entry: object) -> int:
        if not isinstance(entry, dict):
            raise PayloadError("corrupt journal entry")
        rel = str(entry.get("path") or "")
        if not (rel.startswith("wiki/") or rel == ".raw/.manifest.json"):
            raise PayloadError(f"journal path is outside mutation scope: {rel!r}")
        path = safe_repo_path(
            self.repo_root,
            rel,
            prefix="wiki/" if rel.startswith("wiki/") else None,
        )
        op = "write" if version == 1 else entry.get("op")
        if op not in {"write", "delete"}:
            raise PayloadError("corrupt journal operation")
        content = entry.get("content")
        expected = entry.get("sha256")
        if op == "write":
            if not isinstance(content, str) or expected != sha256_text(content):
                raise PayloadError("journal content checksum mismatch")
            actual = (
                sha256_text(path.read_text(encoding="utf-8"))
                if path.is_file()
                else None
            )
            if actual == expected:
                return 0
            path.parent.mkdir(parents=True, exist_ok=True)
            self.atomic_writer(path, content)
            return 1

        if not isinstance(expected, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected
        ):
            raise PayloadError("journal delete checksum is invalid")
        if not path.exists():
            return 0
        actual = (
            sha256_text(path.read_text(encoding="utf-8")) if path.is_file() else None
        )
        if actual != expected:
            raise PayloadError(f"journal delete conflict for {rel}")
        path.unlink()
        return 1
