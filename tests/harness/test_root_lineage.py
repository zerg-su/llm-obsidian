#!/usr/bin/env python3
"""Durable root/parent lineage across registered operation families."""

from __future__ import annotations

import tempfile
from pathlib import Path

import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
from harness.store import OperationStore, StoreError  # noqa: E402
from harness.verification_attempt import pipeline_verify_identity  # noqa: E402
from harness.workflows.review_gate_contracts import ReviewPreset  # noqa: E402
from harness.workflows.review_contracts import (  # noqa: E402
    ReviewContext,
    ReviewOperationRequest,
    operation_spec as review_operation_spec,
    review_session_specs,
)


failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}")
        failures.append(label)


route = RuntimeRoute(
    "codex", "gpt-5.6-sol", "xhigh", "reviewer-callback", "a" * 64
)
root_id = "lineage-root"
root = OperationSpec(
    operation_id=root_id,
    idempotency_key="lineage-root-key",
    kind="dispatch",
    owner_id=root_id,
    route=route,
    context_manifest="packets/root/manifest.json",
    verification_profile="scoped",
    contract_sha256="b" * 64,
    root_operation_id=root_id,
)
verify, _lane, _run = pipeline_verify_identity(
    root,
    definition_sha256="b" * 64,
    input_sha256="c" * 64,
    profile="scoped",
)
check(
    "verification inherits the exact durable root and parent",
    verify.parent_operation_id == root_id
    and verify.root_operation_id == root_id,
)

context = ReviewContext(
    manifest="packets/review/manifest.json",
    head_sha="d" * 40,
    verification_profile="scoped",
    verification_profile_sha256="e" * 64,
    purpose="implementation",
    boundary_input_sha256="f" * 64,
)
policy = ReviewPreset.from_flags(
    deep=True, runtime="codex", model="sol", effort="xhigh"
).request("review-facade-rooted", purpose="implementation", selected_provider="openai")
request = ReviewOperationRequest(
    policy, root_id, route, context, root_operation_id=root_id
)
review = review_operation_spec(request)
sessions = review_session_specs(request)
check(
    "review facade parent and every provider lane carry the task root",
    review.parent_operation_id == root_id
    and review.root_operation_id == root_id
    and all(item.spec.parent_operation_id == root_id for item in sessions)
    and all(item.spec.root_operation_id == root_id for item in sessions),
)

with tempfile.TemporaryDirectory(prefix="root-lineage.") as raw:
    store = OperationStore(Path(raw) / "store")
    store.create(root, lane_id="root-lane", run_id="root-run")
    store.create(verify, lane_id="verify-lane", run_id="verify-run")
    foreign = OperationSpec(
        operation_id="foreign-child",
        idempotency_key="foreign-child-key",
        kind="pipeline-verify",
        owner_id=root_id,
        route=route,
        context_manifest=root.context_manifest,
        verification_profile="scoped",
        contract_sha256="b" * 64,
        parent_operation_id=root_id,
        root_operation_id="foreign-root",
    )
    try:
        store.create(foreign, lane_id="foreign-lane", run_id="foreign-run")
    except StoreError:
        rejected = True
    else:
        rejected = False
    check(
        "the durable store rejects foreign-root lineage before publication",
        rejected
        and not (
            Path(raw)
            / "store"
            / "owners"
            / root_id
            / "operations"
            / "foreign-child.json"
        ).exists(),
    )

if failures:
    raise SystemExit(f"{len(failures)} root lineage test(s) failed")
print("All root lineage tests passed.")
