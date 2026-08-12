"""One registered fresh-context provider effect for model-owned artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from model_routing_config import RoutingConfig

from .artifact_repair import (
    ArtifactRepairError,
    ContractArtifactOwner,
    _atomic_write,
    _canonical_bytes,
    _write_once,
)
from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractFamily,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)
from .runtime_session_contracts import RuntimeSessionRequest


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ELIGIBLE_FAMILIES = frozenset(
    {
        ContractFamily.TASK_SUMMARY,
        ContractFamily.PIPELINE_STEP_RESULT,
    }
)


class FreshRepairError(RuntimeError):
    pass


class FreshRepairExhausted(FreshRepairError):
    pass


class FreshRepairEffectUncertain(FreshRepairError):
    pass


class FreshRepairInvalid(FreshRepairError):
    """The single fresh effect completed with a terminal invalid artifact."""


@dataclass(frozen=True)
class ProviderAvailability:
    runtime: str
    status: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if (
            self.runtime not in {"claude", "codex"}
            or self.status not in {"available", "unavailable", "unknown"}
            or not SHA256.fullmatch(self.evidence_sha256)
        ):
            raise ValueError("provider availability evidence is invalid")


def select_fresh_repair_route(
    config: RoutingConfig,
    prior_route: RuntimeRoute,
    *,
    same_provider: bool = False,
    opposite_availability: ProviderAvailability | None = None,
) -> RuntimeRoute:
    """Prefer the opposite provider; same-provider fallback needs proof."""

    opposite = "claude" if prior_route.runtime == "codex" else "codex"
    selected = opposite
    if same_provider:
        if (
            opposite_availability is None
            or opposite_availability.runtime != opposite
            or opposite_availability.status != "unavailable"
        ):
            raise ValueError(
                "same-provider repair requires durable opposite unavailability"
            )
        selected = prior_route.runtime
    elif opposite_availability is not None:
        raise ValueError("opposite availability is valid only for fallback")
    default = config.runtime_default(selected)
    return RuntimeRoute(
        selected,
        default["model"],
        "xhigh",
        "artifact-repair",
        config.fingerprint,
    )


@dataclass(frozen=True)
class FreshRepairStart:
    status: str
    family: str
    repair_id: str
    operation_id: str


@dataclass(frozen=True)
class FreshRepairReceipt:
    status: str
    family: str
    stage: str
    repair_id: str
    input_sha256: str
    output_sha256: str
    route_sha256: str


@dataclass(frozen=True)
class FreshRepairReconciliation:
    """One durable child-to-parent adoption state."""

    status: str
    receipt: FreshRepairReceipt | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pending", "adopted", "accepted"}:
            raise ValueError("fresh repair reconciliation status is invalid")
        if (self.status == "pending") != (self.receipt is None):
            raise ValueError("fresh repair reconciliation receipt is invalid")


class FreshArtifactRepair:
    """Durable fresh-repair authority bound to one ContractArtifactOwner."""

    def __init__(
        self,
        *,
        owner: ContractArtifactOwner,
        reservation: Mapping[str, object],
    ) -> None:
        self.owner = owner
        self.reservation = dict(reservation)
        self.fault_observer: Callable[[str], None] | None = None

    @property
    def repair_id(self) -> str:
        return str(self.reservation["repair_id"])

    @property
    def root(self) -> Path:
        return (
            self.owner.state_root
            / "fresh-artifact-repair"
            / self.owner.template.family.value
            / self.owner.template.attempt_id
        )

    @property
    def scratch(self) -> Path:
        return self.root / "scratch"

    @classmethod
    def reserve(
        cls,
        *,
        owner: ContractArtifactOwner,
        parent: OperationRecord,
        invalid_sha256: str,
        route: RuntimeRoute,
        origin_surface: str,
        family: ContractFamily | None = None,
    ) -> "FreshArtifactRepair":
        selected = family or owner.template.family
        if (
            selected not in ELIGIBLE_FAMILIES
            or selected is not owner.template.family
            or not SHA256.fullmatch(invalid_sha256)
            or route.effort != "xhigh"
            or route.profile != "artifact-repair"
            or parent.spec.owner_id != parent.spec.root_operation_id
            or parent.spec.operation_id != owner.template.attempt_id
        ):
            raise ValueError("fresh artifact repair authority is invalid")
        root = (
            owner.state_root
            / "fresh-artifact-repair"
            / selected.value
            / owner.template.attempt_id
        )
        reservation_path = root / "reservation.json"
        if reservation_path.exists() or reservation_path.is_symlink():
            existing = cls._read_reservation(owner, reservation_path)
            if existing.get("invalid_sha256") != invalid_sha256:
                raise FreshRepairExhausted("fresh repair budget exhausted")
            return cls(owner=owner, reservation=existing)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        repair_id = hashlib.sha256(
            (
                f"{selected.value}:{owner.template.attempt_id}:"
                f"{owner.template.template_sha256}:{invalid_sha256}:fresh-1"
            ).encode()
        ).hexdigest()
        root_id = parent.spec.root_operation_id or parent.spec.operation_id
        operation_suffix = f"-repair-{repair_id[:12]}"
        operation_id = (
            f"{parent.spec.operation_id[:128-len(operation_suffix)]}"
            f"{operation_suffix}"
        )
        reservation = {
            "schema_version": 1,
            "family": selected.value,
            "attempt_id": owner.template.attempt_id,
            "template_sha256": owner.template.template_sha256,
            "invalid_sha256": invalid_sha256,
            "repair_id": repair_id,
            "operation_id": operation_id,
            "owner_id": parent.spec.owner_id,
            "parent_operation_id": parent.spec.operation_id,
            "root_operation_id": root_id,
            "origin_surface": origin_surface,
            "route": {
                "runtime": route.runtime,
                "model": route.model,
                "effort": route.effort,
                "profile": route.profile,
                "routing_sha256": route.routing_sha256,
            },
        }
        _write_once(reservation_path, reservation)
        return cls(owner=owner, reservation=reservation)

    @classmethod
    def _read_reservation(
        cls, owner: ContractArtifactOwner, path: Path
    ) -> dict[str, object]:
        try:
            if path.is_symlink() or not path.is_file():
                raise FreshRepairError("fresh repair reservation is invalid")
            value = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise FreshRepairError(
                "fresh repair reservation is unreadable"
            ) from exc
        route = value.get("route") if isinstance(value, dict) else None
        expected = {
            "schema_version",
            "family",
            "attempt_id",
            "template_sha256",
            "invalid_sha256",
            "repair_id",
            "operation_id",
            "owner_id",
            "parent_operation_id",
            "root_operation_id",
            "origin_surface",
            "route",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != 1
            or value.get("family") != owner.template.family.value
            or value.get("attempt_id") != owner.template.attempt_id
            or value.get("template_sha256")
            != owner.template.template_sha256
            or not SHA256.fullmatch(str(value.get("invalid_sha256") or ""))
            or not SHA256.fullmatch(str(value.get("repair_id") or ""))
            or not isinstance(route, dict)
            or route.get("effort") != "xhigh"
            or route.get("profile") != "artifact-repair"
        ):
            raise FreshRepairError("fresh repair reservation identity changed")
        return value

    @classmethod
    def load(cls, *, owner: ContractArtifactOwner) -> "FreshArtifactRepair":
        root = (
            owner.state_root
            / "fresh-artifact-repair"
            / owner.template.family.value
            / owner.template.attempt_id
        )
        return cls(
            owner=owner,
            reservation=cls._read_reservation(owner, root / "reservation.json"),
        )

    def _route(self) -> RuntimeRoute:
        value = self.reservation["route"]
        assert isinstance(value, dict)
        return RuntimeRoute(
            str(value["runtime"]),
            str(value["model"]),
            str(value["effort"]),
            str(value["profile"]),
            str(value["routing_sha256"]),
        )

    def _prepare_scratch(self) -> RuntimeSessionRequest:
        self.scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scratch.chmod(0o700)
        template_path = self.scratch / "template.json"
        _write_once(template_path, self.owner.template_value)
        operation_id = str(self.reservation["operation_id"])
        run_id = self.repair_id[:32]
        callback_pointer = ".artifact-repair-callback.json"
        prompt = (
            "Repair exactly one registered model-owned JSON artifact. The only "
            "writable target is the callback file named below in this scratch "
            "directory. Do not inspect or edit repository code or any durable "
            "authority. Read template.json, fill only its model-owned fields, "
            "then write one CallbackEnvelope JSON with kind=result and payload "
            f"{{schema_version:1,family:{self.owner.template.family.value!r},"
            f"repair_id:{self.repair_id!r},artifact:<repaired object>}} to "
            f"{callback_pointer}. Bind operation_id={operation_id!r} and "
            f"run_id={run_id!r}; compute payload_sha256 from canonical compact "
            "sorted-key JSON. Stop after writing that file.\n"
        )
        prompt_path = self.scratch / "prompt.md"
        if prompt_path.exists() or prompt_path.is_symlink():
            if prompt_path.is_symlink() or prompt_path.read_text() != prompt:
                raise FreshRepairError("fresh repair prompt changed")
        else:
            prompt_path.write_text(prompt, encoding="utf-8")
            prompt_path.chmod(0o400)
        spec = OperationSpec(
            operation_id=operation_id,
            idempotency_key=self.repair_id,
            kind="artifact-repair",
            owner_id=str(self.reservation["owner_id"]),
            route=self._route(),
            context_manifest="template.json",
            verification_profile="artifact-only",
            parent_operation_id=str(self.reservation["parent_operation_id"]),
            root_operation_id=str(self.reservation["root_operation_id"]),
        )
        return RuntimeSessionRequest(
            spec=spec,
            lane_id=self.repair_id[32:],
            run_id=run_id,
            origin_surface=str(self.reservation["origin_surface"]),
            cwd=self.scratch,
            prompt_pointer="prompt.md",
            callback_pointer=callback_pointer,
            placement="split",
            product_root=None,
            callback_mode="artifact-repair",
            attempt_limit=1,
            model_restart_limit=0,
        )

    def start(self, manager: object) -> FreshRepairStart:
        if (self.root / "failed.json").exists():
            raise FreshRepairInvalid("fresh repair is terminally invalid")
        started_path = self.root / "started.json"
        if started_path.is_file() and not started_path.is_symlink():
            return FreshRepairStart(
                "already-started",
                self.owner.template.family.value,
                self.repair_id,
                str(self.reservation["operation_id"]),
            )
        effect_path = self.root / "effect-reserved.json"
        if effect_path.exists() or effect_path.is_symlink():
            raise FreshRepairEffectUncertain(
                "fresh repair provider effect is unresolved"
            )
        request = self._prepare_scratch()
        _write_once(
            effect_path,
            {
                "schema_version": 1,
                "repair_id": self.repair_id,
                "operation_id": request.spec.operation_id,
                "route_sha256": request.spec.route.routing_sha256,
                "status": "reserved",
            },
        )
        if self.fault_observer is not None:
            self.fault_observer("fresh-effect-reserved")
        result = manager.start(request)
        record = getattr(result, "record", None)
        if (
            record is None
            or record.spec != request.spec
            or record.run_id != request.run_id
            or record.attempt != 1
            or record.model_restart_limit != 0
        ):
            raise FreshRepairEffectUncertain(
                "fresh repair start receipt is ambiguous"
            )
        _write_once(
            started_path,
            {
                "schema_version": 1,
                "repair_id": self.repair_id,
                "operation_id": request.spec.operation_id,
                "run_id": request.run_id,
                "route_sha256": request.spec.route.routing_sha256,
                "status": "started",
            },
        )
        return FreshRepairStart(
            "started",
            self.owner.template.family.value,
            self.repair_id,
            request.spec.operation_id,
        )

    def accept(
        self, validator: Callable[[Mapping[str, object]], object]
    ) -> FreshRepairReceipt:
        callback_path = self.scratch / ".artifact-repair-callback.json"
        failed_path = self.root / "failed.json"
        if failed_path.exists() or failed_path.is_symlink():
            raise FreshRepairInvalid("fresh repair is terminally invalid")
        existing = self._accepted_receipt()
        if existing is not None:
            return existing
        callback_sha256 = ""
        try:
            raw = callback_path.read_bytes()
            callback_sha256 = hashlib.sha256(raw).hexdigest()
            value = json.loads(raw)
            envelope = CallbackEnvelope(
                callback_id=value.get("callback_id", ""),
                operation_id=value.get("operation_id", ""),
                run_id=value.get("run_id", ""),
                kind=value.get("kind", ""),
                payload=value.get("payload", {}),
                payload_sha256=value.get("payload_sha256", ""),
                schema_version=value.get("schema_version", 0),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._record_invalid("callback-invalid", callback_sha256)
            raise FreshRepairInvalid("fresh repair callback is invalid") from exc
        payload = envelope.payload
        artifact = payload.get("artifact")
        if (
            envelope.operation_id != self.reservation["operation_id"]
            or envelope.run_id != self.repair_id[:32]
            or envelope.kind != "result"
            or set(payload) != {"schema_version", "family", "repair_id", "artifact"}
            or payload.get("schema_version") != 1
            or payload.get("family") != self.owner.template.family.value
            or payload.get("repair_id") != self.repair_id
            or not isinstance(artifact, dict)
        ):
            self._record_invalid("callback-identity-invalid", callback_sha256)
            raise FreshRepairInvalid("fresh repair callback identity changed")
        _atomic_write(self.owner.actual_target, artifact)
        authoritative = {
            field: self.owner.template_value[field]
            for field in self.owner.template.code_owned_fields
        }
        try:
            repaired = self.owner.repair(authoritative_fields=authoritative)
            validator(repaired.value)
        except (ArtifactRepairError, TypeError, ValueError) as exc:
            self.owner.restore_template()
            self._record_invalid("authoritative-validation-failed", callback_sha256)
            raise FreshRepairInvalid(
                "fresh repair failed authoritative validation"
            ) from exc
        receipt = FreshRepairReceipt(
            "self-healed",
            self.owner.template.family.value,
            "fresh-context",
            self.repair_id,
            str(self.reservation["invalid_sha256"]),
            repaired.output_sha256,
            self._route().routing_sha256,
        )
        _write_once(self.root / "receipt.json", receipt.__dict__)
        return receipt

    def _accepted_receipt(self) -> FreshRepairReceipt | None:
        path = self.root / "receipt.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            if path.is_symlink() or not path.is_file():
                raise FreshRepairError("fresh repair receipt is invalid")
            value = json.loads(path.read_bytes())
            receipt = FreshRepairReceipt(**value)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FreshRepairError("fresh repair receipt is unreadable") from exc
        if (
            set(value) != set(receipt.__dict__)
            or receipt.status != "self-healed"
            or receipt.family != self.owner.template.family.value
            or receipt.stage != "fresh-context"
            or receipt.repair_id != self.repair_id
            or receipt.input_sha256 != self.reservation["invalid_sha256"]
            or not SHA256.fullmatch(receipt.output_sha256)
            or receipt.route_sha256 != self._route().routing_sha256
        ):
            raise FreshRepairError("fresh repair receipt identity changed")
        return receipt

    def reconcile(
        self,
        child: OperationRecord,
        validator: Callable[[Mapping[str, object]], object],
    ) -> FreshRepairReconciliation:
        """Adopt one child result, then expose the ordinary submit path."""

        receipt = self._accepted_receipt()
        if receipt is not None:
            return FreshRepairReconciliation("accepted", receipt)
        failed_path = self.root / "failed.json"
        if failed_path.exists() or failed_path.is_symlink():
            raise FreshRepairInvalid("fresh repair is terminally invalid")
        if (
            child.spec.operation_id != self.reservation["operation_id"]
            or child.spec.owner_id != self.reservation["owner_id"]
            or child.run_id != self.repair_id[:32]
            or child.spec.parent_operation_id
            != self.reservation["parent_operation_id"]
            or child.spec.root_operation_id != self.reservation["root_operation_id"]
        ):
            raise FreshRepairError("fresh repair child identity changed")
        if child.accepted_callback_kind:
            if child.accepted_callback_kind != "result":
                self._record_invalid("callback-invalid", "")
                raise FreshRepairInvalid("fresh repair callback kind is invalid")
            receipt = self.accept(validator)
            return FreshRepairReconciliation("adopted", receipt)
        if child.state in {
            "attention-required",
            "complete",
            "failed",
            "cancelled",
        }:
            stage = (
                "callback-invalid"
                if child.attention_reason is AttentionReason.CALLBACK_INVALID
                else "provider-exited-without-result"
            )
            self._record_invalid(stage, "")
            raise FreshRepairInvalid(
                "fresh repair child terminated without an accepted result"
            )
        return FreshRepairReconciliation("pending")

    def _record_invalid(self, stage: str, output_sha256: str) -> None:
        self.owner.restore_template()
        _write_once(
            self.root / "failed.json",
            {
                "status": "invalid",
                "family": self.owner.template.family.value,
                "stage": stage,
                "repair_id": self.repair_id,
                "input_sha256": str(self.reservation["invalid_sha256"]),
                "output_sha256": output_sha256,
                "route_sha256": self._route().routing_sha256,
            },
        )


def launch_fresh_repair_for_worker(
    worker: object,
    owner: ContractArtifactOwner,
    invalid_sha256: str,
) -> FreshRepairStart:
    """Use RuntimeSessionManager for one worker-owned registered escalation."""

    from model_routing_config import load_config

    from .runtime_sessions import RuntimeSessionManager

    spec = getattr(worker, "spec", None)
    store = getattr(worker, "store", None)
    if not isinstance(spec, dict) or store is None:
        raise FreshRepairError("fresh repair worker authority is unavailable")
    try:
        parent = store.read(
            str(spec["owner_id"]), owner.template.attempt_id
        )
        vault_root = Path(spec["vault_root"]).expanduser().resolve()
        config = load_config(vault_root)
        manager = RuntimeSessionManager.for_root(
            vault_root, store_root=store.root
        )
        route = select_fresh_repair_route(config, parent.spec.route)
        callback_dir = (
            owner.state_root
            / "fresh-artifact-repair"
            / owner.template.family.value
            / owner.template.attempt_id
            / "scratch"
        )
        callback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = manager.check_route(
            route,
            callback_dir,
            origin_surface=str(spec["origin_surface"]),
        )
        if not report.compatible:
            if report.reason is not AttentionReason.RUNTIME_UNAVAILABLE:
                raise FreshRepairError(
                    "opposite repair route failed without unavailability proof"
                )
            proof = {
                "schema_version": 1,
                "runtime": route.runtime,
                "route_sha256": route.routing_sha256,
                "status": "unavailable",
            }
            proof_sha256 = hashlib.sha256(_canonical_bytes(proof)).hexdigest()
            _write_once(callback_dir.parent / "opposite-unavailable.json", proof)
            route = select_fresh_repair_route(
                config,
                parent.spec.route,
                same_provider=True,
                opposite_availability=ProviderAvailability(
                    report.route.runtime,
                    "unavailable",
                    proof_sha256,
                ),
            )
            fallback = manager.check_route(
                route,
                callback_dir,
                origin_surface=str(spec["origin_surface"]),
            )
            if not fallback.compatible:
                raise FreshRepairError("fresh repair routes are unavailable")
        repair = FreshArtifactRepair.reserve(
            owner=owner,
            parent=parent,
            invalid_sha256=invalid_sha256,
            route=route,
            origin_surface=str(spec["origin_surface"]),
        )
        return manager.start_fresh_artifact_repair(repair)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise FreshRepairError("fresh repair launch authority is invalid") from exc


__all__ = [
    "ELIGIBLE_FAMILIES",
    "FreshArtifactRepair",
    "FreshRepairEffectUncertain",
    "FreshRepairError",
    "FreshRepairExhausted",
    "FreshRepairInvalid",
    "FreshRepairReceipt",
    "FreshRepairStart",
    "ProviderAvailability",
    "select_fresh_repair_route",
    "launch_fresh_repair_for_worker",
]
