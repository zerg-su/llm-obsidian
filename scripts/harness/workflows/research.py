"""Safe two-stage research workflow over the generic provider runtime.

This compatibility façade preserves the original import surface while the
durable request/state, sandbox, and artifact boundaries live in collaborators.

Source-audit compatibility: the sandbox retains
``stage == 'fetch' else 'disabled'``, treats fetched material as
``UNTRUSTED DATA``, and isolates stage homes below ``codex-home-``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping, Protocol

from ..context import ContextBuilder, ContextInput
from ..contracts import (
    AttentionReason,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)
from ..runtime_sessions import RuntimeSessionRequest
from ..state_machine import TERMINAL
from ..store import StoreError
from research_contract import (
    ResearchContractError,
    load_artifact,
    validate_result_artifact,
)
from .research_artifacts import (
    _copy_file_exact,
    _pin_synth_provenance,
    _prepare_synthesis_bundle,
    _synth_provenance_path,
    _synth_provenance_value,
    _verify_synth_provenance,
)
from .research_contracts import (
    PreparedResearch,
    ResearchContext,
    ResearchExecution,
    ResearchOperationRequest,
    ResearchRequest,
    ResearchRuntime,
    ResearchStore,
    _advance_parent,
    _derived_id,
    _finish_stage,
    _record,
    _relative_pointer,
    _runtime_request,
    _stage_identity,
    _stage_spec,
    enqueue,
    fetch_callback_payload,
    operation_spec,
    research_callback_identity,
)
from .research_sandbox import (
    _ensure_private_directory,
    _fetch_prompt,
    _permitted_runtime_roots,
    _runtime_home,
    _synth_prompt,
    _toml_string,
    prepare_research,
    research_runtime_config,
)


def _accepted_research_receipt(
    record: OperationRecord,
    payload: Mapping[str, object],
) -> bool:
    """Match a cancelled fetch stage to its exact durable callback payload."""

    callback_id, digest = research_callback_identity(payload)
    stage = payload.get("stage")
    return (
        stage == "fetch"
        and record.accepted_callback_kind == "research"
        and record.accepted_callback_sha256 == digest
        and record.accepted_callback_id == callback_id
    )


def start_research(
    request: ResearchOperationRequest,
    runtime: ResearchRuntime,
    store: ResearchStore,
    *,
    origin_surface: str,
    fetch_cwd: Path,
    fetch_runtime_home: Path,
    callback_wake: str,
) -> ResearchExecution:
    """Start the vaultless fetch stage through the generic runtime."""

    parent = enqueue(request, store)
    if parent.state == "created":
        parent = _advance_parent(
            store,
            parent,
            ("preflight", "starting", "running"),
        )
    fetch_spec, _lane_id, _run_id = _stage_identity(request, "fetch")
    try:
        fetch = store.read(request.owner_id, fetch_spec.operation_id)
    except StoreError:
        session = _runtime_request(
            request,
            "fetch",
            origin_surface=origin_surface,
            cwd=fetch_cwd,
            runtime_home=fetch_runtime_home,
            callback_wake=callback_wake,
        )
        fetch = _record(runtime.start(session))
    if parent.state == "running":
        parent = _advance_parent(store, parent, ("awaiting-callback",))
    return ResearchExecution(request, parent, fetch, None, "fetch")


def advance_research(
    request: ResearchOperationRequest,
    runtime: ResearchRuntime,
    store: ResearchStore,
    *,
    origin_surface: str,
    fetch_cwd: Path,
    synth_cwd: Path,
    synth_runtime_home: Path,
    callback_wake: str,
) -> ResearchExecution:
    """Validate one fetch receipt, clean it up, then start synthesis."""

    parent = store.read(request.owner_id, request.policy.operation_id)
    if parent.state in TERMINAL:
        raise ValueError("terminal research composition cannot be resumed")
    fetch_spec, _fetch_lane, fetch_run = _stage_identity(request, "fetch")
    fetch = store.read(request.owner_id, fetch_spec.operation_id)
    if fetch.state not in {"finalizing", "exiting", "complete", "cancelled"}:
        raise ValueError("research fetch callback has not been accepted")
    artifact_path = fetch_cwd.expanduser().resolve() / "artifact.json"
    artifact = load_artifact(
        str(artifact_path),
        expected_run_id=fetch_run,
        expected_request_sha256=request.context.request_sha256,
    )
    fetch_payload = fetch_callback_payload(
        artifact_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        source_count=len(artifact["sources"]),
    )
    recovered_cancelled = (
        fetch.state == "cancelled"
        and _accepted_research_receipt(fetch, fetch_payload)
    )
    if fetch.state == "cancelled" and not recovered_cancelled:
        raise ValueError("research fetch callback has not been accepted")
    if parent.state == "awaiting-callback":
        parent = _advance_parent(store, parent, ("verifying",))
    if not recovered_cancelled:
        fetch = _finish_stage(runtime, store, fetch)
    if fetch.state != "complete" and not recovered_cancelled:
        return ResearchExecution(
            request, parent, fetch, None, "fetch-cleanup"
        )
    _prepare_synthesis_bundle(request, fetch_cwd, synth_cwd, artifact)
    synth_spec, _synth_lane, synth_run = _stage_identity(request, "synth")
    _pin_synth_provenance(
        request,
        store,
        synth_spec,
        synth_run,
        synth_cwd.expanduser().resolve(),
        artifact,
    )
    try:
        synth = store.read(request.owner_id, synth_spec.operation_id)
    except StoreError:
        session = _runtime_request(
            request,
            "synth",
            origin_surface=origin_surface,
            cwd=synth_cwd,
            runtime_home=synth_runtime_home,
            callback_wake=callback_wake,
        )
        synth = _record(runtime.start(session))
    if parent.state == "verifying":
        parent = _advance_parent(
            store,
            parent,
            ("running", "awaiting-callback"),
        )
    return ResearchExecution(request, parent, fetch, synth, "synth")


def finalize_research(
    request: ResearchOperationRequest,
    runtime: ResearchRuntime,
    store: ResearchStore,
    *,
    synth_cwd: Path,
) -> ResearchExecution:
    """Validate the cited synthesis result and finish exact owned resources."""

    parent = store.read(request.owner_id, request.policy.operation_id)
    if parent.state in TERMINAL:
        raise ValueError("terminal research composition cannot be resumed")
    fetch_spec, _fetch_lane, fetch_run = _stage_identity(request, "fetch")
    fetch = store.read(request.owner_id, fetch_spec.operation_id)
    synth_spec, _synth_lane, synth_run = _stage_identity(request, "synth")
    synth = store.read(request.owner_id, synth_spec.operation_id)
    if synth.state not in {"finalizing", "exiting", "complete"}:
        raise ValueError("research synthesis callback has not been accepted")
    synth_cwd = synth_cwd.expanduser().resolve()
    artifact = load_artifact(
        str(synth_cwd / "artifact.json"),
        expected_run_id=fetch_run,
        expected_request_sha256=request.context.request_sha256,
    )
    _verify_synth_provenance(
        request,
        store,
        synth,
        synth_cwd,
        artifact,
    )
    result = validate_result_artifact(
        json.loads((synth_cwd / "complete.json").read_text(encoding="utf-8")),
        root=synth_cwd,
        expected_run_id=synth_run,
        source_urls={str(source["url"]) for source in artifact["sources"]},
    )
    synth = _finish_stage(runtime, store, synth)
    if synth.state != "complete":
        return ResearchExecution(
            request, parent, fetch, synth, "synth-cleanup"
        )
    if parent.state == "awaiting-callback":
        parent = _advance_parent(
            store,
            parent,
            ("finalizing", "exiting", "complete"),
        )
    result_summary = {
        "kind": result["artifact"]["kind"],
        "path": str(
            (synth_cwd / str(result["artifact"]["path"])).resolve()
        ),
        "sha256": result["artifact"]["sha256"],
        "citation_count": len(result["artifact"]["citations"]),
    }
    return ResearchExecution(
        request,
        parent,
        fetch,
        synth,
        "complete",
        result_summary,
    )


def status_research(
    request: ResearchOperationRequest,
    store: ResearchStore,
) -> ResearchExecution:
    """Read the exact composition state without probing or mutating resources."""

    parent = store.read(request.owner_id, request.policy.operation_id)
    fetch_spec, _fetch_lane, _fetch_run = _stage_identity(request, "fetch")
    fetch = store.read(request.owner_id, fetch_spec.operation_id)
    synth_spec, _synth_lane, _synth_run = _stage_identity(request, "synth")
    try:
        synth = store.read(request.owner_id, synth_spec.operation_id)
    except StoreError:
        synth = None
    if parent.state == "complete":
        stage = "complete"
    elif synth is not None:
        stage = (
            "synth-cleanup"
            if synth.state in {"finalizing", "exiting"}
            else "synth"
        )
    else:
        stage = (
            "fetch-cleanup"
            if fetch.state in {"finalizing", "exiting"}
            else "fetch"
        )
    return ResearchExecution(request, parent, fetch, synth, stage)
