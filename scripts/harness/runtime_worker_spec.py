"""Strict parser for one immutable runtime-worker launch specification."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .runtime_worker_contracts import (
    IDENTIFIER,
    SURFACE_UUID,
    RuntimeWorkerError,
)


CALLBACK_MODES = frozenset(
    {"envelope", "task-summary", "research-fetch", "research-synth"}
)


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise RuntimeWorkerError(f"{label} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeWorkerError(f"{label} must be an absolute path")
    return path.resolve()


def _read_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError("runtime launch spec is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeWorkerError("runtime launch spec schema is invalid")
    return value


def _validate_identity(value: dict[str, Any]) -> str:
    for field in ("owner_id", "operation_id", "run_id"):
        if not IDENTIFIER.fullmatch(str(value.get(field) or "")):
            raise RuntimeWorkerError(f"runtime launch {field} is invalid")
    if not SURFACE_UUID.fullmatch(str(value.get("surface_id") or "")):
        raise RuntimeWorkerError("runtime launch surface identity is invalid")
    if value.get("runtime") not in {"claude", "codex"}:
        raise RuntimeWorkerError("runtime launch provider is invalid")
    callback_mode = str(value.get("callback_mode") or "envelope")
    if callback_mode not in CALLBACK_MODES:
        raise RuntimeWorkerError("runtime callback mode is invalid")
    return callback_mode


def _validate_argv(value: dict[str, Any]) -> None:
    argv = value.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or "\0" in part for part in argv)
        or not Path(argv[0]).is_absolute()
    ):
        raise RuntimeWorkerError("runtime launch argv is invalid")


def _initial_input(value: dict[str, Any], cwd: Path) -> Path | None:
    raw = value.get("initial_input_pointer")
    if raw in {None, ""}:
        return None
    pointer = _absolute(raw, "initial_input_pointer")
    try:
        pointer.relative_to(cwd)
    except ValueError as exc:
        raise RuntimeWorkerError("initial provider input escapes cwd") from exc
    if pointer.is_symlink() or not pointer.is_file():
        raise RuntimeWorkerError("initial provider input is unavailable")
    return pointer


def _task_summary_pointer(
    value: dict[str, Any], callback_mode: str, origin_surface: str
) -> Path | None:
    if callback_mode != "task-summary":
        return None
    task_summary = _absolute(
        value.get("task_summary_pointer"), "task_summary_pointer"
    )
    if (
        task_summary.name != ".task-summary.json"
        or not SURFACE_UUID.fullmatch(origin_surface)
    ):
        raise RuntimeWorkerError(
            "task-summary source or origin identity is invalid"
        )
    return task_summary


def _runtime_interpreter(value: dict[str, Any]) -> Path | None:
    raw = value.get("runtime_interpreter")
    if not raw:
        return None
    runtime_interpreter = _absolute(raw, "runtime_interpreter")
    try:
        interpreter_stat = runtime_interpreter.stat()
    except OSError as exc:
        raise RuntimeWorkerError(
            "runtime interpreter is unavailable"
        ) from exc
    if (
        not runtime_interpreter.is_file()
        or not os.access(runtime_interpreter, os.X_OK)
        or interpreter_stat.st_mode & 0o022
    ):
        raise RuntimeWorkerError("runtime interpreter is untrusted")
    return runtime_interpreter


def _research_runtime(
    value: dict[str, Any],
    *,
    callback_mode: str,
    cwd: Path,
    callback: Path,
    origin_surface: str,
    callback_wake: str,
    research_request_sha256: str,
) -> tuple[Path, Path | None]:
    raw_runtime_home = value.get("runtime_home")
    if (
        value.get("runtime") != "codex"
        or not isinstance(raw_runtime_home, str)
        or not raw_runtime_home
        or Path(raw_runtime_home).expanduser().is_symlink()
        or not SURFACE_UUID.fullmatch(origin_surface)
        or not callback_wake
        or callback_wake != callback_wake.strip()
        or "\0" in callback_wake
        or "\n" in callback_wake
        or "\r" in callback_wake
        or len(callback_wake.encode()) > 4096
    ):
        raise RuntimeWorkerError("research launch identity is invalid")
    runtime_home = _absolute(raw_runtime_home, "runtime_home")
    runtime_interpreter = _runtime_interpreter(value)
    try:
        runtime_stat = runtime_home.stat()
    except OSError as exc:
        raise RuntimeWorkerError(
            "research runtime home is unavailable"
        ) from exc
    if (
        not runtime_home.is_dir()
        or runtime_stat.st_uid != os.getuid()
        or runtime_stat.st_mode & 0o077
        or runtime_home == cwd
        or runtime_home in cwd.parents
        or cwd in runtime_home.parents
    ):
        raise RuntimeWorkerError(
            "research runtime home must be owner-only and disjoint"
        )
    expected_name = (
        "artifact.json"
        if callback_mode == "research-fetch"
        else "complete.json"
    )
    if callback.name != expected_name:
        raise RuntimeWorkerError("research callback pointer is not canonical")
    if callback_mode == "research-fetch":
        if not re.fullmatch(r"[0-9a-f]{64}", research_request_sha256):
            raise RuntimeWorkerError("research request digest is invalid")
    elif research_request_sha256:
        raise RuntimeWorkerError(
            "research synth request digest must be derived"
        )
    return runtime_home, runtime_interpreter


def _reject_nonresearch_fields(
    value: dict[str, Any],
    *,
    callback_mode: str,
    origin_surface: str,
    callback_wake: str,
    research_request_sha256: str,
) -> None:
    if callback_mode == "envelope" and callback_wake:
        if (
            not SURFACE_UUID.fullmatch(origin_surface)
            or callback_wake != callback_wake.strip()
            or "\0" in callback_wake
            or "\n" in callback_wake
            or "\r" in callback_wake
            or len(callback_wake.encode()) > 4096
        ):
            raise RuntimeWorkerError("review callback wake is invalid")
        if (
            value.get("runtime_home") or research_request_sha256
        ):
            raise RuntimeWorkerError(
                "research runtime fields require research callback mode"
            )
    elif (
        value.get("runtime_home") or research_request_sha256
        or callback_wake
    ):
        raise RuntimeWorkerError(
            "research launch fields require research callback mode"
        )


def _validate_markers(
    *,
    launch_path: Path,
    cwd: Path,
    callback: Path,
    registration: Path,
    task_summary: Path | None,
    store_root: Path,
    ready: Path,
    exit_path: Path,
) -> None:
    if (
        ready.parent != launch_path.parent
        or exit_path.parent != launch_path.parent
        or registration.parent != launch_path.parent
    ):
        raise RuntimeWorkerError("runtime worker markers escape launch state")
    try:
        callback.relative_to(cwd)
    except ValueError as exc:
        raise RuntimeWorkerError("runtime callback pointer escapes cwd") from exc
    if task_summary is not None:
        try:
            task_summary.relative_to(cwd)
        except ValueError as exc:
            raise RuntimeWorkerError("task summary pointer escapes cwd") from exc
    if not cwd.is_dir() or not store_root.is_dir():
        raise RuntimeWorkerError("runtime launch roots are unavailable")


def load_spec(path: Path) -> dict[str, Any]:
    value = _read_spec(path)
    callback_mode = _validate_identity(value)
    _validate_argv(value)
    cwd = _absolute(value.get("cwd"), "cwd")
    initial_input = _initial_input(value, cwd)
    callback = _absolute(value.get("callback_pointer"), "callback_pointer")
    product_root = None
    reviewer_sandbox = value.get("reviewer_sandbox", False)
    if not isinstance(reviewer_sandbox, bool):
        raise RuntimeWorkerError("reviewer sandbox identity is invalid")
    raw_product_root = value.get("product_root")
    if raw_product_root is not None and raw_product_root != "":
        product_root = _absolute(raw_product_root, "product_root")
        if (
            product_root.is_symlink()
            or not product_root.is_dir()
        ):
            raise RuntimeWorkerError("runtime product root is invalid")
        overlaps_cwd = (
            product_root == cwd
            or product_root in cwd.parents
            or cwd in product_root.parents
        )
        if reviewer_sandbox and overlaps_cwd:
            raise RuntimeWorkerError("runtime product root is invalid")
        if not reviewer_sandbox and product_root != cwd:
            raise RuntimeWorkerError("runtime product root is invalid")
    if reviewer_sandbox and (
        callback_mode != "envelope" or product_root is None
    ):
        raise RuntimeWorkerError("reviewer product root is required")
    if (
        not reviewer_sandbox
        and callback_mode not in {"research-fetch", "research-synth"}
        and product_root is None
    ):
        raise RuntimeWorkerError("ordinary runtime product root is required")
    registration = _absolute(
        value.get("callback_registration"), "callback_registration"
    )
    origin_surface = str(value.get("origin_surface") or "")
    callback_wake = str(value.get("callback_wake") or "")
    request_sha = str(value.get("research_request_sha256") or "")
    task_summary = _task_summary_pointer(
        value, callback_mode, origin_surface
    )
    runtime_home = None
    runtime_interpreter = _runtime_interpreter(value)
    if callback_mode in {"research-fetch", "research-synth"}:
        runtime_home, research_runtime_interpreter = _research_runtime(
            value,
            callback_mode=callback_mode,
            cwd=cwd,
            callback=callback,
            origin_surface=origin_surface,
            callback_wake=callback_wake,
            research_request_sha256=request_sha,
        )
        if research_runtime_interpreter != runtime_interpreter:
            raise RuntimeWorkerError("runtime interpreter identity drifted")
    else:
        _reject_nonresearch_fields(
            value,
            callback_mode=callback_mode,
            origin_surface=origin_surface,
            callback_wake=callback_wake,
            research_request_sha256=request_sha,
        )
    store_root = _absolute(value.get("store_root"), "store_root")
    ready = _absolute(value.get("ready_path"), "ready_path")
    exit_path = _absolute(value.get("exit_path"), "exit_path")
    _validate_markers(
        launch_path=path,
        cwd=cwd,
        callback=callback,
        registration=registration,
        task_summary=task_summary,
        store_root=store_root,
        ready=ready,
        exit_path=exit_path,
    )
    value.update(
        {
            "cwd": cwd,
            "callback_pointer": callback,
            "product_root": product_root,
            "reviewer_sandbox": reviewer_sandbox,
            "callback_registration": registration,
            "callback_mode": callback_mode,
            "task_summary_pointer": task_summary,
            "runtime_home": runtime_home,
            "runtime_interpreter": runtime_interpreter,
            "research_request_sha256": request_sha,
            "callback_wake": callback_wake,
            "origin_surface": origin_surface,
            "store_root": store_root,
            "ready_path": ready,
            "exit_path": exit_path,
            "initial_input_pointer": initial_input,
        }
    )
    return value
