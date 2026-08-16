#!/usr/bin/env python3
"""Bounded exact-HEAD delta packet and fail-closed validation regressions."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_review_delta_packet import (  # noqa: E402
    DeltaPacketError,
    build_delta_packet,
    validate_delta_packet,
    validate_materialized_delta_packet,
)
from harness.context import ContextBuilder, ContextInput  # noqa: E402
from harness.contracts import OperationRecord, RuntimeRoute  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewOperationRequest,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from review_resolution import FindingResolution, ReviewResolution  # noqa: E402
from task_review_shared import ResolutionBundle  # noqa: E402


REVIEWED = "a" * 40
RESOLVED = "b" * 40


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


def resolution_packet_ready(
    gate: ReviewGateController,
    run: object,
    context_manifest: Path,
    bundle: ResolutionBundle,
) -> bool:
    try:
        delta = validate_materialized_delta_packet(
            context_manifest,
            expected_reviewed_head=bundle.resolution.reviewed_head_sha,
            expected_resolved_head=bundle.resolution.resolved_head_sha,
            expected_review_identity_sha256=bundle.review_identity_sha256,
        )
        if delta != bundle.fix_delta:
            raise DeltaPacketError("materialized delta differs from Git evidence")
    except (DeltaPacketError, OSError):
        gate._mark_attention(run.execution.lanes)
        return False
    return True


def section(name: str, payload_bytes: int) -> bytes:
    header = f"diff --git a/{name} b/{name}\n".encode()
    line = ("+" + name + "-" + "x" * 119 + "\n").encode()
    rows = max(1, (payload_bytes - len(header)) // len(line))
    return header + line * rows


delta = section("first.txt", 60_000) + section("second.txt", 60_000)
packet = build_delta_packet(delta, REVIEWED, RESOLVED)
replay = build_delta_packet(delta, REVIEWED, RESOLVED)
manifest = json.loads(packet.manifest)
parts = {part.name: part.content for part in packet.parts}
check(
    "large exact delta splits at a file boundary into two bounded parts",
    len(packet.parts) == 2
    and packet.parts[1].content.startswith(b"diff --git a/second.txt")
    and all(0 < len(part.content) <= 65_536 for part in packet.parts)
    and b"".join(part.content for part in packet.parts) == delta,
)
check(
    "delta packet manifest binds exact heads complete digest and ordered parts",
    manifest["reviewed_head_sha"] == REVIEWED
    and manifest["resolved_head_sha"] == RESOLVED
    and manifest["complete_delta_bytes"] == len(delta)
    and manifest["part_count"] == 2
    and [item["name"] for item in manifest["parts"]]
    == ["fix-delta.part-001.patch", "fix-delta.part-002.patch"]
    and validate_delta_packet(
        packet.manifest,
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
    )
    == delta,
)
check(
    "delta packet generation is byte-identical on replay",
    replay == packet,
)

single_file = section("only.txt", 100_000)
line_split = build_delta_packet(single_file, REVIEWED, RESOLVED)
check(
    "one-file delta uses a safe UTF-8 line boundary",
    len(line_split.parts) == 2
    and line_split.parts[0].content.endswith(b"\n")
    and b"".join(part.content for part in line_split.parts) == single_file,
)
boundary_header = b"diff --git a/boundary b/boundary\n"
at_boundary = (
    boundary_header
    + b"x" * (65_536 - len(boundary_header) - 1)
    + b"\n"
)
check(
    "exact per-artifact boundary remains one part",
    len(at_boundary) == 65_536
    and len(build_delta_packet(at_boundary, REVIEWED, RESOLVED).parts) == 1,
)
over_boundary = at_boundary + b"+\n"
check(
    "first byte over the artifact boundary becomes two ordered parts",
    [len(part.content) for part in build_delta_packet(
        over_boundary, REVIEWED, RESOLVED
    ).parts]
    == [65_536, 2],
)

tampered = dict(parts)
tampered[packet.parts[0].name] += b"x"
rejected(
    "tampered delta part fails closed",
    lambda: validate_delta_packet(
        packet.manifest,
        tampered,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
    ),
)
missing = dict(parts)
missing.pop(packet.parts[1].name)
rejected(
    "missing delta part fails closed",
    lambda: validate_delta_packet(
        packet.manifest,
        missing,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
    ),
)
reordered = json.loads(packet.manifest)
reordered["parts"] = list(reversed(reordered["parts"]))
rejected(
    "reordered delta manifest fails closed",
    lambda: validate_delta_packet(
        (json.dumps(reordered, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
    ),
)
rejected(
    "foreign resolved HEAD fails closed",
    lambda: validate_delta_packet(
        packet.manifest,
        parts,
        expected_reviewed_head=REVIEWED,
        expected_resolved_head="c" * 40,
    ),
)
third = json.loads(packet.manifest)
third["part_count"] = 3
third["parts"].append(
    {
        "name": "fix-delta.part-003.patch",
        "bytes": 1,
        "sha256": "0" * 64,
    }
)
rejected(
    "third delta part fails closed",
    lambda: validate_delta_packet(
        (json.dumps(third, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        {**parts, "fix-delta.part-003.patch": b"x"},
        expected_reviewed_head=REVIEWED,
        expected_resolved_head=RESOLVED,
    ),
)
rejected(
    "oversized total delta fails closed",
    lambda: build_delta_packet(
        b"diff --git a/x b/x\n" + b"+x\n" * 50_000,
        REVIEWED,
        RESOLVED,
    ),
)
rejected(
    "non-UTF-8 delta fails closed",
    lambda: build_delta_packet(b"diff --git a/x b/x\n\xff\n", REVIEWED, RESOLVED),
)


@dataclass(frozen=True)
class SessionResult:
    record: OperationRecord
    checkpoint: str


class FakeRuntime:
    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.continue_effects = 0

    def start(
        self,
        request: object,
        *,
        on_surface_opened=None,
        admit_provider_start=None,
    ) -> SessionResult:
        if admit_provider_start is not None:
            admit_provider_start()
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        record = replace(
            record,
            resources=replace(
                record.resources,
                surface_id="11111111-1111-4111-8111-111111111111",
            ),
            revision=record.revision + 1,
        )
        self.store.save(record, expected_revision=0)
        result = SessionResult(record, "checkpoint-owned")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def register_callback_target(self, *args: object) -> None:
        return None

    def status(self, owner_id: str, operation_id: str) -> SessionResult:
        return SessionResult(
            self.store.read(owner_id, operation_id), "checkpoint-owned"
        )


with tempfile.TemporaryDirectory(prefix="review-delta-attention.") as raw:
    root = Path(raw)
    product_root = root / "product"
    scratch_root = root / "scratch"
    product_root.mkdir()
    scratch_root.mkdir()
    store = OperationStore(root / "store")
    runtime = FakeRuntime(store)
    gate = ReviewGateController(root / "gate", runtime, store)
    context = ReviewContext(
        "packets/initial/manifest.json",
        REVIEWED,
        "scoped",
        "f" * 64,
    )
    preset = ReviewPreset.from_flags()
    request = ReviewOperationRequest(
        preset.request("delta-attention", selected_provider="openai"),
        "delta-attention",
        RuntimeRoute("codex", "sol", "high", "reviewer-callback", "e" * 64),
        context,
    )
    run = gate.begin(
        dispatch_operation_id="delta-attention",
        request=request,
        origin_surface="22222222-2222-4222-8222-222222222222",
        cwd=scratch_root,
        product_root=product_root,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks",
    )
    bounded = build_delta_packet(delta, REVIEWED, RESOLVED)
    delta_source = f"git:diff:{REVIEWED}..{RESOLVED}"
    inputs = [
        ContextInput(
            "fix-delta.manifest.json",
            delta_source + "#manifest",
            bounded.manifest,
            role="fix",
        )
    ]
    inputs.extend(
        ContextInput(
            part.name,
            delta_source + f"#part={index}/{len(bounded.parts)}",
            part.content,
            role="fix",
        )
        for index, part in enumerate(bounded.parts, start=1)
    )
    built = ContextBuilder(root / "packets").build(
        "delta-attention", tuple(inputs), metadata={"head_sha": RESOLVED}
    )
    context_manifest = root / "packets" / built.packet_id / "manifest.json"
    bundle = ResolutionBundle(
        ReviewResolution(
            "delta-attention",
            REVIEWED,
            RESOLVED,
            (
                FindingResolution(
                    "F-delta", "applied", "The exact fix is present."
                ),
            ),
        ),
        delta,
        {},
        "d" * 64,
    )
    check(
        "complete materialized packet is valid before prompt delivery",
        resolution_packet_ready(gate, run, context_manifest, bundle)
        and gate.read()["status"] == "reviewing",
    )
    materialized = json.loads(context_manifest.read_text(encoding="utf-8"))
    part_index = next(
        index
        for index, item in enumerate(materialized["inputs"])
        if item["name"] == bounded.parts[0].name
    )
    part_path = context_manifest.parent / (
        f"{part_index:03d}-fix-{bounded.parts[0].name}"
    )
    part_path.write_bytes(part_path.read_bytes() + b"tamper")
    check(
        "tampered packet becomes durable attention before continuation effect",
        not resolution_packet_ready(gate, run, context_manifest, bundle)
        and gate.read()["status"] == "attention-required"
        and store.read(
            run.execution.lanes[0].owner_id,
            run.execution.lanes[0].operation_id,
        ).state
        == "attention-required"
        and runtime.continue_effects == 0,
    )

print("\nAll review delta packet tests passed.")
