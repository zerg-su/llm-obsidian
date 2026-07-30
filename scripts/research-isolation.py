#!/usr/bin/env python3
"""Thin CLI for the harness-owned two-stage protected research workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import RuntimeRoute  # noqa: E402
from harness.runtime_sessions import (  # noqa: E402
    RuntimeSessionError,
    RuntimeSessionManager,
)
from harness.store import OperationStore, StoreError  # noqa: E402
from harness.workflows.research import (  # noqa: E402
    PreparedResearch,
    ResearchContext,
    ResearchOperationRequest,
    ResearchRequest,
    advance_research,
    finalize_research,
    prepare_research,
    start_research,
    status_research,
)
from model_routing import (  # noqa: E402
    RoutingError,
    load_config as load_routing_config,
    resolve as resolve_model_route,
    routing_from_environment,
)
from research_contract import ResearchContractError  # noqa: E402


FLOWS = frozenset({"research", "url-ingest", "deep-query"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
RuntimeFactory = Callable[[Path, Path], object]
StoreFactory = Callable[[Path], OperationStore]


class ResearchCliError(ValueError):
    pass


def die(message: str, code: int = 3) -> NoReturn:
    print(f"research-isolation: {message}", file=sys.stderr)
    raise SystemExit(code)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchCliError("research pipeline state is unreadable") from exc
    if not isinstance(value, dict):
        raise ResearchCliError("research pipeline state must be an object")
    return value


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ResearchCliError(f"{label} must be a bounded identifier")
    return value


def _origin_surface(value: str) -> str:
    surface = value or str(os.environ.get("CMUX_SURFACE_ID") or "")
    if not SURFACE_UUID.fullmatch(surface):
        raise ResearchCliError(
            "protected research requires an exact current cmux surface"
        )
    return surface


def _store_root(args: argparse.Namespace) -> Path:
    return (
        args.store_root.expanduser().resolve()
        if args.store_root is not None
        else args.vault_root.expanduser().resolve()
        / ".vault-meta"
        / "harness"
    )


def _pipeline_root(store_root: Path, owner_id: str, operation_id: str) -> Path:
    _identifier(owner_id, "owner")
    _identifier(operation_id, "operation id")
    return (
        store_root
        / "owners"
        / owner_id
        / "research"
        / operation_id
    ).resolve()


def _pipeline_path(store_root: Path, owner_id: str, operation_id: str) -> Path:
    return _pipeline_root(store_root, owner_id, operation_id) / "pipeline.json"


def _route(vault_root: Path) -> RuntimeRoute:
    config = load_routing_config(vault_root)
    session, source = routing_from_environment(config)
    session["source"] = source
    selected = resolve_model_route(
        config,
        "protected-research",
        session=session,
    )
    if selected["runtime"] != "codex":
        raise ResearchCliError("protected research requires the Codex runtime")
    return RuntimeRoute(
        str(selected["runtime"]),
        str(selected["model"]),
        str(selected["effort"]),
        "research-safe",
        str(selected["config_sha256"]),
    )


def _route_value(route: RuntimeRoute) -> dict[str, str]:
    return {
        "runtime": route.runtime,
        "model": route.model,
        "effort": route.effort,
        "profile": route.profile,
        "routing_sha256": route.routing_sha256,
    }


def _request_from_state(value: dict[str, Any]) -> ResearchOperationRequest:
    if value.get("schema_version") != 1:
        raise ResearchCliError("unsupported research pipeline schema")
    route_value = value.get("route")
    if not isinstance(route_value, dict):
        raise ResearchCliError("research pipeline route is unavailable")
    route = RuntimeRoute(
        str(route_value.get("runtime") or ""),
        str(route_value.get("model") or ""),
        str(route_value.get("effort") or ""),
        str(route_value.get("profile") or ""),
        str(route_value.get("routing_sha256") or ""),
    )
    operation_id = _identifier(
        str(value.get("operation_id") or ""),
        "operation id",
    )
    owner_id = _identifier(str(value.get("owner_id") or ""), "owner")
    context_manifest = str(value.get("context_manifest") or "")
    return ResearchOperationRequest(
        policy=ResearchRequest(
            operation_id=operation_id,
            query_pointer=str(value.get("query_pointer") or ""),
            context_manifest=context_manifest,
        ),
        owner_id=owner_id,
        route=route,
        context=ResearchContext(
            manifest=context_manifest,
            request_sha256=str(value.get("request_sha256") or ""),
        ),
    )


def _prepared_from_state(
    value: dict[str, Any],
    store_root: Path,
) -> PreparedResearch:
    request = _request_from_state(value)
    root = _pipeline_root(
        store_root,
        request.owner_id,
        request.policy.operation_id,
    )
    expected = {
        "root": root,
        "fetch_cwd": root / "fetch",
        "synth_cwd": root / "synth",
        "fetch_runtime_home": root / "runtime" / "codex-home-fetch",
        "synth_runtime_home": root / "runtime" / "codex-home-synth",
    }
    for path in expected.values():
        if path.is_symlink():
            raise ResearchCliError("research pipeline contains a symlinked root")
    return PreparedResearch(request=request, **expected)


def _state_value(
    prepared: PreparedResearch,
    *,
    flow: str,
    origin_surface: str,
) -> dict[str, object]:
    request = prepared.request
    return {
        "schema_version": 1,
        "operation_id": request.policy.operation_id,
        "owner_id": request.owner_id,
        "flow": flow,
        "origin_surface": origin_surface,
        "context_manifest": request.context.manifest,
        "query_pointer": request.policy.query_pointer,
        "request_sha256": request.context.request_sha256,
        "route": _route_value(request.route),
    }


def _wake_command(
    *,
    vault_root: Path,
    store_root: Path,
    owner_id: str,
    operation_id: str,
    origin_surface: str,
) -> str:
    return (
        "Protected research stage completed. Run: "
        + shlex.join(
            [
                str(Path(sys.executable).resolve()),
                str(Path(__file__).resolve()),
                "--vault-root",
                str(vault_root),
                "--store-root",
                str(store_root),
                "advance",
                "--operation-id",
                operation_id,
                "--owner",
                owner_id,
                "--coordinator-surface",
                origin_surface,
            ]
        )
    )


def _execution_value(
    execution: object,
    *,
    operation_id: str,
    result_artifact: object = None,
) -> dict[str, object]:
    parent = execution.parent
    fetch = execution.fetch
    synth = execution.synth
    result = result_artifact or execution.result_artifact
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "stage": execution.stage,
        "status": parent.state,
        "fetch": {
            "operation_id": fetch.spec.operation_id,
            "run_id": fetch.run_id,
            "status": fetch.state,
            "surface_id": fetch.resources.surface_id,
        },
        "synth": (
            {
                "operation_id": synth.spec.operation_id,
                "run_id": synth.run_id,
                "status": synth.state,
                "surface_id": synth.resources.surface_id,
            }
            if synth is not None
            else None
        ),
        "result_artifact": dict(result) if result is not None else None,
    }


def _default_runtime(vault_root: Path, store_root: Path) -> object:
    return RuntimeSessionManager.for_root(
        vault_root,
        store_root=store_root,
    )


def cmd_start(
    args: argparse.Namespace,
    *,
    runtime_factory: RuntimeFactory,
    store_factory: StoreFactory,
) -> int:
    vault_root = args.vault_root.expanduser().resolve()
    store_root = _store_root(args)
    operation_id = (
        str(uuid.UUID(args.operation_id))
        if args.operation_id
        else str(uuid.uuid4())
    )
    owner_id = _identifier(args.owner or args.task_id or "local", "owner")
    origin_surface = _origin_surface(args.coordinator_surface)
    topic = args.topic.strip()
    route = _route(vault_root)
    root = _pipeline_root(store_root, owner_id, operation_id)
    prepared = prepare_research(
        root,
        operation_id=operation_id,
        owner_id=owner_id,
        flow=args.flow,
        topic=topic,
        route=route,
    )
    state = _state_value(
        prepared,
        flow=args.flow,
        origin_surface=origin_surface,
    )
    state_path = _pipeline_path(store_root, owner_id, operation_id)
    if state_path.exists():
        existing = _object(state_path)
        if any(existing.get(key) != value for key, value in state.items()):
            raise ResearchCliError(
                "same research operation id changed its immutable request"
            )
    else:
        _atomic_json(state_path, state)
    wake = _wake_command(
        vault_root=vault_root,
        store_root=store_root,
        owner_id=owner_id,
        operation_id=operation_id,
        origin_surface=origin_surface,
    )
    runtime = runtime_factory(vault_root, store_root)
    store = store_factory(store_root)
    execution = start_research(
        prepared.request,
        runtime,
        store,
        origin_surface=origin_surface,
        fetch_cwd=prepared.fetch_cwd,
        fetch_runtime_home=prepared.fetch_runtime_home,
        callback_wake=wake,
    )
    print(
        json.dumps(
            _execution_value(execution, operation_id=operation_id),
            sort_keys=True,
        )
    )
    return 0


def cmd_advance(
    args: argparse.Namespace,
    *,
    runtime_factory: RuntimeFactory,
    store_factory: StoreFactory,
) -> int:
    vault_root = args.vault_root.expanduser().resolve()
    store_root = _store_root(args)
    owner_id = _identifier(args.owner, "owner")
    operation_id = _identifier(args.operation_id, "operation id")
    state_path = _pipeline_path(store_root, owner_id, operation_id)
    state = _object(state_path)
    prepared = _prepared_from_state(state, store_root)
    origin_surface = _origin_surface(
        args.coordinator_surface
        or str(state.get("origin_surface") or "")
    )
    wake = _wake_command(
        vault_root=vault_root,
        store_root=store_root,
        owner_id=owner_id,
        operation_id=operation_id,
        origin_surface=origin_surface,
    )
    runtime = runtime_factory(vault_root, store_root)
    store = store_factory(store_root)
    current = status_research(prepared.request, store)
    if current.synth is None:
        execution = advance_research(
            prepared.request,
            runtime,
            store,
            origin_surface=origin_surface,
            fetch_cwd=prepared.fetch_cwd,
            synth_cwd=prepared.synth_cwd,
            synth_runtime_home=prepared.synth_runtime_home,
            callback_wake=wake,
        )
    elif current.synth.state in {"finalizing", "exiting", "complete"}:
        execution = finalize_research(
            prepared.request,
            runtime,
            store,
            synth_cwd=prepared.synth_cwd,
        )
        if execution.result_artifact is not None:
            updated = dict(state)
            updated["result_artifact"] = dict(execution.result_artifact)
            _atomic_json(state_path, updated)
    else:
        execution = current
    print(
        json.dumps(
            _execution_value(execution, operation_id=operation_id),
            sort_keys=True,
        )
    )
    return 0


def cmd_status(
    args: argparse.Namespace,
    *,
    runtime_factory: RuntimeFactory,
    store_factory: StoreFactory,
) -> int:
    del runtime_factory
    store_root = _store_root(args)
    owner_id = _identifier(args.owner, "owner")
    operation_id = _identifier(args.operation_id, "operation id")
    state = _object(_pipeline_path(store_root, owner_id, operation_id))
    prepared = _prepared_from_state(state, store_root)
    execution = status_research(
        prepared.request,
        store_factory(store_root),
    )
    print(
        json.dumps(
            _execution_value(
                execution,
                operation_id=operation_id,
                result_artifact=state.get("result_artifact"),
            ),
            sort_keys=True,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--vault-root", type=Path, default=ROOT)
    result.add_argument("--store-root", type=Path)
    commands = result.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start")
    start.add_argument("--flow", choices=sorted(FLOWS), required=True)
    start.add_argument("--topic", required=True)
    start.add_argument("--operation-id", default="")
    start.add_argument("--owner", default="")
    start.add_argument(
        "--task-id",
        default="",
        help="deprecated owner alias retained for 2.2 caller migration",
    )
    start.add_argument("--coordinator-surface", default="")
    start.set_defaults(func=cmd_start)

    for name, function in (("advance", cmd_advance), ("status", cmd_status)):
        command = commands.add_parser(name)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--owner", required=True)
        command.add_argument("--coordinator-surface", default="")
        command.set_defaults(func=function)
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: RuntimeFactory = _default_runtime,
    store_factory: StoreFactory = OperationStore,
) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(
            args,
            runtime_factory=runtime_factory,
            store_factory=store_factory,
        )
    except (
        OSError,
        ResearchCliError,
        ResearchContractError,
        RoutingError,
        RuntimeSessionError,
        StoreError,
        TypeError,
        ValueError,
    ) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
