"""Content-free callback liveness evidence and bounded recovery decisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .contracts import ContractError, ID_RE, SHA256_RE, to_dict


PROCESS_STATES = frozenset({"alive", "dead", "unknown"})
PROMPT_STATES = frozenset({"interactive", "non-interactive", "unknown"})
ACTIONS = frozenset(
    {
        "observe",
        "reconcile-result",
        "suspected-idle",
        "nudge",
        "restart",
        "attention-required",
    }
)


def _sha(value: str, label: str, *, optional: bool = True) -> None:
    if optional and not value:
        return
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase sha256")


@dataclass(frozen=True)
class LivenessPolicy:
    probe_seconds: int
    suspected_idle_seconds: int
    nudge_after_seconds: int
    restart_after_seconds: int
    max_nudges: int
    max_restarts: int
    stable_result_reads: int
    schema_version: int = 1

    @classmethod
    def default(cls) -> "LivenessPolicy":
        return cls(60, 600, 900, 1200, 1, 1, 2)

    def __post_init__(self) -> None:
        values = (
            self.probe_seconds,
            self.suspected_idle_seconds,
            self.nudge_after_seconds,
            self.restart_after_seconds,
            self.max_nudges,
            self.max_restarts,
            self.stable_result_reads,
        )
        if (
            self.schema_version != 1
            or any(type(value) is not int or value < 0 for value in values)
            or self.probe_seconds < 30
            or self.suspected_idle_seconds < 600
            or self.nudge_after_seconds < self.suspected_idle_seconds
            or self.restart_after_seconds < self.nudge_after_seconds
            or self.max_nudges > 1
            or self.max_restarts > 1
            or self.stable_result_reads < 2
        ):
            raise ContractError("liveness policy exceeds its code-owned bounds")


@dataclass(frozen=True)
class LivenessEvidence:
    observed_at: float
    process_status: str
    operation_revision: int
    operation_state: str
    screen_sha256: str = ""
    prompt_state: str = "unknown"
    typed_result_sha256: str = ""
    callback_sha256: str = ""
    receipt_sha256: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not isinstance(self.observed_at, (int, float))
            or isinstance(self.observed_at, bool)
            or self.observed_at < 0
            or type(self.operation_revision) is not int
            or self.operation_revision < 0
            or self.process_status not in PROCESS_STATES
            or self.prompt_state not in PROMPT_STATES
            or not isinstance(self.operation_state, str)
            or not ID_RE.fullmatch(self.operation_state)
        ):
            raise ContractError("liveness evidence is invalid")
        for value, label in (
            (self.screen_sha256, "screen_sha256"),
            (self.typed_result_sha256, "typed_result_sha256"),
            (self.callback_sha256, "callback_sha256"),
            (self.receipt_sha256, "receipt_sha256"),
        ):
            _sha(value, label)


@dataclass(frozen=True)
class LivenessState:
    started_at: float
    last_progress_at: float
    operation_revision: int
    operation_state: str
    screen_sha256: str = ""
    typed_result_sha256: str = ""
    callback_sha256: str = ""
    receipt_sha256: str = ""
    stable_result_reads: int = 0
    nudge_count: int = 0
    restart_count: int = 0
    callback_submit_binding: str = ""
    callback_submit_status: str = ""
    schema_version: int = 1

    @classmethod
    def start(cls, evidence: LivenessEvidence) -> "LivenessState":
        return cls(
            started_at=evidence.observed_at,
            last_progress_at=evidence.observed_at,
            operation_revision=evidence.operation_revision,
            operation_state=evidence.operation_state,
            screen_sha256=evidence.screen_sha256,
            typed_result_sha256=evidence.typed_result_sha256,
            callback_sha256=evidence.callback_sha256,
            receipt_sha256=evidence.receipt_sha256,
            stable_result_reads=1 if evidence.typed_result_sha256 else 0,
        )


@dataclass(frozen=True)
class LivenessDecision:
    action: str
    action_id: str
    model_call: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.action not in ACTIONS:
            raise ContractError("liveness decision is invalid")
        _sha(self.action_id, "liveness action_id", optional=False)


def _decision(action: str, evidence: LivenessEvidence, state: LivenessState) -> LivenessDecision:
    identity = json.dumps(
        {
            "action": action,
            "operation_revision": evidence.operation_revision,
            "operation_state": evidence.operation_state,
            "screen_sha256": evidence.screen_sha256,
            "typed_result_sha256": evidence.typed_result_sha256,
            "callback_sha256": evidence.callback_sha256,
            "receipt_sha256": evidence.receipt_sha256,
            "nudge_count": state.nudge_count,
            "restart_count": state.restart_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return LivenessDecision(
        action,
        hashlib.sha256(identity.encode()).hexdigest(),
        model_call=action in {"nudge", "restart"},
    )


def observe_liveness(
    previous: LivenessState,
    evidence: LivenessEvidence,
    policy: LivenessPolicy,
) -> tuple[LivenessDecision, LivenessState]:
    """Derive one recovery action without retaining screen or callback bodies."""

    if evidence.observed_at < previous.started_at:
        raise ContractError("liveness observation moved backwards")
    changed = any(
        (
            evidence.operation_revision != previous.operation_revision,
            evidence.operation_state != previous.operation_state,
            bool(evidence.screen_sha256)
            and evidence.screen_sha256 != previous.screen_sha256,
            bool(evidence.typed_result_sha256)
            and evidence.typed_result_sha256 != previous.typed_result_sha256,
            bool(evidence.callback_sha256)
            and evidence.callback_sha256 != previous.callback_sha256,
            bool(evidence.receipt_sha256)
            and evidence.receipt_sha256 != previous.receipt_sha256,
        )
    )
    stable_result_reads = (
        previous.stable_result_reads + 1
        if evidence.typed_result_sha256
        and evidence.typed_result_sha256 == previous.typed_result_sha256
        else (1 if evidence.typed_result_sha256 else 0)
    )
    current = replace(
        previous,
        last_progress_at=(evidence.observed_at if changed else previous.last_progress_at),
        operation_revision=evidence.operation_revision,
        operation_state=evidence.operation_state,
        screen_sha256=evidence.screen_sha256 or previous.screen_sha256,
        typed_result_sha256=(
            evidence.typed_result_sha256 or previous.typed_result_sha256
        ),
        callback_sha256=evidence.callback_sha256 or previous.callback_sha256,
        receipt_sha256=evidence.receipt_sha256 or previous.receipt_sha256,
        stable_result_reads=stable_result_reads,
    )

    if evidence.receipt_sha256 or evidence.callback_sha256:
        return _decision("observe", evidence, current), current
    if (
        evidence.typed_result_sha256
        and stable_result_reads >= policy.stable_result_reads
    ):
        return _decision("reconcile-result", evidence, current), current
    if evidence.process_status == "dead":
        if current.restart_count < policy.max_restarts:
            current = replace(current, restart_count=current.restart_count + 1)
            return _decision("restart", evidence, current), current
        return _decision("attention-required", evidence, current), current
    if (
        evidence.process_status != "alive"
        or evidence.prompt_state != "non-interactive"
    ):
        return _decision("observe", evidence, current), current

    idle_seconds = evidence.observed_at - current.last_progress_at
    if idle_seconds >= policy.restart_after_seconds:
        if current.nudge_count < policy.max_nudges:
            current = replace(current, nudge_count=current.nudge_count + 1)
            return _decision("nudge", evidence, current), current
        if current.restart_count < policy.max_restarts:
            current = replace(current, restart_count=current.restart_count + 1)
            return _decision("restart", evidence, current), current
        return _decision("attention-required", evidence, current), current
    if idle_seconds >= policy.nudge_after_seconds:
        if current.nudge_count < policy.max_nudges:
            current = replace(current, nudge_count=current.nudge_count + 1)
            return _decision("nudge", evidence, current), current
        return _decision("suspected-idle", evidence, current), current
    if idle_seconds >= policy.suspected_idle_seconds:
        return _decision("suspected-idle", evidence, current), current
    return _decision("observe", evidence, current), current


class LivenessController:
    """Persist content-free state and idempotent recovery receipts."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @contextmanager
    def _locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        lock_path = self.root / ".lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        )
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _state(self) -> LivenessState | None:
        path = self.root / "state.json"
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ContractError("liveness state is not owner-only")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError
            return LivenessState(**value)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("liveness state is invalid") from exc

    def observe(
        self,
        evidence: LivenessEvidence,
        policy: LivenessPolicy,
    ) -> LivenessDecision:
        with self._locked():
            previous = self._state()
            if previous is None:
                current = LivenessState.start(evidence)
                decision = _decision("observe", evidence, current)
            else:
                decision, current = observe_liveness(previous, evidence, policy)
            self._write(self.root / "state.json", to_dict(current))
            if decision.action != "observe":
                receipt = {
                    "schema_version": 1,
                    "action": decision.action,
                    "action_id": decision.action_id,
                    "observed_at": evidence.observed_at,
                    "operation_revision": evidence.operation_revision,
                    "operation_state": evidence.operation_state,
                    "screen_sha256": evidence.screen_sha256,
                    "typed_result_sha256": evidence.typed_result_sha256,
                    "callback_sha256": evidence.callback_sha256,
                    "receipt_sha256": evidence.receipt_sha256,
                    "nudge_count": current.nudge_count,
                    "restart_count": current.restart_count,
                }
                path = self.root / "receipts" / f"{decision.action_id}.json"
                if path.is_file() and not path.is_symlink():
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if existing != receipt:
                        raise ContractError("liveness receipt changed during replay")
                elif path.exists() or path.is_symlink():
                    raise ContractError("liveness receipt is not a regular file")
                else:
                    self._write(path, receipt)
            return decision

    def current_state(self) -> LivenessState | None:
        """Read the content-free state under the same process-safe lock."""

        with self._locked():
            return self._state()

    @staticmethod
    def _callback_submit_receipt(
        binding_sha256: str, state: LivenessState
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "binding_sha256": binding_sha256,
            "nudge_count": state.nudge_count,
            "status": state.callback_submit_status,
        }

    def _write_callback_submit_receipt(
        self,
        binding_sha256: str,
        state: LivenessState,
        *,
        allowed_existing: tuple[dict[str, object], ...] = (),
    ) -> None:
        path = (
            self.root
            / "receipts"
            / f"callback-submit-{binding_sha256}.json"
        )
        target = self._callback_submit_receipt(binding_sha256, state)
        if path.is_file() and not path.is_symlink():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ContractError("callback submit receipt is invalid") from exc
            if existing != target and existing not in allowed_existing:
                raise ContractError("callback submit receipt changed during replay")
            if existing == target:
                return
        elif path.exists() or path.is_symlink():
            raise ContractError("callback submit receipt is not a regular file")
        self._write(path, target)

    def reserve_callback_submit(self, binding_sha256: str) -> bool:
        """Atomically consume the existing nudge ceiling for one generation."""

        _sha(binding_sha256, "callback submit binding", optional=False)
        with self._locked():
            current = self._state()
            if current is None:
                raise ContractError("callback submit reservation has no liveness state")
            if current.callback_submit_binding:
                if current.callback_submit_binding != binding_sha256:
                    return False
                self._write_callback_submit_receipt(binding_sha256, current)
                return False
            if current.nudge_count >= 1:
                return False
            reserved = replace(
                current,
                nudge_count=current.nudge_count + 1,
                callback_submit_binding=binding_sha256,
                callback_submit_status="reserved",
            )
            self._write(self.root / "state.json", to_dict(reserved))
            self._write_callback_submit_receipt(binding_sha256, reserved)
            return True

    def mark_callback_submit_sent(self, binding_sha256: str) -> None:
        """Idempotently mark the exact reserved generation effect sent."""

        _sha(binding_sha256, "callback submit binding", optional=False)
        with self._locked():
            current = self._state()
            if (
                current is None
                or current.callback_submit_binding != binding_sha256
                or current.callback_submit_status not in {"reserved", "sent"}
            ):
                raise ContractError("callback submit reservation identity changed")
            reserved_receipt = self._callback_submit_receipt(
                binding_sha256,
                replace(current, callback_submit_status="reserved"),
            )
            self._write_callback_submit_receipt(
                binding_sha256,
                current,
                allowed_existing=(reserved_receipt,),
            )
            sent = (
                current
                if current.callback_submit_status == "sent"
                else replace(current, callback_submit_status="sent")
            )
            self._write(self.root / "state.json", to_dict(sent))
            self._write_callback_submit_receipt(
                binding_sha256,
                sent,
                allowed_existing=(
                    reserved_receipt,
                ),
            )

    def mark_callback_submit_uncertain(self, binding_sha256: str) -> None:
        """Fail closed when transport may have partially delivered the nudge."""

        _sha(binding_sha256, "callback submit binding", optional=False)
        with self._locked():
            current = self._state()
            if (
                current is None
                or current.callback_submit_binding != binding_sha256
                or current.callback_submit_status not in {"reserved", "uncertain"}
            ):
                raise ContractError("callback submit reservation identity changed")
            reserved_receipt = self._callback_submit_receipt(
                binding_sha256,
                replace(current, callback_submit_status="reserved"),
            )
            self._write_callback_submit_receipt(
                binding_sha256,
                current,
                allowed_existing=(reserved_receipt,),
            )
            uncertain = (
                current
                if current.callback_submit_status == "uncertain"
                else replace(current, callback_submit_status="uncertain")
            )
            self._write(self.root / "state.json", to_dict(uncertain))
            self._write_callback_submit_receipt(
                binding_sha256,
                uncertain,
                allowed_existing=(
                    reserved_receipt,
                ),
            )

    def retire_callback_submit_after_acceptance(
        self,
        binding_sha256: str,
        accepted_callback_receipt_sha256: str,
    ) -> None:
        """Retire one sent generation after its exact callback is durable.

        The shared nudge/restart budgets remain consumed.  The binding alone is
        cleared so a later generation can be observed normally without gaining
        another provider-facing recovery effect.
        """

        _sha(binding_sha256, "callback submit binding", optional=False)
        _sha(
            accepted_callback_receipt_sha256,
            "accepted callback receipt",
            optional=False,
        )
        with self._locked():
            current = self._state()
            if (
                current is None
                or current.callback_submit_binding != binding_sha256
                or current.callback_submit_status != "sent"
            ):
                raise ContractError(
                    "callback submit acceptance identity changed"
                )
            path = (
                self.root
                / "receipts"
                / f"callback-submit-{binding_sha256}.json"
            )
            sent_receipt = self._callback_submit_receipt(
                binding_sha256,
                current,
            )
            accepted_receipt = {
                **sent_receipt,
                "status": "accepted",
                "accepted_callback_receipt_sha256": (
                    accepted_callback_receipt_sha256
                ),
            }
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "callback submit receipt is invalid"
                ) from exc
            if existing not in (sent_receipt, accepted_receipt):
                raise ContractError(
                    "callback submit receipt changed during acceptance"
                )
            if existing != accepted_receipt:
                self._write(path, accepted_receipt)
            retired = replace(
                current,
                callback_submit_binding="",
                callback_submit_status="",
            )
            self._write(self.root / "state.json", to_dict(retired))
