"""Deterministic two-part transport for one exact review fix delta."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from review_resolution import (
    MAX_FIX_DELTA_BYTES as MAX_DELTA_PART_BYTES,
    MAX_FIX_DELTA_TOTAL_BYTES as MAX_DELTA_TOTAL_BYTES,
)
from task_review_delta_chunks import (
    MAX_CANONICAL_BYTES,
    ChunkedDeltaError,
    build_chunked_packet,
    parse_chunk_manifest,
    validate_chunked_packet,
)

GIT_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DeltaPacketError(ValueError):
    pass


@dataclass(frozen=True)
class DeltaPart:
    name: str
    content: bytes


@dataclass(frozen=True)
class DeltaPacket:
    manifest: bytes
    parts: tuple[DeltaPart, ...]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _split_position(delta: bytes) -> int:
    lower = len(delta) - MAX_DELTA_PART_BYTES
    upper = MAX_DELTA_PART_BYTES
    file_boundaries = [
        match.start()
        for match in re.finditer(br"(?m)^diff --git ", delta)
        if lower <= match.start() <= upper and match.start() > 0
    ]
    if file_boundaries:
        return max(file_boundaries)
    line_boundaries = [
        index + 1
        for index, value in enumerate(delta)
        if value == 0x0A and lower <= index + 1 <= upper
    ]
    if not line_boundaries:
        raise DeltaPacketError(
            "review delta has no safe bounded line boundary"
        )
    return max(line_boundaries)


def build_delta_packet(
    delta: bytes,
    reviewed_head: str,
    resolved_head: str,
    *,
    review_identity_sha256: str = "",
) -> DeltaPacket:
    if (
        not GIT_HEAD.fullmatch(reviewed_head)
        or not GIT_HEAD.fullmatch(resolved_head)
        or reviewed_head == resolved_head
    ):
        raise DeltaPacketError("review delta packet HEAD identity is invalid")
    if not delta or len(delta) > MAX_CANONICAL_BYTES:
        raise DeltaPacketError(
            "review delta must be non-empty and at most 1048576 bytes"
        )
    if len(delta) > MAX_DELTA_TOTAL_BYTES:
        try:
            packet = build_chunked_packet(
                delta,
                reviewed_head,
                resolved_head,
                review_identity_sha256,
            )
        except ChunkedDeltaError as exc:
            raise DeltaPacketError(str(exc)) from exc
        return DeltaPacket(
            packet.manifest,
            tuple(DeltaPart(name, content) for name, content in packet.chunks),
        )
    try:
        delta.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeltaPacketError("review delta must be UTF-8") from exc
    if len(delta) <= MAX_DELTA_PART_BYTES:
        chunks = (delta,)
    else:
        split = _split_position(delta)
        chunks = (delta[:split], delta[split:])
    if (
        not 1 <= len(chunks) <= 2
        or any(
            not chunk or len(chunk) > MAX_DELTA_PART_BYTES
            for chunk in chunks
        )
    ):
        raise DeltaPacketError("review delta parts exceed the bounded transport")
    parts = tuple(
        DeltaPart(f"fix-delta.part-{index:03d}.patch", content)
        for index, content in enumerate(chunks, start=1)
    )
    manifest = {
        "schema_version": 1,
        "reviewed_head_sha": reviewed_head,
        "resolved_head_sha": resolved_head,
        "complete_delta_sha256": hashlib.sha256(delta).hexdigest(),
        "complete_delta_bytes": len(delta),
        "part_count": len(parts),
        "parts": [
            {
                "name": part.name,
                "sha256": hashlib.sha256(part.content).hexdigest(),
                "bytes": len(part.content),
            }
            for part in parts
        ],
    }
    return DeltaPacket(_canonical(manifest), parts)


def validate_delta_packet(
    manifest_bytes: bytes,
    parts: Mapping[str, bytes],
    *,
    expected_reviewed_head: str,
    expected_resolved_head: str,
    expected_review_identity_sha256: str = "",
) -> bytes:
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeltaPacketError("review delta manifest is invalid") from exc
    if isinstance(manifest, dict) and manifest.get("schema_version") == 2:
        try:
            return validate_chunked_packet(
                manifest_bytes,
                parts,
                expected_reviewed_head=expected_reviewed_head,
                expected_resolved_head=expected_resolved_head,
                expected_review_identity_sha256=(
                    expected_review_identity_sha256
                ),
            )
        except ChunkedDeltaError as exc:
            raise DeltaPacketError(str(exc)) from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "reviewed_head_sha",
        "resolved_head_sha",
        "complete_delta_sha256",
        "complete_delta_bytes",
        "part_count",
        "parts",
    }:
        raise DeltaPacketError("review delta manifest fields are invalid")
    raw_parts = manifest.get("parts")
    part_count = manifest.get("part_count")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("reviewed_head_sha") != expected_reviewed_head
        or manifest.get("resolved_head_sha") != expected_resolved_head
        or not isinstance(part_count, int)
        or isinstance(part_count, bool)
        or part_count not in {1, 2}
        or not isinstance(raw_parts, list)
        or len(raw_parts) != part_count
    ):
        raise DeltaPacketError("review delta manifest identity is invalid")
    expected_names = [
        f"fix-delta.part-{index:03d}.patch"
        for index in range(1, part_count + 1)
    ]
    if set(parts) != set(expected_names):
        raise DeltaPacketError("review delta part set is invalid")
    ordered: list[bytes] = []
    for index, raw_part in enumerate(raw_parts):
        if not isinstance(raw_part, dict) or set(raw_part) != {
            "name",
            "sha256",
            "bytes",
        }:
            raise DeltaPacketError("review delta part manifest is invalid")
        name = raw_part.get("name")
        size = raw_part.get("bytes")
        digest = raw_part.get("sha256")
        if (
            name != expected_names[index]
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= MAX_DELTA_PART_BYTES
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
        ):
            raise DeltaPacketError("review delta part identity is invalid")
        content = parts[name]
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise DeltaPacketError("review delta part content changed")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeltaPacketError("review delta part is not UTF-8") from exc
        ordered.append(content)
    delta = b"".join(ordered)
    total = manifest.get("complete_delta_bytes")
    complete_sha256 = manifest.get("complete_delta_sha256")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not 0 < total <= MAX_DELTA_TOTAL_BYTES
        or total != len(delta)
        or not isinstance(complete_sha256, str)
        or not SHA256.fullmatch(complete_sha256)
        or hashlib.sha256(delta).hexdigest() != complete_sha256
    ):
        raise DeltaPacketError("complete review delta identity changed")
    return delta


def validate_materialized_delta_packet(
    context_manifest: Path,
    *,
    expected_reviewed_head: str,
    expected_resolved_head: str,
    expected_review_identity_sha256: str = "",
) -> bytes:
    if not context_manifest.is_file() or context_manifest.is_symlink():
        raise DeltaPacketError("review context manifest is unavailable")
    try:
        context = json.loads(context_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeltaPacketError("review context manifest is invalid") from exc
    inputs = context.get("inputs") if isinstance(context, dict) else None
    if not isinstance(inputs, list):
        raise DeltaPacketError("review context inputs are invalid")
    expected_source = (
        f"git:diff:{expected_reviewed_head}..{expected_resolved_head}"
    )
    fix_rows: dict[str, tuple[int, dict[str, object]]] = {}
    for index, raw in enumerate(inputs):
        if not isinstance(raw, dict):
            raise DeltaPacketError("review context input is invalid")
        name = raw.get("name")
        if raw.get("role") == "fix" and isinstance(name, str):
            if name in fix_rows:
                raise DeltaPacketError("review context delta inputs repeat")
            fix_rows[name] = (index, raw)
    manifest_name = "fix-delta.manifest.json"
    if manifest_name not in fix_rows:
        raise DeltaPacketError("review context delta input set is invalid")

    def inline(name: str, expected: str) -> bytes:
        index, row = fix_rows[name]
        path = context_manifest.parent / f"{index:03d}-fix-{name}"
        if (
            str(row.get("source") or "") != expected
            or row.get("storage") != "inline"
            or not path.is_file()
            or path.is_symlink()
        ):
            raise DeltaPacketError("review context delta pointer is invalid")
        content = path.read_bytes()
        if (
            row.get("bytes") != len(content)
            or row.get("sha256") != hashlib.sha256(content).hexdigest()
        ):
            raise DeltaPacketError("review context delta input changed")
        return content

    manifest_bytes = inline(manifest_name, expected_source + "#manifest")
    try:
        raw_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeltaPacketError("review delta manifest is invalid") from exc
    schema_version = (
        raw_manifest.get("schema_version")
        if isinstance(raw_manifest, dict)
        else None
    )
    if schema_version == 1:
        part_names = sorted(
            name for name in fix_rows if name != manifest_name
        )
        if part_names not in (
            ["fix-delta.part-001.patch"],
            ["fix-delta.part-001.patch", "fix-delta.part-002.patch"],
        ):
            raise DeltaPacketError("review context delta input set is invalid")
        return validate_delta_packet(
            manifest_bytes,
            {
                name: inline(
                    name,
                    expected_source
                    + f"#part={part_names.index(name) + 1}/{len(part_names)}",
                )
                for name in part_names
            },
            expected_reviewed_head=expected_reviewed_head,
            expected_resolved_head=expected_resolved_head,
        )
    if schema_version != 2:
        raise DeltaPacketError("review delta manifest version is unsupported")
    try:
        chunk_manifest = parse_chunk_manifest(
            manifest_bytes,
            expected_reviewed_head=expected_reviewed_head,
            expected_resolved_head=expected_resolved_head,
            expected_review_identity_sha256=(
                expected_review_identity_sha256
            ),
        )
    except ChunkedDeltaError as exc:
        raise DeltaPacketError(str(exc)) from exc
    chunk_names = [item.name for item in chunk_manifest.chunks]
    if set(fix_rows) != {manifest_name, *chunk_names}:
        raise DeltaPacketError("review context delta input set is invalid")
    resolved_context = context_manifest.expanduser().resolve(strict=True)
    if (
        resolved_context.name != "manifest.json"
        or resolved_context.parent.parent.name != "packets"
    ):
        raise DeltaPacketError("review context manifest location is invalid")
    runtime_root = resolved_context.parents[2]
    raw_pointer_root = runtime_root / "pointers/fix-delta-v2"
    if any(
        path.is_symlink()
        for path in (runtime_root / "pointers", raw_pointer_root)
    ):
        raise DeltaPacketError("review context delta pointer is invalid")
    pointer_root = raw_pointer_root.resolve()
    if runtime_root not in pointer_root.parents:
        raise DeltaPacketError("review context delta pointer is invalid")

    def pointed(name: str) -> bytes:
        _, row = fix_rows[name]
        source_text = str(row.get("source") or "")
        source = Path(source_text)
        if (
            row.get("storage") != "pointer"
            or not source.is_absolute()
            or ".." in source.parts
            or source.name != name
            or not pointer_root.is_dir()
        ):
            raise DeltaPacketError("review context delta pointer is invalid")
        try:
            lexical_relative = source.relative_to(raw_pointer_root)
            lexical_cursor = raw_pointer_root
            for component in lexical_relative.parts:
                lexical_cursor = lexical_cursor / component
                if lexical_cursor.is_symlink():
                    raise DeltaPacketError(
                        "review context delta pointer is invalid"
                    )
            resolved = source.resolve(strict=True)
            relative = resolved.relative_to(pointer_root)
        except (OSError, ValueError) as exc:
            raise DeltaPacketError(
                "review context delta pointer is invalid"
            ) from exc
        cursor = pointer_root
        for component in relative.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise DeltaPacketError(
                    "review context delta pointer is invalid"
                )
        if not resolved.is_file():
            raise DeltaPacketError("review context delta pointer is invalid")
        content = resolved.read_bytes()
        if (
            row.get("bytes") != len(content)
            or row.get("sha256") != hashlib.sha256(content).hexdigest()
        ):
            raise DeltaPacketError("review context delta input changed")
        return content

    return validate_delta_packet(
        manifest_bytes,
        {name: pointed(name) for name in chunk_names},
        expected_reviewed_head=expected_reviewed_head,
        expected_resolved_head=expected_resolved_head,
        expected_review_identity_sha256=expected_review_identity_sha256,
    )
