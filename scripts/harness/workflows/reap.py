"""Internal typed Wiki Summary callback adapter."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..callbacks import CallbackBroker
from ..contracts import CallbackEnvelope, OperationRecord
from ..state_machine import TERMINAL
from ..store import OperationStore
from ..supervisor import OperationSupervisor


def summary_callback(
    *,
    callback_id: str,
    operation_id: str,
    run_id: str,
    summary: Mapping[str, object],
) -> CallbackEnvelope:
    payload = dict(summary)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return CallbackEnvelope(
        callback_id,
        operation_id,
        run_id,
        "wiki-summary",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )


@dataclass(frozen=True)
class ReapRun:
    record: OperationRecord
    result: Mapping[str, object] | None


def run_reap(
    store: OperationStore | Path,
    *,
    owner_id: str,
    operation_id: str,
    summary: Mapping[str, object],
    finalize,
) -> ReapRun:
    """Accept one Wiki Summary callback and finalize its exact operation."""

    store = store if isinstance(store, OperationStore) else OperationStore(store)
    record = store.read(owner_id, operation_id)
    if record.spec.kind != "dispatch":
        raise ValueError("Wiki Summary callback requires a dispatch operation")
    encoded = json.dumps(
        dict(summary), sort_keys=True, separators=(",", ":")
    ).encode()
    callback_id = str(
        uuid.uuid5(
            uuid.UUID(operation_id),
            f"wiki-summary:{hashlib.sha256(encoded).hexdigest()}",
        )
    )
    envelope = summary_callback(
        callback_id=callback_id,
        operation_id=operation_id,
        run_id=record.run_id,
        summary=summary,
    )
    if record.accepted_callback_id:
        if (
            record.accepted_callback_kind != envelope.kind
            or record.accepted_callback_sha256 != envelope.payload_sha256
        ):
            raise RuntimeError(
                "reap summary mismatches the accepted callback receipt"
            )
    else:
        CallbackBroker(store, owner_id).accept(envelope)
    supervisor = OperationSupervisor(store, owner_id, operation_id)
    record = supervisor.read()
    if record.state in TERMINAL:
        return ReapRun(record, None)
    if record.state == "exiting":
        return ReapRun(record, None)
    if record.state != "finalizing":
        raise ValueError(f"reap cannot finalize operation from {record.state}")
    effected = supervisor.effect(
        "reap-finalize",
        finalize,
        resume_pending=True,
    )
    return ReapRun(supervisor.read(), effected.value)
