"""Versioned content-addressed chunks for large canonical review deltas."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping


SCHEMA_VERSION = 2
REPRESENTATION = "content-addressed-chunks"
CANONICAL_DIFF_MODE = "git-diff-binary-no-ext-diff-v1"
LEGACY_TOTAL_BYTES = 131_072
MAX_CHUNK_BYTES = 131_072
MAX_CHUNK_COUNT = 8
MAX_CANONICAL_BYTES = 1_048_576

GIT_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CHUNK_NAME = re.compile(
    r"fix-delta\.chunk-([0-9]{3})-([0-9a-f]{64})\.bin\Z"
)


class ChunkedDeltaError(ValueError):
    pass


@dataclass(frozen=True)
class ChunkDescriptor:
    index: int
    name: str
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class ChunkManifest:
    reviewed_head_sha: str
    resolved_head_sha: str
    review_identity_sha256: str
    canonical_bytes: int
    canonical_sha256: str
    chunks: tuple[ChunkDescriptor, ...]


@dataclass(frozen=True)
class ChunkedPacket:
    manifest: bytes
    chunks: tuple[tuple[str, bytes], ...]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _identity(
    reviewed_head: str,
    resolved_head: str,
    review_identity_sha256: str,
) -> None:
    if (
        not GIT_HEAD.fullmatch(reviewed_head)
        or not GIT_HEAD.fullmatch(resolved_head)
        or reviewed_head == resolved_head
    ):
        raise ChunkedDeltaError("chunked review delta HEAD identity is invalid")
    if not SHA256.fullmatch(review_identity_sha256):
        raise ChunkedDeltaError("chunked review delta identity is invalid")


def build_chunked_packet(
    delta: bytes,
    reviewed_head: str,
    resolved_head: str,
    review_identity_sha256: str,
) -> ChunkedPacket:
    """Build the unique fixed-boundary v2 representation for one large delta."""

    _identity(reviewed_head, resolved_head, review_identity_sha256)
    if not LEGACY_TOTAL_BYTES < len(delta) <= MAX_CANONICAL_BYTES:
        raise ChunkedDeltaError(
            "chunked review delta must exceed 131072 and be at most 1048576 bytes"
        )
    chunks = tuple(
        delta[offset : offset + MAX_CHUNK_BYTES]
        for offset in range(0, len(delta), MAX_CHUNK_BYTES)
    )
    if not 2 <= len(chunks) <= MAX_CHUNK_COUNT:
        raise ChunkedDeltaError("chunked review delta count is invalid")
    rows = []
    named_chunks = []
    for index, content in enumerate(chunks, start=1):
        digest = hashlib.sha256(content).hexdigest()
        name = f"fix-delta.chunk-{index:03d}-{digest}.bin"
        rows.append(
            {
                "index": index,
                "name": name,
                "bytes": len(content),
                "sha256": digest,
            }
        )
        named_chunks.append((name, content))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "representation": REPRESENTATION,
        "reviewed_head_sha": reviewed_head,
        "resolved_head_sha": resolved_head,
        "review_identity_sha256": review_identity_sha256,
        "canonical_diff": {
            "mode": CANONICAL_DIFF_MODE,
            "bytes": len(delta),
            "sha256": hashlib.sha256(delta).hexdigest(),
        },
        "chunk_count": len(rows),
        "chunks": rows,
    }
    return ChunkedPacket(_canonical(manifest), tuple(named_chunks))


def parse_chunk_manifest(
    manifest_bytes: bytes,
    *,
    expected_reviewed_head: str,
    expected_resolved_head: str,
    expected_review_identity_sha256: str,
) -> ChunkManifest:
    """Validate every manifest binding before any chunk is trusted."""

    _identity(
        expected_reviewed_head,
        expected_resolved_head,
        expected_review_identity_sha256,
    )
    try:
        raw = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChunkedDeltaError("chunked review delta manifest is invalid") from exc
    if not isinstance(raw, dict) or manifest_bytes != _canonical(raw):
        raise ChunkedDeltaError("chunked review delta manifest is not canonical")
    if set(raw) != {
        "schema_version",
        "representation",
        "reviewed_head_sha",
        "resolved_head_sha",
        "review_identity_sha256",
        "canonical_diff",
        "chunk_count",
        "chunks",
    }:
        raise ChunkedDeltaError("chunked review delta manifest fields are invalid")
    canonical = raw.get("canonical_diff")
    rows = raw.get("chunks")
    chunk_count = raw.get("chunk_count")
    if (
        raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("representation") != REPRESENTATION
        or raw.get("reviewed_head_sha") != expected_reviewed_head
        or raw.get("resolved_head_sha") != expected_resolved_head
        or raw.get("review_identity_sha256")
        != expected_review_identity_sha256
        or not isinstance(canonical, dict)
        or set(canonical) != {"mode", "bytes", "sha256"}
        or canonical.get("mode") != CANONICAL_DIFF_MODE
        or not isinstance(rows, list)
        or not isinstance(chunk_count, int)
        or isinstance(chunk_count, bool)
        or not 2 <= chunk_count <= MAX_CHUNK_COUNT
        or len(rows) != chunk_count
    ):
        raise ChunkedDeltaError("chunked review delta manifest identity is invalid")
    total = canonical.get("bytes")
    digest = canonical.get("sha256")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not LEGACY_TOTAL_BYTES < total <= MAX_CANONICAL_BYTES
        or not isinstance(digest, str)
        or not SHA256.fullmatch(digest)
    ):
        raise ChunkedDeltaError("chunked review delta canonical binding is invalid")
    descriptors = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or set(row) != {
            "index",
            "name",
            "bytes",
            "sha256",
        }:
            raise ChunkedDeltaError("chunked review delta row is invalid")
        index = row.get("index")
        name = row.get("name")
        size = row.get("bytes")
        chunk_sha256 = row.get("sha256")
        match = CHUNK_NAME.fullmatch(name) if isinstance(name, str) else None
        expected_size = (
            MAX_CHUNK_BYTES
            if position < chunk_count
            else total - MAX_CHUNK_BYTES * (chunk_count - 1)
        )
        if (
            index != position
            or not isinstance(index, int)
            or isinstance(index, bool)
            or match is None
            or int(match.group(1)) != position
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size != expected_size
            or not 0 < size <= MAX_CHUNK_BYTES
            or not isinstance(chunk_sha256, str)
            or not SHA256.fullmatch(chunk_sha256)
            or match.group(2) != chunk_sha256
        ):
            raise ChunkedDeltaError("chunked review delta ordering is invalid")
        descriptors.append(
            ChunkDescriptor(position, name, size, chunk_sha256)
        )
    if sum(item.byte_count for item in descriptors) != total:
        raise ChunkedDeltaError("chunked review delta length binding is invalid")
    return ChunkManifest(
        expected_reviewed_head,
        expected_resolved_head,
        expected_review_identity_sha256,
        total,
        digest,
        tuple(descriptors),
    )


def validate_chunked_packet(
    manifest_bytes: bytes,
    chunks: Mapping[str, bytes],
    *,
    expected_reviewed_head: str,
    expected_resolved_head: str,
    expected_review_identity_sha256: str,
) -> bytes:
    """Reconstruct the exact canonical bytes from one complete chunk set."""

    manifest = parse_chunk_manifest(
        manifest_bytes,
        expected_reviewed_head=expected_reviewed_head,
        expected_resolved_head=expected_resolved_head,
        expected_review_identity_sha256=expected_review_identity_sha256,
    )
    expected_names = [item.name for item in manifest.chunks]
    if set(chunks) != set(expected_names) or len(chunks) != len(expected_names):
        raise ChunkedDeltaError("chunked review delta set is invalid")
    ordered = []
    for item in manifest.chunks:
        content = chunks[item.name]
        if (
            len(content) != item.byte_count
            or hashlib.sha256(content).hexdigest() != item.sha256
        ):
            raise ChunkedDeltaError("chunked review delta content changed")
        ordered.append(content)
    delta = b"".join(ordered)
    if (
        len(delta) != manifest.canonical_bytes
        or hashlib.sha256(delta).hexdigest() != manifest.canonical_sha256
    ):
        raise ChunkedDeltaError("canonical review delta identity changed")
    return delta
