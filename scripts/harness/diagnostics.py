"""Content-free deterministic anomaly packets for optional fast triage."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .state_machine import TERMINAL
from .review_continuation_recovery import RecoveryReceipt
from .store import OperationStore, StoreError
from .verification_invalidation import superseded_verification_ids


MAX_SIGNALS = 8
MAX_WAKE_OBSERVATIONS = 8
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
WAKE_SOURCES = frozenset(
    {
        "cmux-event",
        "reconnect",
        "cursor-gap",
        "degraded",
        "unavailable",
        "fallback-poll",
        "stability-confirmation",
    }
)
WAKE_EVENTS = frozenset(
    {
        "",
        "agent.hook.SessionStart",
        "agent.hook.PostToolUse",
        "agent.hook.Stop",
        "agent.hook.SessionEnd",
        "notification.created",
        "surface.created",
        "surface.closed",
        "workspace.created",
        "workspace.closed",
    }
)
MODEL_POLICY = {
    "role": "diagnostic-fast",
    "context": "minimal",
    "write": False,
}


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _signal(
    code: str,
    *,
    operation_id: str,
    state: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "code": code,
        "operation_id": operation_id,
        "state": state,
        "evidence": evidence,
    }


def _recovery_signal(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    path: Path,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    evidence = [_relative(store.root, path)]
    raw = None if path.is_symlink() or not path.is_file() else _read_object(path)
    recovery_class = ""
    if isinstance(raw, dict):
        identity = raw.get("identity")
        if isinstance(identity, dict):
            recovery_class = str(identity.get("recovery_class") or "")
    prefix = {
        "review-drive": "review-drive-recovery",
        "accepted-callback": "review-callback-ingestion",
    }.get(recovery_class, "review-continuation")
    try:
        if raw is None or raw.get("schema_version") != 1:
            raise ValueError("receipt unavailable")
        receipt = RecoveryReceipt.from_mapping(raw)
        if (
            receipt.identity.owner_id != owner_id
            or receipt.identity.root_operation_id != operation_id
        ):
            raise ValueError("receipt owner changed")
        if receipt.status == "prepared":
            outcome = "prepared"
        else:
            outcome = str(raw.get("outcome") or "")
            if outcome not in {"advanced", "refused"}:
                raise ValueError("receipt outcome changed")
        state = str(raw.get("reason") or outcome)
        code = f"{prefix}-{outcome}"
    except (TypeError, ValueError):
        state = "attention-required"
        code = f"{prefix}-receipt-invalid"
    return _signal(
        code,
        operation_id=operation_id,
        state=state,
        evidence=evidence,
    )


def _recovery_signals(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
) -> list[dict[str, Any]]:
    runtime = (
        store.root / "owners" / owner_id / "runtime" / operation_id
    )
    receipt_root = runtime / "review-continuation-recovery"
    paths: list[Path] = []
    if receipt_root.is_dir() and not receipt_root.is_symlink():
        paths.extend(sorted(receipt_root.glob("*.json")))
    values = [
        signal
        for path in paths
        if (signal := _recovery_signal(
            store, owner_id, operation_id, path
        )) is not None
    ]
    if not values:
        return []
    # Keep the current actionable state visible without allowing old finalized
    # identities to consume the bounded diagnostic packet.
    priority = {
        "prepared": 0,
        "receipt-invalid": 1,
        "refused": 2,
        "advanced": 3,
    }
    values.sort(
        key=lambda signal: next(
            (
                rank
                for suffix, rank in priority.items()
                if str(signal.get("code") or "").endswith(suffix)
            ),
            4,
        )
    )
    return values[:1]


def _wake_observation(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    path: Path,
    kind: str,
) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    value = _read_object(path)
    if value is None:
        return None
    scalar_identity = (
        value.get("owner_id"),
        value.get("operation_id"),
        value.get("run_id"),
    )
    source = value.get("source")
    event_name = value.get("event_name")
    sequence = value.get("sequence")
    generation = value.get("generation")
    observed_at = value.get("observed_at")
    outcome = value.get("outcome")
    if (
        value.get("schema_version") != 1
        or scalar_identity[0] != owner_id
        or scalar_identity[1] != operation_id
        or any(
            not isinstance(item, str) or not IDENTIFIER.fullmatch(item)
            for item in scalar_identity
        )
        or source not in WAKE_SOURCES
        or event_name not in WAKE_EVENTS
        or type(sequence) is not int
        or sequence < 0
        or type(generation) is not int
        or generation < 1
        or not isinstance(observed_at, (int, float))
        or observed_at < 0
        or outcome not in {"progressed", "no-change"}
    ):
        return None
    return {
        "kind": kind,
        "owner_id": owner_id,
        "operation_id": operation_id,
        "run_id": scalar_identity[2],
        "generation": generation,
        "source": source,
        "event_name": event_name,
        "sequence": sequence,
        "observed_at": observed_at,
        "outcome": outcome,
        "evidence": _relative(store.root, path),
    }


def observe(store_root: Path | str, owner_id: str) -> dict[str, Any]:
    """Classify durable invariants without reading callback or prompt bodies."""

    store = OperationStore(store_root)
    records = store.list(owner_id)
    wake_observations = [
        observation
        for record in records
        for kind, filename in (
            ("latest-full-reconcile", "wake-observation.json"),
            ("latest-progress", "wake-progress.json"),
        )
        if (
            observation := _wake_observation(
                store,
                owner_id,
                record.spec.operation_id,
                store.root
                / "owners"
                / owner_id
                / "runtime"
                / record.spec.operation_id
                / filename,
                kind,
            )
        )
        is not None
    ][:MAX_WAKE_OBSERVATIONS]
    signals = [
        signal
        for record in records
        for signal in _recovery_signals(
            store, owner_id, record.spec.operation_id
        )
    ]
    gate_path = (
        store.root
        / "review-data"
        / owner_id
        / owner_id
        / "review-gate.json"
    )
    gate: dict[str, Any] | None = None
    if gate_path.is_file() and not gate_path.is_symlink():
        gate = _read_object(gate_path)
        gate_evidence = [_relative(store.root, gate_path)]
        if (
            gate is None
            or gate.get("schema_version") != 1
            or gate.get("owner_id") != owner_id
        ):
            signals.append(
                _signal(
                    "review-state-invalid",
                    operation_id=owner_id,
                    state="invalid",
                    evidence=gate_evidence,
                )
            )
            gate = None

    if gate is not None:
        operation_id = str(
            gate.get("dispatch_operation_id") or owner_id
        )
        callback_root = (
            store.root
            / "review-runtime"
            / owner_id
            / "callbacks"
        )
        callbacks = (
            [
                path
                for path in callback_root.rglob(".review-callback.json")
                if path.is_file() and not path.is_symlink()
            ]
            if callback_root.is_dir()
            else []
        )
        round_results = gate.get("round_results")
        result_count = (
            len(round_results) if isinstance(round_results, dict) else 0
        )
        if (
            gate.get("status") in {"reviewing", "verifying"}
            and len(callbacks) > result_count
            and not any(
                str(signal.get("code") or "").startswith(
                    "review-callback-ingestion-"
                )
                for signal in signals
            )
        ):
            signals.append(
                _signal(
                    "review-callback-pending-ingestion",
                    operation_id=operation_id,
                    state=str(gate.get("status") or ""),
                    evidence=[
                        _relative(store.root, gate_path),
                        _relative(store.root, callback_root),
                    ],
                )
            )
        if gate.get("status") == "awaiting-resolution":
            product_raw = gate.get("product_root")
            product = (
                Path(product_raw).expanduser().resolve()
                if isinstance(product_raw, str) and product_raw
                else None
            )
            if (
                product is not None
                and product.is_dir()
                and not (product / ".task-review.json").is_file()
            ):
                signals.append(
                    _signal(
                        "review-resolution-pending-delivery",
                        operation_id=operation_id,
                        state="awaiting-resolution",
                        evidence=[
                            _relative(store.root, gate_path),
                            "product:.task-review.json",
                        ],
                    )
                )

    model_required = any(
        str(signal.get("code") or "").endswith("receipt-invalid")
        for signal in signals
    )
    explained_operations = {
        str(signal.get("operation_id") or "") for signal in signals
    }
    root = next(
        (record for record in records if record.spec.operation_id == owner_id),
        None,
    )
    superseded = (
        superseded_verification_ids(store, root, records)
        if root is not None
        else frozenset()
    )
    for record in records:
        if (
            record.state != "attention-required"
            or record.spec.operation_id in explained_operations
            or record.spec.operation_id in superseded
        ):
            continue
        reason = (
            record.attention_reason.value
            if record.attention_reason is not None
            else "unknown"
        )
        signals.append(
            _signal(
                "operation-attention-unclassified",
                operation_id=record.spec.operation_id,
                state=reason,
                evidence=[
                    (
                        "owners/"
                        f"{owner_id}/operations/"
                        f"{record.spec.operation_id}.json"
                    )
                ],
            )
        )
        model_required = True
        if len(signals) >= MAX_SIGNALS:
            break

    signals = signals[:MAX_SIGNALS]
    status = (
        "needs-model"
        if model_required
        else "actionable"
        if signals
        else "healthy"
    )
    return {
        "schema_version": 1,
        "owner_id": owner_id,
        "status": status,
        "model_required": model_required,
        "model_policy": dict(MODEL_POLICY),
        "counts": {
            "operations": len(records),
            "active": sum(
                record.state not in TERMINAL for record in records
            ),
            "signals": len(signals),
            "wake_observations": len(wake_observations),
        },
        "signals": signals,
        "wake_observations": wake_observations,
    }
