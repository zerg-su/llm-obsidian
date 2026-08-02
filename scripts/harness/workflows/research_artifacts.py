"""Exact synthesis bundle and provenance handling for protected research."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Mapping

from ..contracts import OperationRecord, OperationSpec
from research_contract import ResearchContractError
from .research_contracts import ResearchOperationRequest, ResearchStore


def _copy_file_exact(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ResearchContractError("research bundle source must be a regular file")
    if target.is_symlink():
        raise ResearchContractError("research bundle target must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            raise ResearchContractError(
                "research bundle target changed during idempotent replay"
            )
        return
    shutil.copy2(source, target)


def _synth_provenance_path(
    store: ResearchStore,
    operation_id: str,
    owner_id: str,
) -> Path:
    root = Path(store.root).expanduser().resolve()
    return (
        root
        / "owners"
        / owner_id
        / "runtime"
        / operation_id
        / "research-input.json"
    )


def _synth_provenance_value(
    request: ResearchOperationRequest,
    synth: OperationSpec,
    synth_run_id: str,
    artifact: Mapping[str, object],
    artifact_path: Path,
) -> dict[str, object]:
    try:
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ResearchContractError(
            "synthesis input artifact is unreadable"
        ) from exc
    return {
        "schema_version": 1,
        "operation_id": synth.operation_id,
        "run_id": synth_run_id,
        "fetch_run_id": artifact["run_id"],
        "request_sha256": request.context.request_sha256,
        "artifact_sha256": artifact_sha256,
    }


def _pin_synth_provenance(
    request: ResearchOperationRequest,
    store: ResearchStore,
    synth: OperationSpec,
    synth_run_id: str,
    synth_cwd: Path,
    artifact: Mapping[str, object],
) -> None:
    path = _synth_provenance_path(
        store,
        synth.operation_id,
        request.owner_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.is_symlink():
        raise ResearchContractError(
            "synthesis input provenance must not be a symlink"
        )
    value = _synth_provenance_value(
        request,
        synth,
        synth_run_id,
        artifact,
        synth_cwd / "artifact.json",
    )
    encoded = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ResearchContractError(
                "synthesis input provenance is unreadable"
            ) from exc
        if existing != encoded:
            raise ResearchContractError(
                "synthesis input provenance changed"
            )
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _verify_synth_provenance(
    request: ResearchOperationRequest,
    store: ResearchStore,
    synth: OperationRecord,
    synth_cwd: Path,
    artifact: Mapping[str, object],
) -> None:
    path = _synth_provenance_path(
        store,
        synth.spec.operation_id,
        request.owner_id,
    )
    if path.is_symlink():
        raise ResearchContractError(
            "synthesis input provenance must not be a symlink"
        )
    expected = _synth_provenance_value(
        request,
        synth.spec,
        synth.run_id,
        artifact,
        synth_cwd / "artifact.json",
    )
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchContractError(
            "synthesis input provenance is unreadable"
        ) from exc
    if recorded != expected:
        raise ResearchContractError(
            "synthesis input changed after accepted fetch validation"
        )


def _prepare_synthesis_bundle(
    request: ResearchOperationRequest,
    fetch_cwd: Path,
    synth_cwd: Path,
    artifact: Mapping[str, object],
) -> None:
    fetch_cwd = fetch_cwd.expanduser().resolve()
    synth_cwd = synth_cwd.expanduser().resolve()
    synth_cwd.mkdir(parents=True, exist_ok=True)
    _copy_file_exact(fetch_cwd / "artifact.json", synth_cwd / "artifact.json")
    for source in artifact["sources"]:
        pointer = Path(str(source["content_path"]))
        _copy_file_exact(fetch_cwd / pointer, synth_cwd / pointer)
    manifest = Path(request.context.manifest)
    context_source = fetch_cwd / manifest
    context_target = synth_cwd / manifest
    _copy_file_exact(context_source, context_target)
    packet_source = context_source.parent
    for path in packet_source.iterdir():
        if path.name == context_source.name:
            continue
        if path.is_file() and not path.is_symlink():
            _copy_file_exact(path, context_target.parent / path.name)
