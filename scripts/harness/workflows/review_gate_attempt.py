"""Exact-HEAD ReviewAttempt compilation and one-round gate decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from ..finalization_ledger import (
    MAX_FINALIZATION_CYCLES,
    predecessor_bound_attempt_id,
)
from ..pre_model_reviewer_retirement import (
    review_attempt_records_are_quiescent,
)
from ..store import StoreError
from ..review_attempt import (
    EXACT_HEAD_REVIEW_PROTOCOL,
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptIdentity,
    ReviewAttemptLaneIdentity,
    ReviewAttemptLaneResult,
    ReviewAttemptPolicy,
    ReviewAttemptTerminal,
    ReviewAttemptTerminalResult,
)
from .review import (
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    accept_review_round,
    finish_review_lane,
    review_round_envelope,
    review_session_specs,
)
from .review_gate_contracts import (
    ReviewGateDecision,
    ReviewGateRun,
    ReviewPreset,
    _atomic_json,
    _read_json,
    _result_from_payload,
)
from .review_results import namespace_review_result
from review_contract import MATERIAL_SEVERITIES, VERIFY_BUDGETS


def compile_review_attempt_identity(
    *,
    request: ReviewOperationRequest,
    finalization_lineage_id: str,
    cycle: int,
    plan_sha256: str,
    outcome_sha256: str,
) -> ReviewAttemptIdentity:
    """Freeze policy, exact HEAD, routes, and deterministic lanes pre-effect."""

    policy = request.policy
    attempt_policy = ReviewAttemptPolicy(
        depth=policy.depth,
        cross_model=policy.cross_model,
        runtime=policy.runtime,
        model=policy.model,
        effort=policy.effort,
        max_verify_iterations=policy.max_verify_iterations,
        purpose=policy.purpose,
        selected_provider=policy.selected_provider,
    )
    lanes = tuple(
        ReviewAttemptLaneIdentity(
            axis=identity.axis,
            owner_id=identity.spec.owner_id,
            operation_id=identity.spec.operation_id,
            lane_id=identity.lane_id,
            run_id=identity.run_id,
            runtime=identity.spec.route.runtime,
            model=identity.spec.route.model,
            effort=identity.spec.route.effort,
            profile=identity.spec.route.profile,
            routing_sha256=identity.spec.route.routing_sha256,
        )
        for identity in review_session_specs(request)
    )
    return ReviewAttemptIdentity(
        attempt_id=policy.operation_id,
        finalization_lineage_id=finalization_lineage_id,
        cycle=cycle,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
        exact_head_sha=request.context.head_sha,
        policy=attempt_policy,
        lanes=lanes,
    )


class ReviewGateAttemptMixin:
    """Own the new one-HEAD path without invoking legacy continuation code."""

    def recover_late_started_attempt(self) -> bool:
        """Replace one false zero-lane terminal with its exact accepted lane.

        This does not rearm a genuine preflight failure.  It requires the
        runtime's completed late-start receipt, the original one-lane attempt,
        a recovered parent, and the already accepted callback child.
        """

        state = self.read()
        raw_attempt = state.get("attempt")
        if not isinstance(raw_attempt, Mapping):
            return False
        attempt = ReviewAttempt.from_mapping(raw_attempt)
        if attempt.status == "awaiting-callback" and state.get("status") == "reviewing":
            return True
        terminal = attempt.terminal
        stored_lanes = state.get("lanes")
        if (
            attempt.status != "terminal"
            or terminal is None
            or terminal.result != ReviewAttemptTerminalResult.ATTENTION_REQUIRED
            or terminal.lane_results
            or len(attempt.identity.lanes) != 1
            or state.get("status") != "attention-required"
            or state.get("owner_id") != attempt.identity.lanes[0].owner_id
            or state.get("active_review_operation_id")
            != attempt.identity.attempt_id
            or not isinstance(stored_lanes, list)
            or len(stored_lanes) > 1
            or any(
                state.get(field) not in ({}, None)
                for field in ("round_results", "final_results", "evidence")
            )
        ):
            return False
        lane = attempt.identity.lanes[0]
        try:
            parent = self.round_store.read(lane.owner_id, lane.operation_id)
            children = [
                record
                for record in self.round_store.list(lane.owner_id)
                if record.spec.parent_operation_id == lane.operation_id
                and record.spec.kind == "review-round"
                and record.lane_id == lane.lane_id
            ]
        except StoreError:
            return False
        receipt_path = (
            self.round_store.root
            / "owners"
            / lane.owner_id
            / "runtime"
            / lane.operation_id
            / "late-start-recovery.json"
        )
        callback_attention_path = receipt_path.with_name(
            "callback-submit-attention.json"
        )
        ready_path = receipt_path.with_name("ready.json")
        try:
            receipt = _read_json(receipt_path)
            ready = _read_json(ready_path)
        except (OSError, ValueError):
            return False
        if len(children) != 1:
            return False
        child = children[0]
        late_callback_attention = False
        if parent.state == "attention-required":
            try:
                callback_attention = _read_json(callback_attention_path)
            except (OSError, ValueError):
                return False
            late_callback_attention = (
                parent.resume_state == "awaiting-callback"
                and str(parent.attention_reason.value) == "attention-required"
                and callback_attention.get("schema_version") == 1
                and callback_attention.get("status") == "attention-required"
                and callback_attention.get("reason")
                == "callback-submit-stale-generation"
                and callback_attention.get("operation_id") == lane.operation_id
                and callback_attention.get("run_id") == lane.run_id
            )
        live_recovered = (
            parent.state in {"awaiting-callback", "attention-required"}
            and (
                parent.state != "attention-required"
                or late_callback_attention
            )
            and parent.effect_id == "start-provider"
            and str(parent.effect_outcome.value) == "succeeded"
            and bool(parent.resources.surface_id)
            and parent.resources.process_group > 1
            and parent.resources.supervisor_pid > 1
            and receipt.get("surface_id") == parent.resources.surface_id
            and receipt.get("process_identity")
            == parent.resources.process_identity
            and receipt.get("supervisor_identity")
            == parent.resources.supervisor_identity
        )
        post_cleanup = (
            parent.state == "complete"
            and parent.effect_id == "request-exit"
            and str(parent.effect_outcome.value) == "succeeded"
            and not parent.resources.surface_id
            and parent.resources.process_group == 0
            and parent.resources.supervisor_pid == 0
            and ready.get("schema_version") == 1
            and ready.get("status") == "ready"
            and receipt.get("process_identity") == ready.get("process_identity")
            and receipt.get("supervisor_identity")
            == ready.get("supervisor_identity")
        )
        exact_stored_lane = (
            len(stored_lanes) == 1
            and isinstance(stored_lanes[0], dict)
            and stored_lanes[0].get("axis") == lane.axis
            and stored_lanes[0].get("operation_id") == lane.operation_id
            and stored_lanes[0].get("lane_id") == lane.lane_id
            and stored_lanes[0].get("run_id") == lane.run_id
            and stored_lanes[0].get("surface_id") == receipt.get("surface_id")
        )
        stored_lane_matches = (
            stored_lanes == []
            or exact_stored_lane
        )
        if (
            parent.spec.route.runtime != lane.runtime
            or parent.spec.route.model != lane.model
            or parent.spec.route.effort != lane.effort
            or parent.spec.route.profile != lane.profile
            or parent.spec.route.routing_sha256 != lane.routing_sha256
            or parent.run_id != lane.run_id
            or parent.pending_effect
            or not (live_recovered or post_cleanup)
            or not stored_lane_matches
            or child.state not in {"verifying", "finalizing", "exiting", "complete"}
            or child.pending_effect
            or child.accepted_callback_kind != "review"
            or not child.accepted_callback_id
            or re.fullmatch(r"[0-9a-f]{64}", child.accepted_callback_sha256)
            is None
            or receipt.get("schema_version") != 1
            or receipt.get("status") != "complete"
            or receipt.get("owner_id") != lane.owner_id
            or receipt.get("parent_operation_id") != lane.operation_id
            or receipt.get("parent_run_id") != lane.run_id
            or receipt.get("child_operation_id") != child.spec.operation_id
            or receipt.get("child_run_id") != child.run_id
        ):
            return False
        raw_lane = (
            {
                **stored_lanes[0],
                "surface_id": "",
                "state": "complete",
            }
            if post_cleanup and stored_lanes
            else {
                "axis": lane.axis,
                "operation_id": lane.operation_id,
                "lane_id": lane.lane_id,
                "run_id": lane.run_id,
                "surface_id": parent.resources.surface_id,
                "checkpoint": "",
                "verification_iteration": lane.verification_iteration,
                "state": parent.state,
            }
        )
        recovered = ReviewAttempt(attempt.identity, "awaiting-callback")
        with self._locked():
            current = _read_json(self.state_path)
            if current != state:
                raise ReviewAttemptError(
                    "late started review gate changed during recovery"
                )
            current.update(
                status="reviewing",
                lanes=[raw_lane],
                attempt=recovered.payload(),
            )
            _atomic_json(self.state_path, current)
        return True

    def _attempt(self) -> ReviewAttempt:
        raw = self.read().get("attempt")
        if not isinstance(raw, Mapping):
            raise ReviewAttemptError(
                "legacy-cross-head-resume-disabled"
            )
        return ReviewAttempt.from_mapping(raw)

    @staticmethod
    def _attempt_lane(
        identity: ReviewAttemptIdentity, lane: ReviewLaneSession
    ) -> ReviewAttemptLaneIdentity:
        matches = tuple(
            item for item in identity.lanes if item.axis == lane.axis
        )
        if len(matches) != 1:
            raise ReviewAttemptError("review attempt lane is not frozen")
        expected = matches[0]
        route = lane.spec.route
        observed = ReviewAttemptLaneIdentity(
            axis=lane.axis,
            owner_id=lane.owner_id,
            operation_id=lane.operation_id,
            lane_id=lane.lane_id,
            run_id=lane.run_id,
            runtime=route.runtime,
            model=route.model,
            effort=route.effort,
            profile=route.profile,
            routing_sha256=route.routing_sha256,
            verification_iteration=lane.verification_iteration,
        )
        if observed != expected:
            raise ReviewAttemptError("review attempt lane identity changed")
        return expected

    def _initialize_attempt(
        self,
        *,
        dispatch_operation_id: str,
        request: ReviewOperationRequest,
        product_root: Path,
        identity: ReviewAttemptIdentity,
        runtime_root: Path | None = None,
        callback_root: str = "",
    ) -> ReviewAttempt:
        preset = ReviewPreset(
            depth=request.policy.depth,
            cross_model=request.policy.cross_model,
            runtime=request.policy.runtime, model=request.policy.model,
            effort=request.policy.effort,
        )
        expected_budget = (
            0
            if request.policy.purpose == "release"
            else min(VERIFY_BUDGETS[request.requested_mode],
                     1 if request.policy.purpose == "intent" else 2)
        )
        if request.policy.max_verify_iterations != expected_budget:
            raise ReviewAttemptError(
                "automatic review must use the deterministic preset budget"
            )
        attempt = ReviewAttempt.pending(identity)
        initial = {
            "schema_version": 1,
            "execution_protocol": EXACT_HEAD_REVIEW_PROTOCOL,
            "dispatch_operation_id": dispatch_operation_id,
            "owner_id": request.owner_id,
            "status": "pending",
            "policy": self._policy(
                preset,
                purpose=request.policy.purpose,
                max_verify_iterations=request.policy.max_verify_iterations,
            ),
            "product_root": str(product_root),
            "active_review_operation_id": request.policy.operation_id,
            "context": self._context(request.context),
            "topology": request.topology.payload(),
            "topology_sha256": request.topology_sha256,
            "lanes": [],
            "round_results": {},
            "final_results": {},
            "evidence": {},
            "attempt": attempt.payload(),
        }
        with self._locked():
            if self.state_path.exists():
                current = _read_json(self.state_path)
                raw_attempt = current.get("attempt")
                if not isinstance(raw_attempt, Mapping):
                    raise ReviewAttemptError(
                        "legacy-cross-head-resume-disabled"
                    )
                existing = ReviewAttempt.from_mapping(raw_attempt)
                normal_next = (
                    existing.status == "terminal"
                    and identity.cycle == existing.identity.cycle + 1
                    and identity.cycle <= MAX_FINALIZATION_CYCLES
                )
                retry_next = False
                if (
                    existing.status == "terminal"
                    and 1 <= identity.cycle <= MAX_FINALIZATION_CYCLES
                ):
                    try:
                        retry_next = identity.attempt_id == (
                            predecessor_bound_attempt_id(
                                lineage_id=(
                                    existing.identity.finalization_lineage_id
                                ),
                                predecessor_attempt_id=(
                                    existing.identity.attempt_id
                                ),
                                exact_head=identity.exact_head_sha,
                                cycle_number=identity.cycle,
                            )
                        )
                    except ValueError:
                        retry_next = False
                if existing.status == "terminal" and (
                    normal_next or retry_next
                ):
                    terminal = existing.terminal
                    zero_effect_boundary = (
                        terminal is not None
                        and terminal.result
                        == ReviewAttemptTerminalResult.ATTENTION_REQUIRED
                        and not terminal.lane_results
                        and current.get("status") == "attention-required"
                        and current.get("active_review_operation_id")
                        == existing.identity.attempt_id
                        and current.get("dispatch_operation_id") == dispatch_operation_id
                        and current.get("product_root") == str(product_root)
                        and current.get("lanes") == []
                        and all(current.get(field) in ({}, None) for field in (
                            "round_results", "final_results", "evidence",
                            "resolution_evidence", "continuation_effects",
                            "review_notification_evidence", "awaiting_resolution",
                            "finalizing_recovery",
                        ))
                        and review_attempt_records_are_quiescent(
                            self.round_store, existing
                        )
                    )
                    effectful_mechanism_boundary = (
                        terminal is not None
                        and terminal.result
                        in {
                            ReviewAttemptTerminalResult.ATTENTION_REQUIRED,
                            ReviewAttemptTerminalResult.BLOCKED,
                        }
                        and isinstance(current.get("lanes"), list)
                        and bool(current.get("lanes"))
                        and existing.identity.exact_head_sha
                        != identity.exact_head_sha
                        and current.get("status") == terminal.result.value
                        and current.get("active_review_operation_id")
                        == existing.identity.attempt_id
                        and current.get("dispatch_operation_id")
                        == dispatch_operation_id
                        and current.get("product_root") == str(product_root)
                    )
                    retry_callbacks_absent = (
                        runtime_root is not None
                        and bool(callback_root)
                        and not any(
                            (
                                runtime_root
                                / callback_root
                                / lane.axis
                                / ".review-callback.json"
                            ).exists()
                            or (
                                runtime_root
                                / callback_root
                                / lane.axis
                                / ".review-callback.json"
                            ).is_symlink()
                            for lane in existing.identity.lanes
                        )
                    )
                    if (
                        terminal is None
                        or existing.identity.finalization_lineage_id
                        != identity.finalization_lineage_id
                        or existing.identity.plan_sha256 != identity.plan_sha256
                        or existing.identity.outcome_sha256 != identity.outcome_sha256
                        or (
                            retry_next
                            and not (
                                (
                                    zero_effect_boundary
                                    and retry_callbacks_absent
                                )
                                or effectful_mechanism_boundary
                            )
                        )
                        or (
                            existing.identity.exact_head_sha
                            == identity.exact_head_sha
                            and normal_next
                        )
                    ):
                        raise ReviewAttemptError(
                            "next review attempt lacks a changed-HEAD terminal boundary"
                        )
                    archive_name = (
                        f"attempt-{existing.identity.attempt_id}.json"
                        if retry_next
                        else f"cycle-{existing.identity.cycle}.json"
                    )
                    archive = self.root / "attempts" / archive_name
                    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    archive.parent.chmod(0o700)
                    encoded = (
                        json.dumps(
                            current,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                    try:
                        descriptor = os.open(
                            archive,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                    except FileExistsError:
                        if archive.read_bytes() != encoded:
                            raise ReviewAttemptError(
                                "archived review attempt changed"
                            ) from None
                    else:
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(encoded)
                            handle.flush()
                            os.fsync(handle.fileno())
                    self._archive_prior_review_input(
                        attempt=existing,
                        runtime_root=runtime_root,
                        callback_root=callback_root,
                    )
                    _atomic_json(self.state_path, initial)
                    return attempt
                existing.assert_identity(identity)
                if existing.status != "pending":
                    raise ReviewAttemptError(
                        "review attempt cannot start or rearm twice"
                    )
                if (
                    current.get("status") != "pending"
                    or current.get("lanes") != []
                    or current.get("round_results") != {}
                    or current.get("final_results") != {}
                    or current.get("evidence") != {}
                ):
                    raise ReviewAttemptError(
                        "pending review attempt gate contains execution state"
                    )
                for field in (
                    "schema_version",
                    "execution_protocol",
                    "dispatch_operation_id",
                    "owner_id",
                    "policy",
                    "product_root",
                    "active_review_operation_id",
                    "context",
                    "topology",
                    "topology_sha256",
                ):
                    if current.get(field) != initial[field]:
                        raise ReviewAttemptError(
                            "review attempt gate identity changed"
                        )
                return existing
            _atomic_json(self.state_path, initial)
        return attempt

    def _archive_prior_review_input(
        self,
        *,
        attempt: ReviewAttempt,
        runtime_root: Path | None,
        callback_root: str,
    ) -> None:
        """Retire model-writable scratch using only prior round identity."""

        if attempt.status != "terminal" or attempt.terminal is None:
            raise ReviewAttemptError(
                "review input rollover requires a terminal attempt"
            )
        if runtime_root is None or not callback_root:
            raise ReviewAttemptError(
                "review input rollover authority is unavailable"
            )
        runtime = runtime_root.expanduser().resolve()
        relative = Path(callback_root)
        if (
            relative.is_absolute()
            or not relative.parts
            or relative == Path(".")
            or ".." in relative.parts
        ):
            raise ReviewAttemptError("review input rollover path is invalid")
        callbacks = runtime / relative
        current = runtime
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ReviewAttemptError(
                    "review input rollover path is invalid"
                )
        try:
            callbacks.resolve(strict=False).relative_to(runtime)
        except (OSError, ValueError) as exc:
            raise ReviewAttemptError(
                "review input rollover path is invalid"
            ) from exc
        archive = (
            self.root
            / "attempts"
            / f"attempt-{attempt.identity.attempt_id}-review-input"
        )
        if archive.is_symlink() or (
            archive.exists() and not archive.is_dir()
        ):
            raise ReviewAttemptError(
                "review input rollover archive is invalid"
            )
        moves: list[tuple[Path, Path]] = []
        for lane in attempt.identity.lanes:
            axis_root = callbacks / lane.axis
            if axis_root.is_symlink():
                raise ReviewAttemptError(
                    "review input rollover path is invalid"
                )
            live_meta = axis_root / ".review-meta.json"
            live_input = axis_root / ".review-input.json"
            archived_meta = archive / f"{lane.axis}.review-meta.json"
            archived_input = archive / f"{lane.axis}.review-input.json"
            if any(
                left.exists() or left.is_symlink()
                for left in (live_meta, live_input)
            ) and any(
                right.exists() or right.is_symlink()
                for right in (archived_meta, archived_input)
            ):
                # A crash may split the two-file move, but never duplicate one
                # exact artifact across mutable and immutable locations.
                if (
                    (live_meta.exists() or live_meta.is_symlink())
                    and (archived_meta.exists() or archived_meta.is_symlink())
                ) or (
                    (live_input.exists() or live_input.is_symlink())
                    and (archived_input.exists() or archived_input.is_symlink())
                ):
                    raise ReviewAttemptError(
                        "review input rollover is ambiguous"
                    )
            meta_path = (
                live_meta
                if live_meta.exists() or live_meta.is_symlink()
                else archived_meta
            )
            input_path = (
                live_input
                if live_input.exists() or live_input.is_symlink()
                else archived_input
            )
            meta_present = meta_path.exists() or meta_path.is_symlink()
            input_present = input_path.exists() or input_path.is_symlink()
            if not meta_present and not input_present:
                continue
            if not meta_present:
                raise ReviewAttemptError(
                    "review input rollover metadata is unavailable"
                )
            if meta_path.is_symlink() or not meta_path.is_file():
                raise ReviewAttemptError(
                    "review input rollover metadata changed"
                )
            try:
                meta = json.loads(meta_path.read_bytes())
            except (OSError, json.JSONDecodeError) as exc:
                raise ReviewAttemptError(
                    "review input rollover metadata changed"
                ) from exc
            try:
                children = [
                    row
                    for row in self.round_store.list(lane.owner_id)
                    if row.spec.kind == "review-round"
                    and row.spec.parent_operation_id == lane.operation_id
                    and row.lane_id == lane.lane_id
                    and row.spec.operation_id == meta.get("operation_id")
                    and row.run_id == meta.get("run_id")
                ]
            except (AttributeError, StoreError) as exc:
                raise ReviewAttemptError(
                    "review input rollover round authority is unavailable"
                ) from exc
            if (
                not isinstance(meta, dict)
                or set(meta) != {
                    "schema_version",
                    "transport",
                    "operation_id",
                    "run_id",
                    "review_id",
                    "parent_session_operation_id",
                    "review_mode",
                    "axis",
                    "verification_iteration",
                    "started_at",
                    "worktree",
                    "task_name",
                    "head_sha",
                    "review_purpose",
                    "review_boundary_input_sha256",
                    "verification_profile",
                    "route",
                }
                or meta.get("schema_version") != 1
                or meta.get("transport") != "review-round"
                or meta.get("axis") != lane.axis
                or meta.get("parent_session_operation_id")
                != lane.operation_id
                or meta.get("head_sha") != attempt.identity.exact_head_sha
                or meta.get("verification_iteration") != 0
                or len(children) != 1
            ):
                raise ReviewAttemptError(
                    "review input rollover metadata changed"
                )
            if input_present:
                if input_path.is_symlink() or not input_path.is_file():
                    raise ReviewAttemptError(
                        "review input rollover scratch changed"
                    )
                try:
                    value = json.loads(input_path.read_bytes())
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReviewAttemptError(
                        "review input rollover scratch changed"
                    ) from exc
                if (
                    not isinstance(value, dict)
                    or set(value)
                    != {
                        "schema_version",
                        "axis",
                        "verdict",
                        "verification_iteration",
                        "findings",
                    }
                    or value.get("schema_version") != 1
                    or value.get("axis") != lane.axis
                    or value.get("verification_iteration") != 0
                ):
                    raise ReviewAttemptError(
                        "review input rollover scratch changed"
                    )
            if live_meta.exists():
                moves.append((live_meta, archived_meta))
            if live_input.exists():
                moves.append((live_input, archived_input))
        if moves:
            archive.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive.chmod(0o700)
        for source, destination in moves:
            source.replace(destination)

    def begin_attempt(
        self,
        *,
        dispatch_operation_id: str,
        finalization_lineage_id: str,
        cycle: int,
        plan_sha256: str,
        outcome_sha256: str,
        request: ReviewOperationRequest,
        origin_surface: str,
        cwd: Path,
        product_root: Path,
        prompt_pointer: str,
        callback_root: str,
        callback_wake: str = "",
        prompt_pointers: Mapping[str, str] | None = None,
        prepare_lane: (
            Callable[[str, object, object, ReviewRound], None] | None
        ) = None,
    ) -> ReviewGateRun:
        """Start exactly one immutable attempt after all identity checks."""

        product_root = product_root.expanduser().resolve()
        identity = compile_review_attempt_identity(
            request=request,
            finalization_lineage_id=finalization_lineage_id,
            cycle=cycle,
            plan_sha256=plan_sha256,
            outcome_sha256=outcome_sha256,
        )
        attempt = self._initialize_attempt(
            dispatch_operation_id=dispatch_operation_id,
            request=request,
            product_root=product_root,
            identity=identity,
            runtime_root=cwd,
            callback_root=callback_root,
        )
        return self._start_execution(
            request=request,
            origin_surface=origin_surface,
            cwd=cwd,
            product_root=product_root,
            prompt_pointer=prompt_pointer,
            callback_root=callback_root,
            callback_wake=callback_wake,
            prompt_pointers=prompt_pointers,
            prepare_lane=prepare_lane,
            attempt=attempt,
        )

    def rehydrate_attempt(self) -> ReviewGateRun:
        """Rehydrate iteration zero without resurrecting a reviewer effect."""

        attempt = self._attempt()
        state = self.read()
        raw_lanes = state.get("lanes")
        if (
            attempt.status not in {"awaiting-callback", "terminal"}
            or not isinstance(raw_lanes, list)
            or not raw_lanes
        ):
            raise ReviewAttemptError("review attempt cannot be rehydrated")
        checkpointless_axes: set[str] = set()
        for raw_lane in raw_lanes:
            if (
                not isinstance(raw_lane, dict)
                or raw_lane.get("verification_iteration") != 0
            ):
                raise ReviewAttemptError(
                    "review attempt checkpoint cannot be resurrected"
                )
            if (
                attempt.status == "awaiting-callback"
                and not str(raw_lane.get("checkpoint") or "")
            ):
                checkpointless_axes.add(str(raw_lane.get("axis") or ""))
        run = self.rehydrate()
        if run.execution.request.context.head_sha != attempt.identity.exact_head_sha:
            raise ReviewAttemptError("review attempt cannot bind a changed HEAD")
        for lane in run.execution.lanes:
            self._attempt_lane(attempt.identity, lane)
            if lane.axis not in checkpointless_axes:
                continue
            round_ = run.rounds[lane.axis]
            accepted = self.round_store.read(
                round_.owner_id, round_.operation_id
            )
            if (
                accepted.spec != round_.spec
                or accepted.lane_id != lane.lane_id
                or accepted.run_id != round_.run_id
                or accepted.state
                not in {"verifying", "finalizing", "exiting", "complete"}
                or accepted.accepted_callback_kind != "review"
                or not accepted.accepted_callback_id
                or re.fullmatch(
                    r"[0-9a-f]{64}", accepted.accepted_callback_sha256
                )
                is None
            ):
                raise ReviewAttemptError(
                    "review attempt checkpoint cannot be resurrected"
                )
        return run

    @staticmethod
    def _lane_terminal_verdict(result: ReviewResult) -> str:
        material = any(
            finding.severity in MATERIAL_SEVERITIES
            for finding in result.findings
        )
        if result.verdict == "changes-requested" and material:
            return "changes-requested"
        if result.verdict == "blocked":
            return "blocked"
        if result.verdict == "approve" or (
            result.verdict == "changes-requested" and result.findings
        ):
            return "approve"
        raise ReviewAttemptError(
            "review attempt result cannot produce a terminal verdict"
        )

    def _attempt_lane_results(
        self,
        run: ReviewGateRun,
        pointers: Mapping[str, object],
    ) -> tuple[ReviewAttemptLaneResult, ...]:
        results: list[ReviewAttemptLaneResult] = []
        for axis in run.execution.request.policy.axes:
            pointer = pointers.get(axis)
            if not isinstance(pointer, str):
                raise ReviewAttemptError(
                    "review attempt terminal lanes are incomplete"
                )
            path = (self.root / pointer).resolve()
            if self.root not in path.parents or not path.is_file() or path.is_symlink():
                raise ReviewAttemptError(
                    "review attempt terminal result is unavailable"
                )
            result = _result_from_payload(_read_json(path))
            if result.axis != axis or result.verification_iteration != 0:
                raise ReviewAttemptError(
                    "review attempt terminal result identity changed"
                )
            results.append(
                ReviewAttemptLaneResult(
                    axis=axis,
                    verdict=self._lane_terminal_verdict(result),
                    result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    finding_ids=tuple(
                        finding.finding_id for finding in result.findings
                    ),
                )
            )
        return tuple(results)

    @staticmethod
    def _terminal_result(
        lanes: tuple[ReviewAttemptLaneResult, ...]
    ) -> ReviewAttemptTerminalResult:
        verdicts = {lane.verdict for lane in lanes}
        if "blocked" in verdicts:
            return ReviewAttemptTerminalResult.BLOCKED
        if "changes-requested" in verdicts:
            return ReviewAttemptTerminalResult.CHANGES_REQUESTED
        return ReviewAttemptTerminalResult.APPROVED

    def _attempt_notification_evidence(
        self,
        run: ReviewGateRun,
        pointers: Mapping[str, object],
    ) -> dict[str, object]:
        """Freeze accepted callback evidence for executor notification only."""

        evidence_by_axis: dict[str, object] = {}
        material_count = 0
        for lane in run.execution.lanes:
            pointer = pointers.get(lane.axis)
            if not isinstance(pointer, str) or not pointer:
                raise ReviewAttemptError(
                    "review attempt notification result is unavailable"
                )
            path = (self.root / pointer).resolve()
            if self.root not in path.parents or not path.is_file() or path.is_symlink():
                raise ReviewAttemptError(
                    "review attempt notification result is unavailable"
                )
            result = _result_from_payload(_read_json(path))
            qualified = namespace_review_result(
                run.execution.request.policy, result
            )
            material_ids = [
                finding.finding_id
                for finding in qualified.findings
                if finding.severity in MATERIAL_SEVERITIES
            ]
            material_count += len(material_ids)
            round_ = run.rounds[lane.axis]
            envelope = review_round_envelope(round_, result)
            accepted = self.round_store.read(
                round_.owner_id, round_.operation_id
            )
            if (
                accepted.spec != round_.spec
                or accepted.lane_id != round_.lane_id
                or accepted.run_id != round_.run_id
                or accepted.state != "complete"
                or accepted.pending_effect
                or accepted.accepted_callback_id != envelope.callback_id
                or accepted.accepted_callback_kind != envelope.kind
                or accepted.accepted_callback_sha256
                != envelope.payload_sha256
            ):
                raise ReviewAttemptError(
                    "review attempt notification callback identity changed"
                )
            evidence_by_axis[lane.axis] = {
                "pointer": pointer,
                "reviewed_head_sha": (
                    run.execution.request.context.head_sha
                ),
                "review_operation_id": (
                    run.execution.request.policy.operation_id
                ),
                "round_operation_id": round_.operation_id,
                "round_run_id": round_.run_id,
                "callback_id": envelope.callback_id,
                "callback_sha256": envelope.payload_sha256,
                "material_finding_ids": material_ids,
            }
        if material_count == 0:
            raise ReviewAttemptError(
                "changes-requested attempt has no material notification"
            )
        return evidence_by_axis

    def complete_attempt_round(
        self,
        run: ReviewGateRun,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        result: ReviewResult,
    ) -> ReviewGateDecision:
        """Accept one initial callback and terminalize after all lanes finish."""

        attempt = self._attempt()
        attempt.assert_identity(attempt.identity)
        if attempt.status != "awaiting-callback":
            raise ReviewAttemptError(
                "review attempt is not accepting an initial callback"
            )
        self._attempt_lane(attempt.identity, lane)
        if (
            run.execution.request.context.head_sha
            != attempt.identity.exact_head_sha
            or result.verification_iteration != 0
            or round_.verification_iteration != 0
        ):
            raise ReviewAttemptError(
                "review attempt rejects changed HEAD or verification iteration"
            )
        terminal_verdict = self._lane_terminal_verdict(result)
        envelope = review_round_envelope(round_, result)
        cleanup = accept_review_round(
            self.runtime, self.round_store, lane, round_, envelope
        )
        if cleanup is None:
            cleanup = finish_review_lane(self.runtime, lane)
        pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            result,
            final=False,
        )
        if cleanup.state != "complete":
            terminal = ReviewAttemptTerminal(
                ReviewAttemptTerminalResult.ATTENTION_REQUIRED,
                attempt.identity.exact_head_sha,
                (),
            )
            finished = attempt.finish(attempt.identity, terminal)
            self._replace(
                status="attention-required", attempt=finished.payload()
            )
            return ReviewGateDecision("attention-required", lane, round_)
        state = self.read()
        stored_lanes = []
        for item in state.get("lanes", []):
            if not isinstance(item, dict):
                raise ReviewAttemptError("review attempt lanes are invalid")
            stored_lanes.append(
                self._lane(
                    replace(
                        lane,
                        state="complete",
                        surface_id="",
                        checkpoint="",
                        checkpoint_sha256="",
                    )
                )
                if item.get("axis") == lane.axis
                else item
            )
        normalized = replace(result, verdict=terminal_verdict)
        final_pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            normalized,
            final=True,
        )
        rounds = dict(state.get("round_results") or {})
        rounds[result.axis] = pointer
        finals = dict(state.get("final_results") or {})
        finals[result.axis] = final_pointer
        self._replace(
            status="reviewing",
            lanes=stored_lanes,
            round_results=rounds,
            final_results=finals,
        )
        state = self.read()
        pointers = state.get("final_results")
        if not isinstance(pointers, dict) or set(pointers) != set(
            run.execution.request.policy.axes
        ):
            return ReviewGateDecision("awaiting-axes", lane, round_)
        lane_results = self._attempt_lane_results(run, pointers)
        terminal_result = self._terminal_result(lane_results)
        terminal = ReviewAttemptTerminal(
            terminal_result,
            attempt.identity.exact_head_sha,
            lane_results,
        )
        if terminal_result == ReviewAttemptTerminalResult.APPROVED:
            prepared = self._approval_artifacts(run.execution)
            if prepared is None:
                terminal = ReviewAttemptTerminal(
                    ReviewAttemptTerminalResult.ATTENTION_REQUIRED,
                    attempt.identity.exact_head_sha,
                    lane_results,
                )
                terminal_result = ReviewAttemptTerminalResult.ATTENTION_REQUIRED
                evidence_path = None
                evidence: dict[str, object] = {}
            else:
                evidence_path, evidence = prepared
        else:
            evidence_path = None
            evidence = {}
        finished = attempt.finish(attempt.identity, terminal)
        updates: dict[str, object] = {
            "status": terminal_result.value,
            "attempt": finished.payload(),
        }
        if terminal_result == ReviewAttemptTerminalResult.APPROVED:
            updates.update(
                context=self._context(run.execution.request.context),
                evidence=evidence,
            )
        elif terminal_result == ReviewAttemptTerminalResult.CHANGES_REQUESTED:
            updates["review_notification_evidence"] = (
                self._attempt_notification_evidence(run, rounds)
            )
        self._replace(**updates)
        return ReviewGateDecision(
            terminal_result.value,
            lane,
            round_,
            evidence_path=evidence_path,
        )


__all__ = ("ReviewGateAttemptMixin", "compile_review_attempt_identity")
