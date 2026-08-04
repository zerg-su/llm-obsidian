"""Durable request, result, and runtime-state contracts for protected research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping, Protocol

from ..contracts import (
    AttentionReason,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)
from ..runtime_sessions import RuntimeSessionRequest
from ..state_machine import TERMINAL


def _relative_pointer(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
    ):
        raise ValueError(f"{label} must be an owner-relative pointer")
    return path.as_posix()


@dataclass(frozen=True)
class ResearchRequest:
    """Public safe/unsafe selection; unsafe is never inferred or a fallback."""

    operation_id: str
    query_pointer: str
    context_manifest: str
    unsafe: bool = False
    context_scope: str = "minimal"
    unsafe_authorized: bool = False

    def __post_init__(self) -> None:
        _relative_pointer(self.query_pointer, "research query")
        _relative_pointer(self.context_manifest, "research context manifest")
        if self.unsafe:
            if self.context_scope != "full-explicit" or not self.unsafe_authorized:
                raise ValueError(
                    "unsafe research requires explicit full-context authorization"
                )
        elif self.context_scope != "minimal" or self.unsafe_authorized:
            raise ValueError("safe research accepts only minimal context")


@dataclass(frozen=True)
class ResearchContext:
    """Content-free identity for one minimal ContextPacket."""

    manifest: str
    request_sha256: str
    scope: str = "minimal"

    def __post_init__(self) -> None:
        _relative_pointer(self.manifest, "research context manifest")
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_sha256):
            raise ValueError("research request digest must be a sha256")
        if self.scope != "minimal":
            raise ValueError("safe research context must be minimal")


@dataclass(frozen=True)
class ResearchOperationRequest:
    policy: ResearchRequest
    owner_id: str
    route: RuntimeRoute
    context: ResearchContext

    def __post_init__(self) -> None:
        if self.policy.unsafe:
            raise ValueError("unsafe research stays in the explicitly authorized current session")
        if self.route.profile != "research-safe":
            raise ValueError("safe research requires the research-safe route")
        if self.policy.context_manifest != self.context.manifest:
            raise ValueError("research policy and ContextPacket identity disagree")


class ResearchStore(Protocol):
    """Narrow durable-store seam used by protected research."""

    root: Path

    def create(
        self,
        spec: OperationSpec,
        *,
        lane_id: str,
        run_id: str,
    ) -> OperationRecord: ...

    def read(self, owner_id: str, operation_id: str) -> OperationRecord: ...

    def transition(
        self,
        owner_id: str,
        operation_id: str,
        state: str,
        *,
        reason: AttentionReason | None = None,
    ) -> object: ...


class ResearchRuntime(Protocol):
    """Narrow generic runtime surface used by protected research."""

    def start(
        self,
        request: RuntimeSessionRequest,
        *,
        on_surface_opened: object | None = None,
    ) -> object: ...

    def request_exit(self, owner_id: str, operation_id: str) -> object: ...

    def cleanup(self, owner_id: str, operation_id: str) -> object: ...


def fetch_callback_payload(
    *, artifact_sha256: str, source_count: int
) -> dict[str, object]:
    """Build the one durable callback payload for a completed fetch stage."""

    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise ValueError("research artifact digest must be a sha256")
    if isinstance(source_count, bool) or source_count < 0:
        raise ValueError("research source count must be non-negative")
    return {
        "stage": "fetch",
        "artifact_path": "artifact.json",
        "artifact_sha256": artifact_sha256,
        "source_count": source_count,
    }


def research_callback_identity(
    payload: Mapping[str, object],
) -> tuple[str, str]:
    """Return the canonical callback id and digest for a research payload."""

    stage = payload.get("stage")
    if stage not in {"fetch", "synth"}:
        raise ValueError("research callback stage is invalid")
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f"research-{stage}-{digest[:24]}", digest


@dataclass(frozen=True)
class ResearchExecution:
    request: ResearchOperationRequest
    parent: OperationRecord
    fetch: OperationRecord
    synth: OperationRecord | None
    stage: str
    result_artifact: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.stage not in {
            "fetch",
            "fetch-cleanup",
            "synth",
            "synth-cleanup",
            "complete",
        }:
            raise ValueError("research execution stage is invalid")
        if self.result_artifact is not None:
            object.__setattr__(
                self,
                "result_artifact",
                MappingProxyType(dict(self.result_artifact)),
            )


@dataclass(frozen=True)
class PreparedResearch:
    request: ResearchOperationRequest
    root: Path
    fetch_cwd: Path
    synth_cwd: Path
    fetch_runtime_home: Path
    synth_runtime_home: Path


def operation_spec(request: ResearchOperationRequest) -> OperationSpec:
    identity = {
        "operation_id": request.policy.operation_id,
        "owner_id": request.owner_id,
        "query_pointer": request.policy.query_pointer,
        "context_manifest": request.context.manifest,
        "request_sha256": request.context.request_sha256,
        "scope": request.context.scope,
        "route": {
            "runtime": request.route.runtime,
            "model": request.route.model,
            "effort": request.route.effort,
            "profile": request.route.profile,
            "routing_sha256": request.route.routing_sha256,
        },
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return OperationSpec(
        operation_id=request.policy.operation_id,
        idempotency_key=hashlib.sha256(canonical).hexdigest(),
        kind="research",
        owner_id=request.owner_id,
        route=request.route,
        context_manifest=request.context.manifest,
        verification_profile="research-cited-artifact",
    )


def enqueue(
    request: ResearchOperationRequest,
    store: ResearchStore,
) -> OperationRecord:
    """Persist one context-ready safe research operation through the harness seam."""

    spec = operation_spec(request)
    lane_id = hashlib.sha256(f"{spec.idempotency_key}:lane".encode()).hexdigest()[:32]
    run_id = hashlib.sha256(f"{spec.idempotency_key}:run".encode()).hexdigest()[:32]
    return store.create(spec, lane_id=lane_id, run_id=run_id)


def _derived_id(parent: str, stage: str) -> str:
    suffix = f"-{stage}-{hashlib.sha256(stage.encode()).hexdigest()[:8]}"
    return f"{parent[: 128 - len(suffix)]}{suffix}"


def _stage_spec(request: ResearchOperationRequest, stage: str) -> OperationSpec:
    if stage not in {"fetch", "synth"}:
        raise ValueError("research stage must be fetch or synth")
    base = operation_spec(request)
    operation_id = _derived_id(base.operation_id, stage)
    identity = (
        f"{base.idempotency_key}:{stage}:{operation_id}:"
        f"{base.route.runtime}:{base.route.model}:{base.route.effort}".encode()
    )
    return replace(
        base,
        operation_id=operation_id,
        idempotency_key=hashlib.sha256(identity).hexdigest(),
        kind=f"research-{stage}",
        parent_operation_id=base.operation_id,
    )


def _stage_identity(
    request: ResearchOperationRequest, stage: str
) -> tuple[OperationSpec, str, str]:
    spec = _stage_spec(request, stage)
    lane_id = hashlib.sha256(
        f"{spec.idempotency_key}:lane".encode()
    ).hexdigest()[:32]
    run_id = hashlib.sha256(
        f"{spec.idempotency_key}:run".encode()
    ).hexdigest()[:32]
    return spec, lane_id, run_id


def _record(value: object) -> OperationRecord:
    record = (
        value
        if isinstance(value, OperationRecord)
        else getattr(value, "record", None)
    )
    if not isinstance(record, OperationRecord):
        raise ValueError("research runtime returned no typed operation record")
    return record


def _advance_parent(
    store: ResearchStore,
    record: OperationRecord,
    states: tuple[str, ...],
) -> OperationRecord:
    current = record
    for state in states:
        if current.state == state:
            continue
        store.transition(
            current.spec.owner_id,
            current.spec.operation_id,
            state,
        )
        current = store.read(
            current.spec.owner_id,
            current.spec.operation_id,
        )
    return current


def _runtime_request(
    request: ResearchOperationRequest,
    stage: str,
    *,
    origin_surface: str,
    cwd: Path,
    runtime_home: Path,
    callback_wake: str,
) -> RuntimeSessionRequest:
    spec, lane_id, run_id = _stage_identity(request, stage)
    prompt_pointer = (
        "fetch-prompt.md" if stage == "fetch" else "synth-prompt.md"
    )
    callback_pointer = "artifact.json" if stage == "fetch" else "complete.json"
    values = {
        "spec": spec,
        "lane_id": lane_id,
        "run_id": run_id,
        "origin_surface": origin_surface,
        "cwd": cwd,
        "prompt_pointer": prompt_pointer,
        "callback_pointer": callback_pointer,
        "callback_mode": f"research-{stage}",
        "runtime_home": runtime_home,
        "research_request_sha256": (
            request.context.request_sha256 if stage == "fetch" else ""
        ),
        "callback_wake": callback_wake,
    }
    return RuntimeSessionRequest(**values)


def _finish_stage(
    runtime: ResearchRuntime,
    store: ResearchStore,
    record: OperationRecord,
) -> OperationRecord:
    if record.state in TERMINAL:
        return record
    if record.state == "finalizing":
        runtime.request_exit(record.spec.owner_id, record.spec.operation_id)
        record = store.read(record.spec.owner_id, record.spec.operation_id)
    if record.state == "exiting":
        runtime.cleanup(record.spec.owner_id, record.spec.operation_id)
        record = store.read(record.spec.owner_id, record.spec.operation_id)
    return record
