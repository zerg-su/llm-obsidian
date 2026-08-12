"""Exact-attempt retirement of model-writable review input scratch."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("archive-authority",)

import json
from pathlib import Path
from typing import Mapping

from .review_attempt import ReviewAttempt, ReviewAttemptError
from .store import StoreError


META_FIELDS = frozenset(
    {
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
)
INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "axis",
        "verdict",
        "verification_iteration",
        "findings",
    }
)


def _callback_directory(runtime: Path, callback_root: str) -> Path:
    relative = Path(callback_root)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative == Path(".")
        or ".." in relative.parts
    ):
        raise ReviewAttemptError("review input rollover path is invalid")
    current = runtime
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ReviewAttemptError("review input rollover path is invalid")
    callbacks = runtime / relative
    try:
        callbacks.resolve(strict=False).relative_to(runtime)
    except (OSError, ValueError) as exc:
        raise ReviewAttemptError(
            "review input rollover path is invalid"
        ) from exc
    return callbacks


def _select_artifact(live: Path, archived: Path, label: str) -> Path | None:
    live_present = live.exists() or live.is_symlink()
    archived_present = archived.exists() or archived.is_symlink()
    if live_present and archived_present:
        raise ReviewAttemptError("review input rollover is ambiguous")
    selected = live if live_present else archived
    if not (live_present or archived_present):
        return None
    if selected.is_symlink() or not selected.is_file():
        raise ReviewAttemptError(f"review input rollover {label} changed")
    return selected


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewAttemptError(
            f"review input rollover {label} changed"
        ) from exc
    if not isinstance(value, dict):
        raise ReviewAttemptError(
            f"review input rollover {label} changed"
        )
    return value


def _validate_meta(
    value: Mapping[str, object], lane: object, attempt: ReviewAttempt
) -> None:
    if (
        set(value) != META_FIELDS
        or value.get("schema_version") != 1
        or value.get("transport") != "review-round"
        or value.get("axis") != lane.axis
        or value.get("parent_session_operation_id") != lane.operation_id
        or value.get("head_sha") != attempt.identity.exact_head_sha
        or value.get("verification_iteration") != 0
    ):
        raise ReviewAttemptError("review input rollover metadata changed")


def _validate_input(value: Mapping[str, object], axis: str) -> None:
    if (
        set(value) != INPUT_FIELDS
        or value.get("schema_version") != 1
        or value.get("axis") != axis
        or value.get("verification_iteration") != 0
    ):
        raise ReviewAttemptError("review input rollover scratch changed")


def _require_round(store: object, lane: object, meta: Mapping[str, object]) -> None:
    try:
        children = [
            row
            for row in store.list(lane.owner_id)
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
    if len(children) != 1:
        raise ReviewAttemptError("review input rollover metadata changed")


def archive_prior_review_input(
    *,
    root: Path,
    store: object,
    attempt: ReviewAttempt,
    runtime_root: Path | None,
    callback_root: str,
) -> None:
    """Archive scratch only after exact metadata and store validation."""

    if attempt.status != "terminal" or attempt.terminal is None:
        raise ReviewAttemptError(
            "review input rollover requires a terminal attempt"
        )
    if runtime_root is None or not callback_root:
        raise ReviewAttemptError(
            "review input rollover authority is unavailable"
        )
    runtime = runtime_root.expanduser().resolve()
    callbacks = _callback_directory(runtime, callback_root)
    archive = (
        root
        / "attempts"
        / f"attempt-{attempt.identity.attempt_id}-review-input"
    )
    if archive.is_symlink() or (archive.exists() and not archive.is_dir()):
        raise ReviewAttemptError("review input rollover archive is invalid")
    moves: list[tuple[Path, Path]] = []
    for lane in attempt.identity.lanes:
        axis_root = callbacks / lane.axis
        if axis_root.is_symlink():
            raise ReviewAttemptError("review input rollover path is invalid")
        live_meta = axis_root / ".review-meta.json"
        live_input = axis_root / ".review-input.json"
        archived_meta = archive / f"{lane.axis}.review-meta.json"
        archived_input = archive / f"{lane.axis}.review-input.json"
        meta_path = _select_artifact(live_meta, archived_meta, "metadata")
        input_path = _select_artifact(live_input, archived_input, "scratch")
        if meta_path is None and input_path is None:
            continue
        if meta_path is None:
            raise ReviewAttemptError(
                "review input rollover metadata is unavailable"
            )
        meta = _read_object(meta_path, "metadata")
        _validate_meta(meta, lane, attempt)
        _require_round(store, lane, meta)
        if input_path is not None:
            _validate_input(_read_object(input_path, "scratch"), lane.axis)
        if live_meta.exists():
            moves.append((live_meta, archived_meta))
        if live_input.exists():
            moves.append((live_input, archived_input))
    if moves:
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive.chmod(0o700)
    for source, destination in moves:
        source.replace(destination)


__all__ = ("archive_prior_review_input",)
