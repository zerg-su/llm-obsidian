"""Deterministic, bounded ContextPacket builder."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .contracts import ContextPacketManifest, ContractError


CONTEXT_ROLES = frozenset(
    {
        "task",
        "plan",
        "instructions",
        "reference",
        "base",
        "head",
        "diff",
        "finding",
        "resolution",
        "fix",
        "route",
        "permissions",
        "verification",
    }
)
_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}\Z")
_RAW_SOURCES = frozenset(
    {"conversation", "raw-conversation", "chat-transcript", "session-transcript"}
)


@dataclass(frozen=True)
class ContextInput:
    name: str
    source: str
    content: bytes | None
    role: str = "reference"
    pointer_bytes: int = 0
    pointer_sha256: str = ""

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ContractError("ContextInput name must be a safe file token")
        if self.role not in CONTEXT_ROLES:
            raise ContractError(f"unknown ContextInput role: {self.role}")
        if not self.source.strip() or "\0" in self.source:
            raise ContractError("ContextInput source must be non-empty")
        if self.source.strip().lower() in _RAW_SOURCES:
            raise ContractError("raw conversation is excluded from ContextPacket")
        if self.content is None:
            if self.pointer_bytes < 0 or not re.fullmatch(
                r"[0-9a-f]{64}", self.pointer_sha256
            ):
                raise ContractError("ContextInput pointer requires bytes and sha256")
        elif self.pointer_bytes or self.pointer_sha256:
            raise ContractError("inline ContextInput cannot also be a pointer")

    @classmethod
    def pointer(
        cls,
        name: str,
        source: str,
        *,
        byte_count: int,
        content_sha256: str,
        role: str = "reference",
    ) -> "ContextInput":
        return cls(
            name,
            source,
            None,
            role=role,
            pointer_bytes=byte_count,
            pointer_sha256=content_sha256,
        )

    @property
    def byte_count(self) -> int:
        return len(self.content) if self.content is not None else self.pointer_bytes

    @property
    def content_sha256(self) -> str:
        return (
            hashlib.sha256(self.content).hexdigest()
            if self.content is not None
            else self.pointer_sha256
        )


class ContextBuilder:
    def __init__(
        self,
        output_dir: Path | str,
        *,
        max_bytes: int = 262_144,
        max_inline_bytes: int = 65_536,
    ):
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.max_bytes = max_bytes
        self.max_inline_bytes = max_inline_bytes
        if max_bytes <= 0 or max_inline_bytes < 0:
            raise ContractError("ContextPacket byte budgets must be positive")

    def build(
        self,
        operation_id: str,
        inputs: tuple[ContextInput, ...],
        *,
        metadata: Mapping[str, str],
    ) -> ContextPacketManifest:
        if len({item.name for item in inputs}) != len(inputs):
            raise ContractError("context input names must be unique")
        raw_metadata = {key.lower().replace("-", "_") for key in metadata}
        if raw_metadata & {"conversation", "raw_conversation", "transcript"}:
            raise ContractError("raw conversation metadata is excluded")
        for item in inputs:
            if item.content is not None and len(item.content) > self.max_inline_bytes:
                raise ContractError(
                    f"ContextInput {item.name} exceeds inline budget; use a pointer"
                )
        ordered = tuple(
            sorted(inputs, key=lambda item: (item.role, item.name, item.source))
        )
        packet = {
            "schema_version": 1,
            "operation_id": operation_id,
            "metadata": dict(sorted(metadata.items())),
            "inputs": [
                {
                    "name": item.name,
                    "role": item.role,
                    "source": item.source,
                    "storage": "inline" if item.content is not None else "pointer",
                    "bytes": item.byte_count,
                    "sha256": item.content_sha256,
                }
                for item in ordered
            ],
        }
        handoff = ["# Harness handoff", "", f"Operation: `{operation_id}`", ""]
        handoff.extend(
            f"- {item.role}/{item.name}: `{item.source}` "
            f"({item.byte_count} bytes, "
            f"{'inline' if item.content is not None else 'pointer'})"
            for item in ordered
        )
        handoff_bytes = ("\n".join(handoff) + "\n").encode()
        manifest_bytes = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
        total = (
            len(handoff_bytes)
            + len(manifest_bytes)
            + sum(len(item.content) for item in ordered if item.content is not None)
        )
        if total > self.max_bytes:
            raise ContractError("ContextPacket exceeds byte budget; use content pointers")
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        packet_id = digest[:24]
        target = self.output_dir / packet_id
        target.mkdir(parents=True, exist_ok=True)
        self._atomic(target / "manifest.json", manifest_bytes)
        self._atomic(target / "handoff.md", handoff_bytes)
        for index, item in enumerate(ordered):
            if item.content is not None:
                self._atomic(
                    target / f"{index:03d}-{item.role}-{item.name}.bin",
                    item.content,
                )
        files = tuple(
            path.relative_to(self.output_dir).as_posix()
            for path in sorted(target.iterdir())
        )
        return ContextPacketManifest(packet_id, operation_id, files, digest, total)

    @staticmethod
    def _atomic(path: Path, content: bytes) -> None:
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
