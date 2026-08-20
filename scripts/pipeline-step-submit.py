#!/usr/bin/env python3
"""Validate one typed pipeline-step result and create its callback outbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import NoReturn

from harness.contracts import CallbackEnvelope, ContractError, to_dict
from harness.artifact_repair import (
    ArtifactRepairError,
    ContractArtifactOwner,
)
from harness.workflows.engineering_fix import FIX_PHASES, PHASE_SCHEMAS


REQUEST_NAME = ".task-pipeline-step-request.json"
OUTBOX_NAME = ".task-pipeline-step-callback.json"
MAX_REQUEST_BYTES = 65_536
MAX_RESULT_BYTES = 8_192
MAX_OUTPUT_BYTES = 65_536
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SCHEMA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID_RE = re.compile(r"[0-9a-f]{40,64}\Z")
FIX_REQUEST_FIELDS = {
    "schema_version",
    "operation_id",
    "run_id",
    "parent_operation_id",
    "lane_id",
    "definition_sha256",
    "step_id",
    "iteration",
    "input_schema",
    "input_sha256",
    "input_head_sha",
    "prior_receipt_sha256",
    "verification_sha256",
    "output_schema",
    "result_pointer",
    "output_pointer",
    "contract_template_pointer",
}
CUSTOM_REQUEST_FIELDS = {
    "schema_version",
    "workflow_kind",
    "operation_id",
    "run_id",
    "parent_operation_id",
    "lane_id",
    "definition_sha256",
    "step_id",
    "visit",
    "input_schema",
    "input_sha256",
    "input_head_sha",
    "prior_receipt_sha256",
    "output_schema",
    "allowed_outcomes",
    "result_pointer",
    "output_pointer",
    "contract_template_pointer",
}
FIX_RESULT_FIELDS = {
    "schema_version",
    "status",
    "output_sha256",
    "head_sha",
}
CUSTOM_RESULT_FIELDS = FIX_RESULT_FIELDS | {"outcome"}


class SubmitError(RuntimeError):
    """The model-provided phase result is not safe to submit."""

    failure_class = "code-authority"
    error_code = "submit-authority-rejected"


class ModelSemanticError(SubmitError):
    """The request is authoritative but model-owned result semantics are invalid."""

    failure_class = "model-semantic"
    error_code = "result-semantics-rejected"


class SubmitMechanismError(SubmitError):
    """The submit chokepoint itself violated an invariant."""

    failure_class = "mechanism"
    error_code = "submit-mechanism-failed"


def die(error: SubmitError, code: int = 2) -> NoReturn:
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "rejected",
                "failure_class": error.failure_class,
                "error_code": error.error_code,
                "detail": str(error)[:500],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=os.sys.stderr,
    )
    raise SystemExit(code)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise SubmitError(f"{label} must be a bounded identifier")
    return value


def _sha256(value: object, label: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SubmitError(f"{label} must be a lowercase sha256")
    return value


def _git_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or not GIT_OID_RE.fullmatch(value):
        raise SubmitError(f"{label} must be a Git object id")
    return value


def _read_json(path: Path, *, limit: int, label: str) -> dict[str, object]:
    try:
        if path.is_symlink():
            raise SubmitError(f"{label} cannot be a symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SubmitError(f"{label} must be a regular file")
        if info.st_size <= 0 or info.st_size > limit:
            raise SubmitError(f"{label} exceeds its bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SubmitError(f"{label} is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SubmitError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise SubmitError(f"{label} must contain an object")
    return value


def _pointer(root: Path, value: object, label: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SubmitError(f"{label} must be owner-relative")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or "." in relative.parts
        or ".." in relative.parts
        or relative.parts[0] == ".git"
    ):
        raise SubmitError(f"{label} must be owner-relative")
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise SubmitError(f"{label} cannot be a symlink")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SubmitError(f"{label} must be owner-relative") from exc
    return relative.as_posix(), resolved


def _regular_bytes(path: Path, *, limit: int, label: str) -> bytes:
    try:
        if path.is_symlink():
            raise SubmitError(f"{label} cannot be a symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SubmitError(f"{label} must be a regular file")
        if info.st_size <= 0 or info.st_size > limit:
            raise SubmitError(f"{label} exceeds its bounded size")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise SubmitError(f"{label} is missing") from exc
    except OSError as exc:
        raise SubmitError(f"{label} is unreadable") from exc


def _contract_owner(
    worktree: Path,
    request: dict[str, object],
    result_path: Path,
) -> ContractArtifactOwner:
    raw = request.get("contract_template_pointer")
    if not isinstance(raw, str) or not raw:
        raise SubmitError("contract template pointer is missing")
    sidecar = Path(raw).expanduser()
    if not sidecar.is_absolute():
        raise SubmitError("contract template pointer must be absolute")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise SubmitError("contract template sidecar is invalid")
    try:
        # .../<state-root>/contract-templates/<family>/<attempt>.json
        state_root = sidecar.parents[2]
        owner = ContractArtifactOwner.load(
            state_root=state_root,
            worktree=worktree,
            family="pipeline-step-result",
            attempt_id=str(request.get("operation_id") or ""),
        )
    except (ArtifactRepairError, IndexError) as exc:
        raise SubmitError("contract template sidecar is invalid") from exc
    if owner.sidecar_path != sidecar.resolve() or owner.actual_target != result_path:
        raise SubmitError("contract template binding changed")
    return owner


def _head(worktree: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise SubmitError("Git HEAD is unavailable") from exc
    value = result.stdout.strip()
    if result.returncode or not GIT_OID_RE.fullmatch(value):
        raise SubmitError("Git HEAD is unavailable")
    return value


def _validate_fix_request(
    worktree: Path, request: dict[str, object]
) -> tuple[dict[str, object], Path, str, Path]:
    if set(request) != FIX_REQUEST_FIELDS:
        raise SubmitError("request keys changed")
    if request.get("schema_version") != 1:
        raise SubmitError("request schema is unsupported")
    for field in (
        "operation_id",
        "run_id",
        "parent_operation_id",
        "lane_id",
    ):
        _identifier(request.get(field), field)
    definition = _sha256(
        request.get("definition_sha256"), "definition_sha256"
    )
    step_id = str(request.get("step_id") or "")
    if step_id not in FIX_PHASES:
        raise SubmitError("step_id is not an engineering/fix phase")
    iteration = request.get("iteration")
    if (
        not isinstance(iteration, int)
        or isinstance(iteration, bool)
        or iteration < 0
    ):
        raise SubmitError("iteration must be non-negative")
    input_schema, output_schema = PHASE_SCHEMAS[step_id]
    if (
        request.get("input_schema") != input_schema
        or request.get("output_schema") != output_schema
    ):
        raise SubmitError("phase schemas changed")
    input_sha256 = _sha256(request.get("input_sha256"), "input_sha256")
    input_head_sha = _git_oid(
        request.get("input_head_sha"), "input_head_sha"
    )
    prior = _sha256(
        request.get("prior_receipt_sha256"),
        "prior_receipt_sha256",
        optional=True,
    )
    if (step_id == "reproduce") != (prior == ""):
        raise SubmitError("prior receipt binding does not match the phase")
    verification = _sha256(
        request.get("verification_sha256"),
        "verification_sha256",
        optional=True,
    )
    if iteration == 0:
        if verification:
            raise SubmitError(
                "initial verification_sha256 binding must be empty"
            )
    elif not verification:
        raise SubmitError(
            "retry verification_sha256 binding must be present"
        )
    elif step_id == "reproduce":
        raise SubmitError("retry cannot repeat the reproduce phase")
    result_pointer, result_path = _pointer(
        worktree, request.get("result_pointer"), "result_pointer"
    )
    output_pointer, output_path = _pointer(
        worktree, request.get("output_pointer"), "output_pointer"
    )
    reserved = {REQUEST_NAME, OUTBOX_NAME}
    if (
        result_pointer in reserved
        or output_pointer in reserved
        or result_path == output_path
    ):
        raise SubmitError("result and output pointers must be distinct")
    normalized = {
        **request,
        "workflow_kind": "fix",
        "definition_sha256": definition,
        "step_id": step_id,
        "iteration": iteration,
        "input_schema": input_schema,
        "input_sha256": input_sha256,
        "input_head_sha": input_head_sha,
        "prior_receipt_sha256": prior,
        "verification_sha256": verification,
        "output_schema": output_schema,
        "result_pointer": result_pointer,
        "output_pointer": output_pointer,
        "contract_template_pointer": request["contract_template_pointer"],
    }
    return normalized, result_path, output_pointer, output_path


def _validate_custom_request(
    worktree: Path, request: dict[str, object]
) -> tuple[dict[str, object], Path, str, Path]:
    if set(request) != CUSTOM_REQUEST_FIELDS:
        raise SubmitError("custom request keys changed")
    if request.get("schema_version") != 1 or request.get("workflow_kind") != "custom":
        raise SubmitError("custom request schema is unsupported")
    for field in ("operation_id", "run_id", "parent_operation_id", "lane_id"):
        _identifier(request.get(field), field)
    definition = _sha256(request.get("definition_sha256"), "definition_sha256")
    step_id = _identifier(request.get("step_id"), "step_id")
    visit = request.get("visit")
    if type(visit) is not int or visit < 0:
        raise SubmitError("visit must be non-negative")
    input_schema = request.get("input_schema")
    output_schema = request.get("output_schema")
    if not isinstance(input_schema, str) or not SCHEMA_RE.fullmatch(input_schema):
        raise SubmitError("input_schema must be a bounded schema identifier")
    if not isinstance(output_schema, str) or not SCHEMA_RE.fullmatch(output_schema):
        raise SubmitError("output_schema must be a bounded schema identifier")
    input_sha256 = _sha256(request.get("input_sha256"), "input_sha256")
    input_head_sha = _git_oid(request.get("input_head_sha"), "input_head_sha")
    prior = _sha256(
        request.get("prior_receipt_sha256"),
        "prior_receipt_sha256",
        optional=True,
    )
    if (visit == 0) != (prior == ""):
        raise SubmitError("prior receipt binding does not match the visit")
    allowed = request.get("allowed_outcomes")
    if (
        not isinstance(allowed, list)
        or not allowed
        or len(allowed) > 8
        or any(not isinstance(item, str) or not IDENTIFIER_RE.fullmatch(item) for item in allowed)
        or len(set(allowed)) != len(allowed)
        or allowed != sorted(allowed)
    ):
        raise SubmitError("allowed_outcomes must be a bounded sorted set")
    result_pointer, result_path = _pointer(
        worktree, request.get("result_pointer"), "result_pointer"
    )
    output_pointer, output_path = _pointer(
        worktree, request.get("output_pointer"), "output_pointer"
    )
    reserved = {REQUEST_NAME, OUTBOX_NAME}
    if (
        result_pointer in reserved
        or output_pointer in reserved
        or result_path == output_path
    ):
        raise SubmitError("result and output pointers must be distinct")
    return (
        {
            **request,
            "definition_sha256": definition,
            "step_id": step_id,
            "visit": visit,
            "input_schema": input_schema,
            "input_sha256": input_sha256,
            "input_head_sha": input_head_sha,
            "prior_receipt_sha256": prior,
            "output_schema": output_schema,
            "allowed_outcomes": allowed,
            "result_pointer": result_pointer,
            "output_pointer": output_pointer,
            "contract_template_pointer": request["contract_template_pointer"],
        },
        result_path,
        output_pointer,
        output_path,
    )


def _validate_request(
    worktree: Path, request: dict[str, object]
) -> tuple[dict[str, object], Path, str, Path]:
    if request.get("workflow_kind") == "custom":
        return _validate_custom_request(worktree, request)
    return _validate_fix_request(worktree, request)


def _envelope(
    worktree: Path,
    request: dict[str, object],
    result_path: Path,
    output_pointer: str,
    output_path: Path,
) -> CallbackEnvelope:
    custom = request.get("workflow_kind") == "custom"
    output = _regular_bytes(
        output_path, limit=MAX_OUTPUT_BYTES, label="output file"
    )
    observed_output_sha256 = hashlib.sha256(output).hexdigest()
    observed_head = _head(worktree)
    owner = _contract_owner(worktree, request, result_path)
    try:
        repaired = owner.repair(
            authoritative_fields={
                "schema_version": 1,
                "output_sha256": observed_output_sha256,
                "head_sha": observed_head,
            }
        )
    except ArtifactRepairError as exc:
        raise ModelSemanticError(f"result contract repair failed: {exc}") from exc
    result = dict(repaired.value)
    expected_result_fields = CUSTOM_RESULT_FIELDS if custom else FIX_RESULT_FIELDS
    if set(result) != expected_result_fields:
        raise SubmitMechanismError("result keys changed")
    if result.get("schema_version") != 1:
        raise SubmitMechanismError("result schema is unsupported")
    status = str(result.get("status") or "")
    if status not in ({"complete"} if custom else {"complete", "cannot-reproduce"}):
        raise ModelSemanticError("result status is invalid")
    if (
        not custom
        and status == "cannot-reproduce"
        and request["step_id"] != "reproduce"
    ):
        raise ModelSemanticError(
            "cannot-reproduce is valid only for the reproduce phase"
        )
    if custom:
        try:
            outcome = _identifier(result.get("outcome"), "result outcome")
        except SubmitError as exc:
            raise ModelSemanticError(str(exc)) from exc
        if outcome not in request["allowed_outcomes"]:
            raise ModelSemanticError("result outcome is not allowed by the request")
        payload = {
            "schema_version": 1,
            "parent_operation_id": request["parent_operation_id"],
            "definition_sha256": request["definition_sha256"],
            "step_id": request["step_id"],
            "visit": request["visit"],
            "input_schema": request["input_schema"],
            "input_sha256": request["input_sha256"],
            "input_head_sha": request["input_head_sha"],
            "prior_receipt_sha256": request["prior_receipt_sha256"],
            "output_schema": request["output_schema"],
            "output_pointer": output_pointer,
            "output_sha256": observed_output_sha256,
            "head_sha": observed_head,
            "outcome": outcome,
        }
    else:
        payload = {
            "schema_version": 1,
            "parent_operation_id": request["parent_operation_id"],
            "definition_sha256": request["definition_sha256"],
            "step_id": request["step_id"],
            "iteration": request["iteration"],
            "input_schema": request["input_schema"],
            "input_sha256": request["input_sha256"],
            "input_head_sha": request["input_head_sha"],
            "prior_receipt_sha256": request["prior_receipt_sha256"],
            "verification_sha256": request["verification_sha256"],
            "output_schema": request["output_schema"],
            "output_pointer": output_pointer,
            "output_sha256": observed_output_sha256,
            "head_sha": observed_head,
            "status": status,
        }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    try:
        return CallbackEnvelope(
            callback_id=f"result-{payload_sha256[:24]}",
            operation_id=str(request["operation_id"]),
            run_id=str(request["run_id"]),
            kind="result",
            payload=payload,
            payload_sha256=payload_sha256,
        )
    except ContractError as exc:
        raise SubmitError("callback envelope is invalid") from exc


def _write_outbox(path: Path, envelope: CallbackEnvelope) -> None:
    encoded = (
        json.dumps(
            to_dict(envelope),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()

    def exact_existing_outbox() -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            return path.stat().st_size == len(encoded) and path.read_bytes() == encoded
        except OSError as exc:
            raise SubmitError("callback outbox is unreadable") from exc

    if path.exists() or path.is_symlink():
        if exact_existing_outbox():
            return
        raise SubmitError("callback outbox already exists")
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            if exact_existing_outbox():
                return
            raise SubmitError("callback outbox already exists") from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def submit(worktree: Path) -> CallbackEnvelope:
    root = worktree.expanduser().resolve()
    if not root.is_dir():
        raise SubmitError("worktree must be an existing directory")
    request_path = root / REQUEST_NAME
    request = _read_json(
        request_path, limit=MAX_REQUEST_BYTES, label="request file"
    )
    normalized, result_path, output_pointer, output_path = _validate_request(
        root, request
    )
    envelope = _envelope(
        root,
        normalized,
        result_path,
        output_pointer,
        output_path,
    )
    _write_outbox(root / OUTBOX_NAME, envelope)
    return envelope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    args = parser.parse_args()
    try:
        envelope = submit(args.worktree)
    except SubmitError as exc:
        die(exc)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "submitted",
                "operation_id": envelope.operation_id,
                "callback_id": envelope.callback_id,
                "outbox": OUTBOX_NAME,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
