#!/usr/bin/env python3
"""Start one harness-owned simple/deep review from a context-ready request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from harness.contracts import RuntimeRoute, to_dict
from harness.review_submit import round_schema_lines
from harness.runtime_sessions import RuntimeSessionError, RuntimeSessionManager
from harness.store import OperationStore, StoreError
from harness.verification import VerificationError, load_profiles
from harness.workflows.review import (
    ReviewContext,
    ReviewOperationRequest,
)
from harness.workflows.review_gate import ReviewGateController, ReviewPreset
from model_routing import (
    RoutingError,
    load_config,
    load_session,
    resolve,
    routing_from_environment,
)
from review_contract import (
    review_axis_provider,
    review_axis_responsibility,
    review_provider_runtime,
    review_runtime_provider,
)

class ReviewRunnerError(ValueError):
    pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--operation-id", required=True)
    result.add_argument("--owner-id", required=True)
    result.add_argument("--store", type=Path, required=True)
    result.add_argument("--state-dir", type=Path)
    result.add_argument("--context-root", type=Path, required=True)
    result.add_argument("--context-manifest", required=True)
    result.add_argument("--origin-surface", required=True)
    result.add_argument("--prompt-pointer", required=True)
    result.add_argument("--callback-root", default="")
    result.add_argument("--runtime-root", type=Path)
    result.add_argument("--deep", action="store_true")
    result.add_argument("--full", action="store_true")
    result.add_argument("--cross-model", action="store_true")
    result.add_argument("--runtime", choices=("claude", "codex"), default="")
    result.add_argument("--model", default="", help="registered model alias only")
    result.add_argument("--effort", default="")
    result.add_argument("--session-id", default="")
    result.add_argument("--session-runtime", choices=("claude", "codex"), default="")
    result.add_argument("--session-model", default="")
    result.add_argument("--session-effort", default="")
    result.add_argument("--verification-profile", default="scoped")
    result.add_argument("--head-sha", default="")
    result.add_argument("--output", type=Path)
    return result


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _context_manifest(root: Path, relative: str, operation_id: str) -> str:
    path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise ReviewRunnerError("context manifest must be context-root relative")
    root = root.expanduser().resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewRunnerError("context manifest escapes context root") from exc
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewRunnerError(f"context manifest is not ready: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("operation_id") != operation_id
    ):
        raise ReviewRunnerError("context manifest does not identify this operation")
    return path.as_posix()


def _head(cwd: Path, explicit: str) -> str:
    if explicit:
        return explicit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ReviewRunnerError("cannot resolve review HEAD")
    return result.stdout.strip()


def _session(args: argparse.Namespace, config: Any) -> dict[str, str]:
    explicit = (args.session_runtime, args.session_model, args.session_effort)
    if any(explicit):
        if not all(explicit):
            raise ReviewRunnerError(
                "session runtime, model, and effort must be supplied together"
            )
        return {
            "runtime": args.session_runtime,
            "model": args.session_model,
            "effort": args.session_effort,
            "source": "explicit-session",
        }
    if args.session_id:
        return load_session(config, args.session_id)
    route, source = routing_from_environment(config)
    return {**route, "source": source}


def _relative_to_cwd(path: Path, cwd: Path, label: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(cwd).as_posix()
    except ValueError as exc:
        raise ReviewRunnerError(f"{label} must remain inside the review worktree") from exc


def _runtime_route(selected: dict[str, Any]) -> RuntimeRoute:
    return RuntimeRoute(
        selected["runtime"],
        selected["model"],
        selected["effort"],
        "reviewer-callback",
        selected["config_sha256"],
    )


def _runtime_root(
    explicit: Path | None,
    *,
    product_root: Path,
    owner_id: str,
    operation_id: str,
) -> Path:
    if explicit is None:
        identity = hashlib.sha256(
            f"{product_root}:{owner_id}:{operation_id}".encode()
        ).hexdigest()[:20]
        path = (
            Path(tempfile.gettempdir())
            / "llm-obsidian-review"
            / identity
        )
    else:
        path = explicit.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    resolved = path.resolve()
    for left, right in (
        (resolved, product_root),
        (product_root, resolved),
    ):
        try:
            left.relative_to(right)
        except ValueError:
            continue
        raise ReviewRunnerError(
            "review runtime scratch must be disjoint from the product worktree"
        )
    return resolved


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


def _materialize_prompts(
    *,
    runtime_root: Path,
    product_root: Path,
    context_manifest: Path,
    source_prompt: Path,
    axes: tuple[str, ...],
    callback_root: str,
    submit_root: Path,
) -> dict[str, str]:
    if (
        not source_prompt.is_file()
        or source_prompt.is_symlink()
        or not context_manifest.is_file()
        or context_manifest.is_symlink()
    ):
        raise ReviewRunnerError("review prompt/context pointers are unavailable")
    prompts: dict[str, str] = {}
    for axis in axes:
        axis_name = axis
        responsibility = review_axis_responsibility(axis)
        responsibility_instruction = {
            "holistic": (
                "Responsibility: independently review the full outcome and "
                "engineering denominator."
            ),
            "intent": (
                "Responsibility: review only the Outcome Contract, success "
                "evidence, specification, scope, and non-goals."
            ),
            "engineering": (
                "Responsibility: review only correctness, failure behavior, "
                "architecture, ownership, maintainability, tests, security, "
                "and applicable recovery, compatibility, and release risks."
            ),
        }[responsibility]
        engineering_instructions = (
            (
                "Authoritative engineering contract: "
                f"`{product_root / 'docs/skill-references/engineering-quality-contract.md'}`."
            ),
            (
                "Repository-specific standards override its heuristics, but "
                "their absence never suppresses engineering-quality judgment."
            ),
        ) if responsibility in {"holistic", "engineering"} else ()
        state_dir = runtime_root / callback_root / axis_name
        review_input = state_dir / ".review-input.json"
        command = shlex.join(
            (
                str(Path(sys.executable).resolve()),
                str(submit_root / "scripts/harness/review_submit.py"),
                "--worktree",
                str(product_root),
                "--state-dir",
                str(state_dir),
                "--input-file",
                str(review_input),
            )
        )
        pointer = f"prompts/{axis_name}.md"
        body = "\n".join(
            (
                "# Harness-owned review lane",
                "",
                f"Review axis: `{axis}`.",
                responsibility_instruction,
                f"Product worktree (read-only): `{product_root}`.",
                f"Context manifest: `{context_manifest}`.",
                f"Source handoff: `{source_prompt}`.",
                f"Review instructions: `{product_root / 'skills/review/SKILL.md'}`.",
                *engineering_instructions,
                "",
                "Read those pointers, inspect the exact product HEAD, and do not edit it.",
                "Use Read, Glob, and Grep with absolute paths for inspection.",
                "Do not run cd or copy packet files; they are readable in place.",
                *round_schema_lines(verification_iteration=0),
                "",
                f"Write the JSON to this exact scratch file: `{review_input}`.",
                "Then run this exact scratch-only command:",
                "",
                f"`{command}`",
                "",
            )
        )
        _atomic_text(runtime_root / pointer, body)
        prompts[axis] = pointer
    return prompts


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_manager: object | None = None,
) -> int:
    args = parser().parse_args(argv)
    cwd = Path.cwd().resolve()
    try:
        preset = ReviewPreset.from_flags(
            deep=args.deep,
            full=args.full,
            cross_model=args.cross_model,
            runtime=args.runtime,
            model=args.model,
            effort=args.effort,
        )
        manifest = _context_manifest(
            args.context_root, args.context_manifest, args.operation_id
        )
        profiles = load_profiles(cwd / "config/verification-profiles.toml")
        if args.verification_profile not in profiles:
            raise ReviewRunnerError("unknown verification profile")
        verification = profiles[args.verification_profile]
        config = load_config(cwd)
        session = _session(args, config)
        depth = preset.depth
        route_profile = "deep" if depth in {"deep", "full"} else "simple"
        single_model = bool(args.runtime or args.model)
        axis_routes: dict[str, RuntimeRoute] | None = None
        selected_provider = ""
        if depth in {"deep", "full"} and not single_model:
            provider_routes = {
                provider: _runtime_route(
                    resolve(
                        config,
                        "review",
                        session=session,
                        explicit_runtime=review_provider_runtime(provider),
                        explicit_effort=args.effort,
                        same_model=False,
                        review_profile="deep",
                    )
                )
                for provider in ("anthropic", "openai")
            }
            route = provider_routes["anthropic"]
        else:
            route = _runtime_route(
                resolve(
                    config,
                    "review",
                    session=session,
                    explicit_runtime=args.runtime,
                    explicit_model=args.model,
                    explicit_effort=args.effort,
                    same_model=not args.cross_model,
                    review_profile=route_profile,
                )
            )
            selected_provider = review_runtime_provider(route.runtime)
        policy = preset.request(
            args.operation_id,
            selected_provider=selected_provider,
        )
        if depth == "deep" and single_model:
            axis_routes = {axis: route for axis in policy.axes}
        elif depth in {"deep", "full"}:
            axis_routes = {
                axis: provider_routes[review_axis_provider(axis)]
                for axis in policy.axes
            }
        context = ReviewContext(
            manifest,
            _head(cwd, args.head_sha),
            verification.name,
            verification.sha256,
        )
        request = ReviewOperationRequest(
            policy, args.owner_id, route, context, axis_routes=axis_routes
        )
        store = OperationStore(args.store)
        state_dir = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else args.store.expanduser().resolve()
            / "review-data"
            / args.owner_id
            / args.operation_id
        )
        runtime_root = _runtime_root(
            args.runtime_root,
            product_root=cwd,
            owner_id=args.owner_id,
            operation_id=args.operation_id,
        )
        callback_root = args.callback_root or "callbacks"
        callback_path = PurePosixPath(callback_root)
        if (
            not callback_root
            or "\\" in callback_root
            or callback_path.is_absolute()
            or ".." in callback_path.parts
            or "." in callback_path.parts
        ):
            raise ReviewRunnerError("callback root must be runtime-root relative")
        callback_root_path = (runtime_root / callback_root).resolve()
        _relative_to_cwd(
            callback_root_path, runtime_root, "callback root"
        )
        for axis in policy.axes:
            axis_name = axis
            path = callback_root_path / axis_name
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
        source_prompt = (cwd / args.prompt_pointer).resolve()
        _relative_to_cwd(source_prompt, cwd, "source prompt")
        context_path = (
            args.context_root.expanduser().resolve() / manifest
        ).resolve()
        prompt_pointers = _materialize_prompts(
            runtime_root=runtime_root,
            product_root=cwd,
            context_manifest=context_path,
            source_prompt=source_prompt,
            axes=policy.axes,
            callback_root=callback_root,
            submit_root=Path(__file__).resolve().parent.parent,
        )
        prepared: dict[str, dict[str, Any]] = {}

        def prepare_lane(
            axis: str,
            session_request: object,
            result: object,
            round_: object,
        ) -> None:
            record = getattr(result, "record", None)
            if record is None:
                raise ReviewRunnerError("runtime surface hook returned no record")
            callback_pointer = str(
                getattr(session_request, "callback_pointer", "")
            )
            callback_dir = (
                runtime_root / callback_pointer
            ).resolve().parent
            meta = {
                "schema_version": 1,
                "transport": "review-round",
                "operation_id": round_.operation_id,
                "run_id": round_.run_id,
                "review_id": policy.operation_id,
                "parent_session_operation_id": record.spec.operation_id,
                "review_mode": depth,
                "axis": axis,
                "verification_iteration": 0,
                "worktree": str(cwd),
                "task_name": policy.operation_id,
                "head_sha": context.head_sha,
                "verification_profile": {
                    "name": context.verification_profile,
                    "sha256": context.verification_profile_sha256,
                },
                "route": to_dict(record.spec.route),
            }
            _atomic_json(callback_dir / ".review-meta.json", meta)
            prepared[axis] = {
                "round": round_,
                "meta": meta,
                "callback_pointer": callback_pointer,
            }

        manager = runtime_manager or RuntimeSessionManager.for_root(
            cwd, store_root=args.store
        )
        gate = ReviewGateController(state_dir, manager, store)
        gate_run = gate.begin(
            dispatch_operation_id=args.operation_id,
            request=request,
            origin_surface=args.origin_surface,
            cwd=runtime_root,
            product_root=cwd,
            prompt_pointer=prompt_pointers[policy.axes[0]],
            prompt_pointers=prompt_pointers,
            callback_root=callback_root,
            prepare_lane=prepare_lane,
        )
        execution = gate_run.execution
        if set(prepared) != set(policy.axes):
            raise ReviewRunnerError("runtime did not prepare every review lane")
        lanes = []
        for lane in execution.lanes:
            initial = prepared[lane.axis]
            round_ = initial["round"]
            lanes.append(
                {
                    "axis": lane.axis,
                    "operation_id": lane.operation_id,
                    "lane_id": lane.lane_id,
                    "run_id": lane.run_id,
                    "surface_id": lane.surface_id,
                    "checkpoint": lane.checkpoint,
                    "round_operation_id": round_.operation_id,
                    "round_run_id": round_.run_id,
                    "callback_pointer": initial["callback_pointer"],
                    "route": to_dict(lane.spec.route),
                }
            )
        meta = {
            "schema_version": 1,
            "operation_id": args.operation_id,
            "review_id": args.operation_id,
            "run_id": lanes[0]["run_id"],
            "review_mode": depth,
            "worktree": str(cwd),
            "task_name": args.operation_id,
            "owner_id": args.owner_id,
            "context_manifest": manifest,
            "head_sha": context.head_sha,
            "verification_profile": {
                "name": context.verification_profile,
                "sha256": context.verification_profile_sha256,
            },
            "axes": list(policy.axes),
            "max_verify_iterations": policy.max_verify_iterations,
            "lanes": lanes,
        }
        _atomic_json(state_dir / ".review-meta.json", meta)
        value = {
            "schema_version": 1,
            "request": {
                "operation_id": policy.operation_id,
                "depth": policy.depth,
                "cross_model": policy.cross_model,
                "axes": list(policy.axes),
                "max_verify_iterations": policy.max_verify_iterations,
                "head_sha": context.head_sha,
                "verification_profile": meta["verification_profile"],
            },
            "lanes": lanes,
            "state_dir": str(state_dir),
        }
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        if args.output:
            _atomic_json(args.output, value)
        print(payload, end="")
        return 0
    except (
        ReviewRunnerError,
        RoutingError,
        RuntimeSessionError,
        StoreError,
        VerificationError,
        ValueError,
        OSError,
    ) as exc:
        print(f"review-runner: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
