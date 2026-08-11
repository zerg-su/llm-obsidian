"""Authoritative read-only receipt classification for the dashboard."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    ContractError,
    OperationRecord,
    OperationSpec,
    VerificationEvidence,
)
from .dashboard_policy import (
    UNKNOWN_REVIEW,
    UNKNOWN_TIMING,
    ReviewSummaryView,
    TimingView,
)
from .liveness import LivenessState
from .review_attempt import (
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptTerminalResult,
)
from .state_machine import TERMINAL
from .store import OperationStore, StoreError
from .verification import output_binding_valid
from .verification_attempt import VerificationAttempt, VerificationAttemptError
from .workflows.engineering_fix import FixWorkflowError, load_receipt
from .workflows.engineering_fix_model import PAYLOAD_FIELDS


MAX_VISITS = 16
RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
NUMERIC_EPOCH = re.compile(
    r"(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z"
)


def absolute_path_is_safe(path: Path | str) -> bool:
    """Reject any symlink in one original absolute path before resolving it."""

    original = Path(path).expanduser()
    # Normalization is lexical: it collapses `..` before any component is
    # inspected, so `<symlink>/../target` would erase the very symlink this
    # walk exists to reject while the kernel still traverses it.  A raw
    # absolute path carrying `..` therefore fails closed instead of being
    # validated as a path nothing will ever open.
    if original.is_absolute() and ".." in original.parts:
        return False
    path = Path(os.path.abspath(original))
    current = Path(path.anchor)
    try:
        if current.is_symlink():
            return False
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _evidence_path_is_safe(path: Path, boundary: Path) -> bool:
    """Reject out-of-boundary paths and symlinks from anchor through leaf."""

    if not absolute_path_is_safe(path):
        return False
    normalized = Path(os.path.abspath(path.expanduser()))
    boundary = Path(os.path.abspath(boundary.expanduser()))
    try:
        normalized.relative_to(boundary)
    except ValueError:
        return False
    return True


def _read_snapshot(
    path: Path, *, boundary: Path
) -> tuple[dict[str, Any], bytes] | None:
    """Read one JSON object and its exact bytes from a single opened file.

    Callers that also need a digest must not read the file twice: an atomic
    replacement between two reads would pair one revision's values with
    another revision's hash.  One descriptor keeps the mapping and its bytes
    describing the same revision, and the no-symlink and regular-file checks
    apply to the file that is actually read.
    """

    if (
        not _evidence_path_is_safe(path, boundary)
        or not path.is_file()
        or path.is_symlink()
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return (value, raw) if isinstance(value, dict) else None


def _read_object(path: Path, *, boundary: Path) -> dict[str, Any] | None:
    snapshot = _read_snapshot(path, boundary=boundary)
    return None if snapshot is None else snapshot[0]


def read_gate(store: OperationStore, owner_id: str) -> dict[str, Any] | None:
    """Read the one owner review gate without following a symlink."""

    return _read_object(
        store.root / "review-data" / owner_id / owner_id / "review-gate.json",
        boundary=store.root,
    )


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _rfc3339_epoch(value: object) -> float | None:
    if not isinstance(value, str) or RFC3339.fullmatch(value) is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _verification_epoch(value: object) -> float | None:
    """Parse one canonical verification timestamp without weakening RFC 3339."""

    rfc3339 = _rfc3339_epoch(value)
    if rfc3339 is not None:
        return rfc3339
    if not isinstance(value, str) or NUMERIC_EPOCH.fullmatch(value) is None:
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _timing(mode: str, start: float, end: float) -> TimingView:
    if end < start:
        return UNKNOWN_TIMING
    return TimingView(mode, int(end - start))


def liveness_timing(
    store: OperationStore,
    record: OperationRecord,
    observed_at: float,
) -> TimingView:
    """Return elapsed time from the exact record's valid liveness state."""

    observed = _epoch(observed_at)
    if observed is None or record.state in TERMINAL:
        return UNKNOWN_TIMING
    path = (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
        / "liveness"
        / "state.json"
    )
    raw = _read_object(path, boundary=store.root)
    try:
        state = LivenessState(**raw) if raw is not None else None
    except (TypeError, ValueError):
        state = None
    if state is None:
        return UNKNOWN_TIMING
    start = _epoch(state.started_at)
    progress = _epoch(state.last_progress_at)
    revision = state.operation_revision
    if (
        start is None
        or progress is None
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or revision > record.revision
        or start > progress
        or progress > observed
    ):
        return UNKNOWN_TIMING
    return _timing("elapsed", start, observed)


def _vault_for_store(store: OperationStore) -> Path | None:
    root = store.root.expanduser().resolve()
    if root.name != "harness" or root.parent.name != ".vault-meta":
        return None
    return root.parents[1]


def _bound_task(
    store: OperationStore,
    record: OperationRecord,
) -> tuple[Path, dict[str, Any], str] | None:
    runtime = (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
    )
    session = _read_object(runtime / "session.json", boundary=store.root)
    if (
        session is None
        or session.get("schema_version") != 1
        or session.get("operation_id") != record.spec.operation_id
        or session.get("run_id") != record.run_id
    ):
        return None
    cwd_raw = session.get("cwd")
    if not isinstance(cwd_raw, str) or not Path(cwd_raw).is_absolute():
        return None
    cwd_path = Path(cwd_raw).expanduser()
    if not absolute_path_is_safe(cwd_path):
        return None
    cwd = cwd_path.resolve()
    snapshot = _read_snapshot(cwd / ".task-meta.json", boundary=cwd)
    vault = _vault_for_store(store)
    if snapshot is None or vault is None:
        return None
    meta, meta_bytes = snapshot
    if (
        meta.get("version") not in {3, 4}
        or meta.get("task_id") != record.spec.operation_id
        or Path(str(meta.get("worktree") or "")).expanduser().resolve() != cwd
        or Path(str(meta.get("vault_root") or "")).expanduser().resolve()
        != vault
    ):
        return None
    return cwd, meta, hashlib.sha256(meta_bytes).hexdigest()


def _bound_task_start(
    store: OperationStore,
    record: OperationRecord,
) -> tuple[float, Path, dict[str, Any], str] | None:
    bound = _bound_task(store, record)
    if bound is None:
        return None
    cwd, meta, digest = bound
    started = _rfc3339_epoch(meta.get("spawned_at"))
    return None if started is None else (started, cwd, meta, digest)


def root_task_name(store: OperationStore, record: OperationRecord) -> str:
    """Return one bounded task name from the exact task-meta binding."""

    bound = _bound_task(store, record)
    if bound is None:
        return ""
    value = bound[1].get("task_name")
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isascii()
        or len(value) > 160
        or any(
            ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
            for char in value
        )
    ):
        return ""
    return " ".join(value.split())


def root_timing(
    store: OperationStore,
    record: OperationRecord,
    observed_at: float,
) -> TimingView:
    """Project root elapsed/duration with task evidence before liveness."""

    observed = _epoch(observed_at)
    if observed is None:
        return UNKNOWN_TIMING
    bound = _bound_task_start(store, record)
    if record.state not in TERMINAL:
        if bound is not None and bound[0] <= observed:
            return _timing("elapsed", bound[0], observed)
        return liveness_timing(store, record, observed)
    if bound is None:
        return UNKNOWN_TIMING
    start, cwd, meta, meta_sha256 = bound
    complete = _read_object(
        cwd / ".task-reap-complete.json", boundary=cwd
    )
    if complete is None:
        return UNKNOWN_TIMING
    end = _rfc3339_epoch(complete.get("completed_at"))
    if (
        end is None
        or end > observed
        or complete.get("validated") is not True
        or complete.get("meta_sha256") != meta_sha256
        or complete.get("task_name") != meta.get("task_name")
        or Path(str(complete.get("vault_root") or "")).expanduser().resolve()
        != _vault_for_store(store)
        or Path(str(complete.get("plan_path") or "")).expanduser().resolve()
        != Path(str(meta.get("plan_file") or "")).expanduser().resolve()
        or complete.get("task_session_status") != "archived"
    ):
        return UNKNOWN_TIMING
    return _timing("duration", start, end)


def fix_receipt_visits(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    step_id: str,
) -> tuple[tuple[int, ...], str]:
    """Return complete fix passes and one bounded invalid/failed issue code."""

    root = runtime / "pipeline-fix"
    if not root.is_dir():
        return (), ""
    visits: list[int] = []
    issue = ""
    for path in sorted(root.glob("pass-*")):
        suffix = path.name.removeprefix("pass-")
        if not suffix.isdigit():
            continue
        receipt_path = path / step_id / "receipt.json"
        if not receipt_path.exists() and not receipt_path.is_symlink():
            continue
        try:
            receipt = load_receipt(receipt_path)
            child = store.read(record.spec.owner_id, receipt.operation_id)
            receipt_fields = receipt.to_dict()
            payload = {key: receipt_fields[key] for key in PAYLOAD_FIELDS}
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if (
                receipt.parent_operation_id != record.spec.operation_id
                or receipt.definition_sha256 != record.spec.contract_sha256
                or receipt.step_id != step_id
                or receipt.iteration != int(suffix)
                or child.spec.kind != "pipeline-model-step"
                or child.spec.parent_operation_id != record.spec.operation_id
                or child.spec.contract_sha256 != record.spec.contract_sha256
                or child.lane_id != receipt.lane_id
                or child.run_id != receipt.run_id
                or child.state != "complete"
                or child.accepted_callback_id != receipt.callback_id
                or child.accepted_callback_kind != "result"
                or child.accepted_callback_sha256 != payload_sha256
                or receipt.callback_id != f"result-{payload_sha256[:24]}"
            ):
                raise FixWorkflowError("fix receipt identity changed")
        except (FixWorkflowError, StoreError, OSError, ValueError):
            issue = "fix-receipt-invalid"
            continue
        if receipt.status == "complete":
            visits.append(int(suffix))
        else:
            issue = "fix-receipt-failed"
    return tuple(visits[:MAX_VISITS]), issue


def verification_identity(
    parent: OperationSpec,
    definition_sha256: str,
    input_sha256: str,
    attempt_index: int,
) -> tuple[str, str, str, str]:
    """Derive the production verification operation, lane, run, and effect."""

    suffix = f"-verify-{input_sha256[:16]}" + (
        f"-a{attempt_index}" if attempt_index else ""
    )
    operation_id = f"{parent.operation_id[: 128 - len(suffix)]}{suffix}"
    attempt = f":attempt:{attempt_index}" if attempt_index else ""
    key = hashlib.sha256(
        (
            f"{parent.idempotency_key}:pipeline-verify:{operation_id}:"
            f"{definition_sha256}:{input_sha256}:{parent.verification_profile}{attempt}"
        ).encode()
    ).hexdigest()
    lane = hashlib.sha256(f"{key}:lane".encode()).hexdigest()[:32]
    run = hashlib.sha256(f"{key}:run".encode()).hexdigest()[:32]
    effect = (
        "pipeline-verify-" + input_sha256[:32]
        if not attempt_index
        else "pipeline-verify-"
        + hashlib.sha256(
            f"{input_sha256}:attempt:{attempt_index}".encode()
        ).hexdigest()[:32]
    )
    return operation_id, lane, run, effect


def verification_receipt_status(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    path: Path,
) -> str:
    """Classify one verification receipt through its accepted durable identity."""

    value = _read_object(path, boundary=runtime)
    evidence = value.get("evidence") if value else None
    try:
        if (
            value is None
            or value.get("schema_version") not in {1, 2}
            or value.get("parent_operation_id") != record.spec.operation_id
            or value.get("definition_sha256") != record.spec.contract_sha256
            or value.get("step_id") != "verify"
            or value.get("profile") != record.spec.verification_profile
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(value.get("profile_sha256") or "")
            )
            or not re.fullmatch(
                r"[0-9a-f]{40,64}", str(value.get("head_sha") or "")
            )
            or value.get("status") not in {"complete", "failed"}
            or not isinstance(evidence, list)
            or not evidence
        ):
            raise ValueError
        attempt = _verification_attempt(value, record)
        input_sha = str(value.get("input_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", input_sha):
            raise ValueError
        operation_id, lane_id, run_id, effect_id = verification_identity(
            record.spec,
            record.spec.contract_sha256,
            input_sha,
            attempt.attempt_index,
        )
        child = store.read(record.spec.owner_id, operation_id)
        expected_path = (
            runtime / "pipeline-verification" / operation_id / "receipt.json"
        )
        if (
            attempt.parent_operation_id != record.spec.operation_id
            or attempt.profile != record.spec.verification_profile
            or attempt.profile_sha256 != value["profile_sha256"]
            or attempt.exact_head_sha != value["head_sha"]
            or value.get("operation_id") != operation_id
            or value.get("lane_id") != lane_id
            or value.get("run_id") != run_id
            or value.get("effect_id") != effect_id
            or child.spec.kind != "pipeline-verify"
            or child.spec.parent_operation_id != record.spec.operation_id
            or child.spec.contract_sha256 != record.spec.contract_sha256
            or child.lane_id != lane_id
            or child.run_id != run_id
            or path.resolve() != expected_path.resolve()
        ):
            raise ValueError
        exit_codes: list[int] = []
        for row in evidence:
            typed = VerificationEvidence(**row)
            if (
                typed.profile != record.spec.verification_profile
                or typed.profile_sha256 != value["profile_sha256"]
                or typed.head_sha != value["head_sha"]
                or not output_binding_valid(typed, pointer_root=runtime)
            ):
                raise ValueError
            exit_codes.append(typed.exit_code)
        succeeded = all(code == 0 for code in exit_codes)
        if (value["status"] == "complete") != succeeded:
            raise ValueError
        return str(value["status"])
    except (
        ContractError,
        StoreError,
        TypeError,
        ValueError,
        VerificationAttemptError,
    ):
        return "invalid"


def _verification_attempt(
    value: dict[str, Any], record: OperationRecord
) -> VerificationAttempt:
    """Load the exact attempt identity carried by one receipt generation."""

    if value.get("schema_version") == 2:
        attempt = VerificationAttempt.from_dict(
            value.get("verification_attempt")
        )
        if value.get("verification_attempt_sha256") != attempt.sha256:
            raise VerificationAttemptError(
                "verification attempt digest is invalid"
            )
        return attempt
    if value.get("schema_version") == 1:
        return VerificationAttempt(
            record.spec.operation_id,
            record.spec.verification_profile,
            str(value.get("profile_sha256") or ""),
            str(value.get("head_sha") or ""),
            0,
        )
    raise VerificationAttemptError("verification attempt schema is invalid")


def verification_receipt_visits(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    *,
    exact_head_sha: str = "",
) -> tuple[tuple[int, ...], str]:
    """Return bounded visit history and current exact-HEAD receipt truth."""

    root = runtime / "pipeline-verification"
    if not root.is_dir():
        return (
            (),
            "verification-receipt-missing" if exact_head_sha else "",
        )
    observations: list[tuple[VerificationAttempt | None, str, str]] = []
    for path in sorted(root.glob("*/receipt.json")):
        value = _read_object(path, boundary=runtime)
        try:
            attempt = (
                _verification_attempt(value, record)
                if value is not None
                else None
            )
        except VerificationAttemptError:
            attempt = None
        status = verification_receipt_status(store, record, runtime, path)
        head_sha = str(value.get("head_sha") or "") if value else ""
        observations.append((attempt, status, head_sha))
    complete_count = min(
        sum(status == "complete" for _attempt, status, _head in observations),
        MAX_VISITS,
    )
    visits = tuple(range(complete_count))
    current = observations
    if exact_head_sha:
        exact = tuple(
            item
            for item in observations
            if item[2] == exact_head_sha
        )
        if not exact:
            return visits, "verification-receipt-missing"
        valid_attempts = tuple(item[0] for item in exact if item[0] is not None)
        if not valid_attempts:
            current = exact
        else:
            latest_attempt = max(item.attempt_index for item in valid_attempts)
            current = [
                item
                for item in exact
                if item[0] is None or item[0].attempt_index == latest_attempt
            ]
    issue = ""
    for _attempt, status, _head in current:
        if status != "complete":
            issue = (
                "verification-receipt-failed"
                if status == "failed"
                else "verification-receipt-invalid"
            )
    return visits, issue


def verification_receipt_timing(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    observed_at: float,
    *,
    exact_head_sha: str = "",
) -> TimingView:
    """Freeze the accepted verification interval selected for display."""

    observed = _epoch(observed_at)
    root = runtime / "pipeline-verification"
    if observed is None or not root.is_dir():
        return UNKNOWN_TIMING
    intervals: list[tuple[float, float]] = []
    candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in sorted(root.glob("*/receipt.json")):
        raw = _read_object(path, boundary=runtime)
        if raw is None or verification_receipt_status(
            store, record, runtime, path
        ) != "complete":
            continue
        try:
            attempt = _verification_attempt(raw, record)
        except VerificationAttemptError:
            continue
        head = str(raw.get("head_sha") or "")
        if exact_head_sha and head != exact_head_sha:
            continue
        candidates.append((attempt.attempt_index, head, path, raw))
    if exact_head_sha and candidates:
        latest = max(item[0] for item in candidates)
        candidates = [item for item in candidates if item[0] == latest]
    for _attempt, _head, _path, raw in candidates:
        evidence = raw.get("evidence")
        if not isinstance(evidence, list):
            continue
        receipt_intervals: list[tuple[float, float]] = []
        for row in evidence:
            if not isinstance(row, Mapping):
                receipt_intervals = []
                break
            start = _verification_epoch(row.get("started_at"))
            end = _verification_epoch(row.get("finished_at"))
            if start is None or end is None or end < start or end > observed:
                receipt_intervals = []
                break
            receipt_intervals.append((start, end))
        intervals.extend(receipt_intervals)
    if not intervals:
        return UNKNOWN_TIMING
    return _timing(
        "duration",
        min(item[0] for item in intervals),
        max(item[1] for item in intervals),
    )


def _review_material_count(
    store: OperationStore,
    attempt: ReviewAttempt,
    gate: Mapping[str, Any],
    finding_ids: set[str],
) -> int | None:
    evidence = gate.get("review_notification_evidence")
    if not isinstance(evidence, Mapping):
        return None
    lanes = {lane.axis: lane for lane in attempt.identity.lanes}
    if set(evidence) != set(lanes):
        return None
    material: set[str] = set()
    try:
        for axis, lane in lanes.items():
            row = evidence[axis]
            if not isinstance(row, Mapping):
                return None
            ids = row.get("material_finding_ids")
            if not isinstance(ids, list) or any(
                not isinstance(item, str) or not item for item in ids
            ):
                return None
            parent = store.read(lane.owner_id, lane.operation_id)
            round_id = str(row.get("round_operation_id") or "")
            round_record = store.read(lane.owner_id, round_id)
            callback_id = str(row.get("callback_id") or "")
            callback_sha = str(row.get("callback_sha256") or "")
            if (
                row.get("reviewed_head_sha") != attempt.identity.exact_head_sha
                or row.get("review_operation_id") != lane.operation_id
                or parent.lane_id != lane.lane_id
                or parent.run_id != lane.run_id
                or round_record.spec.parent_operation_id != lane.operation_id
                or round_record.run_id != row.get("round_run_id")
                or round_record.state != "complete"
                or round_record.accepted_callback_id != callback_id
                or round_record.accepted_callback_sha256 != callback_sha
                or not re.fullmatch(r"[0-9a-f]{64}", callback_sha)
            ):
                return None
            for finding_id in ids:
                if finding_id not in finding_ids and not any(
                    finding_id.endswith(f":{candidate}")
                    for candidate in finding_ids
                ):
                    return None
                material.add(finding_id)
    except StoreError:
        return None
    return len(material)


def _review_lane_is_bound(
    store: OperationStore,
    lane: Any,
    row: Mapping[str, Any],
) -> bool:
    try:
        stored = store.read(lane.owner_id, lane.operation_id)
    except StoreError:
        return False
    return (
        row.get("operation_id") == lane.operation_id
        and row.get("lane_id") == lane.lane_id
        and row.get("run_id") == lane.run_id
        and row.get("verification_iteration") == 0
        and stored.spec.owner_id == lane.owner_id
        and stored.spec.operation_id == lane.operation_id
        and stored.lane_id == lane.lane_id
        and stored.run_id == lane.run_id
        and stored.spec.route.runtime == lane.runtime
        and stored.spec.route.model == lane.model
        and stored.spec.route.effort == lane.effort
        and stored.spec.route.profile == lane.profile
        and stored.spec.route.routing_sha256 == lane.routing_sha256
    )


def _review_attempt_is_bound(
    store: OperationStore,
    record: OperationRecord,
    gate: Mapping[str, Any],
    attempt: ReviewAttempt,
) -> bool:
    """Bind display metrics to the exact current gate and stored lane rows."""

    identity = attempt.identity
    context = gate.get("context")
    raw_lanes = gate.get("lanes")
    if (
        gate.get("schema_version") != 1
        or gate.get("dispatch_operation_id") != record.spec.operation_id
        or gate.get("owner_id") != record.spec.owner_id
        or gate.get("active_review_operation_id") != identity.attempt_id
        or not isinstance(context, Mapping)
        or context.get("head_sha") != identity.exact_head_sha
        or not isinstance(raw_lanes, list)
        or len(raw_lanes) != len(identity.lanes)
        or any(lane.owner_id != record.spec.owner_id for lane in identity.lanes)
    ):
        return False
    lanes = {lane.axis: lane for lane in identity.lanes}
    rows: dict[str, Mapping[str, Any]] = {}
    for raw in raw_lanes:
        if not isinstance(raw, Mapping):
            return False
        axis = raw.get("axis")
        if not isinstance(axis, str) or axis in rows:
            return False
        rows[axis] = raw
    if set(rows) != set(lanes):
        return False
    if any(
        not _review_lane_is_bound(store, lane, rows[axis])
        for axis, lane in lanes.items()
    ):
        return False
    if attempt.status == "terminal" and attempt.terminal is not None:
        return gate.get("status") == attempt.terminal.result.value
    return gate.get("status") in {
        "pending",
        "reviewing",
        "verifying",
        "awaiting-resolution",
    }


def review_summary(
    store: OperationStore,
    record: OperationRecord,
    gate: Mapping[str, Any] | None,
    *,
    limit: int,
) -> ReviewSummaryView:
    """Extract bounded review scalars without reading review prose."""

    if gate is None:
        return UNKNOWN_REVIEW
    raw = gate.get("attempt")
    if not isinstance(raw, Mapping):
        return ReviewSummaryView(limit=limit)
    try:
        attempt = ReviewAttempt.from_mapping(raw)
    except ReviewAttemptError:
        return ReviewSummaryView(limit=limit)
    if not _review_attempt_is_bound(store, record, gate, attempt):
        return ReviewSummaryView(limit=limit)
    if attempt.status != "terminal" or attempt.terminal is None:
        return ReviewSummaryView(cycle=attempt.identity.cycle, limit=limit)
    findings = {
        finding_id
        for lane in attempt.terminal.lane_results
        for finding_id in lane.finding_ids
    }
    result = attempt.terminal.result
    material = (
        0
        if result == ReviewAttemptTerminalResult.APPROVED
        else _review_material_count(store, attempt, gate, findings)
        if result == ReviewAttemptTerminalResult.CHANGES_REQUESTED
        else None
    )
    return ReviewSummaryView(
        cycle=attempt.identity.cycle,
        limit=limit,
        findings=len(findings),
        material_findings=material,
    )
