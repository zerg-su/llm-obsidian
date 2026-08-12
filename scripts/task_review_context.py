"""Exact review context, routing, prompts, and callback envelopes."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from harness.context import (
    OUTCOME_POINTER_ID,
    ContextBuilder,
    ContextInput,
    outcome_contract_input,
)
from harness.contracts import CallbackEnvelope, ContractError as HarnessContractError, RuntimeRoute
from harness.review_program import QUESTIONS as REVIEW_QUESTIONS, ReviewBoundaryInput
from harness.review_submit import round_schema_lines
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
)
from harness.workflows.review_gate import ReviewPreset
from model_routing import load_config, resolve, session_from_meta
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_contract import normalize
from task_review_delta_packet import DeltaPacket, build_delta_packet
from task_escalation_records import EscalationRecordError, load_amendments
from task_review_resolution_bundle import _bounded_input
from task_review_identity import (
    _current_review_is_quiescent,
    _zero_effect_attention_is_quiescent,
    _zero_effect_attention_shape,
    _current_runtime_root,
    _gate_root,
    _runtime_root,
    _validate_task,
)
from task_review_request import (
    _callback_path,
    _canonical_sha256,
    _envelope,
    _prompt,
    _request as _unbound_request,
    _route,
)
from task_review_shared import (
    ResolutionBundle,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_bytes,
    _atomic_text,
    _git,
    _git_bytes,
    _load_review_boundary_input,
    _read_json,
)


_BOUNDARY_ARTIFACTS = {
    "intent": (
        ("review-design", "design_path", "design_sha256"),
        ("review-capability-dispositions", "capability_dispositions_path", "capability_dispositions_sha256"),
        ("review-success-evidence-map", "success_evidence_map_path", "success_evidence_map_sha256"),
    ),
    "implementation": (
        ("review-verification-evidence", "verification_evidence_path", "verification_evidence_sha256"),
    ),
    "release": (
        ("review-outcome-evidence", "outcome_evidence_map_path", "outcome_evidence_map_sha256"),
        ("review-accepted-deviations", "accepted_deviations_path", "accepted_deviations_sha256"),
    ),
}


def _request(
    meta: Mapping[str, Any],
    vault: Path,
    task_id: str,
    context: ReviewContext,
) -> tuple[ReviewPreset, ReviewOperationRequest | None]:
    """Compile the frozen task preset and bind its canonical topology."""

    routing = meta.get("routing")
    effective = routing.get("effective") if isinstance(routing, Mapping) else None
    if isinstance(effective, Mapping):
        expected_config = str(effective.get("config_sha256") or "")
        if expected_config and load_config(vault).fingerprint != expected_config:
            raise TaskReviewError(
                "review routing config drifted from frozen task metadata"
            )
    preset, request = _unbound_request(meta, vault, task_id, context)
    if request is None:
        return preset, None
    frozen = meta.get("review_topology")
    expected_topology_sha256 = ""
    expected_topology_payload: Mapping[str, Any] | None = None
    if isinstance(frozen, Mapping):
        expected_topology_sha256 = str(frozen.get("sha256") or "")
        payload = frozen.get("payload")
        if isinstance(payload, Mapping):
            expected_topology_payload = payload
    bound = replace(
        request,
        requested_mode=str(meta["review_policy"]["mode"]),
    )
    # Explicit task routes are already final here.  Default finalization routes
    # are selected only when the exact-HEAD attempt is reserved, so their
    # frozen topology must be checked after that binding instead of against the
    # provisional session route.
    if (
        expected_topology_payload is not None
        and bound.topology.payload() == expected_topology_payload
    ):
        if bound.topology.sha256 != expected_topology_sha256:
            raise TaskReviewError(
                "review effective topology digest changed from frozen task metadata"
            )
        bound = replace(bound, topology_sha256=expected_topology_sha256)
    return (
        preset,
        bound,
    )


def _assert_frozen_topology(
    meta: Mapping[str, Any], request: ReviewOperationRequest
) -> None:
    """Validate the frozen topology after finalization route selection.

    RC4 evidence E1 is enforced in two places, and this is only one of them.
    The binding is *created* at dispatch: ``dispatch_workspace`` writes
    ``review_topology`` for every task whose review is enabled, so no newly
    dispatched task reaches this function unbound.  Here, a binding that
    disagrees with the compiled topology fails closed.

    A *missing* binding is tolerated at this boundary.  Records written before
    the field existed, and current-checkout reviews that synthesize their
    metadata at launch, carry no topology and must still be able to start a
    review.  Failing closed on absence would strand them.  The residual gap —
    an unbound v4 review-enabled record can launch a lane with no digest to
    compare against — is recorded as D-266-RC4-02 in the RC4 accepted-deviations
    artifact.
    """

    frozen = meta.get("review_topology")
    if not isinstance(frozen, Mapping):
        # Absence is tolerated here, and that is a narrowing, not a fail-open.
        # The binding is created at dispatch: dispatch_workspace writes
        # review_topology for every review-enabled task, so no new task can
        # reach this point unbound.  Records written before the field existed —
        # and current-checkout reviews, which synthesize their metadata at
        # launch and have no dispatch record — stay launchable.  RC4 evidence E1
        # is worded to claim exactly that, and the residual gap is recorded as
        # D-266-RC4-02 in the RC4 accepted-deviations artifact.  What is NOT
        # tolerated is a binding that disagrees with the compiled topology; that
        # is enforced below.
        return
    expected_sha256 = str(frozen.get("sha256") or "")
    expected_payload = frozen.get("payload")
    topology = request.topology
    if (
        not isinstance(expected_payload, Mapping)
        or expected_sha256 != topology.sha256
        or expected_payload != topology.payload()
        or request.topology_sha256 != expected_sha256
    ):
        raise TaskReviewError(
            "review effective topology changed from frozen task metadata"
        )


def _bounded_review_diff(raw: bytes) -> bytes:
    """Normalize arbitrary Git output and truncate on a UTF-8 boundary."""

    encoded = raw.decode("utf-8", errors="replace").encode("utf-8")
    if len(encoded) <= 65_536:
        return encoded
    prefix = encoded[:65_000].decode("utf-8", errors="ignore").encode("utf-8")
    return prefix + b"\n[diff truncated; inspect product HEAD]\n"


class _ReviewedArtifactMissing(TaskReviewError):
    """The exact reviewed tree has no entry for a boundary artifact."""


def _amendment_evidence(
    meta: Mapping[str, Any], worktree: Path
) -> tuple[tuple[ContextInput, ...], dict[str, str]] | None:
    """Bind one ordered protected-amendment chain and its terminal authority."""

    try:
        amendments = load_amendments(worktree)
    except EscalationRecordError as exc:
        raise TaskReviewError(
            "authoritative amendment evidence is invalid"
        ) from exc
    if not amendments:
        return None
    plan_sha256 = str(meta.get("approved_plan_sha256") or "")
    outcome_sha256 = str(meta.get("outcome_contract_sha256") or "")
    if any(
        record.payload.get("plan_sha256") != plan_sha256
        or record.payload.get("outcome_sha256") != outcome_sha256
        for record in amendments
    ):
        raise TaskReviewError(
            "authoritative amendment does not match reviewed task metadata"
        )
    amendment_ids = {record.record_id for record in amendments}
    if (
        len(amendment_ids) != len(amendments)
        or amendments[0].previous_record_id in amendment_ids
    ):
        raise TaskReviewError("authoritative amendment evidence is ambiguous")
    inputs: list[ContextInput] = []
    chain_identity: list[dict[str, str]] = []
    for index, record in enumerate(amendments, start=1):
        try:
            if record.path.is_symlink():
                raise OSError("amendment record is a symlink")
            raw = record.path.read_bytes()
        except OSError as exc:
            raise TaskReviewError(
                "authoritative amendment evidence is invalid"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != record.sha256:
            raise TaskReviewError(
                "authoritative amendment evidence is invalid"
            )
        inputs.append(
            ContextInput(
                (
                    "approved-amendment.json"
                    if len(amendments) == 1
                    else f"approved-amendment-{index:03d}.json"
                ),
                str(record.path),
                raw,
                role="outcome",
            )
        )
        chain_identity.append(
            {"record_id": record.record_id, "record_sha256": record.sha256}
        )
    terminal = amendments[-1]
    metadata = {
        "amendment_record_id": terminal.record_id,
        "amendment_record_sha256": terminal.sha256,
    }
    if len(amendments) > 1:
        metadata.update(
            {
                "amendment_chain_length": str(len(amendments)),
                "amendment_chain_sha256": hashlib.sha256(
                    json.dumps(
                        chain_identity, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            }
        )
    return tuple(inputs), metadata


def _reviewed_artifact_input(
    worktree: Path,
    relative: Path,
    *,
    name: str,
    artifact_head: str,
    expected_sha256: str,
    pointer_root: Path,
) -> ContextInput:
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        raise TaskReviewError(
            f"review boundary artifact is unavailable: {relative}"
        )
    tracked_path = _git(
        worktree,
        "ls-tree",
        "--name-only",
        artifact_head,
        "--",
        relative.as_posix(),
    )
    if tracked_path != relative.as_posix():
        raise _ReviewedArtifactMissing(
            f"review boundary artifact has no reviewed Git blob: {relative}"
        )
    raw = _git_bytes(
        worktree,
        "show",
        f"{artifact_head}:{relative.as_posix()}",
    )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise TaskReviewError(
            f"review boundary artifact digest is stale: {relative}"
        )
    source = f"git:{artifact_head}:{relative.as_posix()}"
    if len(raw) <= 65_536:
        return ContextInput(name, source, raw, role="outcome")
    _atomic_bytes(pointer_root / name, raw)
    return ContextInput.pointer(
        name,
        source,
        byte_count=len(raw),
        content_sha256=digest,
        role="outcome",
    )


def _reviewed_plan_outcome_input(
    worktree: Path,
    plan: Path,
    boundary: ReviewBoundaryInput,
    *,
    artifact_head: str,
) -> ContextInput:
    """Bind resolution semantics to the exact plan bytes originally reviewed."""

    root = worktree.expanduser().resolve()
    resolved_plan = plan.expanduser().resolve()
    try:
        relative = resolved_plan.relative_to(root)
    except ValueError as exc:
        raise TaskReviewError("review program plan is outside the worktree") from exc
    raw = _git_bytes(
        worktree,
        "show",
        f"{artifact_head}:{relative.as_posix()}",
    )
    if hashlib.sha256(raw).hexdigest() != boundary.plan_sha256:
        raise TaskReviewError("review program plan digest is stale")
    try:
        contract = extract_from_bytes(raw)
    except OutcomeContractError as exc:
        raise TaskReviewError(f"review Outcome Contract is invalid: {exc}") from exc
    if contract.sha256 != boundary.outcome_contract_sha256:
        raise TaskReviewError("review Outcome Contract input digest changed")
    return ContextInput(
        "outcome-contract.json",
        f"git:{artifact_head}:{relative.as_posix()}",
        contract.canonical,
        role="outcome",
        pointer_id=OUTCOME_POINTER_ID,
    )


def _boundary_file_input(
    source_root: Path,
    relative: Path,
    *,
    name: str,
    expected_sha256: str,
    pointer_root: Path,
) -> ContextInput:
    """Materialize one path-confined artifact with its frozen digest."""

    source_root = source_root.resolve()
    candidate = source_root / relative
    target = candidate.resolve()
    if (
        target == source_root
        or source_root not in target.parents
        or not target.is_file()
        or candidate.is_symlink()
    ):
        raise TaskReviewError(
            f"review boundary artifact is unavailable: {relative}"
        )
    try:
        item = _bounded_input(
            name,
            target,
            role="outcome",
            pointer_root=pointer_root,
        )
    except OSError as exc:
        raise TaskReviewError(
            f"review boundary artifact is unavailable: {relative}"
        ) from exc
    if item.content_sha256 != expected_sha256:
        raise TaskReviewError(
            f"review boundary artifact digest is stale: {relative}"
        )
    return item


def _purpose_boundary_inputs(
    worktree: Path,
    plan: Path,
    boundary: ReviewBoundaryInput,
    *,
    pointer_root: Path,
    artifact_root: Path | None = None,
    artifact_head: str = "",
) -> tuple[ContextInput, ...]:
    """Validate and materialize every exact artifact named by a purpose boundary."""

    if artifact_head and artifact_root is None:
        contract = _reviewed_plan_outcome_input(
            worktree,
            plan,
            boundary,
            artifact_head=artifact_head,
        )
    else:
        try:
            contract = outcome_contract_input(
                plan,
                expected_sha256=boundary.outcome_contract_sha256,
            )
            plan_bytes = plan.read_bytes()
        except (OSError, HarnessContractError) as exc:
            raise TaskReviewError(
                f"review Outcome Contract is invalid: {exc}"
            ) from exc
        if hashlib.sha256(plan_bytes).hexdigest() != boundary.plan_sha256:
            raise TaskReviewError("review program plan digest is stale")
    inputs = [contract]
    source_root = (artifact_root or worktree).resolve()
    for name, path_field, digest_field in _BOUNDARY_ARTIFACTS[boundary.purpose]:
        relative = Path(str(getattr(boundary, path_field)))
        expected_sha256 = str(getattr(boundary, digest_field))
        if artifact_head:
            if artifact_root is not None:
                raise TaskReviewError(
                    f"review boundary artifact is unavailable: {relative}"
                )
            try:
                item = _reviewed_artifact_input(
                    worktree, relative, name=name,
                    artifact_head=artifact_head,
                    expected_sha256=expected_sha256,
                    pointer_root=pointer_root,
                )
            except _ReviewedArtifactMissing:
                # Ignored release receipts have no Git blob. Their frozen
                # boundary digest remains authoritative, and exact path
                # confinement prevents substituting a foreign artifact.
                item = _boundary_file_input(
                    worktree,
                    relative,
                    name=name,
                    expected_sha256=expected_sha256,
                    pointer_root=pointer_root,
                )
            inputs.append(item)
            continue
        inputs.append(
            _boundary_file_input(
                source_root,
                relative,
                name=name,
                expected_sha256=expected_sha256,
                pointer_root=pointer_root,
            )
        )
    return tuple(inputs)


def _head_diff_input(worktree: Path) -> ContextInput:
    diff = _bounded_review_diff(
        _git_bytes(
            worktree,
            "show",
            "--format=fuller",
            "--stat",
            "--patch",
            "--find-renames",
            "HEAD",
        )
    )
    return ContextInput("head-diff.patch", "git:show:HEAD", diff, role="diff")


def _delta_inputs(
    packet: DeltaPacket,
    *,
    delta_source: str,
    runtime_root: Path,
) -> tuple[ContextInput, ...]:
    """Materialize one legacy-inline or v2 pointer-backed delta packet."""

    inputs = [
        ContextInput(
            "fix-delta.manifest.json",
            delta_source + "#manifest",
            packet.manifest,
            role="fix",
        )
    ]
    delta_schema = json.loads(packet.manifest)["schema_version"]
    if delta_schema == 1:
        inputs.extend(
            ContextInput(
                part.name,
                delta_source + f"#part={index}/{len(packet.parts)}",
                part.content,
                role="fix",
            )
            for index, part in enumerate(packet.parts, start=1)
        )
        return tuple(inputs)
    try:
        resolved_runtime = runtime_root.expanduser().resolve(strict=True)
        pointer_root = resolved_runtime
        for component in (
            "pointers",
            "fix-delta-v2",
            hashlib.sha256(packet.manifest).hexdigest(),
        ):
            pointer_root = pointer_root / component
            if pointer_root.is_symlink():
                raise TaskReviewError(
                    "review delta pointer root cannot be a symlink"
                )
            pointer_root.mkdir(mode=0o700, exist_ok=True)
            if (
                pointer_root.is_symlink()
                or not pointer_root.is_dir()
                or resolved_runtime not in pointer_root.resolve().parents
            ):
                raise TaskReviewError(
                    "review delta pointer root escaped its owner"
                )
            pointer_root.chmod(0o700)
    except OSError as exc:
        raise TaskReviewError(
            "review delta pointer root is unavailable"
        ) from exc
    for part in packet.parts:
        pointer = pointer_root / part.name
        _atomic_bytes(pointer, part.content)
        inputs.append(
            ContextInput.pointer(
                part.name,
                str(pointer.resolve(strict=True)),
                byte_count=len(part.content),
                content_sha256=hashlib.sha256(part.content).hexdigest(),
                role="fix",
            )
        )
    return tuple(inputs)


def _bound_review_artifact_root(
    meta: Mapping[str, Any],
) -> Path | None:
    """Revalidate the immutable external root stored by current review."""

    raw = str(meta.get("review_artifact_root") or "")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_symlink():
        raise TaskReviewError("review artifact root is invalid")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TaskReviewError("review artifact root is unavailable") from exc
    if not resolved.is_dir():
        raise TaskReviewError("review artifact root is invalid")
    return resolved


def _context(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    resolution_bundle: ResolutionBundle | None = None,
) -> tuple[ReviewContext, Path]:
    head = _git(worktree, "rev-parse", "HEAD")
    policy = meta["review_policy"]
    purpose = str(policy.get("purpose") or "implementation")
    boundary_input_sha256 = str(
        policy.get("boundary_input_sha256") or ""
    )
    plan = Path(str(meta["plan_file"])).expanduser().resolve()
    plan_review = meta.get("plan_review")
    plan_artifact_root: Path | None = None
    review_artifact_root = _bound_review_artifact_root(meta)
    plan_review_inputs: list[ContextInput] = []
    packet_metadata = {
        "task_id": task_id,
        "task_name": str(meta.get("task_name") or ""),
        "head_sha": head,
    }
    amendment_inputs: list[ContextInput] = []
    amendment = _amendment_evidence(meta, worktree)
    if amendment is not None:
        amendment_chain, amendment_metadata = amendment
        amendment_inputs.extend(amendment_chain)
        packet_metadata.update(amendment_metadata)
    if plan_review is not None:
        if not isinstance(plan_review, Mapping):
            raise TaskReviewError("plan review metadata is invalid")
        base_sha = str(plan_review.get("base_sha") or "")
        bound_head = str(plan_review.get("head_sha") or "")
        if (
            plan_review.get("schema_version") != 1
            or plan_review.get("artifact_root") != "runtime"
            or bound_head != head
            or not base_sha
        ):
            raise TaskReviewError("plan review OID binding is stale")
        inspection = runtime_root / "inputs/plan-review-inspection.json"
        evidence = _read_json(inspection, "plan review inspection evidence")
        commands = evidence.get("commands")
        if (
            evidence.get("schema_version") != 1
            or evidence.get("base_sha") != base_sha
            or evidence.get("head_sha") != head
            or not isinstance(commands, list)
            or len(commands) != 4
            or any(not isinstance(item, str) or not item for item in commands)
        ):
            raise TaskReviewError("plan review inspection evidence is stale")
        plan_artifact_root = runtime_root
        packet_metadata["base_sha"] = base_sha
        plan_review_inputs.extend(
            (
                ContextInput(
                    "exact-base.txt",
                    "git:base",
                    (base_sha + "\n").encode(),
                    role="base",
                ),
                ContextInput(
                    "review-inspect-commands.txt",
                    str(inspection),
                    ("\n".join(commands) + "\n").encode(),
                    role="instructions",
                ),
            )
        )
    inputs = [
        _bounded_input(
            "approved-plan.md",
            plan,
            role="plan",
            pointer_root=runtime_root / "pointers",
        ),
        _bounded_input(
            "review-skill.md",
            vault / "skills/review/SKILL.md",
            role="instructions",
            pointer_root=runtime_root / "pointers",
        ),
        ContextInput(
            (
                "current-review.json"
                if meta.get("lifecycle") == "current-checkout"
                else "task-meta.json"
            ),
            (
                str(runtime_root / "current-review.json")
                if meta.get("lifecycle") == "current-checkout"
                else str(worktree / ".task-meta.json")
            ),
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode(),
            role="task",
        ),
        ContextInput(
            "exact-head.txt",
            "git:HEAD",
            (head + "\n").encode(),
            role="head",
        ),
        *amendment_inputs,
        *plan_review_inputs,
    ]
    if boundary_input_sha256:
        boundary_path = Path(
            str(meta.get("review_boundary_input_file") or "")
        )
        boundary = _load_review_boundary_input(
            boundary_path, purpose=purpose
        )
        if boundary.input_sha256 != boundary_input_sha256:
            raise TaskReviewError("review boundary input digest is stale")
        rebound_reviewed_head = (
            resolution_bundle.origin_reviewed_head_sha
            or resolution_bundle.resolution.reviewed_head_sha
            if resolution_bundle is not None
            else ""
        )
        rebound_resolved_head = (
            resolution_bundle.resolution.resolved_head_sha
            if resolution_bundle is not None
            else ""
        )
        boundary_head = (
            boundary.product_head_sha
            if purpose == "implementation"
            else boundary.integration_head_sha
            if purpose == "release"
            else ""
        )
        exact_resolution_rebind = (
            bool(boundary_head)
            and boundary_head == rebound_reviewed_head
            and head == rebound_resolved_head
        )
        if (
            purpose == "implementation"
            and boundary.product_head_sha != head
        ) or (
            purpose == "release" and boundary.integration_head_sha != head
        ):
            if not exact_resolution_rebind:
                raise TaskReviewError("review boundary input targets another HEAD")
        inputs.append(
            ContextInput(
                "review-boundary-input.json",
                str(boundary_path),
                (
                    json.dumps(
                        boundary.payload(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
                role="outcome",
            )
        )
        inputs.extend(
            _purpose_boundary_inputs(
                worktree,
                plan,
                boundary,
                pointer_root=runtime_root / "pointers",
                artifact_root=plan_artifact_root or review_artifact_root,
                artifact_head=(
                    boundary_head
                    if exact_resolution_rebind and plan_artifact_root is None
                    else ""
                ),
            )
        )
    elif purpose != "implementation":
        raise TaskReviewError(
            "non-legacy review purpose requires a boundary input"
        )
    implementer_summary_sha256 = ""
    if meta.get("version") == 4 and meta.get("lifecycle") != "current-checkout":
        summary_path = worktree / ".task-summary.json"
        if not summary_path.is_file() or summary_path.is_symlink():
            raise TaskReviewError(
                "v4 review requires the exact implementer summary"
            )
        summary_input = _bounded_input(
            "implementer-summary.json",
            summary_path,
            role="task",
            pointer_root=runtime_root / "pointers",
        )
        inputs.append(summary_input)
        implementer_summary_sha256 = summary_input.content_sha256
        inputs.append(
            outcome_contract_input(
                plan,
                expected_sha256=str(meta.get("outcome_contract_sha256") or ""),
            )
        )
    instructions = worktree / "AGENTS.md"
    if instructions.is_file() and not instructions.is_symlink():
        inputs.append(
            _bounded_input(
                "product-agents.md",
                instructions,
                role="instructions",
                pointer_root=runtime_root / "pointers",
            )
        )
    inputs.append(_head_diff_input(worktree))
    if resolution_bundle is not None:
        delta_packet = build_delta_packet(
            resolution_bundle.fix_delta,
            resolution_bundle.resolution.reviewed_head_sha,
            resolution_bundle.resolution.resolved_head_sha,
            review_identity_sha256=(
                resolution_bundle.review_identity_sha256
            ),
        )
        resolution_payload = {
            "schema_version": 1,
            "operation_id": resolution_bundle.resolution.operation_id,
            "reviewed_head_sha": (
                resolution_bundle.resolution.reviewed_head_sha
            ),
            "resolved_head_sha": (
                resolution_bundle.resolution.resolved_head_sha
            ),
            "previous_finding_ids": [
                item.finding_id
                for item in resolution_bundle.resolution.resolutions
            ],
            "resolutions": [
                item.payload()
                for item in resolution_bundle.resolution.resolutions
            ],
            "fix_delta_sha256": hashlib.sha256(
                resolution_bundle.fix_delta
            ).hexdigest(),
        }
        delta_source = (
            "git:diff:"
            f"{resolution_bundle.resolution.reviewed_head_sha}.."
            f"{resolution_bundle.resolution.resolved_head_sha}"
        )
        inputs.extend(
            _delta_inputs(
                delta_packet,
                delta_source=delta_source,
                runtime_root=runtime_root,
            )
        )
        inputs.append(
            ContextInput(
                "resolution-evidence.json",
                str(worktree / ".task-review-resolution.json"),
                (
                    json.dumps(
                        resolution_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
                role="resolution",
            )
        )
    builder = ContextBuilder(runtime_root / "packets")
    manifest = builder.build(
        task_id,
        tuple(inputs),
        metadata=packet_metadata,
    )
    manifest_path = (
        runtime_root
        / "packets"
        / manifest.packet_id
        / "manifest.json"
    )
    return (
        ReviewContext(
            manifest_path.relative_to(runtime_root).as_posix(),
            head,
            str(policy["verification_profile"]),
            str(policy["verification_profile_sha256"]),
            implementer_summary_sha256,
            purpose,
            boundary_input_sha256,
        ),
        manifest_path,
    )
