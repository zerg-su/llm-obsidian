"""Typed contracts, identities, and evidence schemas for live acceptance."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from harness.contracts import AttentionReason, CallbackEnvelope, RuntimeRoute


CELL_IDS = (
    "claude-lifecycle",
    "codex-lifecycle",
    "cross-runtime-composition",
    "deep-review",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
EVIDENCE_KEYS = {
    "schema_version",
    "cell_id",
    "commit_sha",
    "dependency_fingerprint",
    "started_at",
    "finished_at",
    "operations",
    "trace",
    "status",
}
OPERATION_KEYS = {
    "operation_id",
    "kind",
    "runtime",
    "lane_id",
    "run_id",
    "terminal_state",
    "effect_outcome",
    "callback_count",
    "owned_resources_remaining",
}
RETRYABLE_CLEANUP_ATTENTION = {
    AttentionReason.ATTENTION_REQUIRED,
    AttentionReason.CLEANUP_INCOMPLETE,
}
PREFLIGHT_KEYS = {
    "schema_version",
    "commit_sha",
    "origin_surface",
    "routes",
    "status",
}
PREFLIGHT_ROUTE_KEYS = {
    "runtime",
    "model",
    "effort",
    "profile",
    "capabilities",
}


class LiveDriverError(ValueError):
    """A live cell cannot start or its typed evidence is invalid."""


class RuntimeSessions(Protocol):
    """Narrow consumption seam implemented by ``RuntimeSessionManager``."""

    store: object

    def start(
        self,
        request: object,
        *,
        on_surface_opened: Callable[[object], None] | None = None,
    ) -> object: ...

    def accept_callback(self, envelope: CallbackEnvelope) -> object: ...

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> object: ...

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> object: ...

    def request_exit(self, owner_id: str, operation_id: str) -> object: ...

    def cleanup(self, owner_id: str, operation_id: str) -> object: ...

    def status(self, owner_id: str, operation_id: str) -> object: ...


@dataclass(frozen=True)
class _PlannedOperation:
    kind: str
    runtime: str
    lane_group: str
    continue_after_callback: bool = False


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise LiveDriverError(f"{label} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveDriverError(f"{label} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveDriverError(f"{label} must be a timezone-aware timestamp")
    return parsed


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LiveDriverError(f"{label} must be a bounded identifier")
    return value


def _operations(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LiveDriverError("operations must be a non-empty list")
    result: list[dict[str, Any]] = []
    for index, operation in enumerate(value):
        if not isinstance(operation, dict) or set(operation) != OPERATION_KEYS:
            raise LiveDriverError(f"operations[{index}] has an invalid typed shape")
        for field in ("operation_id", "kind", "lane_id", "run_id"):
            _identifier(operation[field], f"operations[{index}].{field}")
        if operation["runtime"] not in {"claude", "codex"}:
            raise LiveDriverError(f"operations[{index}].runtime is invalid")
        if operation["terminal_state"] != "complete":
            raise LiveDriverError(f"operations[{index}] is not terminal complete")
        if operation["effect_outcome"] != "succeeded":
            raise LiveDriverError(f"operations[{index}] has an unresolved effect")
        if type(operation["callback_count"]) is not int or operation["callback_count"] != 1:
            raise LiveDriverError(f"operations[{index}] callback count is not exact")
        if (
            type(operation["owned_resources_remaining"]) is not int
            or operation["owned_resources_remaining"] != 0
        ):
            raise LiveDriverError(f"operations[{index}] retains owned resources")
        result.append(operation)
    if len({row["operation_id"] for row in result}) != len(result):
        raise LiveDriverError("operation identities must be unique")
    return result


def _validate_cell_shape(cell_id: str, operations: list[dict[str, Any]]) -> None:
    kinds = [str(row["kind"]) for row in operations]
    runtimes = [str(row["runtime"]) for row in operations]
    lanes = [str(row["lane_id"]) for row in operations]
    runs = [str(row["run_id"]) for row in operations]
    if cell_id == "claude-lifecycle":
        valid = len(operations) == 1 and kinds == ["runtime-lifecycle"] and runtimes == ["claude"]
    elif cell_id == "codex-lifecycle":
        valid = len(operations) == 1 and kinds == ["runtime-lifecycle"] and runtimes == ["codex"]
    elif cell_id == "cross-runtime-composition":
        valid = (
            kinds == ["dispatch", "simple-review-holistic"]
            and runtimes == ["codex", "claude"]
            and len(set(lanes)) == 1
            and len(set(runs)) == 2
        )
    else:
        valid = (
            kinds == ["deep-review-spec", "deep-review-correctness"]
            and runtimes == ["claude", "codex"]
            and len(set(lanes)) == 2
            and len(set(runs)) == 2
        )
    if not valid:
        raise LiveDriverError(f"{cell_id}: operations do not satisfy the live cell contract")


def _operations_for(cell_id: str) -> tuple[_PlannedOperation, ...]:
    if cell_id == "claude-lifecycle":
        return (_PlannedOperation("runtime-lifecycle", "claude", "lifecycle", True),)
    if cell_id == "codex-lifecycle":
        return (_PlannedOperation("runtime-lifecycle", "codex", "lifecycle", True),)
    if cell_id == "cross-runtime-composition":
        return (
            _PlannedOperation("dispatch", "codex", "composition"),
            _PlannedOperation("simple-review", "claude", "composition"),
            _PlannedOperation("reap", "codex", "composition"),
        )
    if cell_id == "deep-review":
        return (
            _PlannedOperation("deep-review-spec", "claude", "spec"),
            _PlannedOperation(
                "deep-review-correctness", "codex", "correctness"
            ),
        )
    raise LiveDriverError("unknown live acceptance cell")


def _stable_id(*parts: str, length: int = 24) -> str:
    canonical = "\0".join(parts).encode()
    return hashlib.sha256(canonical).hexdigest()[:length]


def _route(root: Path, operation: _PlannedOperation) -> RuntimeRoute:
    try:
        from model_routing import load_tracked_config

        config = load_tracked_config(root)
        if operation.kind == "simple-review":
            value = config.reviewer_default(operation.runtime, "simple")
        elif operation.kind.startswith("deep-review-"):
            value = config.reviewer_default(operation.runtime, "deep")
        else:
            value = config.runtime_default(operation.runtime)
    except (OSError, ValueError) as exc:
        raise LiveDriverError(f"cannot resolve tracked live route: {exc}") from exc
    profile = (
        "reviewer-callback"
        if "review" in operation.kind
        else "executor"
    )
    return RuntimeRoute(
        operation.runtime,
        value["model"],
        value["effort"],
        profile,
        config.fingerprint,
    )


def validate_cell_evidence(
    expected: dict[str, Any],
    evidence: object,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    """Validate one content-free schema-v2 live result against its clean commit."""
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise LiveDriverError("cell evidence has an invalid typed shape")
    cell_id = evidence.get("cell_id")
    if cell_id not in CELL_IDS or cell_id != expected.get("cell_id"):
        raise LiveDriverError("cell evidence identity mismatches the release contract")
    if not SHA.fullmatch(commit_sha) or evidence.get("commit_sha") != commit_sha:
        raise LiveDriverError(f"{cell_id}: evidence is not bound to the exact commit")
    fingerprint = expected.get("dependency_fingerprint")
    if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint):
        raise LiveDriverError(f"{cell_id}: release dependency fingerprint is invalid")
    if evidence.get("dependency_fingerprint") != fingerprint:
        raise LiveDriverError(f"{cell_id}: dependency fingerprint changed")
    if evidence.get("schema_version") != 2 or evidence.get("status") != "passed":
        raise LiveDriverError(f"{cell_id}: live status is not a schema-v2 pass")
    started = _timestamp(evidence.get("started_at"), "started_at")
    finished = _timestamp(evidence.get("finished_at"), "finished_at")
    if finished < started:
        raise LiveDriverError(f"{cell_id}: live timestamps are reversed")
    required_trace = expected.get("required_trace")
    trace = evidence.get("trace")
    if (
        not isinstance(required_trace, list)
        or not all(isinstance(item, str) for item in required_trace)
        or trace != required_trace
    ):
        raise LiveDriverError(f"{cell_id}: required lifecycle trace is incomplete")
    operations = _operations(evidence.get("operations"))
    _validate_cell_shape(str(cell_id), operations)
    return evidence


def validate_release_evidence(
    release: dict[str, Any],
    report: object,
) -> dict[str, Any]:
    """Validate the complete four-cell report and global operation identity."""
    if (
        not isinstance(report, dict)
        or set(report)
        != {
            "schema_version",
            "commit_sha",
            "preflight",
            "cells",
            "failures",
        }
        or report.get("schema_version") != 3
        or report.get("commit_sha") != release.get("commit_sha")
    ):
        raise LiveDriverError("live report has an invalid schema-v3 commit binding")
    validate_preflight_evidence(
        report.get("preflight"),
        commit_sha=str(release.get("commit_sha") or ""),
    )
    if report.get("failures") != []:
        raise LiveDriverError("complete live report retains failed cells")
    rows = report.get("cells")
    if not isinstance(rows, list) or len(rows) != len(CELL_IDS):
        raise LiveDriverError("live report must contain exactly four cells")
    expected_rows = release.get("cells")
    if not isinstance(expected_rows, list):
        raise LiveDriverError("release contract cells are invalid")
    expected = {
        row.get("cell_id"): row for row in expected_rows if isinstance(row, dict)
    }
    if set(expected) != set(CELL_IDS):
        raise LiveDriverError("release contract must contain exactly four cells")
    validated: list[dict[str, Any]] = []
    operation_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("cell_id") not in expected:
            raise LiveDriverError("live report contains an unknown cell")
        value = validate_cell_evidence(
            expected[row["cell_id"]],
            row,
            commit_sha=release["commit_sha"],
        )
        for operation in value["operations"]:
            operation_id = operation["operation_id"]
            if operation_id in operation_ids:
                raise LiveDriverError("operation identity is reused across live cells")
            operation_ids.add(operation_id)
        validated.append(value)
    if {row["cell_id"] for row in validated} != set(CELL_IDS):
        raise LiveDriverError("live report contains duplicate or missing cells")
    return report


def validate_preflight_evidence(
    evidence: object,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    """Validate the content-free global host proof bound to one release SHA."""

    if (
        not isinstance(evidence, dict)
        or set(evidence) != PREFLIGHT_KEYS
        or evidence.get("schema_version") != 1
        or evidence.get("commit_sha") != commit_sha
        or not SHA.fullmatch(commit_sha)
        or evidence.get("status") != "compatible"
        or not re.fullmatch(
            r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}"
            r"[0-9A-Fa-f]{12}",
            str(evidence.get("origin_surface") or ""),
        )
    ):
        raise LiveDriverError("global preflight evidence is invalid")
    routes = evidence.get("routes")
    if not isinstance(routes, list) or not routes:
        raise LiveDriverError("global preflight routes are missing")
    identities: set[tuple[str, str, str, str]] = set()
    for index, route in enumerate(routes):
        if (
            not isinstance(route, dict)
            or set(route) != PREFLIGHT_ROUTE_KEYS
            or route.get("runtime") not in {"claude", "codex"}
        ):
            raise LiveDriverError(
                f"global preflight route {index} has an invalid shape"
            )
        identity = tuple(
            _identifier(route.get(field), f"preflight.routes[{index}].{field}")
            for field in ("runtime", "model", "effort", "profile")
        )
        capabilities = route.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(
                not isinstance(item, str)
                or not IDENTIFIER.fullmatch(item)
                for item in capabilities
            )
            or len(capabilities) != len(set(capabilities))
        ):
            raise LiveDriverError(
                f"global preflight route {index} capabilities are invalid"
            )
        if identity in identities:
            raise LiveDriverError("global preflight route is duplicated")
        identities.add(identity)
    return evidence
