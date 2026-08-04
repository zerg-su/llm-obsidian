"""Callback wake, provenance, containment, and bounded filesystem I/O."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from .contracts import CallbackEnvelope
from .callback_submit_recovery import ArtifactEvidence
from .runtime_worker_contracts import IDENTIFIER, RuntimeWorkerError
from research_contract import load_artifact


MAX_OUTBOX_BYTES = 70_000


def observe_review_artifact(
    path: Path,
    previous_sha256: str,
    stable_reads: int,
    *,
    limit: int = MAX_OUTBOX_BYTES,
) -> tuple[ArtifactEvidence, str, int]:
    """Classify one canonical artifact without retaining its contents."""

    try:
        if path.is_symlink():
            return ArtifactEvidence("symlink"), "", 0
        if not path.exists():
            return ArtifactEvidence(), "", 0
        if not path.is_file():
            return ArtifactEvidence("malformed"), "", 0
        size = path.stat().st_size
        if size <= 0:
            return ArtifactEvidence("unstable"), "", 0
        if size > limit:
            return ArtifactEvidence("oversize"), "", 0
        raw = path.read_bytes()
    except OSError:
        return ArtifactEvidence("malformed"), "", 0
    if not raw or len(raw) > limit:
        return ArtifactEvidence("oversize"), "", 0
    digest = hashlib.sha256(raw).hexdigest()
    reads = stable_reads + 1 if digest == previous_sha256 else 1
    state = "stable" if reads >= 2 else "unstable"
    return ArtifactEvidence(state, digest), digest, reads


def submit_stable_review_input(
    *,
    vault_root: Path,
    worktree: Path,
    callback_path: Path,
) -> subprocess.CompletedProcess[str]:
    """Publish a canonical stable reviewer input through review_submit.py."""

    state_dir = callback_path.parent
    input_path = state_dir / ".review-input.json"
    if (
        callback_path.name != ".review-callback.json"
        or callback_path.is_symlink()
        or state_dir.is_symlink()
        or not state_dir.is_dir()
        or worktree.is_symlink()
        or not worktree.is_dir()
    ):
        raise RuntimeWorkerError("review callback fast path identity is invalid")
    if callback_path.is_file():
        if not _bounded_file_sha256(callback_path):
            raise RuntimeWorkerError("existing review callback is invalid")
        return subprocess.CompletedProcess((), 0, "callback-ready\n", "")
    submit = vault_root / "scripts/harness/review_submit.py"
    if submit.is_symlink() or not submit.is_file():
        raise RuntimeWorkerError("trusted review submit validator is unavailable")
    return subprocess.run(
        [
            sys.executable,
            str(submit),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(state_dir),
            "--input-file",
            str(input_path),
        ],
        cwd=vault_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def publish_callback_wake(
    spec: dict[str, Any],
    state_root: Path,
    callback_id: str,
    cmux_adapter: object,
) -> bool:
    """Publish one idempotent coordinator wake after durable acceptance."""

    wake = str(spec.get("callback_wake") or "")
    if not wake:
        return True
    notify_path = state_root / "callback-wake.json"
    notified: dict[str, object] = {}
    if notify_path.is_file() and not notify_path.is_symlink():
        value = json.loads(notify_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeWorkerError("callback wake marker is invalid")
        notified = value
    if (
        notified.get("callback_id") == callback_id
        and notified.get("status") == "sent"
    ):
        return True
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "pending",
        },
    )
    try:
        cmux_adapter.send(spec["origin_surface"], wake)
        cmux_adapter.send_key(spec["origin_surface"], "Enter")
    except Exception:
        return False
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "sent",
        },
    )
    return True


def _normalize_fetch_errors_at_provider_boundary(
    path: Path,
    raw: bytes,
) -> bytes:
    """Normalize only bounded provider error forms before strict validation."""

    value = json.loads(raw)
    if not isinstance(value, dict):
        return raw
    errors = value.get("fetch_errors")
    if not isinstance(errors, list):
        return raw
    normalized: list[object] = []
    changed = False
    for item in errors:
        if isinstance(item, str):
            if not item.strip():
                changed = True
                continue
            normalized.append(item)
            continue
        if isinstance(item, Mapping) and set(item) == {"url", "error"}:
            url = item["url"]
            error = item["error"]
            if (
                isinstance(url, str)
                and url.strip()
                and isinstance(error, str)
                and error.strip()
            ):
                canonical = f"{url}: {error}"
                if len(canonical) <= 2000:
                    normalized.append(canonical)
                    changed = True
                    continue
        normalized.append(item)
    if not changed:
        return raw
    value["fetch_errors"] = normalized
    _atomic_json(path, value)
    return path.read_bytes()


def _bounded_file_sha256(
    path: Path, *, limit: int = MAX_OUTBOX_BYTES
) -> str:
    """Return only a bounded content digest; invalid pointers are no evidence."""

    try:
        if path.is_symlink() or not path.is_file():
            return ""
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw or len(raw) > limit:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _current_callback_receipt_sha256(
    runtime_root: Path,
    *,
    expected_callback_id: str = "",
    expected_payload_sha256: str = "",
) -> str:
    """Return receipt evidence only for the currently bound callback target."""

    values: list[tuple[dict[str, Any], bytes]] = []
    for path in (
        runtime_root / "callback-target.json",
        runtime_root / "callback-receipt.json",
    ):
        try:
            if path.is_symlink() or not path.is_file():
                return ""
            raw = path.read_bytes()
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                return ""
            value = json.loads(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(value, dict):
            return ""
        values.append((value, raw))
    target, _target_raw = values[0]
    receipt, receipt_raw = values[1]
    generation = target.get("generation")
    operation_id = target.get("operation_id")
    if (
        target.get("schema_version") != 1
        or receipt.get("schema_version") != 1
        or type(generation) is not int
        or generation < 1
        or not isinstance(operation_id, str)
        or not operation_id
        or receipt.get("generation") != generation
        or receipt.get("operation_id") != operation_id
        or receipt.get("status") not in {"accepted", "duplicate"}
    ):
        return ""
    if (
        receipt.get("run_id") != target.get("run_id")
        or IDENTIFIER.fullmatch(str(receipt.get("callback_id") or "")) is None
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(receipt.get("payload_sha256") or "")
        )
        or (
            expected_callback_id
            and receipt.get("callback_id") != expected_callback_id
        )
        or (
            expected_payload_sha256
            and receipt.get("payload_sha256") != expected_payload_sha256
        )
    ):
        return ""
    return hashlib.sha256(receipt_raw).hexdigest()


def _submit_failure_requires_attention(
    result: subprocess.CompletedProcess[str], callback_path: Path
) -> bool:
    """Ignore the benign race where the model published the same callback."""

    return result.returncode != 0 and not callback_path.is_file()


def _write_once_json(path: Path, value: object) -> None:
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
            raise RuntimeWorkerError(
                "research input provenance is unreadable"
            ) from exc
        if existing != encoded:
            raise RuntimeWorkerError(
                "research input provenance changed"
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


def _research_input_provenance(
    spec: dict[str, Any],
    spec_path: Path,
    *,
    create: bool,
) -> str:
    if spec["callback_mode"] != "research-synth":
        return ""
    artifact_path = spec["cwd"] / "artifact.json"
    artifact = load_artifact(str(artifact_path))
    try:
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeWorkerError(
            "research input artifact is unreadable"
        ) from exc
    value = {
        "schema_version": 1,
        "operation_id": spec["operation_id"],
        "run_id": spec["run_id"],
        "fetch_run_id": artifact["run_id"],
        "request_sha256": artifact["request_sha256"],
        "artifact_sha256": artifact_sha256,
    }
    marker = spec_path.parent / "research-input.json"
    if marker.is_symlink():
        raise RuntimeWorkerError(
            "research input provenance must not be a symlink"
        )
    if create:
        _write_once_json(marker, value)
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError(
            "research input provenance is unreadable"
        ) from exc
    if recorded != value:
        raise RuntimeWorkerError(
            "research input artifact changed after validation"
        )
    return artifact_sha256


def _callback_target(spec: dict[str, Any]) -> tuple[int, str, str, Path]:
    try:
        value = json.loads(
            spec["callback_registration"].read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError("callback target registration is unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or type(value.get("generation")) is not int
        or int(value["generation"]) < 1
    ):
        raise RuntimeWorkerError("callback target registration is invalid")
    operation_id = str(value.get("operation_id") or "")
    run_id = str(value.get("run_id") or "")
    if not IDENTIFIER.fullmatch(operation_id) or not IDENTIFIER.fullmatch(run_id):
        raise RuntimeWorkerError("callback target identity is invalid")
    raw_pointer = value.get("callback_pointer")
    if not isinstance(raw_pointer, str) or not raw_pointer:
        raise RuntimeWorkerError("callback target pointer is invalid")
    cwd = Path(spec["cwd"]).expanduser().resolve()
    pointer = Path(raw_pointer).expanduser()
    if not pointer.is_absolute():
        pointer = cwd / pointer
    # Normalize lexical traversal without dereferencing the callback artifact.
    # observe_review_artifact() must retain the chance to classify a symlink
    # leaf instead of silently reading its target.
    pointer = Path(os.path.abspath(pointer))
    try:
        relative = pointer.relative_to(cwd)
    except ValueError:
        # macOS commonly exposes a lexical /var path whose exact cwd ancestor
        # resolves to /private/var. Preserve the leaf and only normalize that
        # already-owned cwd alias; an in-cwd alias is handled by the strict
        # component walk below.
        lexical_cwd = next(
            (
                ancestor
                for ancestor in (pointer.parent, *pointer.parents)
                if ancestor.resolve(strict=False) == cwd
            ),
            None,
        )
        if lexical_cwd is None:
            raise RuntimeWorkerError("callback target pointer escapes cwd")
        relative = pointer.relative_to(lexical_cwd)
        pointer = cwd / relative
    current = cwd
    for component in relative.parts[:-1]:
        current /= component
        if current.is_symlink():
            raise RuntimeWorkerError("callback target parent is a symlink")
    try:
        pointer.resolve(strict=False).relative_to(cwd)
    except (OSError, ValueError) as exc:
        raise RuntimeWorkerError("callback target pointer escapes cwd") from exc
    return int(value["generation"]), operation_id, run_id, pointer


def _envelope(value: object) -> CallbackEnvelope:
    if not isinstance(value, dict):
        raise RuntimeWorkerError("callback envelope must be an object")
    return CallbackEnvelope(
        callback_id=value.get("callback_id", ""),
        operation_id=value.get("operation_id", ""),
        run_id=value.get("run_id", ""),
        kind=value.get("kind", ""),
        payload=value.get("payload", {}),
        payload_sha256=value.get("payload_sha256", ""),
        schema_version=value.get("schema_version", 0),
    )
