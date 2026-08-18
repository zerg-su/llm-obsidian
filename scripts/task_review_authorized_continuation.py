"""One resolution-bound verification-profile continuation for task review."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from harness.context import ContextInput
from harness.runtime_callback_io import _atomic_json as _atomic_packet_json
from harness.contracts import (
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
)
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.verification import (
    VerificationAuthority,
    VerificationAuthorityError,
    compose_commands,
    load_profiles,
    run_profile,
)
from harness.verification_attempt import (
    VERIFICATION_STEP_SCHEMA_VERSION,
    VerificationAttempt,
    pipeline_verify_effect_id,
    pipeline_verify_identity,
    verification_input_sha256,
)
from task_escalation_records import (
    DecisionRecord,
    EscalationRecordError,
    load_chain,
)
from task_plan_authority import PlanAuthorityError, resolve_plan_authority
from task_review_identity import _validate_task
from task_review_resolution_bundle import _bounded_input
from task_review_shared import TaskReviewError, _atomic_json


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
BINDING_NAME = ".task-review-authorized-continuation.json"
ATTENTION_PACKET_NAME = ".task-verification.json"
#: The standard packet consumer (``pipeline-verification-resubmit.py``) bounds
#: the raw packet file, so this bound is measured against the exact bytes the
#: writer emits and an unconsumable packet is never published.
MAX_ATTENTION_PACKET_BYTES = 65_536


class AuthorizedContinuationError(TaskReviewError):
    """The bounded coordinator-authorized continuation is unavailable."""


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AuthorizedContinuationError(
            "authorized continuation cannot inspect the product checkout"
        )
    return result.stdout.strip()


def _resolved_authorization(
    records: tuple[DecisionRecord, ...],
    *,
    escalation_id: str,
    outcome_sha256: str,
) -> tuple[DecisionRecord, DecisionRecord, str]:
    matches = tuple(
        record
        for record in records
        if record.record_type == "resolution"
        and record.payload.get("id") == escalation_id
    )
    if len(matches) != 1:
        raise AuthorizedContinuationError(
            "full-profile continuation resolution is not exact"
        )
    decision = str(matches[0].payload.get("decision") or "")
    prefix = (
        "authorize-one-exact-head-full-profile-receipt-and-bind-amended-repair-contract; "
        "create at most one immutable full-profile verification receipt for exact clean HEAD "
    )
    suffix = (
        f", bind that receipt and amended Outcome Contract {outcome_sha256} "
        "into one fresh repair review continuation, and do not replay accepted phases, "
        "reviewers, providers, callbacks, or predecessor effects"
    )
    predecessor_head = decision[len(prefix) : -len(suffix)] if (
        decision.startswith(prefix) and decision.endswith(suffix)
    ) else ""
    if (
        matches[0].payload.get("status") != "resolved"
        or matches[0].payload.get("category") != "contract-drift"
        or not GIT_OID.fullmatch(predecessor_head)
    ):
        raise AuthorizedContinuationError(
            "full-profile continuation resolution is not exact"
        )
    successor_decision = (
        "A: authorize one new committed mechanism HEAD in the existing "
        "task/llm-obsidian-2-8-1-concurrency-fix-sol worktree. Implement and "
        "regression-test the narrow registered full-profile continuation primitive "
        "as part of the 2.8.1 product repair; supersede "
        f"{predecessor_head} as the final candidate while preserving it as the clean "
        "proven predecessor. After the single mechanism commit, bind exactly one "
        "immutable full-profile receipt and exactly one fresh repair Deep review to "
        "the resulting exact clean HEAD and amended Outcome Contract "
        f"{outcome_sha256}. Do not replay accepted engineering phases, predecessor "
        "verification, prior reviewers/providers/callbacks, or external effects; any "
        "further product mutation requires a new exact-head receipt/review boundary."
    )
    successors = tuple(
        record
        for record in records
        if record.record_type == "resolution"
        and record.payload.get("status") == "resolved"
        and record.payload.get("category") == "contract-drift"
        and record.payload.get("decision") == successor_decision
    )
    if len(successors) != 1:
        raise AuthorizedContinuationError(
            "mechanism successor resolution is not exact"
        )
    return matches[0], successors[0], predecessor_head


def _rebound_meta(
    meta: Mapping[str, Any], *, profile: str, profile_sha256: str
) -> dict[str, Any]:
    rebound = copy.deepcopy(dict(meta))
    policy = rebound.get("review_policy")
    topology = rebound.get("review_topology")
    if not isinstance(policy, dict) or not isinstance(topology, dict):
        raise AuthorizedContinuationError(
            "authorized continuation review policy is unavailable"
        )
    payload = topology.get("payload")
    if not isinstance(payload, dict):
        raise AuthorizedContinuationError(
            "authorized continuation topology is unavailable"
        )
    policy["verification_profile"] = profile
    policy["verification_profile_sha256"] = profile_sha256
    payload["verification_profile"] = {
        "name": profile,
        "sha256": profile_sha256,
    }
    topology["sha256"] = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return rebound


def _binding_identity(
    *,
    task_id: str,
    resolution: DecisionRecord,
    successor_resolution: DecisionRecord,
    amendment: DecisionRecord,
    head_sha: str,
    profile_sha256: str,
    outcome_sha256: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "task_id": task_id,
                "resolution_id": resolution.record_id,
                "resolution_sha256": resolution.sha256,
                "successor_resolution_id": successor_resolution.record_id,
                "successor_resolution_sha256": successor_resolution.sha256,
                "amendment_id": amendment.record_id,
                "amendment_sha256": amendment.sha256,
                "head_sha": head_sha,
                "profile_sha256": profile_sha256,
                "outcome_sha256": outcome_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _write_once(path: Path, value: Mapping[str, object]) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise AuthorizedContinuationError(
                "authorized continuation binding is invalid"
            )
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthorizedContinuationError(
                "authorized continuation binding is unreadable"
            ) from exc
        if current != dict(value):
            raise AuthorizedContinuationError(
                "authorized continuation binding changed"
            )
        return
    _atomic_json(path, dict(value))


def _failed_attention_packet(
    authority: VerificationAuthority,
    *,
    runtime_root: Path,
    receipt_path: Path,
) -> dict[str, object]:
    """Derive the standard identity-bound packet from one failed receipt."""

    return {
        "schema_version": VerificationAuthority.SCHEMA_VERSION,
        "operation_id": authority.parent.spec.operation_id,
        "verification_operation_id": authority.operation_id,
        "verification_lane_id": authority.lane_id,
        "verification_run_id": authority.run_id,
        "definition_sha256": authority.definition_sha256,
        "step_id": "verify",
        "head_sha": authority.head_sha,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": ["escalate"],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": str(receipt_path),
        "evidence": [
            {
                "command_id": item.command_id,
                "exit_code": item.exit_code,
                "output_pointer": str(
                    (runtime_root / item.output_pointer).resolve()
                ),
            }
            for item in authority.evidence
        ],
        "verification_attempt": authority.attempt.as_dict(),
        "verification_attempt_sha256": authority.attempt.sha256,
    }


def _fail_with_attention_handoff(
    authority: VerificationAuthority,
    *,
    worktree: Path,
    runtime_root: Path,
    receipt_path: Path,
) -> NoReturn:
    """Hand one durable failed receipt to attention, then refuse to continue.

    The bounded authorization allows at most one immutable receipt for its exact
    clean HEAD, so ``escalate`` is the only truthful response: a repaired HEAD or
    a same-HEAD retry needs its own receipt/review boundary. For that same reason
    the verify child stays terminal ``failed`` instead of the resumable
    ``attention-required`` state the pipeline owner uses, which is also why the
    durable ingress requires ``child.state == status``. Publishing is idempotent
    and never destructive: a packet this identity did not derive is refused
    rather than replaced, and a published packet is cleared only by the
    coordinator decision that resolves it — never by this owner.
    """

    packet = _failed_attention_packet(
        authority, runtime_root=runtime_root, receipt_path=receipt_path
    )
    #: Exactly the bytes ``_atomic_packet_json`` emits, so the bound the standard
    #: consumer applies to the file is the bound measured here.
    encoded = (
        json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(encoded) > MAX_ATTENTION_PACKET_BYTES:
        raise AuthorizedContinuationError(
            "authorized continuation attention packet is too large"
        )
    packet_path = worktree / ATTENTION_PACKET_NAME
    if packet_path.is_symlink() or (
        packet_path.exists() and not packet_path.is_file()
    ):
        raise AuthorizedContinuationError(
            "authorized continuation attention packet is invalid"
        )
    if packet_path.is_file():
        try:
            published = json.loads(packet_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthorizedContinuationError(
                "authorized continuation attention packet is unreadable"
            ) from exc
        if published != packet:
            if (
                not isinstance(published, Mapping)
                or published.get("verification_operation_id")
                != packet["verification_operation_id"]
            ):
                raise AuthorizedContinuationError(
                    "authorized continuation cannot replace an attention packet"
                    " it did not derive"
                )
            raise AuthorizedContinuationError(
                "authorized continuation attention packet changed"
            )
    else:
        _atomic_packet_json(packet_path, packet)
    raise AuthorizedContinuationError(
        "authorized continuation full-profile verification failed; typed "
        f"attention is ready in {packet_path}"
    )


def _authorized_continuation_inputs(
    meta: Mapping[str, Any],
    worktree: Path,
    runtime_root: Path,
    head: str,
) -> tuple[ContextInput, ...]:
    """Expose one validated full-profile receipt and its outputs to reviewers."""

    binding = meta.get("authorized_review_continuation")
    if binding is None:
        return ()
    if not isinstance(binding, Mapping):
        raise TaskReviewError("authorized review continuation is invalid")
    binding_path = worktree / BINDING_NAME
    try:
        persisted = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError(
            "authorized review continuation is unavailable"
        ) from exc
    policy = meta.get("review_policy")
    if (
        persisted != dict(binding)
        or binding.get("schema_version") != 1
        or binding.get("status") != "prepared"
        or binding.get("task_id") != meta.get("task_id")
        or binding.get("head_sha") != head
        or not isinstance(policy, Mapping)
        or binding.get("verification_profile")
        != policy.get("verification_profile")
        or binding.get("verification_profile_sha256")
        != policy.get("verification_profile_sha256")
    ):
        raise TaskReviewError("authorized review continuation changed")
    verification_root = Path(
        str(binding.get("verification_runtime_root") or "")
    )
    receipt_path = Path(str(binding.get("receipt_pointer") or ""))
    if (
        not verification_root.is_absolute()
        or verification_root.is_symlink()
        or not receipt_path.is_absolute()
        or receipt_path.is_symlink()
        or verification_root.resolve() not in receipt_path.resolve().parents
        or not receipt_path.is_file()
    ):
        raise TaskReviewError(
            "authorized review continuation receipt is unavailable"
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError(
            "authorized review continuation receipt is unavailable"
        ) from exc
    canonical_sha256 = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence = receipt.get("evidence") if isinstance(receipt, dict) else None
    if (
        canonical_sha256 != binding.get("receipt_sha256")
        or receipt.get("status") != "complete"
        or receipt.get("head_sha") != head
        or receipt.get("profile") != binding.get("verification_profile")
        or receipt.get("profile_sha256")
        != binding.get("verification_profile_sha256")
        or not isinstance(evidence, list)
        or not evidence
    ):
        raise TaskReviewError(
            "authorized review continuation receipt changed"
        )
    inputs = [
        _bounded_input(
            "authorized-continuation.json",
            binding_path,
            role="verification",
            pointer_root=runtime_root / "pointers",
        ),
        _bounded_input(
            "full-verification-receipt.json",
            receipt_path,
            role="verification",
            pointer_root=runtime_root / "pointers",
        ),
    ]
    resolved_root = verification_root.resolve()
    for index, item in enumerate(evidence, start=1):
        pointer = (
            item.get("output_pointer") if isinstance(item, Mapping) else None
        )
        if not isinstance(pointer, str):
            raise TaskReviewError(
                "authorized review continuation evidence is invalid"
            )
        relative = Path(pointer)
        output = (resolved_root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or resolved_root not in output.parents
            or output.is_symlink()
            or not output.is_file()
        ):
            raise TaskReviewError(
                "authorized review continuation evidence is unavailable"
            )
        inputs.append(
            _bounded_input(
                f"full-verification-{index}.log",
                output,
                role="verification",
                pointer_root=runtime_root / "pointers",
            )
        )
    return tuple(inputs)


def _complete_receipt(
    *,
    store: OperationStore,
    parent: object,
    runtime_root: Path,
    worktree: Path,
    profile: object,
    head_sha: str,
    command_runner: Callable[..., object],
) -> VerificationAuthority:
    definition_sha256 = str(parent.spec.contract_sha256)
    input_sha256 = verification_input_sha256(
        definition_sha256,
        head_sha,
        profile.sha256,
        VERIFICATION_STEP_SCHEMA_VERSION,
    )
    attempt = VerificationAttempt(
        parent.spec.operation_id,
        profile.name,
        profile.sha256,
        head_sha,
        0,
    )
    child_spec, lane_id, run_id = pipeline_verify_identity(
        parent.spec,
        definition_sha256=definition_sha256,
        input_sha256=input_sha256,
        profile=profile.name,
    )
    receipt_path = (
        runtime_root
        / "pipeline-verification"
        / child_spec.operation_id
        / "receipt.json"
    )
    command_ids = tuple(
        f"{profile.name}-{index + 1}"
        for index in range(len(compose_commands(profile)))
    )
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            durable = VerificationAuthority.load(
                receipt_path,
                store=store,
                parent=parent,
                runtime_root=runtime_root,
                expected_definition_sha256=definition_sha256,
                expected_profile=profile.name,
                expected_profile_sha256=profile.sha256,
                expected_head_sha=head_sha,
                allowed_statuses=("complete", "failed"),
                expected_command_ids=command_ids,
                child_states=("complete", "failed"),
                require_released=True,
                require_effect_succeeded=True,
            )
        except VerificationAuthorityError as exc:
            raise AuthorizedContinuationError(
                "authorized continuation verification receipt is invalid"
            ) from exc
        if durable.child.state != durable.status:
            raise AuthorizedContinuationError(
                "authorized continuation verification state is invalid"
            )
        if durable.status == "failed":
            _fail_with_attention_handoff(
                durable,
                worktree=worktree,
                runtime_root=runtime_root,
                receipt_path=receipt_path,
            )
        return durable
    child = store.create(child_spec, lane_id=lane_id, run_id=run_id)
    supervisor = OperationSupervisor(
        store, parent.spec.owner_id, child_spec.operation_id
    )
    supervisor.configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
        token_limit=DEFAULT_TOKEN_LIMIT,
    )
    child = supervisor.read()
    if child.pending_effect:
        raise AuthorizedContinuationError(
            "authorized continuation verification effect is uncertain"
        )
    if child.state == "created":
        for state in ("preflight", "starting", "running", "verifying"):
            supervisor.transition(state)
        supervisor.consume_attempt()
    if supervisor.read().state != "verifying":
        raise AuthorizedContinuationError(
            "authorized continuation verification state is invalid"
        )

    def execute(_record: object) -> tuple[object, ...]:
        evidence = tuple(
            run_profile(
                profile,
                root=worktree,
                evidence_dir=receipt_path.parent / "evidence",
                runner=command_runner,
                pointer_root=runtime_root,
            )
        )
        if _git(worktree, "rev-parse", "HEAD") != head_sha or _git(
            worktree, "status", "--porcelain"
        ):
            raise AuthorizedContinuationError(
                "authorized continuation verification changed the exact candidate"
            )
        return evidence

    def persist(_record: object, evidence: tuple[object, ...]) -> None:
        authority = VerificationAuthority.issue(
            store=store,
            parent=parent,
            runtime_root=runtime_root,
            definition_sha256=definition_sha256,
            input_sha256=input_sha256,
            profile=profile.name,
            profile_sha256=profile.sha256,
            attempt=attempt,
            evidence=evidence,
            expected_command_ids=command_ids,
        )
        _write_once(receipt_path, authority.to_dict())

    effect_id = pipeline_verify_effect_id(input_sha256)
    supervisor.effect(effect_id, execute, persist_result=persist)
    authority = VerificationAuthority.load(
        receipt_path,
        store=store,
        parent=parent,
        runtime_root=runtime_root,
        expected_definition_sha256=definition_sha256,
        expected_profile=profile.name,
        expected_profile_sha256=profile.sha256,
        expected_head_sha=head_sha,
        allowed_statuses=("complete", "failed"),
        expected_command_ids=command_ids,
        require_effect_succeeded=True,
    )
    if authority.status != "complete":
        supervisor.transition("failed")
        _fail_with_attention_handoff(
            authority,
            worktree=worktree,
            runtime_root=runtime_root,
            receipt_path=receipt_path,
        )
    for state in ("finalizing", "exiting", "complete"):
        supervisor.transition(state)
    return VerificationAuthority.load(
        receipt_path,
        store=store,
        parent=parent,
        runtime_root=runtime_root,
        expected_definition_sha256=definition_sha256,
        expected_profile=profile.name,
        expected_profile_sha256=profile.sha256,
        expected_head_sha=head_sha,
        allowed_statuses=("complete",),
        expected_command_ids=command_ids,
        child_states=("complete",),
        require_released=True,
        require_effect_succeeded=True,
    )


def run_authorized_continuation(
    worktree: Path,
    *,
    authorization_escalation_id: str,
    expected_head: str,
    verification_profile: str,
    verification_profile_sha256: str,
    outcome_contract_sha256: str,
    verification_runner: Callable[..., object] = subprocess.run,
    review_driver: Callable[..., dict[str, Any]],
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Issue/reuse one exact receipt and drive its one fresh review cycle."""

    root = worktree.expanduser().resolve()
    if (
        not IDENTIFIER.fullmatch(authorization_escalation_id)
        or not GIT_OID.fullmatch(expected_head)
        or not SHA256.fullmatch(verification_profile_sha256)
        or not SHA256.fullmatch(outcome_contract_sha256)
        or verification_profile != "full"
    ):
        raise AuthorizedContinuationError(
            "authorized continuation arguments are invalid"
        )
    meta, vault, task_id = _validate_task(root)
    if _git(root, "rev-parse", "HEAD") != expected_head or _git(
        root, "status", "--porcelain"
    ):
        raise AuthorizedContinuationError(
            "authorized continuation requires its exact clean HEAD"
        )
    try:
        records = load_chain(root)
        resolution, successor_resolution, predecessor_head = _resolved_authorization(
            records,
            escalation_id=authorization_escalation_id,
            outcome_sha256=outcome_contract_sha256,
        )
        plan = resolve_plan_authority(meta, root)
    except (EscalationRecordError, PlanAuthorityError) as exc:
        raise AuthorizedContinuationError(
            "authorized continuation authority is invalid"
        ) from exc
    if (
        plan.outcome_sha256 != outcome_contract_sha256
        or not plan.amendments
        or plan.amendments[-1].payload.get("new_outcome_sha256")
        != outcome_contract_sha256
    ):
        raise AuthorizedContinuationError(
            "authorized continuation amendment is not exact"
        )
    if _git(root, "rev-parse", "HEAD^") != predecessor_head:
        raise AuthorizedContinuationError(
            "authorized continuation is not the single mechanism successor"
        )
    amendment = plan.amendments[-1]
    profiles = load_profiles(vault / "config/verification-profiles.toml")
    profile = profiles.get(verification_profile)
    if profile is None or profile.sha256 != verification_profile_sha256:
        raise AuthorizedContinuationError(
            "authorized continuation verification profile is stale"
        )
    store = OperationStore(vault / ".vault-meta/harness")
    from task_review_flow import _dispatch_record_for_task

    parent = _dispatch_record_for_task(store, task_id)
    if parent is None:
        raise AuthorizedContinuationError(
            "authorized continuation dispatch parent is unavailable"
        )
    identity = _binding_identity(
        task_id=task_id,
        resolution=resolution,
        successor_resolution=successor_resolution,
        amendment=amendment,
        head_sha=expected_head,
        profile_sha256=profile.sha256,
        outcome_sha256=outcome_contract_sha256,
    )
    verification_root = (
        vault
        / ".vault-meta/harness/owners"
        / parent.spec.owner_id
        / "runtime"
        / task_id
        / "authorized-continuations"
        / identity
    ).resolve()
    review_root = (
        vault
        / ".vault-meta/harness/review-runtime"
        / task_id
        / "authorized-continuations"
        / identity
    ).resolve()
    authority = _complete_receipt(
        store=store,
        parent=parent,
        runtime_root=verification_root,
        worktree=root,
        profile=profile,
        head_sha=expected_head,
        command_runner=verification_runner,
    )
    receipt_path = (
        verification_root
        / "pipeline-verification"
        / authority.operation_id
        / "receipt.json"
    )
    admission = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": authority.operation_id,
        "verification_lane_id": authority.lane_id,
        "verification_run_id": authority.run_id,
        "receipt_sha256": authority.receipt_sha256,
        "receipt_pointer": str(receipt_path),
        "head_sha": expected_head,
        "status": "admitted",
    }
    _write_once(review_root / "review-launch-admission.json", admission)
    binding = {
        "schema_version": 1,
        "task_id": task_id,
        "authorization_escalation_id": authorization_escalation_id,
        "authorization_resolution_id": resolution.record_id,
        "authorization_resolution_sha256": resolution.sha256,
        "successor_resolution_id": successor_resolution.record_id,
        "successor_resolution_sha256": successor_resolution.sha256,
        "predecessor_head_sha": predecessor_head,
        "amendment_record_id": amendment.record_id,
        "amendment_record_sha256": amendment.sha256,
        "head_sha": expected_head,
        "verification_profile": profile.name,
        "verification_profile_sha256": profile.sha256,
        "outcome_contract_sha256": outcome_contract_sha256,
        "verification_runtime_root": str(verification_root),
        "review_runtime_root": str(review_root),
        "receipt_pointer": str(receipt_path),
        "receipt_sha256": authority.receipt_sha256,
        "binding_id": identity,
        "status": "prepared",
    }
    _write_once(root / BINDING_NAME, binding)
    rebound = _rebound_meta(
        meta, profile=profile.name, profile_sha256=profile.sha256
    )
    rebound["authorized_review_continuation"] = binding
    return review_driver(
        rebound,
        vault,
        root,
        task_id,
        review_root,
        runtime_manager=runtime_manager,
    )


__all__ = [
    "AuthorizedContinuationError",
    "run_authorized_continuation",
]
