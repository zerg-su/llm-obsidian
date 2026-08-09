"""Bounded read-only structural pivot boundary after the third material failure.

The exact third material product failure freezes one typed pivot packet
compiled only from accepted ledger attempt artifacts.  A structural analysis
route (the registered independent finalization route) consumes the packet
read-only and returns one receipt; product cycle four cannot be reserved
until that receipt is accepted, and the pivot itself performs no product
mutation, session, or provider effect through this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


HEAD_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PIVOT_ROUTE_ALIAS = "finalization-independent"
PIVOT_MATERIAL_FAILURES = 3
MAX_RECOMMENDATION_BYTES = 32_768
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "lineage_id",
        "packet_sha256",
        "route_alias",
        "read_only",
        "structural_recommendation",
        "status",
    }
)


class FinalizationPivotError(ValueError):
    """The pivot packet or receipt is invalid or not yet accepted."""


def material_cycles(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    cycles = snapshot.get("cycles")
    if not isinstance(cycles, list):
        raise FinalizationPivotError("ledger snapshot cycles are invalid")
    return [
        cycle
        for cycle in cycles
        if isinstance(cycle, dict)
        and cycle.get("terminal_result") == "changes-requested"
    ]


def pivot_required(snapshot: Mapping[str, Any]) -> bool:
    """True when the next product reservation needs an accepted pivot receipt."""

    return (
        not snapshot.get("terminal_disposition")
        and len(material_cycles(snapshot)) >= PIVOT_MATERIAL_FAILURES
    )


def compile_pivot_packet(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the typed packet from the first three material failures."""

    failures = material_cycles(snapshot)
    if len(failures) < PIVOT_MATERIAL_FAILURES:
        raise FinalizationPivotError(
            "structural pivot requires three material product failures"
        )
    rows = []
    for cycle in failures[:PIVOT_MATERIAL_FAILURES]:
        exact_head = str(cycle.get("exact_head") or "")
        if not HEAD_SHA.fullmatch(exact_head):
            raise FinalizationPivotError("material cycle head is invalid")
        rows.append(
            {
                "number": int(cycle["number"]),
                "attempt_id": str(cycle["attempt_id"]),
                "exact_head": exact_head,
            }
        )
    return {
        "schema_version": 1,
        "kind": "structural-pivot-packet",
        "lineage_id": str(snapshot.get("lineage_id") or ""),
        "origin_task_id": str(snapshot.get("origin_task_id") or ""),
        "plan_sha256": str(snapshot.get("plan_sha256") or ""),
        "outcome_contract_sha256": str(
            snapshot.get("outcome_contract_sha256") or ""
        ),
        "material_cycles": rows,
    }


def pivot_packet_sha256(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(packet), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_pivot_receipt(
    receipt: Any, *, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Accept exactly one receipt bound to the frozen packet and pivot route."""

    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise FinalizationPivotError("structural pivot receipt shape is invalid")
    packet = compile_pivot_packet(snapshot)
    expected_sha256 = pivot_packet_sha256(packet)
    recommendation = receipt.get("structural_recommendation")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "structural-pivot-receipt"
        or receipt.get("lineage_id") != packet["lineage_id"]
        or receipt.get("packet_sha256") != expected_sha256
        or receipt.get("route_alias") != PIVOT_ROUTE_ALIAS
        or receipt.get("read_only") is not True
        or receipt.get("status") != "accepted"
        or not isinstance(recommendation, str)
        or not recommendation.strip()
        or len(recommendation.encode()) > MAX_RECOMMENDATION_BYTES
    ):
        raise FinalizationPivotError(
            "structural pivot receipt is not an accepted packet binding"
        )
    return receipt


def pivot_receipt_path(ledger_root: Path, lineage_id: str) -> Path:
    return Path(ledger_root) / f"{lineage_id}.pivot.json"


def load_accepted_pivot_receipt(
    ledger_root: Path,
    *,
    snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Load and validate the durable receipt; absent means not yet accepted."""

    path = pivot_receipt_path(ledger_root, str(snapshot.get("lineage_id") or ""))
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise FinalizationPivotError(
            "structural pivot receipt must be a regular file"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizationPivotError(
            "structural pivot receipt is unreadable"
        ) from exc
    return validate_pivot_receipt(value, snapshot=snapshot)
