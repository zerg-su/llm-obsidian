"""Exact durable workspace authority for one review program."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .contracts import OperationRecord
from .runtime_session_contracts import IDENTIFIER, SURFACE_UUID


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


__all__ = ["ReviewWorkspaceBinding"]
