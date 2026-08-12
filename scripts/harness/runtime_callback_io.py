"""Callback wake, provenance, containment, and bounded filesystem I/O."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("review-resolution", "callbacks")

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts import CallbackEnvelope
from .callback_submit_recovery import ArtifactEvidence
from .runtime_worker_contracts import IDENTIFIER, RuntimeWorkerError
from research_contract import load_artifact
from review_resolution import DISPOSITIONS
from .artifact_repair import (
    ArtifactRepairError,
    ContractArtifactOwner,
    CorrectionBudgetExhausted,
    CorrectionNotificationUncertain,
    review_resolution_contract_template,
)


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


def record_review_drive_failure(
    runtime_root: Path, receipt: dict[str, object]
) -> None:
    """Preserve every failed review drive and refresh its latest projection.

    Review findings can produce several exact-HEAD cycles.  A single immutable
    failure path makes a later drive collide with an earlier receipt, so every
    drive is archived by its bound digest while the legacy path remains a
    replaceable, content-free latest projection.
    """

    latest = runtime_root / "review-drive-failure.json"

    def archive(value: dict[str, object]) -> None:
        drive_sha256 = value.get("drive_sha256")
        if (
            value.get("schema_version") != 1
            or not isinstance(drive_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", drive_sha256)
        ):
            raise RuntimeWorkerError(
                "review drive failure receipt identity is invalid"
            )
        archive_root = runtime_root / "review-drive-failures"
        archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_once_json(archive_root / f"{drive_sha256}.json", value)

    if latest.is_symlink():
        raise RuntimeWorkerError(
            "review drive failure projection cannot be a symlink"
        )
    if latest.is_file():
        try:
            existing = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError(
                "review drive failure projection is invalid"
            ) from exc
        if not isinstance(existing, dict):
            raise RuntimeWorkerError(
                "review drive failure projection is invalid"
            )
        archive(existing)
    elif latest.exists():
        raise RuntimeWorkerError("review drive failure projection is invalid")
    archive(receipt)
    _atomic_json(latest, receipt)


@contextmanager
def _callback_wake_lock(state_root: Path):
    lock_path = state_root / ".callback-wake.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish_callback_wake_locked(
    spec: dict[str, Any],
    state_root: Path,
    callback_id: str,
    cmux_adapter: object,
    *,
    resume_uncertain: bool = False,
) -> bool:
    """Publish while the exact operation wake lock is held."""

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
    if notified:
        if (
            notified.get("schema_version") != 1
            or not isinstance(notified.get("callback_id"), str)
            or notified.get("status")
            not in {
                "paste-reserved",
                "transport-accepted",
                "submit-accepted",
                "sent",
                "effect-uncertain",
            }
        ):
            raise RuntimeWorkerError("callback wake marker is invalid")
        if notified.get("callback_id") == callback_id:
            status = str(notified["status"])
            if status in {"submit-accepted", "sent"}:
                if status != "sent":
                    _atomic_json(
                        notify_path,
                        {
                            "schema_version": 1,
                            "callback_id": callback_id,
                            "status": "sent",
                        },
                    )
                return True
            if not resume_uncertain:
                if status != "effect-uncertain":
                    _atomic_json(
                        notify_path,
                        {
                            "schema_version": 1,
                            "callback_id": callback_id,
                            "status": "effect-uncertain",
                        },
                    )
                return False
            # A restarted generation resumes a torn same-identity wake once:
            # wake text instructs an idempotent runner, so re-sending the
            # exact message converges, unlike a provider input whose replay
            # would be a second effect.  Live retries stay fail-closed.
        elif notified.get("status") != "sent":
            raise RuntimeWorkerError("prior callback wake effect is uncertain")
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "paste-reserved",
        },
    )
    try:
        cmux_adapter.send(spec["origin_surface"], wake)
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "callback_id": callback_id,
                "status": "transport-accepted",
            },
        )
        cmux_adapter.send_key(spec["origin_surface"], "Enter")
    except Exception:
        _atomic_json(
            notify_path,
            {
                "schema_version": 1,
                "callback_id": callback_id,
                "status": "effect-uncertain",
            },
        )
        return False
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "submit-accepted",
        },
    )
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "sent",
        },
    )
    return True


def wake_resume_once(worker: object, identity: str) -> bool:
    """Authorize at most one torn-wake resume per identity per generation.

    Only a real worker generation (which initializes its resumed-identity
    set) may resume; bare probes and CLI-side publishers stay fail-closed.
    """

    resumed = getattr(worker, "resumed_wake_identities", None)
    if resumed is None or identity in resumed:
        return False
    resumed.add(identity)
    return True


def publish_callback_wake(
    spec: dict[str, Any],
    state_root: Path,
    callback_id: str,
    cmux_adapter: object,
    *,
    resume_uncertain: bool = False,
) -> bool:
    """Publish one crash-safe, concurrent-idempotent coordinator wake."""

    with _callback_wake_lock(state_root):
        return _publish_callback_wake_locked(
            spec,
            state_root,
            callback_id,
            cmux_adapter,
            resume_uncertain=resume_uncertain,
        )


def review_resolution_template(
    packet: dict[str, object], material_findings: list[dict[str, object]]
) -> dict[str, object]:
    """Return the only executor-editable resolution object shape."""

    return {
        "schema_version": 1,
        "operation_id": packet["operation_id"],
        "review_identity_sha256": packet["review_identity_sha256"],
        "reviewed_head_sha": packet["reviewed_head_sha"],
        "resolved_head_sha": "",
        "resolutions": [
            {
                "finding_id": str(finding.get("finding_id") or ""),
                "disposition": "",
                "rationale": "",
                "follow_up": "",
            }
            for finding in material_findings
        ],
    }


def _review_resolution_shape_valid(
    value: object, template: dict[str, object]
) -> bool:
    if not isinstance(value, dict) or set(value) != set(template):
        return False
    identity_fields = (
        "schema_version",
        "operation_id",
        "review_identity_sha256",
        "reviewed_head_sha",
    )
    if any(value.get(field) != template[field] for field in identity_fields):
        return False
    rows = value.get("resolutions")
    expected = template["resolutions"]
    fields = {"finding_id", "disposition", "rationale", "follow_up"}
    if not isinstance(rows, list) or not isinstance(expected, list):
        return False
    if len(rows) != len(expected) or any(
        not isinstance(row, dict)
        or set(row) != fields
        or row.get("finding_id") != wanted.get("finding_id")
        for row, wanted in zip(rows, expected, strict=True)
    ):
        return False
    resolved_head = value.get("resolved_head_sha")
    if resolved_head == "" and all(
        not row["disposition"] and not row["rationale"] and not row["follow_up"]
        for row in rows
    ):
        return True
    return (
        isinstance(resolved_head, str)
        and re.fullmatch("[0-9a-f]{40,64}", resolved_head) is not None
        and all(
            row["disposition"] in DISPOSITIONS
            and isinstance(row["rationale"], str)
            and bool(row["rationale"])
            and isinstance(row["follow_up"], str)
            and (row["disposition"] != "out-of-scope" or bool(row["follow_up"]))
            for row in rows
        )
    )


def _resolution_correction_message(path: Path, attempt: int) -> str:
    return (
        "The review resolution artifact was rejected before continuation. "
        f"Harness restored the exact template in {path.name}. "
        "Edit that existing object in place; do not rename, add, or remove fields. "
        "Set resolved_head_sha to the current committed HEAD. For every listed "
        "finding, fill only disposition, rationale, and follow_up; follow_up may "
        "be empty unless disposition is out-of-scope. Keep operation_id, "
        "review_identity_sha256, reviewed_head_sha, finding ids, and schema_version "
        f"unchanged. Then refresh .task-summary.json. This is correction {attempt}/2; "
        "do not relaunch review or repeat already completed product work."
    )


def _resolution_correction_receipts(
    worker: object, packet: dict[str, object], root: Path
) -> tuple[list[Path], list[dict[str, object]]]:
    paths = sorted(root.glob("attempt-*.json"))
    values = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    fields = {
        "schema_version",
        "operation_id",
        "review_identity_sha256",
        "invalid_sha256",
        "attempt",
        "wake_id",
        "status",
    }
    invalid = any(
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema_version") != 1
        or value.get("operation_id") != worker.spec["operation_id"]
        or value.get("review_identity_sha256")
        != packet["review_identity_sha256"]
        or value.get("attempt") != index
        or value.get("status") not in {"pending", "sent"}
        or re.fullmatch("[0-9a-f]{64}", str(value.get("invalid_sha256"))) is None
        or re.fullmatch("[0-9a-f]{64}", str(value.get("wake_id"))) is None
        for index, value in enumerate(values, start=1)
    )
    if invalid:
        raise RuntimeWorkerError("review resolution correction receipt is invalid")
    return paths, values


def _send_resolution_correction(
    worker: object,
    *,
    root: Path,
    path: Path,
    template: dict[str, object],
    receipt_path: Path,
    receipt: dict[str, object],
) -> None:
    attempt = int(receipt["attempt"])
    _atomic_json(path, template)
    wake_root = root / f"attempt-{attempt}-wake"
    wake_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    wake_id = str(receipt["wake_id"])
    if not publish_callback_wake(
        {
            "origin_surface": worker.spec["surface_id"],
            "callback_wake": _resolution_correction_message(path, attempt),
        },
        wake_root,
        wake_id,
        worker.cmux_adapter,
        resume_uncertain=wake_resume_once(worker, wake_id),
    ):
        raise RuntimeWorkerError(
            "review resolution correction notification effect is uncertain"
        )
    _atomic_json(receipt_path, receipt | {"status": "sent"})
    worker.review_resolution_correction_sent = True


def _resume_resolution_correction(
    worker: object,
    *,
    packet: dict[str, object],
    template: dict[str, object],
    path: Path,
) -> bool:
    root = worker.spec_path.parent / "review-resolution-corrections"
    paths, values = _resolution_correction_receipts(worker, packet, root)
    pending = [value for value in values if value["status"] == "pending"]
    if not pending:
        return False
    if len(pending) != 1 or pending[0] is not values[-1]:
        raise RuntimeWorkerError("review resolution correction receipt order is invalid")
    _send_resolution_correction(
        worker,
        root=root,
        path=path,
        template=template,
        receipt_path=paths[-1],
        receipt=pending[0],
    )
    return True


def _publish_resolution_correction(
    worker: object,
    *,
    packet: dict[str, object],
    template: dict[str, object],
    path: Path,
    invalid_sha256: str,
) -> None:
    root = worker.spec_path.parent / "review-resolution-corrections"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths, _values = _resolution_correction_receipts(worker, packet, root)
    if len(paths) >= 2:
        raise RuntimeWorkerError("review resolution correction budget exhausted")
    attempt = len(paths) + 1
    receipt_path = root / f"attempt-{attempt}.json"
    wake_id = hashlib.sha256(
        (
            f"{worker.spec['operation_id']}:{packet['review_identity_sha256']}:"
            f"{invalid_sha256}:{attempt}"
        ).encode()
    ).hexdigest()
    receipt = {
        "schema_version": 1,
        "operation_id": worker.spec["operation_id"],
        "review_identity_sha256": packet["review_identity_sha256"],
        "invalid_sha256": invalid_sha256,
        "attempt": attempt,
        "wake_id": wake_id,
        "status": "pending",
    }
    _atomic_json(receipt_path, receipt)
    _send_resolution_correction(
        worker,
        root=root,
        path=path,
        template=template,
        receipt_path=receipt_path,
        receipt=receipt,
    )


def ensure_review_resolution(
    worker: object,
    *,
    packet: dict[str, object],
    material_findings: list[dict[str, object]],
) -> Path:
    """Keep one exact resolution shape and correct it twice in-session."""

    path = worker.spec["cwd"] / ".task-review-resolution.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeWorkerError("review resolution response path is invalid")
    worker.review_resolution_correction_sent = False
    template = review_resolution_template(packet, material_findings)
    owner = ContractArtifactOwner.publish(
        state_root=worker.spec_path.parent,
        worktree=worker.spec["cwd"],
        template=review_resolution_contract_template(
            attempt_id=str(packet["review_identity_sha256"]),
            value=template,
        ),
        actual_target=path,
    )
    if owner.publication_created or not path.exists():
        owner.restore_template()
    if owner.has_uncertain_correction:
        raise RuntimeWorkerError(
            "review resolution correction notification effect is uncertain"
        )
    try:
        repaired = owner.repair(authoritative_fields={})
    except ArtifactRepairError as exc:
        raise RuntimeWorkerError("review resolution repair failed") from exc
    current = dict(repaired.value)
    if _review_resolution_shape_valid(current, template):
        return path
    if owner.awaiting_semantic_edit(repaired.output_sha256):
        worker.review_resolution_correction_sent = True
        return path
    try:
        reservation = owner.reserve_correction(repaired.input_sha256)
        owner.restore_template()

        def send(message: str) -> None:
            worker.cmux_adapter.send(worker.spec["surface_id"], message)
            worker.cmux_adapter.send_key(worker.spec["surface_id"], "Enter")

        owner.deliver_correction(
            reservation,
            _resolution_correction_message(path, reservation.attempt),
            send,
            fault_observer=getattr(worker, "fault_observer", None),
        )
    except CorrectionBudgetExhausted as exc:
        raise RuntimeWorkerError(
            "review resolution correction budget exhausted"
        ) from exc
    except CorrectionNotificationUncertain as exc:
        raise RuntimeWorkerError(
            "review resolution correction notification effect is uncertain"
        ) from exc
    except ArtifactRepairError as exc:
        raise RuntimeWorkerError("review resolution correction is invalid") from exc
    worker.review_resolution_correction_sent = True
    return path


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
