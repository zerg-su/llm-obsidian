"""Exact durable workspace authority for one review program."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .contracts import OperationRecord, OwnedResources
from .runtime_session_contracts import IDENTIFIER, SURFACE_UUID
from .finalization_pivot import pivot_required


REF = re.compile(r"(surface|workspace|window):[1-9][0-9]*\Z")


def _result_identity(value: object) -> tuple[str, str, str, str, str, str]:
    record = getattr(value, "record", None)
    if not isinstance(record, OperationRecord):
        raise ValueError("review workspace result has no operation record")
    return (
        record.resources.surface_id,
        str(getattr(value, "surface_ref", "") or ""),
        str(getattr(value, "workspace_id", "") or ""),
        str(getattr(value, "workspace_ref", "") or ""),
        str(getattr(value, "window_id", "") or ""),
        str(getattr(value, "window_ref", "") or ""),
    )


def _validate_ref(value: str, kind: str) -> None:
    if value and (not REF.fullmatch(value) or not value.startswith(f"{kind}:")):
        raise ValueError(f"review {kind} reference is invalid")


@dataclass(frozen=True)
class ReviewWorkspaceBinding:
    """One review program's exact cmux workspace, window, and launch anchor."""

    review_operation_id: str
    workspace_id: str
    workspace_ref: str
    window_id: str
    window_ref: str
    anchor_surface_id: str
    anchor_surface_ref: str = ""

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.review_operation_id):
            raise ValueError("review workspace operation identity is invalid")
        for value, label in (
            (self.workspace_id, "workspace"),
            (self.window_id, "window"),
            (self.anchor_surface_id, "anchor surface"),
        ):
            if not SURFACE_UUID.fullmatch(value):
                raise ValueError(f"review {label} identity is invalid")
        _validate_ref(self.workspace_ref, "workspace")
        _validate_ref(self.window_ref, "window")
        _validate_ref(self.anchor_surface_ref, "surface")

    @classmethod
    def from_result(
        cls, review_operation_id: str, value: object
    ) -> "ReviewWorkspaceBinding":
        surface, surface_ref, workspace, workspace_ref, window, window_ref = (
            _result_identity(value)
        )
        return cls(
            review_operation_id=review_operation_id,
            workspace_id=workspace,
            workspace_ref=workspace_ref,
            window_id=window,
            window_ref=window_ref,
            anchor_surface_id=surface,
            anchor_surface_ref=surface_ref,
        )

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> "ReviewWorkspaceBinding":
        expected = {
            "schema_version",
            "review_operation_id",
            "workspace_id",
            "workspace_ref",
            "window_id",
            "window_ref",
            "anchor_surface_id",
            "anchor_surface_ref",
        }
        if set(raw) != expected or raw.get("schema_version") != 1:
            raise ValueError("review workspace binding payload is invalid")
        try:
            return cls(
                review_operation_id=str(raw["review_operation_id"]),
                workspace_id=str(raw["workspace_id"]),
                workspace_ref=str(raw["workspace_ref"]),
                window_id=str(raw["window_id"]),
                window_ref=str(raw["window_ref"]),
                anchor_surface_id=str(raw["anchor_surface_id"]),
                anchor_surface_ref=str(raw["anchor_surface_ref"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("review workspace binding payload is invalid") from exc

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "review_operation_id": self.review_operation_id,
            "workspace_id": self.workspace_id,
            "workspace_ref": self.workspace_ref,
            "window_id": self.window_id,
            "window_ref": self.window_ref,
            "anchor_surface_id": self.anchor_surface_id,
            "anchor_surface_ref": self.anchor_surface_ref,
        }

    def validate_member(self, value: object) -> tuple[str, str]:
        surface, _surface_ref, workspace, _workspace_ref, window, _window_ref = (
            _result_identity(value)
        )
        if not SURFACE_UUID.fullmatch(surface):
            raise ValueError("review lane surface identity is invalid")
        if workspace.casefold() != self.workspace_id.casefold():
            raise ValueError("review workspace identity changed")
        if window.casefold() != self.window_id.casefold():
            raise ValueError("review window identity changed")
        return workspace, window


@dataclass(frozen=True)
class ReviewWorkspaceCleanup:
    """Durable proof that one terminal review program released its container."""

    review_operation_id: str
    workspace_id: str
    window_id: str
    status: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "review_operation_id": self.review_operation_id,
            "workspace_id": self.workspace_id,
            "window_id": self.window_id,
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> "ReviewWorkspaceCleanup":
        if set(raw) != {
            "schema_version",
            "review_operation_id",
            "workspace_id",
            "window_id",
            "status",
        } or raw.get("schema_version") != 1:
            raise ValueError("review workspace cleanup receipt is invalid")
        value = cls(
            review_operation_id=str(raw.get("review_operation_id") or ""),
            workspace_id=str(raw.get("workspace_id") or ""),
            window_id=str(raw.get("window_id") or ""),
            status=str(raw.get("status") or ""),
        )
        if (
            not IDENTIFIER.fullmatch(value.review_operation_id)
            or not SURFACE_UUID.fullmatch(value.workspace_id)
            or not SURFACE_UUID.fullmatch(value.window_id)
            or value.status not in {"closed", "already-gone"}
        ):
            raise ValueError("review workspace cleanup receipt is invalid")
        return value


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def close_review_workspace(
    root: Path,
    runtime: object,
    round_store: object,
    state: Mapping[str, object],
) -> ReviewWorkspaceCleanup:
    """Release a shared review workspace only at a quiescent terminal boundary."""

    if state.get("status") not in {"approved", "changes-requested"}:
        raise ValueError("review workspace cleanup requires a terminal program")
    raw_binding = state.get("review_workspace")
    raw_lanes = state.get("lanes")
    owner_id = str(state.get("owner_id") or "")
    operation_id = str(state.get("active_review_operation_id") or "")
    if (
        not isinstance(raw_binding, Mapping)
        or not isinstance(raw_lanes, list)
        or not raw_lanes
        or not IDENTIFIER.fullmatch(owner_id)
        or not IDENTIFIER.fullmatch(operation_id)
    ):
        raise ValueError("review workspace cleanup authority is incomplete")
    binding = ReviewWorkspaceBinding.from_payload(raw_binding)
    if binding.review_operation_id != operation_id:
        raise ValueError("review workspace cleanup operation changed")
    for raw_lane in raw_lanes:
        if not isinstance(raw_lane, Mapping):
            raise ValueError("review workspace cleanup lane is invalid")
        lane_operation = str(raw_lane.get("operation_id") or "")
        if (
            not IDENTIFIER.fullmatch(lane_operation)
            or raw_lane.get("state") != "complete"
            or raw_lane.get("surface_id")
            or str(raw_lane.get("workspace_id") or "").casefold()
            != binding.workspace_id.casefold()
            or str(raw_lane.get("window_id") or "").casefold()
            != binding.window_id.casefold()
        ):
            raise ValueError("review workspace cleanup lanes are not quiescent")
        record = round_store.read(owner_id, lane_operation)
        if (
            not isinstance(record, OperationRecord)
            or record.state != "complete"
            or record.resources != OwnedResources()
        ):
            raise ValueError("review workspace cleanup resources remain owned")

    receipt_path = root / operation_id / "workspace-cleanup.json"
    expected_identity = (
        operation_id,
        binding.workspace_id.casefold(),
        binding.window_id.casefold(),
    )
    if receipt_path.is_symlink():
        raise ValueError("review workspace cleanup receipt is invalid")
    if receipt_path.exists():
        try:
            raw_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("review workspace cleanup receipt is invalid") from exc
        if not isinstance(raw_receipt, Mapping):
            raise ValueError("review workspace cleanup receipt is invalid")
        receipt = ReviewWorkspaceCleanup.from_payload(raw_receipt)
        observed_identity = (
            receipt.review_operation_id,
            receipt.workspace_id.casefold(),
            receipt.window_id.casefold(),
        )
        if observed_identity != expected_identity:
            raise ValueError("review workspace cleanup receipt identity changed")
        return receipt

    closer = getattr(runtime, "close_workspace", None)
    if not callable(closer):
        raise ValueError("review runtime cannot close an exact workspace")
    status = closer(binding.workspace_id, binding.window_id)
    receipt = ReviewWorkspaceCleanup(
        operation_id,
        binding.workspace_id,
        binding.window_id,
        str(status),
    )
    ReviewWorkspaceCleanup.from_payload(receipt.payload())
    _atomic_json(receipt_path, receipt.payload())
    return receipt


def late_started_workspace_binding(
    session: Mapping[str, object],
    receipt: Mapping[str, object],
    *,
    review_operation_id: str,
    lane_operation_id: str,
    lane_run_id: str,
) -> ReviewWorkspaceBinding:
    """Recover exact workspace authority from one late-start session receipt."""

    if (
        session.get("schema_version") != 1
        or session.get("operation_id") != lane_operation_id
        or session.get("run_id") != lane_run_id
    ):
        raise ValueError("late-start review session identity changed")
    return ReviewWorkspaceBinding(
        review_operation_id=review_operation_id,
        workspace_id=str(session.get("workspace_id") or ""),
        workspace_ref=str(session.get("workspace_ref") or ""),
        window_id=str(session.get("window_id") or ""),
        window_ref=str(session.get("window_ref") or ""),
        anchor_surface_id=str(receipt.get("surface_id") or ""),
        anchor_surface_ref=str(session.get("surface_ref") or ""),
    )


def recovered_review_lane_payload(
    stored_lanes: list[object],
    lane: object,
    parent: OperationRecord,
    *,
    post_cleanup: bool,
    workspace: ReviewWorkspaceBinding,
) -> dict[str, object]:
    """Project one exact recovered lane without expanding gate branch authority."""

    if post_cleanup and stored_lanes:
        return {
            **dict(stored_lanes[0]),
            "surface_id": "",
            "state": "complete",
        }
    return {
        "axis": str(getattr(lane, "axis")),
        "operation_id": str(getattr(lane, "operation_id")),
        "lane_id": str(getattr(lane, "lane_id")),
        "run_id": str(getattr(lane, "run_id")),
        "surface_id": parent.resources.surface_id,
        "checkpoint": "",
        "verification_iteration": int(
            getattr(lane, "verification_iteration")
        ),
        "state": parent.state,
        "workspace_id": workspace.workspace_id,
        "window_id": workspace.window_id,
    }


def close_terminal_review_workspace(
    gate: object,
    ledger: object,
    terminal_result: str,
) -> bool:
    """Close ordinary terminal programs; retain cycle three for its pivot."""

    if terminal_result not in {"approved", "changes-requested"}:
        return False
    snapshot = ledger.snapshot()
    if terminal_result == "changes-requested" and pivot_required(snapshot):
        return True
    gate.close_terminal_workspace()
    return False


__all__ = [
    "ReviewWorkspaceBinding",
    "ReviewWorkspaceCleanup",
    "close_review_workspace",
    "close_terminal_review_workspace",
    "late_started_workspace_binding",
    "recovered_review_lane_payload",
]
