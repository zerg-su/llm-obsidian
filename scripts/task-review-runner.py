#!/usr/bin/env python3
"""Drive the automatic review gate for one exact v3 dispatch worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

from harness.context import ContextBuilder, ContextInput
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    RuntimeRoute,
    to_dict,
)
from harness.runtime_sessions import RuntimeSessionManager
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError
from harness.verification import load_profiles
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    review_session_specs,
    runtime_status_is_live,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewPreset,
)
from model_routing import (
    load_config,
    resolve,
    routing_from_environment,
    session_from_meta,
)
from task_contract import normalize


class TaskReviewError(ValueError):
    pass


class ActiveReviewRound(NamedTuple):
    run: ReviewGateRun
    lane: object
    round: ReviewRound


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(value, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(value)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError(f"{label} is unavailable") from exc
    if not isinstance(value, dict):
        raise TaskReviewError(f"{label} must be an object")
    return value


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise TaskReviewError("cannot resolve the exact product revision")
    return result.stdout.strip()


def _bounded_input(
    name: str,
    source: Path,
    *,
    role: str,
    pointer_root: Path,
) -> ContextInput:
    raw = source.read_bytes()
    if len(raw) <= 65_536:
        return ContextInput(name, str(source), raw, role=role)
    pointer = pointer_root / name
    _atomic_bytes(pointer, raw)
    return ContextInput.pointer(
        name,
        str(pointer),
        byte_count=len(raw),
        content_sha256=hashlib.sha256(raw).hexdigest(),
        role=role,
    )


def _validate_task(worktree: Path) -> tuple[dict[str, Any], Path, str]:
    worktree = worktree.expanduser().resolve()
    if not worktree.is_dir():
        raise TaskReviewError("task worktree is unavailable")
    meta = _read_json(worktree / ".task-meta.json", "v3 task metadata")
    if meta.get("version") != 3:
        raise TaskReviewError("automatic review requires v3 task metadata")
    normalize(meta)
    try:
        task_id = str(uuid.UUID(str(meta.get("task_id") or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaskReviewError("task identity is invalid") from exc
    if task_id != meta.get("task_id"):
        raise TaskReviewError("task identity must be canonical")
    declared = Path(str(meta.get("worktree") or "")).expanduser()
    if not declared.is_absolute() or declared.resolve() != worktree:
        raise TaskReviewError("task metadata identifies another worktree")
    vault = Path(str(meta.get("vault_root") or "")).expanduser()
    if (
        not vault.is_absolute()
        or not (vault.resolve() / "wiki").is_dir()
        or not (vault.resolve() / "scripts").is_dir()
    ):
        raise TaskReviewError("coordinator vault is unavailable")
    vault = vault.resolve()
    if vault == worktree:
        raise TaskReviewError(
            "coordinator vault and generic product worktree must be separate"
        )
    policy = meta.get("review_policy")
    if not isinstance(policy, dict):
        raise TaskReviewError("review policy is unavailable")
    required = {
        "mode",
        "cross_model",
        "runtime",
        "model",
        "effort",
        "max_verify_iterations",
        "verification_profile",
        "verification_profile_sha256",
        "auto_resolve_severities",
        "escalate_severities",
    }
    if set(policy) != required:
        raise TaskReviewError("v3 review policy fields are not exact")
    mode = str(policy.get("mode") or "")
    budget = policy.get("max_verify_iterations")
    if (
        mode not in {"simple", "deep", "skip"}
        or budget != {"simple": 1, "deep": 2, "skip": 0}[mode]
        or not isinstance(policy.get("cross_model"), bool)
        or not all(
            isinstance(policy.get(field), str)
            for field in (
                "runtime",
                "model",
                "effort",
                "verification_profile",
                "verification_profile_sha256",
            )
        )
    ):
        raise TaskReviewError("v3 review policy values are invalid")
    if mode == "skip" and any(
        (
            policy["cross_model"],
            policy["runtime"],
            policy["model"],
            policy["effort"],
        )
    ):
        raise TaskReviewError("typed review skip cannot carry overrides")
    return meta, vault, task_id


def _runtime_root(vault: Path, task_id: str) -> Path:
    root = (
        vault
        / ".vault-meta"
        / "harness"
        / "review-runtime"
        / task_id
    ).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _current_runtime_root(
    worktree: Path,
    task_id: str,
    scratch_root: Path | None,
) -> Path:
    if scratch_root is None:
        checkout_key = hashlib.sha256(
            str(worktree).encode("utf-8")
        ).hexdigest()[:16]
        base = (
            Path(tempfile.gettempdir())
            / "llm-obsidian-current-review"
            / checkout_key
        )
    else:
        base = scratch_root.expanduser().resolve()
    root = (base / task_id).resolve()
    if root == worktree or worktree in root.parents:
        raise TaskReviewError(
            "current review scratch must stay outside the product checkout"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _gate_root(vault: Path, task_id: str) -> Path:
    return (
        vault
        / ".vault-meta"
        / "harness"
        / "review-data"
        / task_id
        / task_id
    ).resolve()


def _current_review_is_quiescent(vault: Path, task_id: str) -> bool:
    """Permit a fresh current review only after exact old ownership is gone."""

    rows = OperationStore(vault / ".vault-meta" / "harness").list(task_id)
    return bool(rows) and all(
        row.state in TERMINAL
        and not row.resources.surface_id
        and row.resources.process_group == 0
        and row.resources.supervisor_pid == 0
        for row in rows
    )


def _context(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
) -> tuple[ReviewContext, Path]:
    head = _git(worktree, "rev-parse", "HEAD")
    plan = Path(str(meta["plan_file"])).expanduser().resolve()
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
    ]
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
    diff = _git(
        worktree,
        "show",
        "--format=fuller",
        "--stat",
        "--patch",
        "--find-renames",
        "HEAD",
    ).encode()
    if len(diff) > 65_536:
        diff = diff[:65_000] + b"\n[diff truncated; inspect product HEAD]\n"
    inputs.append(
        ContextInput("head-diff.patch", "git:show:HEAD", diff, role="diff")
    )
    builder = ContextBuilder(runtime_root / "packets")
    manifest = builder.build(
        task_id,
        tuple(inputs),
        metadata={
            "task_id": task_id,
            "task_name": str(meta.get("task_name") or ""),
            "head_sha": head,
        },
    )
    manifest_path = (
        runtime_root
        / "packets"
        / manifest.packet_id
        / "manifest.json"
    )
    policy = meta["review_policy"]
    return (
        ReviewContext(
            manifest_path.relative_to(runtime_root).as_posix(),
            head,
            str(policy["verification_profile"]),
            str(policy["verification_profile_sha256"]),
        ),
        manifest_path,
    )


def _route(value: Mapping[str, Any]) -> RuntimeRoute:
    return RuntimeRoute(
        str(value["runtime"]),
        str(value["model"]),
        str(value["effort"]),
        "reviewer-callback",
        str(value["config_sha256"]),
    )


def _request(
    meta: Mapping[str, Any],
    vault: Path,
    task_id: str,
    context: ReviewContext,
) -> tuple[ReviewPreset, ReviewOperationRequest | None]:
    raw = meta["review_policy"]
    preset = ReviewPreset.from_flags(
        deep=raw["mode"] == "deep",
        cross_model=raw["cross_model"],
        runtime=raw["runtime"],
        model=raw["model"],
        effort=raw["effort"],
        no_review=raw["mode"] == "skip",
    )
    if not preset.enabled:
        return preset, None
    config = load_config(vault)
    profiles = load_profiles(vault / "config/verification-profiles.toml")
    profile = profiles.get(context.verification_profile)
    if (
        profile is None
        or profile.sha256 != context.verification_profile_sha256
    ):
        raise TaskReviewError("verification profile binding is stale")
    session = session_from_meta(dict(meta))
    if session is None:
        raise TaskReviewError("v3 task has no captured session route")
    selected = resolve(
        config,
        "review",
        session=session,
        explicit_runtime=raw["runtime"],
        explicit_model=raw["model"],
        explicit_effort=raw["effort"],
        same_model=not raw["cross_model"],
        review_profile=preset.depth,
    )
    primary = _route(selected)
    axis_routes: dict[str, RuntimeRoute] | None = None
    if preset.depth == "deep":
        if any((raw["runtime"], raw["model"], raw["effort"])):
            axis_routes = {axis: primary for axis in preset.request(task_id).axes}
        else:
            axis_routes = {
                "spec": _route(
                    resolve(
                        config,
                        "review",
                        session=session,
                        explicit_runtime="claude",
                        same_model=False,
                        review_profile="deep",
                    )
                ),
                "standards-correctness-architecture-security": _route(
                    resolve(
                        config,
                        "review",
                        session=session,
                        explicit_runtime="codex",
                        same_model=False,
                        review_profile="deep",
                    )
                ),
            }
    return (
        preset,
        ReviewOperationRequest(
            preset.request(task_id),
            task_id,
            primary,
            context,
            axis_routes=axis_routes,
        ),
    )


def _axis_name(axis: str) -> str:
    return (
        "standards"
        if axis == "standards-correctness-architecture-security"
        else axis
    )


def _callback_path(runtime_root: Path, axis: str) -> Path:
    return (
        runtime_root
        / "callbacks"
        / _axis_name(axis)
        / ".review-callback.json"
    )


def _write_round_meta(
    *,
    runtime_root: Path,
    worktree: Path,
    task_id: str,
    depth: str,
    context: ReviewContext,
    lane_operation_id: str,
    round_: ReviewRound,
) -> None:
    directory = _callback_path(runtime_root, round_.axis).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    _atomic_json(
        directory / ".review-meta.json",
        {
            "schema_version": 1,
            "transport": "review-round",
            "operation_id": round_.operation_id,
            "run_id": round_.run_id,
            "review_id": task_id,
            "parent_session_operation_id": lane_operation_id,
            "review_mode": depth,
            "axis": round_.axis,
            "verification_iteration": round_.verification_iteration,
            "worktree": str(worktree),
            "task_name": task_id,
            "head_sha": context.head_sha,
            "verification_profile": {
                "name": context.verification_profile,
                "sha256": context.verification_profile_sha256,
            },
        },
    )


def _prompt(
    *,
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context: ReviewContext,
    axis: str,
    verification: bool,
) -> str:
    name = (
        f"verify-{_axis_name(axis)}.md"
        if verification
        else f"review-{_axis_name(axis)}.md"
    )
    callback_directory = _callback_path(runtime_root, axis).parent
    if callback_directory.exists() and (
        callback_directory.is_symlink()
        or not callback_directory.is_dir()
    ):
        raise TaskReviewError("review callback directory is invalid")
    callback_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    callback_directory.chmod(0o700)
    review_input = callback_directory / ".review-input.json"
    pointer = f"prompts/{name}"
    submit = shlex.join(
        (
            str(Path(sys.executable).resolve()),
            str(vault / "scripts/harness/review_submit.py"),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(_callback_path(runtime_root, axis).parent),
            "--input-file",
            str(review_input),
        )
    )
    _atomic_text(
        runtime_root / pointer,
        "\n".join(
            (
                "# Harness-owned review verification"
                if verification
                else "# Harness-owned review",
                "",
                f"Axis: `{axis}`.",
                f"Exact product HEAD: `{context.head_sha}`.",
                f"Product worktree (read-only): `{worktree}`.",
                f"ContextPacket: `{runtime_root / context.manifest}`.",
                "The review standard and approved plan are inside the ContextPacket.",
                "",
                "Inspect the exact ContextPacket and product HEAD. Do not edit product files.",
                f"Write exactly one review-round JSON to `{review_input}`.",
                "Then submit it through this exact command:",
                "",
                f"`{submit}`",
                "",
            )
        ),
    )
    return pointer


def _envelope(path: Path, round_: ReviewRound) -> tuple[CallbackEnvelope, ReviewResult]:
    raw = _read_json(path, "review callback")
    envelope = CallbackEnvelope(
        callback_id=raw.get("callback_id", ""),
        operation_id=raw.get("operation_id", ""),
        run_id=raw.get("run_id", ""),
        kind=raw.get("kind", ""),
        payload=raw.get("payload", {}),
        payload_sha256=raw.get("payload_sha256", ""),
        schema_version=raw.get("schema_version", 0),
    )
    payload = envelope.payload
    if (
        envelope.operation_id != round_.operation_id
        or envelope.run_id != round_.run_id
        or envelope.kind != "review"
        or payload.get("parent_session_operation_id")
        != round_.parent_operation_id
        or payload.get("axis") != round_.axis
        or payload.get("verification_iteration")
        != round_.verification_iteration
    ):
        raise TaskReviewError("review callback does not match the active round")
    findings = tuple(
        ReviewFinding(
            finding_id=str(item.get("finding_id") or ""),
            axis=round_.axis,
            severity=str(item.get("severity") or ""),
            summary=str(item.get("summary") or ""),
            evidence=str(item.get("evidence") or ""),
            file=str(item.get("file") or ""),
            line=item.get("line"),
            recommendation=str(item.get("recommendation") or ""),
        )
        for item in payload.get("findings", [])
        if isinstance(item, dict)
    )
    if len(findings) != len(payload.get("findings", [])):
        raise TaskReviewError("review callback findings are invalid")
    result = ReviewResult(
        round_.axis,
        str(payload.get("verdict") or ""),
        findings,
        int(payload.get("verification_iteration", -1)),
    )
    return envelope, result


def load_active_round(
    gate_root: Path,
    store: OperationStore,
    runtime_manager: object,
    *,
    axis: str,
) -> ActiveReviewRound:
    run = ReviewGateController(
        gate_root, runtime_manager, store
    ).rehydrate()
    for lane in run.execution.lanes:
        if lane.axis == axis:
            return ActiveReviewRound(run, lane, run.rounds[axis])
    raise TaskReviewError("review axis is not active")


def _receipt(
    *,
    status: str,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context_manifest: Path,
    run: ReviewGateRun | None = None,
) -> dict[str, Any]:
    lanes = []
    if run is not None:
        lanes = [
            {
                "axis": lane.axis,
                "operation_id": lane.operation_id,
                "run_id": lane.run_id,
                "surface_id": lane.surface_id,
                "verification_iteration": lane.verification_iteration,
                "callback_path": str(
                    _callback_path(
                        runtime_root,
                        lane.axis,
                    )
                ),
            }
            for lane in run.execution.lanes
        ]
    return {
        "schema_version": 1,
        "status": status,
        "task_id": meta["task_id"],
        "worktree": str(worktree),
        "vault_root": str(vault),
        "context_manifest": str(context_manifest),
        "lanes": lanes,
    }


def _run_review(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate_root = _gate_root(vault, task_id)
    context, context_manifest = _context(
        meta, vault, worktree, runtime_root, task_id
    )
    preset, request = _request(meta, vault, task_id, context)
    gate = ReviewGateController(gate_root, runtime, store)
    gate_exists = gate.state_path.exists()
    pending_replay = False
    if gate_exists:
        initial_state = gate.read()
        if (
            initial_state.get("status") == "attention-required"
            and initial_state.get("lanes") == []
        ):
            try:
                dispatch_record = store.read(task_id, task_id)
            except StoreError:
                dispatch_record = None
            if (
                dispatch_record is not None
                and dispatch_record.state not in {
                    "attention-required",
                    *TERMINAL,
                }
            ):
                gate.resume_unbound_attention()
                initial_state = gate.read()
        pending_replay = initial_state.get("status") == "pending"
        if pending_replay and initial_state.get("lanes") != []:
            raise TaskReviewError("pending review gate already owns lanes")
    if not gate_exists or pending_replay:
        if not preset.enabled:
            ReviewGateController.skip(
                gate_root,
                dispatch_operation_id=task_id,
                owner_id=task_id,
                preset=preset,
                context=context,
                product_root=worktree,
            )
            return _receipt(
                status="skipped",
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
            )
        if request is None:
            raise TaskReviewError("enabled review has no request")
        if pending_replay and not _pending_replay_is_safe(
            request, store, gate, runtime
        ):
            return _receipt(
                status="attention-required",
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
            )
        prompt_pointers = {
            axis: _prompt(
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context=context,
                axis=axis,
                verification=False,
            )
            for axis in request.policy.axes
        }

        def prepare_lane(
            axis: str,
            _session_request: object,
            _result: object,
            round_: ReviewRound,
        ) -> None:
            _write_round_meta(
                runtime_root=runtime_root,
                worktree=worktree,
                task_id=task_id,
                depth=preset.depth,
                context=context,
                lane_operation_id=round_.parent_operation_id,
                round_=round_,
            )

        try:
            run = gate.begin(
                dispatch_operation_id=task_id,
                request=request,
                origin_surface=str(meta.get("task_surface") or ""),
                cwd=runtime_root,
                product_root=worktree,
                prompt_pointer=prompt_pointers[request.policy.axes[0]],
                prompt_pointers=prompt_pointers,
                callback_root="callbacks",
                prepare_lane=prepare_lane,
            )
        except ValueError:
            if pending_replay and not _pending_replay_is_safe(
                request, store, gate, runtime
            ):
                return _receipt(
                    status="attention-required",
                    meta=meta,
                    vault=vault,
                    worktree=worktree,
                    runtime_root=runtime_root,
                    context_manifest=context_manifest,
                )
            raise
        return _receipt(
            status="reviewing",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )

    state = gate.read()
    status = str(state.get("status") or "")
    stored_lanes = state.get("lanes")
    if (
        status == "attention-required"
        and isinstance(stored_lanes, list)
        and stored_lanes
    ):
        owner_id = str(state.get("owner_id") or "")
        recoverable = bool(owner_id)
        for lane in stored_lanes:
            if not isinstance(lane, dict):
                recoverable = False
                break
            axis = str(lane.get("axis") or "")
            operation_id = str(lane.get("operation_id") or "")
            callback = _callback_path(runtime_root, axis)
            if (
                not axis
                or not operation_id
                or not callback.is_file()
                or callback.is_symlink()
            ):
                recoverable = False
                break
            try:
                record = store.read(owner_id, operation_id)
            except StoreError:
                recoverable = False
                break
            if record.state not in {"awaiting-callback", "verifying"}:
                recoverable = False
                break
        if recoverable:
            gate.resume_bound_attention()
            state = gate.read()
            status = str(state.get("status") or "")
    if status in {"approved", "skipped", "attention-required"}:
        bound = state.get("context")
        if (
            status in {"approved", "skipped"}
            and (
                not isinstance(bound, dict)
                or bound.get("head_sha") != context.head_sha
            )
        ):
            raise TaskReviewError(
                "terminal review evidence is stale for the product HEAD"
            )
        stored_lanes = state.get("lanes")
        receipt_run = (
            None
            if status == "skipped"
            or (
                status == "attention-required"
                and stored_lanes == []
            )
            else gate.rehydrate()
        )
        return _receipt(
            status=status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=receipt_run,
        )
    run = gate.rehydrate()
    if status == "awaiting-resolution":
        awaiting = state.get("awaiting_resolution")
        if not isinstance(awaiting, dict) or not awaiting:
            raise TaskReviewError("awaiting review has no finding evidence")
        if any(
            not isinstance(value, dict)
            or not str(value.get("reviewed_head_sha") or "")
            for value in awaiting.values()
        ):
            raise TaskReviewError("review resolution boundary is invalid")
        reviewed_heads = {
            str(value["reviewed_head_sha"])
            for value in awaiting.values()
        }
        if reviewed_heads == {context.head_sha}:
            return _receipt(
                status=status,
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
                run=run,
            )
        decision = None
        for lane in run.execution.lanes:
            if lane.axis not in awaiting:
                continue
            pointer = _prompt(
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context=context,
                axis=lane.axis,
                verification=True,
            )

            def prepare_round(
                next_lane: object,
                round_: ReviewRound,
            ) -> None:
                _write_round_meta(
                    runtime_root=runtime_root,
                    worktree=worktree,
                    task_id=task_id,
                    depth=preset.depth,
                    context=context,
                    lane_operation_id=round_.parent_operation_id,
                    round_=round_,
                )

            decision = gate.continue_after_resolution(
                run,
                lane,
                context=context,
                verification_prompt_pointer=pointer,
                callback_pointer=(
                    _callback_path(runtime_root, lane.axis)
                    .relative_to(runtime_root)
                    .as_posix()
                ),
                prepare_round=prepare_round,
            )
        next_status = (
            decision.action
            if decision is not None
            and decision.action == "attention-required"
            else str(gate.read().get("status") or "")
        )
        return _receipt(
            status=next_status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=gate.rehydrate(),
        )
    if status not in {"reviewing", "verifying"}:
        raise TaskReviewError("review gate has an unsupported state")
    if context.head_sha != run.execution.request.context.head_sha:
        raise TaskReviewError(
            "product HEAD changed outside an awaiting-resolution boundary"
        )
    ready: list[tuple[object, ReviewRound, ReviewResult]] = []
    for lane in run.execution.lanes:
        round_ = run.rounds[lane.axis]
        callback = _callback_path(runtime_root, lane.axis)
        if not callback.is_file() or callback.is_symlink():
            continue
        _unused, result = _envelope(callback, round_)
        ready.append((lane, round_, result))
    if preset.depth == "deep" and len(ready) != len(
        run.execution.lanes
    ):
        return _receipt(
            status=status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if preset.depth == "deep" and any(
        result.verdict == "changes-requested"
        and any(
            finding.severity in {"critical", "important"}
            for finding in result.findings
        )
        for _lane, _round, result in ready
    ):
        for lane, round_, result in ready:
            decision = gate.defer_round_for_resolution(
                run, lane, round_, result
            )
            if decision.action == "attention-required":
                break
    else:
        for lane, round_, result in ready:
            decision = gate.complete_round(
                run,
                lane,
                round_,
                result,
            )
            if decision.action == "attention-required":
                break
    next_status = str(gate.read().get("status") or "")
    return _receipt(
        status=next_status,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=None if next_status == "skipped" else gate.rehydrate(),
    )


def run_task_review(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    return _run_review(
        meta,
        vault,
        worktree,
        task_id,
        _runtime_root(vault, task_id),
        runtime_manager=runtime_manager,
    )


def _validate_current_checkout(worktree: Path) -> Path:
    worktree = worktree.expanduser().resolve()
    required = (
        worktree / "wiki",
        worktree / "scripts",
        worktree / "skills/review/SKILL.md",
        worktree / "config/model-routing.toml",
        worktree / "config/verification-profiles.toml",
    )
    if (
        not worktree.is_dir()
        or any(not path.exists() for path in required)
        or _git(worktree, "rev-parse", "--show-toplevel") != str(worktree)
    ):
        raise TaskReviewError(
            "current review requires an exact llm-obsidian checkout root"
        )
    return worktree


def _current_policy(
    *,
    deep: bool,
    cross_model: bool,
    runtime: str,
    model: str,
    effort: str,
    no_review: bool,
    profile_sha256: str,
) -> dict[str, Any]:
    preset = ReviewPreset.from_flags(
        deep=deep,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
    )
    mode = preset.depth if preset.enabled else "skip"
    return {
        "mode": mode,
        "cross_model": cross_model,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "max_verify_iterations": {
            "simple": 1,
            "deep": 2,
            "skip": 0,
        }[mode],
        "verification_profile": "scoped",
        "verification_profile_sha256": profile_sha256,
        "auto_resolve_severities": ["warning", "nit"],
        "escalate_severities": ["blocking"],
    }


def _same_requested_policy(
    stored: Mapping[str, Any],
    requested: Mapping[str, Any],
) -> bool:
    return all(
        stored.get(name) == requested.get(name)
        for name in (
            "mode",
            "cross_model",
            "runtime",
            "model",
            "effort",
            "max_verify_iterations",
            "verification_profile",
            "verification_profile_sha256",
        )
    )


def _pending_replay_is_safe(
    request: ReviewOperationRequest,
    store: OperationStore,
    gate: ReviewGateController,
    runtime: object,
) -> bool:
    for identity in review_session_specs(request):
        record = None
        safe = False
        for _attempt in range(2):
            try:
                record = store.read(
                    request.owner_id, identity.spec.operation_id
                )
            except StoreError:
                safe = True
                break
            resources = record.resources
            clean_created = (
                record.state == "created"
                and not record.pending_effect
                and not any(
                    (
                        resources.surface_id,
                        resources.process_group,
                        resources.supervisor_pid,
                        resources.process_identity,
                        resources.supervisor_identity,
                    )
                )
            )
            if clean_created:
                safe = True
                break
            if record.state not in {
                "running",
                "awaiting-callback",
                "verifying",
            }:
                break
            try:
                observed = runtime.status(
                    request.owner_id, identity.spec.operation_id
                )
                latest = store.read(
                    request.owner_id, identity.spec.operation_id
                )
            except Exception:
                continue
            observed_record = getattr(observed, "record", None)
            observed_resources = getattr(
                observed_record, "resources", None
            )
            if (
                observed_record == latest
                and observed_resources is not None
                and bool(observed_resources.surface_id)
                and observed_resources.process_group > 1
                and observed_resources.supervisor_pid > 1
                and bool(observed_resources.process_identity)
                and bool(observed_resources.supervisor_identity)
                and runtime_status_is_live(observed)
            ):
                safe = True
                break
        if safe:
            continue
        try:
            record = store.read(
                request.owner_id, identity.spec.operation_id
            )
        except StoreError:
            continue
        if (
            record.state not in TERMINAL
            and record.state != "attention-required"
        ):
            store.transition(
                request.owner_id,
                record.spec.operation_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        gate.mark_pending_attention()
        return False
    return True


def run_current_review(
    worktree: Path,
    *,
    deep: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    no_review: bool = False,
    plan_file: Path | None = None,
    origin_surface: str = "",
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    worktree = _validate_current_checkout(worktree)
    vault = worktree
    profiles = load_profiles(vault / "config/verification-profiles.toml")
    profile = profiles.get("scoped")
    if profile is None:
        raise TaskReviewError("scoped verification profile is unavailable")
    requested_policy = _current_policy(
        deep=deep,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
        profile_sha256=profile.sha256,
    )
    active_path = (
        vault
        / ".vault-meta"
        / "harness"
        / "current-review"
        / "active.json"
    )
    meta: dict[str, Any] | None = None
    if active_path.is_file() and not active_path.is_symlink():
        candidate = _read_json(active_path, "current review state")
        if (
            candidate.get("lifecycle") != "current-checkout"
            or candidate.get("worktree") != str(worktree)
        ):
            raise TaskReviewError("current review state belongs to another checkout")
        try:
            task_id = str(uuid.UUID(str(candidate.get("task_id") or "")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TaskReviewError("current review identity is invalid") from exc
        gate_state_path = _gate_root(vault, task_id) / "review-gate.json"
        stored_policy = candidate.get("review_policy")
        same_policy = isinstance(stored_policy, dict) and _same_requested_policy(
            stored_policy, requested_policy
        )
        terminal_stale = False
        if gate_state_path.is_file() and not gate_state_path.is_symlink():
            gate_state = _read_json(gate_state_path, "current review gate")
            status = str(gate_state.get("status") or "")
            bound = gate_state.get("context")
            bound_head = (
                str(bound.get("head_sha") or "")
                if isinstance(bound, dict)
                else ""
            )
            terminal_stale = (
                status in {"approved", "skipped"}
                and (
                    bound_head != _git(worktree, "rev-parse", "HEAD")
                    or not same_policy
                )
            ) or (
                status == "attention-required"
                and _current_review_is_quiescent(vault, task_id)
            )
        elif gate_state_path.exists():
            raise TaskReviewError("current review gate is not a regular file")
        if not terminal_stale:
            if not same_policy:
                raise TaskReviewError(
                    "an active current review uses another preset or override"
                )
            if plan_file is not None and (
                Path(str(candidate.get("plan_file") or "")).resolve()
                != plan_file.expanduser().resolve()
            ):
                raise TaskReviewError(
                    "an active current review uses another plan"
                )
            meta = candidate

    if meta is None:
        task_id = str(uuid.uuid4())
        runtime_root = _current_runtime_root(
            worktree, task_id, scratch_root
        )
        surface = (
            origin_surface.strip()
            or str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
        )
        if not surface:
            raise TaskReviewError(
                "current review requires an exact cmux origin surface"
            )
        config = load_config(vault)
        session, source = routing_from_environment(config)
        if source == "tracked-default":
            raise TaskReviewError(
                "current review requires a host-confirmed current session route"
            )
        session = {**session, "source": source}
        if plan_file is None:
            plan = runtime_root / "inputs/current-review-scope.md"
            _atomic_text(
                plan,
                "\n".join(
                    (
                        "# Current checkout review scope",
                        "",
                        "Review the exact current checkout and HEAD against its "
                        "repository instructions, tests, and public contract.",
                        "",
                    )
                ),
            )
        else:
            plan = plan_file.expanduser().resolve()
            if not plan.is_file() or plan.is_symlink():
                raise TaskReviewError("current review plan is unavailable")
        meta = {
            "version": 3,
            "lifecycle": "current-checkout",
            "task_id": task_id,
            "task_name": "current checkout review",
            "task_surface": surface,
            "worktree": str(worktree),
            "vault_root": str(vault),
            "plan_file": str(plan),
            "routing": {"session": session},
            "review_policy": requested_policy,
            "runtime_root": str(runtime_root),
        }
        _request(
            meta,
            vault,
            task_id,
            ReviewContext(
                "pending/manifest.json",
                _git(worktree, "rev-parse", "HEAD"),
                "scoped",
                profile.sha256,
            ),
        )
        _atomic_json(runtime_root / "current-review.json", meta)
        _atomic_json(active_path, meta)
    else:
        task_id = str(meta["task_id"])
        runtime_root = Path(str(meta.get("runtime_root") or "")).resolve()
        expected_root = _current_runtime_root(
            worktree, task_id, scratch_root
        )
        if runtime_root != expected_root:
            raise TaskReviewError(
                "current review scratch root changed during an active gate"
            )
        if (
            runtime_root == worktree
            or worktree in runtime_root.parents
            or not (runtime_root / "current-review.json").is_file()
        ):
            raise TaskReviewError("current review scratch is unavailable")

    return _run_review(
        meta,
        vault,
        worktree,
        task_id,
        runtime_root,
        runtime_manager=runtime_manager,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--worktree", type=Path, required=True)
    current = sub.add_parser("current")
    current.add_argument("--worktree", type=Path, required=True)
    current.add_argument("--deep", action="store_true")
    current.add_argument("--cross-model", action="store_true")
    current.add_argument("--runtime", choices=("claude", "codex"), default="")
    current.add_argument("--model", default="")
    current.add_argument("--effort", default="")
    current.add_argument("--no-review", action="store_true")
    current.add_argument("--plan", type=Path)
    current.add_argument("--origin-surface", default="")
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_manager: object | None = None,
) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_task_review(
                args.worktree, runtime_manager=runtime_manager
            )
        else:
            result = run_current_review(
                args.worktree,
                deep=args.deep,
                cross_model=args.cross_model,
                runtime=args.runtime,
                model=args.model,
                effort=args.effort,
                no_review=args.no_review,
                plan_file=args.plan,
                origin_surface=args.origin_surface,
                runtime_manager=runtime_manager,
            )
    except (OSError, TaskReviewError, ValueError, RuntimeError) as exc:
        print(f"task-review-runner: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
