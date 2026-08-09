"""Durable five-cycle lineage ledger for exact-HEAD finalization attempts.

The ledger is deliberately smaller than an operation or event store.  It owns
only an immutable lineage binding, one active reservation at a time, terminal
attempt results, and the code-owned cycle ceiling.  Callers must reserve before
creating a reviewer session or causing a provider effect.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEAD_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
TERMINAL_RESULTS = frozenset(
    {"approved", "changes-requested", "blocked", "attention-required"}
)
#: Product cycles are consumed only by material review outcomes; mechanism
#: outcomes release their reservation into an immutable attempt receipt.
PRODUCT_RESULTS = frozenset({"approved", "changes-requested"})
MECHANISM_RESULTS = frozenset({"attention-required", "blocked"})
MAX_FINALIZATION_CYCLES = 5
#: Mechanism recovery evidence is separately bounded; an unbounded durable
#: receipt list would be unvalidatable and hide a broken retry loop.
MECHANISM_ATTEMPT_CEILING = 25
LEDGER_FIELDS = frozenset(
    {
        "schema_version",
        "lineage_id",
        "origin_task_id",
        "plan_sha256",
        "outcome_contract_sha256",
        "max_cycles",
        "cycles",
        "terminal_disposition",
    }
)
LEDGER_FIELDS_WITH_ATTEMPTS = LEDGER_FIELDS | {"attempts"}
CYCLE_FIELDS = frozenset(
    {
        "number",
        "attempt_id",
        "exact_head",
        "task_id",
        "worktree",
        "provider_policy",
        "terminal_result",
    }
)
ATTEMPT_RECEIPT_FIELDS = frozenset(
    {"attempt_id", "cycle_number", "classification"}
)


class FinalizationLedgerError(ValueError):
    """The requested ledger transition is invalid or contradicts disk state."""


@dataclass(frozen=True)
class CycleDecision:
    """Effect authorization and current result for one reservation request."""

    allowed: bool
    created: bool
    reason: str
    cycle_number: int | None = None
    attempt_id: str = ""
    terminal_result: str = ""
    terminal_disposition: str = ""


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        rendered = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise FinalizationLedgerError(f"{label} must be a canonical UUID") from exc
    if rendered != value:
        raise FinalizationLedgerError(f"{label} must be a canonical UUID")
    return rendered


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise FinalizationLedgerError(f"{label} must be a lowercase sha256")
    return value


def _head(value: Any) -> str:
    if not isinstance(value, str) or not HEAD_SHA.fullmatch(value):
        raise FinalizationLedgerError("exact_head must be a lowercase Git hash")
    return value


def _worktree(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinalizationLedgerError("worktree must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise FinalizationLedgerError("worktree must be an absolute path")
    return str(path)


def _provider_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FinalizationLedgerError("provider_policy must be an object")
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        canonical = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizationLedgerError(
            "provider_policy must contain bounded JSON data"
        ) from exc
    if len(encoded) > 32_768 or not isinstance(canonical, dict):
        raise FinalizationLedgerError("provider_policy exceeds its byte ceiling")
    routes = canonical.get("routes")
    reason = canonical.get("reason")
    if (
        not isinstance(routes, list)
        or not 1 <= len(routes) <= 2
        or len(routes) != len(set(routes))
        or any(not isinstance(route, str) or not IDENTIFIER.fullmatch(route) for route in routes)
        or not isinstance(reason, str)
        or not IDENTIFIER.fullmatch(reason)
    ):
        raise FinalizationLedgerError(
            "provider_policy requires one or two unique routes and a typed reason"
        )
    return canonical


def _decision(
    cycle: Mapping[str, Any] | None,
    *,
    allowed: bool,
    created: bool,
    reason: str,
    disposition: str,
) -> CycleDecision:
    return CycleDecision(
        allowed=allowed,
        created=created,
        reason=reason,
        cycle_number=int(cycle["number"]) if cycle is not None else None,
        attempt_id=str(cycle["attempt_id"]) if cycle is not None else "",
        terminal_result=str(cycle["terminal_result"]) if cycle is not None else "",
        terminal_disposition=disposition,
    )


class FinalizationLedger:
    """Atomically reserve and finish an exact-HEAD finalization cycle."""

    def __init__(
        self,
        root: Path | str,
        *,
        lineage_id: str,
        origin_task_id: str,
        plan_sha256: str,
        outcome_contract_sha256: str,
        max_cycles: int = MAX_FINALIZATION_CYCLES,
    ):
        self.root = Path(root).expanduser()
        self.lineage_id = _canonical_uuid(lineage_id, "lineage_id")
        self.origin_task_id = _canonical_uuid(origin_task_id, "origin_task_id")
        self.plan_sha256 = _sha256(plan_sha256, "plan_sha256")
        self.outcome_contract_sha256 = _sha256(
            outcome_contract_sha256, "outcome_contract_sha256"
        )
        if (
            isinstance(max_cycles, bool)
            or not isinstance(max_cycles, int)
            or not 1 <= max_cycles <= MAX_FINALIZATION_CYCLES
        ):
            raise FinalizationLedgerError("max_cycles must be an integer from 1 to 5")
        self.max_cycles = max_cycles

    @property
    def path(self) -> Path:
        return self.root / f"{self.lineage_id}.json"

    @property
    def lock_path(self) -> Path:
        return self.root / f"{self.lineage_id}.lock"

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise FinalizationLedgerError("ledger root cannot be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise FinalizationLedgerError("ledger root must be a directory")
        if stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise FinalizationLedgerError("ledger root must be owner-only")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_root()
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "lineage_id": self.lineage_id,
            "origin_task_id": self.origin_task_id,
            "plan_sha256": self.plan_sha256,
            "outcome_contract_sha256": self.outcome_contract_sha256,
            "max_cycles": self.max_cycles,
            "cycles": [],
            "attempts": [],
            "terminal_disposition": "",
        }

    def _read(self, *, missing_ok: bool = False) -> dict[str, Any]:
        if not self.path.exists():
            if missing_ok:
                return self._empty()
            raise FinalizationLedgerError("finalization ledger is unavailable")
        if self.path.is_symlink() or not self.path.is_file():
            raise FinalizationLedgerError("finalization ledger must be a regular file")
        if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise FinalizationLedgerError("finalization ledger must be owner-only")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinalizationLedgerError("finalization ledger is invalid") from exc
        if isinstance(value, dict) and "attempts" not in value:
            # Pre-2.6.7 ledgers carry no mechanism attempt receipts.
            value["attempts"] = []
        self._validate(value)
        return value

    def _validate(self, value: Any) -> None:
        if not isinstance(value, dict) or set(value) != LEDGER_FIELDS_WITH_ATTEMPTS:
            raise FinalizationLedgerError("finalization ledger shape is invalid")
        expected = self._empty()
        for field in (
            "schema_version",
            "lineage_id",
            "origin_task_id",
            "plan_sha256",
            "outcome_contract_sha256",
            "max_cycles",
        ):
            if value.get(field) != expected[field]:
                raise FinalizationLedgerError(
                    f"finalization ledger {field} binding changed"
                )
        cycles = value.get("cycles")
        if not isinstance(cycles, list) or len(cycles) > self.max_cycles:
            raise FinalizationLedgerError("finalization ledger cycles are invalid")
        active = 0
        seen_attempts: set[str] = set()
        for index, cycle in enumerate(cycles, start=1):
            if not isinstance(cycle, dict) or set(cycle) != CYCLE_FIELDS:
                raise FinalizationLedgerError("finalization cycle shape is invalid")
            if cycle.get("number") != index:
                raise FinalizationLedgerError("finalization cycle numbering changed")
            attempt_id = _canonical_uuid(cycle.get("attempt_id"), "attempt_id")
            if attempt_id in seen_attempts:
                raise FinalizationLedgerError("finalization attempt_id was reused")
            seen_attempts.add(attempt_id)
            _head(cycle.get("exact_head"))
            _canonical_uuid(cycle.get("task_id"), "task_id")
            _worktree(cycle.get("worktree"))
            _provider_policy(cycle.get("provider_policy"))
            result = cycle.get("terminal_result")
            if result not in {*TERMINAL_RESULTS, ""}:
                raise FinalizationLedgerError("finalization terminal result is invalid")
            if not result:
                active += 1
        if active > 1 or (active and not cycles[-1].get("terminal_result") == ""):
            raise FinalizationLedgerError("finalization active reservation is invalid")
        attempts = value.get("attempts")
        if not isinstance(attempts, list):
            raise FinalizationLedgerError("finalization attempts are invalid")
        if len(attempts) > MECHANISM_ATTEMPT_CEILING:
            raise FinalizationLedgerError(
                "mechanism attempt receipts exceed their bounded ceiling"
            )
        for receipt in attempts:
            if not isinstance(receipt, dict) or set(receipt) != ATTEMPT_RECEIPT_FIELDS:
                raise FinalizationLedgerError(
                    "mechanism attempt receipt shape is invalid"
                )
            _canonical_uuid(receipt.get("attempt_id"), "attempt_id")
            cycle_number = receipt.get("cycle_number")
            if (
                isinstance(cycle_number, bool)
                or not isinstance(cycle_number, int)
                or not 1 <= cycle_number <= self.max_cycles
            ):
                raise FinalizationLedgerError(
                    "mechanism attempt cycle number is invalid"
                )
            if receipt.get("classification") not in MECHANISM_RESULTS:
                raise FinalizationLedgerError(
                    "mechanism attempt classification is invalid"
                )
        disposition = value.get("terminal_disposition")
        if disposition not in {"", "approved", "finalization-budget-exhausted"}:
            raise FinalizationLedgerError("finalization terminal disposition is invalid")
        if disposition == "approved" and (
            not cycles or cycles[-1].get("terminal_result") != "approved"
        ):
            raise FinalizationLedgerError("approved lineage lacks an approved attempt")
        if disposition == "finalization-budget-exhausted" and (
            len(cycles) != self.max_cycles
            or not cycles[-1].get("terminal_result")
            or cycles[-1].get("terminal_result") == "approved"
        ):
            raise FinalizationLedgerError("exhausted lineage has an invalid last attempt")

    def _write(self, value: dict[str, Any]) -> None:
        self._validate(value)
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _reserve_with_policies(
        self,
        *,
        attempt_id: str,
        exact_head: str,
        task_id: str,
        worktree: str,
        provider_policies: Mapping[int, dict[str, Any]],
    ) -> CycleDecision:
        requested_identity = {
            "attempt_id": _canonical_uuid(attempt_id, "attempt_id"),
            "exact_head": _head(exact_head),
            "task_id": _canonical_uuid(task_id, "task_id"),
            "worktree": _worktree(worktree),
        }
        with self._locked():
            value = self._read(missing_ok=True)
            disposition = value["terminal_disposition"]
            if disposition:
                latest = value["cycles"][-1] if value["cycles"] else None
                if not (
                    disposition == "approved"
                    and latest is not None
                    and latest["exact_head"] != requested_identity["exact_head"]
                ):
                    return _decision(
                        None,
                        allowed=False,
                        created=False,
                        reason=disposition,
                        disposition=disposition,
                    )
                value["terminal_disposition"] = ""
            for cycle in value["cycles"]:
                if cycle["attempt_id"] != requested_identity["attempt_id"]:
                    continue
                requested = {
                    **requested_identity,
                    "provider_policy": provider_policies[cycle["number"]],
                }
                if any(cycle[field] != requested[field] for field in requested):
                    raise FinalizationLedgerError(
                        "attempt_id reservation binding is immutable"
                    )
                return _decision(
                    cycle,
                    allowed=False,
                    created=False,
                    reason=(
                        "attempt-terminal"
                        if cycle["terminal_result"]
                        else "already-reserved"
                    ),
                    disposition="",
                )
            if value["cycles"] and not value["cycles"][-1]["terminal_result"]:
                raise FinalizationLedgerError(
                    "a distinct attempt cannot overlap the active reservation"
                )
            if len(value["cycles"]) >= self.max_cycles:
                raise FinalizationLedgerError(
                    "finalization cycle ceiling lacks a terminal disposition"
                )
            cycle_number = len(value["cycles"]) + 1
            cycle = {
                "number": cycle_number,
                **requested_identity,
                "provider_policy": provider_policies[cycle_number],
                "terminal_result": "",
            }
            value["cycles"].append(cycle)
            self._write(value)
            return _decision(
                cycle,
                allowed=True,
                created=True,
                reason="reserved",
                disposition="",
            )

    def reserve(
        self,
        *,
        attempt_id: str,
        exact_head: str,
        task_id: str,
        worktree: str,
        provider_policy: Mapping[str, Any],
    ) -> CycleDecision:
        """Reserve the sole active cycle and authorize at most one effect."""

        policy = _provider_policy(provider_policy)
        return self._reserve_with_policies(
            attempt_id=attempt_id,
            exact_head=exact_head,
            task_id=task_id,
            worktree=worktree,
            provider_policies={
                cycle: policy for cycle in range(1, self.max_cycles + 1)
            },
        )

    def reserve_from_policy_matrix(
        self,
        *,
        attempt_id: str,
        exact_head: str,
        task_id: str,
        worktree: str,
        provider_policies: Mapping[int, Mapping[str, Any]],
    ) -> CycleDecision:
        """Atomically select the policy for the cycle actually reserved."""

        if not isinstance(provider_policies, Mapping) or set(
            provider_policies
        ) != set(range(1, self.max_cycles + 1)):
            raise FinalizationLedgerError(
                "provider policy matrix must cover every configured cycle"
            )
        canonical = {
            cycle: _provider_policy(provider_policies[cycle])
            for cycle in range(1, self.max_cycles + 1)
        }
        return self._reserve_with_policies(
            attempt_id=attempt_id,
            exact_head=exact_head,
            task_id=task_id,
            worktree=worktree,
            provider_policies=canonical,
        )

    def record_terminal(
        self, *, attempt_id: str, terminal_result: str
    ) -> CycleDecision:
        """Record exactly one terminal result for the active attempt."""

        attempt_id = _canonical_uuid(attempt_id, "attempt_id")
        if terminal_result not in TERMINAL_RESULTS:
            raise FinalizationLedgerError("terminal_result is invalid")
        with self._locked():
            value = self._read()
            matching = [
                cycle
                for cycle in value["cycles"]
                if cycle["attempt_id"] == attempt_id
            ]
            if not matching:
                recorded_attempts = [
                    receipt
                    for receipt in value["attempts"]
                    if receipt["attempt_id"] == attempt_id
                ]
                if recorded_attempts and terminal_result in MECHANISM_RESULTS:
                    latest = recorded_attempts[-1]
                    if latest["classification"] != terminal_result:
                        raise FinalizationLedgerError(
                            "mechanism attempt classification is immutable"
                        )
                    return CycleDecision(
                        allowed=False,
                        created=False,
                        reason="already-mechanism",
                        cycle_number=int(latest["cycle_number"]),
                        attempt_id=attempt_id,
                        terminal_result=terminal_result,
                        terminal_disposition=value["terminal_disposition"],
                    )
                raise FinalizationLedgerError("attempt reservation is unavailable")
            cycle = matching[0]
            if terminal_result in MECHANISM_RESULTS:
                if cycle["terminal_result"]:
                    raise FinalizationLedgerError(
                        "a product-terminal attempt cannot become mechanism evidence"
                    )
                if cycle is not value["cycles"][-1]:
                    raise FinalizationLedgerError("only the active attempt can finish")
                value["cycles"].remove(cycle)
                value["attempts"].append(
                    {
                        "attempt_id": attempt_id,
                        "cycle_number": int(cycle["number"]),
                        "classification": terminal_result,
                    }
                )
                self._write(value)
                return CycleDecision(
                    allowed=False,
                    created=False,
                    reason="mechanism-recorded",
                    cycle_number=int(cycle["number"]),
                    attempt_id=attempt_id,
                    terminal_result=terminal_result,
                    terminal_disposition=value["terminal_disposition"],
                )
            recorded = cycle["terminal_result"]
            if recorded:
                if recorded != terminal_result:
                    raise FinalizationLedgerError("terminal result is immutable")
                return _decision(
                    cycle,
                    allowed=False,
                    created=False,
                    reason="already-terminal",
                    disposition=value["terminal_disposition"],
                )
            if cycle is not value["cycles"][-1]:
                raise FinalizationLedgerError("only the active attempt can finish")
            cycle["terminal_result"] = terminal_result
            if terminal_result == "approved":
                value["terminal_disposition"] = "approved"
            elif cycle["number"] == self.max_cycles:
                value["terminal_disposition"] = "finalization-budget-exhausted"
            self._write(value)
            return _decision(
                cycle,
                allowed=False,
                created=False,
                reason="terminal-recorded",
                disposition=value["terminal_disposition"],
            )

    def snapshot(self, *, missing_ok: bool = False) -> dict[str, Any]:
        """Return a validated detached view for inspect/status projections."""

        with self._locked():
            return json.loads(json.dumps(self._read(missing_ok=missing_ok)))
