"""Content-free deterministic anomaly packets for optional fast triage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state_machine import TERMINAL
from .store import OperationStore, StoreError


MAX_SIGNALS = 8
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


def observe(store_root: Path | str, owner_id: str) -> dict[str, Any]:
    """Classify durable invariants without reading callback or prompt bodies."""

    store = OperationStore(store_root)
    records = store.list(owner_id)
    signals: list[dict[str, Any]] = []
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

    model_required = False
    if not signals:
        for record in records:
            if record.state != "attention-required":
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
        },
        "signals": signals,
    }
