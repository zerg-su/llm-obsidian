#!/usr/bin/env python3
"""Identity-bound content-addressed review delta chunk regressions."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.context import ContextBuilder, ContextInput  # noqa: E402
from review_resolution import (  # noqa: E402
    FindingResolution,
    ResolutionError,
    ReviewResolution,
    build_resolution_evidence,
)
from task_review_delta_packet import (  # noqa: E402
    DeltaPacketError,
    build_delta_packet,
    validate_delta_packet,
    validate_materialized_delta_packet,
)
from task_review_context import _delta_inputs  # noqa: E402
from task_review_shared import TaskReviewError  # noqa: E402


REVIEWED = "a" * 40
RESOLVED = "b" * 40
IDENTITY = "c" * 64
FOREIGN_IDENTITY = "d" * 64
MODE = "git-diff-binary-no-ext-diff-v1"
CHUNK_BYTES = 131_072
MAX_TOTAL_BYTES = 1_048_576


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def rejected(label: str, action: object) -> None:
    try:
        action()
    except DeltaPacketError:
        check(label, True)
    else:
        check(label, False)


def context_rejected(label: str, action: object) -> None:
    try:
        action()
    except TaskReviewError:
        check(label, True)
    else:
        check(label, False)


def resolution_rejected(label: str, action: object) -> None:
    try:
        action()
    except ResolutionError:
        check(label, True)
    else:
        check(label, False)


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def text_delta(size: int = 784_216) -> bytes:
    header = b"diff --git a/evidence.log b/evidence.log\n"
    body = b"+immutable verification output\n"
    repeats = (size - len(header) + len(body) - 1) // len(body)
    return (header + body * repeats)[:size]


def binary_delta(size: int = 270_000) -> bytes:
    header = b"diff --git a/blob.bin b/blob.bin\nGIT binary patch\n"
    payload = bytes(range(256))
    repeats = (size - len(header) + len(payload) - 1) // len(payload)
    return (header + payload * repeats)[:size]


def packet_parts(packet: object) -> dict[str, bytes]:
    return {part.name: part.content for part in packet.parts}


def validate(packet: object, parts: dict[str, bytes] | None = None) -> bytes:
    return validate_delta_packet(
        packet.manifest,
        packet_parts(packet) if parts is None else parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    )


delta = text_delta()
packet = build_delta_packet(
    delta,
    REVIEWED,
    RESOLVED,
    review_identity_sha256=IDENTITY,
)
replay = build_delta_packet(
    delta,
    REVIEWED,
    RESOLVED,
    review_identity_sha256=IDENTITY,
)
manifest = json.loads(packet.manifest)
parts = packet_parts(packet)
check(
    "large canonical text delta uses deterministic v2 chunks",
    manifest["schema_version"] == 2
    and manifest["representation"] == "content-addressed-chunks"
    and packet == replay
    and 1 < len(packet.parts) <= 8
    and all(0 < len(part.content) <= CHUNK_BYTES for part in packet.parts)
    and b"".join(part.content for part in packet.parts) == delta,
)
check(
    "v2 manifest binds heads identity canonical mode length and digest",
    manifest["reviewed_head_sha"] == REVIEWED
    and manifest["resolved_head_sha"] == RESOLVED
    and manifest["review_identity_sha256"] == IDENTITY
    and manifest["canonical_diff"]
    == {
        "mode": MODE,
        "bytes": len(delta),
        "sha256": hashlib.sha256(delta).hexdigest(),
    }
    and manifest["chunk_count"] == len(packet.parts)
    and [row["index"] for row in manifest["chunks"]]
    == list(range(1, len(packet.parts) + 1))
    and all(
        row["name"]
        == f"fix-delta.chunk-{row['index']:03d}-{row['sha256']}.bin"
        for row in manifest["chunks"]
    )
    and validate(packet) == delta,
)

binary = binary_delta()
binary_packet = build_delta_packet(
    binary,
    REVIEWED,
    RESOLVED,
    review_identity_sha256=IDENTITY,
)
check(
    "v2 chunks round-trip opaque binary canonical bytes",
    validate(binary_packet) == binary,
)

legacy_delta = b"diff --git a/small b/small\n+small\n"
legacy_plain = build_delta_packet(legacy_delta, REVIEWED, RESOLVED)
legacy_bound = build_delta_packet(
    legacy_delta,
    REVIEWED,
    RESOLVED,
    review_identity_sha256=IDENTITY,
)
check(
    "legacy inline packet remains byte-identical and compatible",
    legacy_plain == legacy_bound
    and json.loads(legacy_plain.manifest)["schema_version"] == 1
    and validate_delta_packet(
        legacy_plain.manifest,
        packet_parts(legacy_plain),
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
    )
    == legacy_delta,
)

resolution = ReviewResolution(
    "chunked-resolution",
    REVIEWED,
    RESOLVED,
    (
        FindingResolution(
            "F-large-delta",
            "applied",
            "The exact identity-bound chunked delta is available.",
        ),
    ),
    IDENTITY,
)
evidence = build_resolution_evidence(
    resolution,
    axis="openai-engineering",
    fix_delta=delta,
)
check(
    "resolution evidence accepts and hashes the complete bounded canonical delta",
    evidence.fix_delta_sha256 == hashlib.sha256(delta).hexdigest(),
)
unbound_resolution = ReviewResolution(
    "unbound-chunked-resolution",
    REVIEWED,
    RESOLVED,
    resolution.resolutions,
)
resolution_rejected(
    "large resolution evidence requires the existing review identity",
    lambda: build_resolution_evidence(
        unbound_resolution,
        axis="openai-engineering",
        fix_delta=delta,
    ),
)

tampered_parts = dict(parts)
first_name = packet.parts[0].name
tampered_parts[first_name] += b"x"
rejected("tampered chunk fails closed", lambda: validate(packet, tampered_parts))

missing_parts = dict(parts)
missing_parts.pop(first_name)
rejected("missing chunk fails closed", lambda: validate(packet, missing_parts))

extra_parts = dict(parts)
extra_parts["fix-delta.chunk-999-" + "0" * 64 + ".bin"] = b"x"
rejected("extra chunk fails closed", lambda: validate(packet, extra_parts))

reordered = json.loads(packet.manifest)
reordered["chunks"] = list(reversed(reordered["chunks"]))
rejected(
    "reordered chunk manifest fails closed",
    lambda: validate_delta_packet(
        canonical(reordered),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

duplicate = json.loads(packet.manifest)
duplicate["chunks"][1] = dict(duplicate["chunks"][0])
rejected(
    "duplicate chunk manifest entry fails closed",
    lambda: validate_delta_packet(
        canonical(duplicate),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

traversal = json.loads(packet.manifest)
traversal["chunks"][0]["name"] = "../fix-delta.chunk-001.bin"
rejected(
    "chunk name traversal fails closed",
    lambda: validate_delta_packet(
        canonical(traversal),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

for label, field, value in (
    ("reviewed HEAD drift fails closed", "reviewed_head_sha", "e" * 40),
    ("resolved HEAD drift fails closed", "resolved_head_sha", "e" * 40),
    ("review identity drift fails closed", "review_identity_sha256", FOREIGN_IDENTITY),
):
    changed = json.loads(packet.manifest)
    changed[field] = value
    rejected(
        label,
        lambda changed=changed: validate_delta_packet(
            canonical(changed),
            parts,
            expected_reviewed_head=REVIEWED,
            expected_resolved_head=RESOLVED,
            expected_review_identity_sha256=IDENTITY,
        ),
    )

digest_drift = json.loads(packet.manifest)
digest_drift["canonical_diff"]["sha256"] = "f" * 64
rejected(
    "canonical digest drift fails closed",
    lambda: validate_delta_packet(
        canonical(digest_drift),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

mode_drift = json.loads(packet.manifest)
mode_drift["canonical_diff"]["mode"] = "git-diff-stat-v1"
rejected(
    "canonical diff mode drift fails closed",
    lambda: validate_delta_packet(
        canonical(mode_drift),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

unsupported = json.loads(packet.manifest)
unsupported["schema_version"] = 3
rejected(
    "unsupported chunk manifest version fails closed",
    lambda: validate_delta_packet(
        canonical(unsupported),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

oversized_chunk = json.loads(packet.manifest)
oversized_bytes = packet.parts[0].content + b"x"
oversized_sha = hashlib.sha256(oversized_bytes).hexdigest()
oversized_name = f"fix-delta.chunk-001-{oversized_sha}.bin"
oversized_chunk["chunks"][0].update(
    {"name": oversized_name, "bytes": len(oversized_bytes), "sha256": oversized_sha}
)
oversized_parts = dict(parts)
oversized_parts.pop(first_name)
oversized_parts[oversized_name] = oversized_bytes
rejected(
    "oversized chunk fails closed",
    lambda: validate_delta_packet(
        canonical(oversized_chunk),
        oversized_parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)

rejected(
    "oversized canonical total fails closed",
    lambda: build_delta_packet(
        binary_delta(MAX_TOTAL_BYTES + 1),
        REVIEWED,
        RESOLVED,
        review_identity_sha256=IDENTITY,
    ),
)
too_many = json.loads(packet.manifest)
too_many["chunk_count"] = 9
rejected(
    "chunk count above eight fails closed",
    lambda: validate_delta_packet(
        canonical(too_many),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
        expected_review_identity_sha256=IDENTITY,
    ),
)
rejected(
    "large delta without review identity fails closed",
    lambda: build_delta_packet(delta, REVIEWED, RESOLVED),
)


def materialized_packet(
    raw_root: Path,
    *,
    extra_inputs: tuple[ContextInput, ...] = (),
    traversal_source: bool = False,
) -> Path:
    delta_source = f"git:diff:{REVIEWED}..{RESOLVED}"
    inputs = list(
        _delta_inputs(
            packet,
            delta_source=delta_source,
            runtime_root=raw_root,
        )
    )
    if traversal_source:
        first = packet.parts[0]
        outside = raw_root / "outside" / first.name
        outside.parent.mkdir()
        outside.write_bytes(first.content)
        for index, item in enumerate(inputs):
            if item.name == first.name:
                inputs[index] = ContextInput.pointer(
                    item.name,
                    str(outside),
                    byte_count=item.byte_count,
                    content_sha256=item.content_sha256,
                    role="fix",
                )
                break
    inputs.extend(extra_inputs)
    built = ContextBuilder(raw_root / "packets").build(
        "chunked-materialization",
        tuple(inputs),
        metadata={"head_sha": RESOLVED},
    )
    return raw_root / "packets" / built.packet_id / "manifest.json"


with tempfile.TemporaryDirectory(prefix="review-chunk-materialized.") as raw:
    materialized_root = Path(raw)
    context_manifest = materialized_packet(materialized_root)
    check(
        "pointer materialization reconstructs the exact canonical delta",
        validate_materialized_delta_packet(
            context_manifest,
            expected_reviewed_head=REVIEWED,
            expected_resolved_head=RESOLVED,
            expected_review_identity_sha256=IDENTITY,
        )
        == delta,
    )
    context = json.loads(context_manifest.read_text(encoding="utf-8"))
    chunk_row = next(
        row
        for row in context["inputs"]
        if str(row["name"]).startswith("fix-delta.chunk-")
    )
    chunk_row["sha256"] = "0" * 64
    context_manifest.write_bytes(canonical(context))
    rejected(
        "materialized chunk hash drift fails closed",
        lambda: validate_materialized_delta_packet(
            context_manifest,
            expected_reviewed_head=REVIEWED,
            expected_resolved_head=RESOLVED,
            expected_review_identity_sha256=IDENTITY,
        ),
    )

with tempfile.TemporaryDirectory(prefix="review-chunk-mixed.") as raw:
    mixed_root = Path(raw)
    mixed_manifest = materialized_packet(
        mixed_root,
        extra_inputs=(
            ContextInput(
                "fix-delta.part-001.patch",
                f"git:diff:{REVIEWED}..{RESOLVED}#part=1/1",
                b"mixed",
                role="fix",
            ),
        ),
    )
    rejected(
        "mixed inline and chunked payloads fail closed",
        lambda: validate_materialized_delta_packet(
            mixed_manifest,
            expected_reviewed_head=REVIEWED,
            expected_resolved_head=RESOLVED,
            expected_review_identity_sha256=IDENTITY,
        ),
    )

with tempfile.TemporaryDirectory(prefix="review-chunk-traversal.") as raw:
    traversal_root = Path(raw)
    traversal_manifest = materialized_packet(
        traversal_root,
        traversal_source=True,
    )
    rejected(
        "chunk pointer traversal outside the owner root fails closed",
        lambda: validate_materialized_delta_packet(
            traversal_manifest,
            expected_reviewed_head=REVIEWED,
            expected_resolved_head=RESOLVED,
            expected_review_identity_sha256=IDENTITY,
        ),
    )

with tempfile.TemporaryDirectory(prefix="review-chunk-symlink.") as raw:
    symlink_root = Path(raw)
    symlink_manifest = materialized_packet(symlink_root)
    symlink_context = json.loads(
        symlink_manifest.read_text(encoding="utf-8")
    )
    symlink_row = next(
        row
        for row in symlink_context["inputs"]
        if str(row["name"]).startswith("fix-delta.chunk-")
    )
    symlink_path = Path(symlink_row["source"])
    real_path = symlink_path.with_name("real-" + symlink_path.name)
    symlink_path.rename(real_path)
    symlink_path.symlink_to(real_path)
    rejected(
        "symlinked chunk pointer fails closed",
        lambda: validate_materialized_delta_packet(
            symlink_manifest,
            expected_reviewed_head=REVIEWED,
            expected_resolved_head=RESOLVED,
            expected_review_identity_sha256=IDENTITY,
        ),
    )

with tempfile.TemporaryDirectory(prefix="review-chunk-root-symlink.") as raw:
    root_symlink = Path(raw)
    outside_root = root_symlink / "outside-root"
    outside_root.mkdir()
    (root_symlink / "pointers").symlink_to(
        outside_root,
        target_is_directory=True,
    )
    context_rejected(
        "writer rejects a symlinked owner pointer root",
        lambda: _delta_inputs(
            packet,
            delta_source=f"git:diff:{REVIEWED}..{RESOLVED}",
            runtime_root=root_symlink,
        ),
    )

print("\nAll chunked review delta tests passed.")
