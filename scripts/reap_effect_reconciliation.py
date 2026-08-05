"""Reconcile one completed durable reap with its pending harness effect."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dispatch_io import atomic_json
from harness.contracts import ContractError, EffectOutcome
from harness.store import OperationStore, StoreError


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
HEAD_RE = re.compile(r"[0-9a-f]{40}\Z")
ADDRESS_RE = re.compile(r"c-\d{6}\Z")
COMPLETION_KEYS = {
    "version",
    "task_name",
    "current_session",
    "result_path",
    "vault_root",
    "summary_sha256",
    "meta_sha256",
    "plan_path",
    "closed_plan_sha256",
    "result_sha256",
    "validated",
    "completed_at",
    "task_session_status",
}
PREPARED_REQUIRED = {
    "version",
    "task_name",
    "current_session",
    "result_path",
    "result_link",
    "vault_root",
    "summary_sha256",
    "meta_sha256",
    "approved_plan_sha256",
    "closed_plan_sha256",
    "plan_path",
    "review_archives",
    "prepared_date",
    "prepared_at",
    "exec_session",
}
PREPARED_OPTIONAL = {
    "previous_closed_plan_sha256",
    "previous_result_link",
    "review_archive_marker_sha256",
    "review_archive_path",
    "review_archive_wikilink",
}
CALLBACK_KEYS = {
    "schema_version",
    "status",
    "callback_id",
    "operation_id",
    "run_id",
    "payload_sha256",
}


class ReapEffectRecoveryError(ValueError):
    """A completed reap cannot be bound to the pending harness effect."""


@dataclass(frozen=True)
class ReapEffectRecoveryRequest:
    owner_id: str
    operation_id: str
    run_id: str
    accepted_callback_id: str
    accepted_callback_sha256: str
    prepared_receipt_sha256: str
    completion_receipt_sha256: str
    committed_vault_head: str
    result_address: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "owner_id": self.owner_id,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "accepted_callback_id": self.accepted_callback_id,
            "accepted_callback_sha256": self.accepted_callback_sha256,
            "prepared_receipt_sha256": self.prepared_receipt_sha256,
            "completion_receipt_sha256": self.completion_receipt_sha256,
            "committed_vault_head": self.committed_vault_head,
            "result_address": self.result_address,
        }


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ReapEffectRecoveryError(f"{label} must be a canonical UUID")
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ReapEffectRecoveryError(
            f"{label} must be a canonical UUID"
        ) from None
    if parsed != value:
        raise ReapEffectRecoveryError(f"{label} must be a canonical UUID")
    return value


def _bounded_string(value: object, label: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\0", "\n", "\r"))
    ):
        raise ReapEffectRecoveryError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ReapEffectRecoveryError(f"{label} must be a lowercase sha256")
    return value


def parse_recovery_request(value: object) -> ReapEffectRecoveryRequest:
    expected = {
        "schema_version",
        "owner_id",
        "operation_id",
        "run_id",
        "accepted_callback_id",
        "accepted_callback_sha256",
        "prepared_receipt_sha256",
        "completion_receipt_sha256",
        "committed_vault_head",
        "result_address",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ReapEffectRecoveryError("recovery request has an invalid shape")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ReapEffectRecoveryError("recovery request schema_version must be 1")
    head = value.get("committed_vault_head")
    address = value.get("result_address")
    if not isinstance(head, str) or HEAD_RE.fullmatch(head) is None:
        raise ReapEffectRecoveryError("committed_vault_head must be an exact Git oid")
    if not isinstance(address, str) or ADDRESS_RE.fullmatch(address) is None:
        raise ReapEffectRecoveryError("result_address must be c-NNNNNN")
    return ReapEffectRecoveryRequest(
        _canonical_uuid(value.get("owner_id"), "owner_id"),
        _canonical_uuid(value.get("operation_id"), "operation_id"),
        _canonical_uuid(value.get("run_id"), "run_id"),
        _bounded_string(value.get("accepted_callback_id"), "accepted_callback_id"),
        _digest(value.get("accepted_callback_sha256"), "accepted_callback_sha256"),
        _digest(value.get("prepared_receipt_sha256"), "prepared_receipt_sha256"),
        _digest(value.get("completion_receipt_sha256"), "completion_receipt_sha256"),
        head,
        address,
    )


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReapEffectRecoveryError(f"cannot hash {path}: {exc}") from exc


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ReapEffectRecoveryError(f"{label} is missing or not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReapEffectRecoveryError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReapEffectRecoveryError(f"{label} must be a JSON object")
    return value


def _git(vault: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=vault,
        text=not binary,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr if binary else str(result.stderr).strip()
        raise ReapEffectRecoveryError(
            f"Git proof failed: {detail[:300] if detail else 'unknown error'}"
        )
    return result.stdout


def _validate_receipts(
    vault: Path,
    worktree: Path,
    request: ReapEffectRecoveryRequest,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path, str]:
    meta_path = worktree / ".task-meta.json"
    summary_path = worktree / ".task-summary.json"
    prepared_path = worktree / ".task-reap-prepared.json"
    completion_path = worktree / ".task-reap-complete.json"
    if _sha256(prepared_path) != request.prepared_receipt_sha256:
        raise ReapEffectRecoveryError("prepared receipt digest mismatched")
    if _sha256(completion_path) != request.completion_receipt_sha256:
        raise ReapEffectRecoveryError("completion receipt digest mismatched")
    meta = _read_json(meta_path, label="task metadata")
    summary = _read_json(summary_path, label="task summary")
    prepared = _read_json(prepared_path, label="prepared reap receipt")
    completion = _read_json(completion_path, label="completion receipt")
    if set(completion) != COMPLETION_KEYS or completion.get("version") != 1:
        raise ReapEffectRecoveryError("completion receipt schema is invalid")
    if (
        not PREPARED_REQUIRED <= set(prepared)
        or set(prepared) - PREPARED_REQUIRED - PREPARED_OPTIONAL
        or prepared.get("version") != 1
    ):
        raise ReapEffectRecoveryError("prepared reap receipt schema is invalid")
    if completion.get("validated") is not True:
        raise ReapEffectRecoveryError("completion receipt is not validated")
    if completion.get("task_session_status") != "archived":
        raise ReapEffectRecoveryError("completion receipt task is not archived")
    if meta.get("task_id") != request.owner_id or request.owner_id != request.operation_id:
        raise ReapEffectRecoveryError("task/owner/operation identity mismatched")
    if meta.get("task_name") != prepared.get("task_name") or prepared.get(
        "task_name"
    ) != completion.get("task_name"):
        raise ReapEffectRecoveryError("task name mismatched across receipts")
    if Path(str(meta.get("vault_root") or "")).resolve() != vault or Path(
        str(meta.get("worktree") or "")
    ).resolve() != worktree:
        raise ReapEffectRecoveryError("task root identity mismatched")
    if Path(str(prepared.get("vault_root") or "")).resolve() != vault or Path(
        str(completion.get("vault_root") or "")
    ).resolve() != vault:
        raise ReapEffectRecoveryError("receipt vault identity mismatched")
    if prepared.get("summary_sha256") != _sha256(summary_path) or completion.get(
        "summary_sha256"
    ) != _sha256(summary_path):
        raise ReapEffectRecoveryError("summary digest mismatched")
    if prepared.get("meta_sha256") != _sha256(meta_path) or completion.get(
        "meta_sha256"
    ) != _sha256(meta_path):
        raise ReapEffectRecoveryError("metadata digest mismatched")
    canonical_summary = hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if canonical_summary != request.accepted_callback_sha256:
        raise ReapEffectRecoveryError("accepted callback does not bind the task summary")
    plan = Path(str(prepared.get("plan_path") or "")).resolve()
    if (
        Path(str(completion.get("plan_path") or "")).resolve() != plan
        or Path(str(meta.get("plan_file") or "")).resolve() != plan
        or prepared.get("closed_plan_sha256") != _sha256(plan)
        or completion.get("closed_plan_sha256") != _sha256(plan)
    ):
        raise ReapEffectRecoveryError("approved plan identity mismatched")
    result = Path(str(prepared.get("result_path") or "")).resolve()
    if Path(str(completion.get("result_path") or "")).resolve() != result:
        raise ReapEffectRecoveryError("result path mismatched across receipts")
    try:
        relative = result.relative_to(vault / "wiki")
    except ValueError:
        raise ReapEffectRecoveryError("result page escaped the selected vault") from None
    if (
        not result.is_file()
        or result.is_symlink()
        or relative.suffix != ".md"
        or completion.get("result_sha256") != _sha256(result)
        or prepared.get("result_link") != f"[[{result.stem}]]"
    ):
        raise ReapEffectRecoveryError("result page identity mismatched")
    return meta, prepared, completion, result, canonical_summary


def _validate_commit(vault: Path, result: Path, expected_head: str) -> None:
    actual = str(_git(vault, "rev-parse", "HEAD")).strip()
    if actual != expected_head:
        raise ReapEffectRecoveryError("committed vault HEAD mismatched")
    status = str(
        _git(vault, "status", "--porcelain=v1", "--untracked-files=all")
    )
    if status:
        raise ReapEffectRecoveryError("committed vault worktree is not clean")
    relative = result.relative_to(vault).as_posix()
    committed = _git(vault, "show", f"{expected_head}:{relative}", binary=True)
    if committed != result.read_bytes():
        raise ReapEffectRecoveryError("result page differs from committed vault HEAD")


def _validate_cardinalities(
    vault: Path,
    prepared: dict[str, Any],
    result: Path,
    address: str,
) -> None:
    result_text = result.read_text(encoding="utf-8")
    addresses = re.findall(r"(?m)^address:\s*(c-\d{6})\s*$", result_text)
    if addresses != [address]:
        raise ReapEffectRecoveryError("result page address cardinality mismatched")
    matches = [path.resolve() for path in (vault / "wiki").rglob(result.name)]
    if matches != [result]:
        raise ReapEffectRecoveryError("result page cardinality mismatched")
    relative = result.relative_to(vault).as_posix()
    address_map = (vault / ".vault-meta" / "address-map.tsv").read_text(
        encoding="utf-8"
    )
    expected_map = f"{address}\t{relative}"
    if (
        address_map.splitlines().count(expected_map) != 1
        or sum(line.startswith(f"{address}\t") for line in address_map.splitlines())
        != 1
    ):
        raise ReapEffectRecoveryError("result address map cardinality mismatched")
    task_name = str(prepared["task_name"])
    result_link = str(prepared["result_link"])
    prepared_date = str(prepared["prepared_date"])
    log = (vault / "wiki" / "log.md").read_text(encoding="utf-8")
    hot = (vault / "wiki" / "hot.md").read_text(encoding="utf-8")
    if log.count(f"## [{prepared_date}] reap | {task_name}") != 1 or log.count(
        f"`{address}` {result_link}."
    ) != 1:
        raise ReapEffectRecoveryError("reap log cardinality mismatched")
    if hot.count(
        f"{prepared_date}: {result_link} — finalized task result (`{address}`)"
    ) != 1:
        raise ReapEffectRecoveryError("reap hot cardinality mismatched")


def _callback_receipt_path(
    store: OperationStore, request: ReapEffectRecoveryRequest
) -> Path:
    return (
        store.root
        / "owners"
        / request.owner_id
        / "runtime"
        / request.operation_id
        / "callback-receipt.json"
    )


def _validate_record_and_callback(
    store: OperationStore, request: ReapEffectRecoveryRequest
) -> tuple[object, bool]:
    try:
        record = store.read(request.owner_id, request.operation_id)
    except StoreError as exc:
        raise ReapEffectRecoveryError(f"operation identity is unknown: {exc}") from exc
    first = (
        record.state == "finalizing"
        and record.pending_effect == "reap-finalize"
        and record.effect_id == "reap-finalize"
        and record.effect_outcome == EffectOutcome.PENDING
    )
    replay = (
        record.state == "finalizing"
        and not record.pending_effect
        and record.effect_id == "reap-finalize"
        and record.effect_outcome == EffectOutcome.SUCCEEDED
    )
    if not first and not replay:
        raise ReapEffectRecoveryError(
            "operation is not the exact pending or reconciled reap-finalize state"
        )
    if (
        record.spec.owner_id != request.owner_id
        or record.spec.operation_id != request.operation_id
        or record.run_id != request.run_id
        or record.accepted_callback_kind != "wiki-summary"
        or record.accepted_callback_id != request.accepted_callback_id
        or record.accepted_callback_sha256
        != request.accepted_callback_sha256
    ):
        raise ReapEffectRecoveryError("operation/callback identity mismatched")
    callback = _read_json(
        _callback_receipt_path(store, request), label="accepted callback receipt"
    )
    if set(callback) != CALLBACK_KEYS or callback != {
        "schema_version": 1,
        "status": "accepted",
        "callback_id": request.accepted_callback_id,
        "operation_id": request.operation_id,
        "run_id": request.run_id,
        "payload_sha256": request.accepted_callback_sha256,
    }:
        raise ReapEffectRecoveryError("accepted callback receipt mismatched")
    return record, replay


def _marker_payload(
    request: ReapEffectRecoveryRequest,
    *,
    result_path: str,
    status: str,
) -> dict[str, object]:
    return {
        **request.payload(),
        "effect_id": "reap-finalize",
        "result_path": result_path,
        "status": status,
    }


def reconcile_completed_reap_effect(
    vault_root: Path,
    worktree_root: Path,
    request: ReapEffectRecoveryRequest,
) -> dict[str, object]:
    """Resolve only a receipt-proven pending ``reap-finalize`` effect."""

    vault = vault_root.expanduser().resolve()
    worktree = worktree_root.expanduser().resolve()
    store = OperationStore(vault / ".vault-meta" / "harness")
    _record, replay = _validate_record_and_callback(store, request)
    _meta, prepared, _completion, result, _summary_sha = _validate_receipts(
        vault, worktree, request
    )
    _validate_commit(vault, result, request.committed_vault_head)
    _validate_cardinalities(vault, prepared, result, request.result_address)

    marker = (
        store.root
        / "owners"
        / request.owner_id
        / "runtime"
        / request.operation_id
        / "reap-effect-reconciliation.json"
    )
    prepared_marker = _marker_payload(
        request, result_path=str(result), status="prepared"
    )
    succeeded_marker = {
        **prepared_marker,
        "status": "succeeded",
    }
    existing = _read_json(marker, label="reap effect reconciliation") if marker.exists() else None
    if existing is not None and existing not in (
        prepared_marker,
        succeeded_marker,
    ):
        raise ReapEffectRecoveryError("reap effect reconciliation receipt was reused")
    if replay:
        if existing not in (prepared_marker, succeeded_marker):
            raise ReapEffectRecoveryError(
                "reconciled effect is missing its exact recovery receipt"
            )
        if existing != succeeded_marker:
            atomic_json(marker, succeeded_marker)
        return {
            "schema_version": 1,
            "status": "already-reconciled",
            "operation_id": request.operation_id,
            "run_id": request.run_id,
        }
    if existing is None:
        atomic_json(marker, prepared_marker)
    try:
        resolved = store.resolve_effect(
            request.owner_id,
            request.operation_id,
            EffectOutcome.SUCCEEDED,
        )
    except (ContractError, StoreError) as exc:
        raise ReapEffectRecoveryError(f"effect resolution failed: {exc}") from exc
    if (
        resolved.state != "finalizing"
        or resolved.pending_effect
        or resolved.effect_id != "reap-finalize"
        or resolved.effect_outcome != EffectOutcome.SUCCEEDED
    ):
        raise ReapEffectRecoveryError("effect resolution produced invalid state")
    atomic_json(marker, succeeded_marker)
    return {
        "schema_version": 1,
        "status": "reconciled",
        "operation_id": request.operation_id,
        "run_id": request.run_id,
    }
