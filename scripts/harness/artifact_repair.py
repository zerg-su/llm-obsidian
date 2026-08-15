"""Deterministic repair and one owner for registered model artifacts.

The module deliberately knows only the five concrete RC5 contract families. It
does not implement a schema language, invoke a provider, or validate model
semantics. Authoritative family consumers still make the final acceptance
decision after this module has repaired representation and uniquely derivable
identity fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from .contracts import (
    CanonicalContractTemplate,
    ContractError,
    ContractFamily,
    contract_registry,
)
from .verification_attempt import (
    GIT_OID,
    IDENTIFIER,
    SHA256,
    VerificationAttempt,
    VerificationAttemptError,
)


MAX_ARTIFACT_BYTES, SHA256 = 200_000, re.compile(r"[0-9a-f]{64}\Z")
PUBLICATION_FIELDS = frozenset({
    "schema_version", "type", "actual_target", "template", "publication_sha256",
})
RESERVATION_FIELDS = frozenset({
    "schema_version", "family", "attempt_id", "template_sha256",
    "invalid_sha256", "attempt", "correction_id",
})
VERIFICATION_PUBLIC_DECISIONS = ("retry-mechanism-flake", "stop", "repair-repository-mechanism")


class ArtifactRepairError(RuntimeError):
    """A registered artifact cannot be repaired without ambiguity."""


class CorrectionBudgetExhausted(ArtifactRepairError):
    """The family's registered same-session correction ceiling was consumed."""


class CorrectionNotificationUncertain(ArtifactRepairError):
    """A notification reservation exists without proof of the external effect."""


@dataclass(frozen=True)
class ArtifactObservation:
    state: str
    sha256: str = ""
    stable_reads: int = 0


@dataclass(frozen=True)
class ArtifactRepairResult:
    value: Mapping[str, object]
    input_sha256: str
    output_sha256: str
    changed: bool


@dataclass(frozen=True)
class CorrectionReservation:
    family: ContractFamily
    attempt_id: str
    attempt: int
    invalid_sha256: str
    correction_id: str
    root: Path
    created: bool = False


_KEY_ALIASES: Mapping[ContractFamily, Mapping[str, str]] = {
    ContractFamily.TASK_SUMMARY: {
        "schemaVersion": "schema_version",
        "session_id": "session",
        "outcome": "outcome_disposition",
        "evidence_ids": "outcome_evidence_ids",
        "gaps": "residual_gap_pointers",
    },
    ContractFamily.REVIEW_INPUT: {
        "schemaVersion": "schema_version",
        "review_axis": "axis",
        "verificationIteration": "verification_iteration",
        "issues": "findings",
    },
    ContractFamily.REVIEW_RESOLUTION: {
        "schemaVersion": "schema_version",
        "operation": "operation_id",
        "review_identity": "review_identity_sha256",
        "reviewed_head": "reviewed_head_sha",
        "resolved_head": "resolved_head_sha",
        "finding_resolutions": "resolutions",
    },
    ContractFamily.PIPELINE_STEP_RESULT: {
        "schemaVersion": "schema_version",
        "state": "status",
        "output_digest": "output_sha256",
        "head": "head_sha",
        "decision": "outcome",
    },
    ContractFamily.VERIFICATION_ESCALATION: {
        "schemaVersion": "schema_version",
        "operation": "operation_id",
        "verification_operation": "verification_operation_id",
        "head_sha": "exact_head_sha",
        "attempt_sha256": "failed_attempt_sha256",
        "response": "action",
        "evidence": "evidence_note",
    },
}

_WRAPPERS: Mapping[ContractFamily, frozenset[str]] = {
    ContractFamily.TASK_SUMMARY: frozenset({"summary", "task_summary"}),
    ContractFamily.REVIEW_INPUT: frozenset({"review", "review_input"}),
    ContractFamily.REVIEW_RESOLUTION: frozenset({"resolution", "review_resolution"}),
    ContractFamily.PIPELINE_STEP_RESULT: frozenset({"result", "pipeline_step_result"}),
    ContractFamily.VERIFICATION_ESCALATION: frozenset({"escalation", "verification_escalation"}),
}


def pipeline_step_contract_template(
    request: Mapping[str, object],
) -> CanonicalContractTemplate:
    """Build the one concrete fix/custom result template for a request."""
    attempt_id = request.get("operation_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ArtifactRepairError("pipeline-step template attempt is invalid")
    custom = request.get("workflow_kind") == "custom"
    value: dict[str, object] = {
        "schema_version": 1, "status": "", "output_sha256": "", "head_sha": "",
    }
    if custom:
        value["outcome"] = ""
    try:
        return CanonicalContractTemplate.create(
            ContractFamily.PIPELINE_STEP_RESULT,
            attempt_id=attempt_id,
            target_pointer=".task-pipeline-step-result.json",
            value=value,
            code_owned_fields={"schema_version", "output_sha256", "head_sha"},
            model_owned_fields={"status", "outcome"} if custom else {"status"},
        )
    except ContractError as exc:
        raise ArtifactRepairError("pipeline-step template is invalid") from exc


def review_input_contract_template(
    meta: Mapping[str, object],
) -> CanonicalContractTemplate:
    attempt_id = meta.get("operation_id")
    axis = meta.get("axis")
    iteration = meta.get("verification_iteration")
    if (
        not isinstance(attempt_id, str)
        or not attempt_id
        or not isinstance(axis, str)
        or not axis
        or type(iteration) is not int
        or iteration < 0
    ):
        raise ArtifactRepairError("review-input template authority is invalid")
    try:
        return CanonicalContractTemplate.create(
            ContractFamily.REVIEW_INPUT,
            attempt_id=attempt_id,
            target_pointer=".review-input.json",
            value={
                "schema_version": 1,
                "axis": axis,
                "verdict": "",
                "verification_iteration": iteration,
                "findings": [],
            },
            code_owned_fields={
                "schema_version",
                "axis",
                "verification_iteration",
            },
            model_owned_fields={"verdict", "findings"},
        )
    except ContractError as exc:
        raise ArtifactRepairError("review-input template is invalid") from exc


def review_resolution_contract_template(
    *,
    attempt_id: str,
    value: Mapping[str, object],
) -> CanonicalContractTemplate:
    try:
        return CanonicalContractTemplate.create(
            ContractFamily.REVIEW_RESOLUTION,
            attempt_id=attempt_id,
            target_pointer=".task-review-resolution.json",
            value=value,
            code_owned_fields={
                "schema_version",
                "operation_id",
                "review_identity_sha256",
                "reviewed_head_sha",
            },
            model_owned_fields={"resolved_head_sha", "resolutions"},
        )
    except ContractError as exc:
        raise ArtifactRepairError("review-resolution template is invalid") from exc


def verification_escalation_contract_template(
    *, attempt_id: str, value: Mapping[str, object]
) -> CanonicalContractTemplate:
    try:
        return CanonicalContractTemplate.create(
            ContractFamily.VERIFICATION_ESCALATION,
            attempt_id=attempt_id,
            target_pointer=".task-verification-contract.json",
            value=value,
            code_owned_fields={
                "schema_version",
                "kind",
                "category",
                "operation_id",
                "verification_operation_id",
                "exact_head_sha",
                "failed_attempt_sha256",
                "decision",
            },
            model_owned_fields={"action", "evidence_note"},
        )
    except ContractError as exc:
        raise ArtifactRepairError(
            "verification-escalation template is invalid"
        ) from exc


def build_verification_escalation(
    failed_attempt: VerificationAttempt, verification_operation_id: str
) -> dict[str, object]:
    """Construct the exact code-owned verification escalation identity."""

    if (
        failed_attempt.attempt_index != 0
        or not isinstance(verification_operation_id, str)
        or not IDENTIFIER.fullmatch(verification_operation_id)
    ):
        raise VerificationAttemptError("verification escalation identity is invalid")
    return {
        "schema_version": 1,
        "kind": "same-head-verification-retry",
        "category": "mechanism-failure",
        "operation_id": failed_attempt.parent_operation_id,
        "verification_operation_id": verification_operation_id,
        "exact_head_sha": failed_attempt.exact_head_sha,
        "failed_attempt_sha256": failed_attempt.sha256,
        "decision": "request-attempt-1",
        "action": "",
        "evidence_note": "",
    }


def resolve_verification_escalation(
    escalation: object, *, decision: str, evidence_note: str
) -> dict[str, object]:
    """Translate the exact public decision into the one private typed action."""

    expected = {
        "schema_version", "kind", "category", "operation_id",
        "verification_operation_id", "exact_head_sha", "failed_attempt_sha256",
        "decision", "action", "evidence_note",
    }
    note = " ".join(str(evidence_note).split()).strip()
    if (
        not isinstance(escalation, dict)
        or set(escalation) != expected
        or escalation.get("schema_version") != 1
        or escalation.get("kind") != "same-head-verification-retry"
        or escalation.get("category") != "mechanism-failure"
        or escalation.get("decision") != "request-attempt-1"
        or escalation.get("action") != ""
        or escalation.get("evidence_note") != ""
        or not IDENTIFIER.fullmatch(str(escalation.get("operation_id") or ""))
        or not IDENTIFIER.fullmatch(str(escalation.get("verification_operation_id") or ""))
        or not GIT_OID.fullmatch(str(escalation.get("exact_head_sha") or ""))
        or not SHA256.fullmatch(str(escalation.get("failed_attempt_sha256") or ""))
        or decision not in VERIFICATION_PUBLIC_DECISIONS
        or not note
        or len(note) > 1000
    ):
        raise VerificationAttemptError("verification escalation resolution is invalid")
    return {
        **escalation,
        "decision": "authorize-attempt-1" if decision == "retry-mechanism-flake" else "do-not-authorize",
        "action": decision,
        "evidence_note": note,
    }


def verification_resolution_authorizes(
    resolution: object,
    failed_attempt: VerificationAttempt,
    verification_operation_id: str,
) -> bool:
    """Validate a typed retry grant against code-owned attempt authority."""

    if not isinstance(resolution, dict):
        return False
    try:
        expected = resolve_verification_escalation(
            build_verification_escalation(failed_attempt, verification_operation_id),
            decision="retry-mechanism-flake",
            evidence_note=str(resolution.get("evidence_note") or ""),
        )
    except VerificationAttemptError:
        return False
    return resolution == expected


def publish_verification_escalation(
    *, state_root: Path, worktree: Path, failed_attempt: VerificationAttempt,
    verification_operation_id: str,
) -> "ContractArtifactOwner":
    template = verification_escalation_contract_template(
        attempt_id=verification_operation_id,
        value=build_verification_escalation(
            failed_attempt, verification_operation_id
        ),
    )
    owner = ContractArtifactOwner.publish(
        state_root=state_root,
        worktree=worktree,
        template=template,
        actual_target=worktree / ".task-verification-contract.json",
    )
    if owner.publication_created or not owner.actual_target.exists():
        owner.restore_template()
    return owner


def publish_pipeline_step_contract(
    *, state_root: Path, worktree: Path, request: Mapping[str, object],
) -> tuple[dict[str, object], "ContractArtifactOwner"]:
    result_pointer = request.get("result_pointer")
    if not isinstance(result_pointer, str) or not result_pointer:
        raise ArtifactRepairError("pipeline-step result pointer is invalid")
    enriched, template = dict(request), pipeline_step_contract_template(request)
    owner = ContractArtifactOwner.publish(
        state_root=state_root, worktree=worktree, template=template, actual_target=worktree / result_pointer,
    )
    enriched["contract_template_pointer"] = str(owner.sidecar_path)
    request_path = owner.worktree / ".task-pipeline-step-request.json"
    if request_path.is_symlink(): raise ArtifactRepairError("pipeline-step request cannot be a symlink")
    try:
        if request_path.is_file() and request_path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("pipeline-step request exceeds the artifact limit")
        current = json.loads(request_path.read_text(encoding="utf-8")) if request_path.is_file() else {}
    except (OSError, ValueError) as exc: raise ArtifactRepairError("pipeline-step request is invalid") from exc
    if not isinstance(current, dict): raise ArtifactRepairError("pipeline-step request is invalid")
    if current.get("operation_id") == enriched["operation_id"] and current != enriched:
        raise ArtifactRepairError("pipeline-step request changed during replay")
    if owner.publication_created or not owner.actual_target.exists(): owner.restore_template()
    _atomic_write(request_path, enriched)
    return enriched, owner


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_once(path: Path, value: object, *, mode: int = 0o400) -> bool:
    encoded = _canonical_bytes(value)
    if path.is_symlink():
        raise ArtifactRepairError("immutable contract artifact cannot be a symlink")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise ArtifactRepairError(
                "immutable contract artifact is unreadable"
            ) from exc
        if current != encoded:
            raise ArtifactRepairError("immutable contract artifact changed")
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return True


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _relative_target(worktree: Path, target: Path) -> str:
    root = worktree.expanduser().resolve()
    lexical = target.expanduser()
    if not lexical.is_absolute():
        lexical = root / lexical
    # Normalize the already-owned parent (not the artifact leaf) so macOS's
    # lexical /var -> /private/var alias does not look like an escape while a
    # symlink artifact remains observable by the caller.
    lexical = lexical.parent.resolve(strict=False) / lexical.name
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ArtifactRepairError("contract target escapes its worktree") from exc
    if not relative.parts or ".." in relative.parts or relative == Path("."):
        raise ArtifactRepairError("contract target is invalid")
    current = root
    for component in relative.parts[:-1]:
        current /= component
        if current.is_symlink():
            raise ArtifactRepairError("contract target parent cannot be a symlink")
    return PurePosixPath(*relative.parts).as_posix()


def _template_from_dict(value: object) -> CanonicalContractTemplate:
    if not isinstance(value, dict):
        raise ArtifactRepairError("contract template sidecar is invalid")
    fields = {
        "schema_version",
        "family",
        "attempt_id",
        "target_pointer",
        "code_owned_fields",
        "model_owned_fields",
        "template",
        "template_sha256",
    }
    if set(value) != fields or value.get("schema_version") != 1:
        raise ArtifactRepairError("contract template sidecar is invalid")
    try:
        template = CanonicalContractTemplate.create(
            str(value.get("family") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            target_pointer=str(value.get("target_pointer") or ""),
            value=value.get("template") if isinstance(value.get("template"), dict) else {},
            code_owned_fields=set(value.get("code_owned_fields") or ()),
            model_owned_fields=set(value.get("model_owned_fields") or ()),
        )
    except (ContractError, TypeError, ValueError) as exc:
        raise ArtifactRepairError("contract template sidecar is invalid") from exc
    if not template.matches(value):
        raise ArtifactRepairError("contract template digest changed")
    return template


def _decode_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ArtifactRepairError("model artifact is not UTF-8 JSON") from exc
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[0] not in {"```", "```json", "```JSON"} or lines[-1] != "```":
            raise ArtifactRepairError("model artifact fence is ambiguous")
        text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    candidate = text.rstrip()
    if candidate.endswith(","):
        candidate = candidate[:-1].rstrip()
    # Repair exactly one missing terminal container delimiter. Counting is
    # string-aware so braces inside model-authored prose never affect repair.
    stack: list[str] = []
    quoted = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for character in candidate:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack or stack[-1] != pairs[character]:
                raise ArtifactRepairError("model artifact syntax is ambiguous")
            stack.pop()
    if quoted or len(stack) != 1:
        raise ArtifactRepairError("model artifact syntax is not uniquely repairable")
    candidate += "}" if stack[-1] == "{" else "]"
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ArtifactRepairError("model artifact syntax is not uniquely repairable") from exc


def _normalize_value_aliases(
    family: ContractFamily, value: dict[str, object]
) -> None:
    if family is ContractFamily.TASK_SUMMARY:
        aliases = {
            "partial": "partially-achieved",
            "partially_achieved": "partially-achieved",
            "complete": "achieved",
            "failed": "not-achieved",
        }
        current = value.get("outcome_disposition")
        if isinstance(current, str) and current in aliases:
            value["outcome_disposition"] = aliases[current]
    elif family is ContractFamily.REVIEW_INPUT:
        aliases = {"approved": "approve", "changes_requested": "changes-requested"}
        current = value.get("verdict")
        if isinstance(current, str) and current in aliases:
            value["verdict"] = aliases[current]
    elif family is ContractFamily.PIPELINE_STEP_RESULT:
        aliases = {"completed": "complete", "done": "complete"}
        current = value.get("status")
        if isinstance(current, str) and current in aliases:
            value["status"] = aliases[current]
    elif family is ContractFamily.VERIFICATION_ESCALATION:
        aliases = {"retry": "retry-mechanism-flake", "same-head-retry": "retry-mechanism-flake"}
        current = value.get("action")
        if isinstance(current, str) and current in aliases:
            value["action"] = aliases[current]


def observe_stable_artifact(
    path: Path,
    *,
    previous_sha256: str,
    stable_reads: int,
    limit: int = MAX_ARTIFACT_BYTES,
) -> ArtifactObservation:
    """Classify a bounded regular artifact without following a symlink leaf."""

    try:
        if path.is_symlink():
            return ArtifactObservation("symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return ArtifactObservation("malformed")
        if info.st_size <= 0:
            return ArtifactObservation("unstable")
        if info.st_size > limit:
            return ArtifactObservation("oversize")
        raw = path.read_bytes()
    except FileNotFoundError:
        return ArtifactObservation("missing")
    except OSError:
        return ArtifactObservation("malformed")
    digest = _sha256(raw)
    reads = stable_reads + 1 if digest == previous_sha256 else 1
    return ArtifactObservation("stable" if reads >= 2 else "unstable", digest, reads)


class ContractArtifactOwner:
    """Bind publication, deterministic repair, and correction budget identity."""

    def __init__(
        self,
        *,
        state_root: Path,
        worktree: Path,
        template: CanonicalContractTemplate,
        actual_target: Path,
        sidecar_path: Path,
        publication_created: bool,
    ) -> None:
        self.state_root = state_root
        self.worktree = worktree
        self.template = template
        self.actual_target = actual_target
        self.sidecar_path = sidecar_path
        self.publication_created = publication_created

    @classmethod
    def publish(
        cls,
        *,
        state_root: Path,
        worktree: Path,
        template: CanonicalContractTemplate,
        actual_target: Path,
    ) -> "ContractArtifactOwner":
        root = state_root.expanduser().resolve()
        tree = worktree.expanduser().resolve()
        if root.is_symlink() or tree.is_symlink() or not tree.is_dir():
            raise ArtifactRepairError("contract publication root is invalid")
        relative = _relative_target(tree, actual_target)
        directory = root / "contract-templates" / template.family.value
        if directory.is_symlink():
            raise ArtifactRepairError("contract template directory cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / f"{template.attempt_id}.json"
        unsigned = {
            "schema_version": 1,
            "type": "contract-template-publication",
            "actual_target": relative,
            "template": template.as_dict(),
        }
        value = {**unsigned, "publication_sha256": _sha256(_canonical_bytes(unsigned))}
        created = _write_once(path, value)
        return cls(
            state_root=root,
            worktree=tree,
            template=template,
            actual_target=tree.joinpath(*PurePosixPath(relative).parts),
            sidecar_path=path,
            publication_created=created,
        )

    @classmethod
    def load(
        cls,
        *,
        state_root: Path,
        worktree: Path,
        family: ContractFamily | str,
        attempt_id: str,
    ) -> "ContractArtifactOwner":
        try:
            selected = ContractFamily(family)
        except (TypeError, ValueError) as exc:
            raise ArtifactRepairError("contract family is not registered") from exc
        root = state_root.expanduser().resolve()
        tree = worktree.expanduser().resolve()
        path = root / "contract-templates" / selected.value / f"{attempt_id}.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ArtifactRepairError("contract template publication is invalid")
            value = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactRepairError("contract template publication is unreadable") from exc
        if not isinstance(value, dict) or set(value) != PUBLICATION_FIELDS:
            raise ArtifactRepairError("contract template publication is invalid")
        unsigned = dict(value)
        recorded_sha256 = unsigned.pop("publication_sha256", None)
        if (
            value.get("schema_version") != 1
            or value.get("type") != "contract-template-publication"
            or recorded_sha256 != _sha256(_canonical_bytes(unsigned))
        ):
            raise ArtifactRepairError("contract template publication digest changed")
        template = _template_from_dict(value.get("template"))
        if template.family is not selected or template.attempt_id != attempt_id:
            raise ArtifactRepairError("contract template publication identity changed")
        relative = value.get("actual_target")
        if not isinstance(relative, str):
            raise ArtifactRepairError("contract target binding is invalid")
        target = tree.joinpath(*PurePosixPath(relative).parts)
        if _relative_target(tree, target) != relative:
            raise ArtifactRepairError("contract target binding changed")
        return cls(
            state_root=root,
            worktree=tree,
            template=template,
            actual_target=target,
            sidecar_path=path,
            publication_created=False,
        )

    @property
    def template_value(self) -> dict[str, object]:
        return dict(self.template.as_dict()["template"])

    @property
    def template_artifact_sha256(self) -> str:
        return _sha256(_canonical_bytes(self.template_value))

    def awaiting_semantic_edit(self, artifact_sha256: str) -> bool:
        """Return true only for a sent correction's untouched restored template."""

        if artifact_sha256 != self.template_artifact_sha256:
            return False
        return any(
            (reservation.root / "notification-sent.json").is_file()
            and not (reservation.root / "notification-sent.json").is_symlink()
            for reservation in self._reservations()
        )

    @property
    def has_sent_correction(self) -> bool:
        return any(
            (reservation.root / "notification-sent.json").is_file()
            and not (reservation.root / "notification-sent.json").is_symlink()
            for reservation in self._reservations()
        )

    @property
    def has_uncertain_correction(self) -> bool:
        return any(
            (reservation.root / "notification-reserved.json").is_file()
            and not (reservation.root / "notification-sent.json").is_file()
            for reservation in self._reservations()
        )

    def restore_template(self) -> None:
        if self.actual_target.is_symlink():
            raise ArtifactRepairError("model artifact cannot be a symlink")
        _relative_target(self.worktree, self.actual_target)
        _atomic_write(self.actual_target, self.template_value)

    def repair(
        self, *, authoritative_fields: Mapping[str, object]
    ) -> ArtifactRepairResult:
        if self.actual_target.is_symlink():
            raise ArtifactRepairError("model artifact cannot be a symlink")
        _relative_target(self.worktree, self.actual_target)
        try:
            info = self.actual_target.stat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARTIFACT_BYTES:
                raise ArtifactRepairError("model artifact is not a bounded regular file")
            raw = self.actual_target.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactRepairError("model artifact is missing") from exc
        except OSError as exc:
            raise ArtifactRepairError("model artifact is unreadable") from exc
        parsed = _decode_json(raw)
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            parsed = parsed[0]
        if isinstance(parsed, dict) and len(parsed) == 1:
            only_key = next(iter(parsed))
            if only_key in _WRAPPERS[self.template.family] and isinstance(parsed[only_key], dict):
                parsed = parsed[only_key]
        if not isinstance(parsed, dict):
            raise ArtifactRepairError("model artifact shape is not uniquely repairable")
        current = dict(parsed)
        for alias, canonical in _KEY_ALIASES[self.template.family].items():
            if alias not in current:
                continue
            if canonical in current:
                raise ArtifactRepairError("model artifact alias is ambiguous")
            current[canonical] = current.pop(alias)
        _normalize_value_aliases(self.template.family, current)
        unknown_authority = set(authoritative_fields) - self.template.code_owned_fields
        if unknown_authority:
            raise ArtifactRepairError("artifact authority override is not registered")
        template = self.template_value
        repaired: dict[str, object] = {}
        for field in template:
            if field in self.template.code_owned_fields:
                repaired[field] = authoritative_fields.get(field, template[field])
            else:
                if field not in current:
                    raise ArtifactRepairError(
                        "model-owned artifact field is missing"
                    )
                repaired[field] = current[field]
        encoded = _canonical_bytes(repaired)
        changed = encoded != raw
        if changed:
            _atomic_write(self.actual_target, repaired)
        return ArtifactRepairResult(
            value=repaired,
            input_sha256=_sha256(raw),
            output_sha256=_sha256(encoded),
            changed=changed,
        )

    @property
    def _correction_root(self) -> Path:
        return (
            self.state_root
            / "contract-corrections"
            / self.template.family.value
            / self.template.attempt_id
        )

    def _reservations(self) -> list[CorrectionReservation]:
        root = self._correction_root
        if root.is_symlink() or not root.exists():
            if root.is_symlink():
                raise ArtifactRepairError("correction ledger cannot be a symlink")
            return []
        paths = sorted(root.glob("attempt-*"))
        if [path.name for path in paths] != [
            f"attempt-{index:02d}" for index in range(1, len(paths) + 1)
        ]:
            raise ArtifactRepairError("correction ledger is not contiguous")
        values: list[CorrectionReservation] = []
        for index, directory in enumerate(paths, start=1):
            reservation_path = directory / "reservation.json"
            try:
                if directory.is_symlink() or reservation_path.is_symlink():
                    raise ArtifactRepairError("correction reservation is invalid")
                raw = json.loads(reservation_path.read_bytes())
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactRepairError("correction reservation is unreadable") from exc
            if (
                not isinstance(raw, dict)
                or set(raw) != RESERVATION_FIELDS
                or raw.get("schema_version") != 1
                or raw.get("family") != self.template.family.value
                or raw.get("attempt_id") != self.template.attempt_id
                or raw.get("template_sha256") != self.template.template_sha256
                or raw.get("attempt") != index
                or not SHA256.fullmatch(str(raw.get("invalid_sha256") or ""))
                or not SHA256.fullmatch(str(raw.get("correction_id") or ""))
            ):
                raise ArtifactRepairError("correction reservation identity changed")
            values.append(
                CorrectionReservation(
                    self.template.family,
                    self.template.attempt_id,
                    index,
                    str(raw["invalid_sha256"]),
                    str(raw["correction_id"]),
                    directory,
                    False,
                )
            )
        return values

    def reserve_correction(self, invalid_sha256: str) -> CorrectionReservation:
        if not SHA256.fullmatch(invalid_sha256):
            raise ArtifactRepairError("invalid artifact digest is not a sha256")
        root = self._correction_root
        if root.is_symlink():
            raise ArtifactRepairError("correction ledger cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        reservations = self._reservations()
        matching = [row for row in reservations if row.invalid_sha256 == invalid_sha256]
        if matching:
            if len(matching) != 1:
                raise ArtifactRepairError("correction reservation is duplicated")
            return matching[0]
        ceiling = contract_registry()[self.template.family].same_session_corrections
        if len(reservations) >= ceiling:
            raise CorrectionBudgetExhausted(
                f"{self.template.family.value} correction budget exhausted"
            )
        attempt = len(reservations) + 1
        correction_id = _sha256(
            (
                f"{self.template.family.value}:{self.template.attempt_id}:"
                f"{self.template.template_sha256}:{invalid_sha256}:{attempt}"
            ).encode()
        )
        directory = root / f"attempt-{attempt:02d}"
        if directory.is_symlink():
            raise ArtifactRepairError("correction attempt directory is invalid")
        directory.mkdir(mode=0o700)
        reservation = {
            "schema_version": 1,
            "family": self.template.family.value,
            "attempt_id": self.template.attempt_id,
            "template_sha256": self.template.template_sha256,
            "invalid_sha256": invalid_sha256,
            "attempt": attempt,
            "correction_id": correction_id,
        }
        _write_once(directory / "reservation.json", reservation)
        return CorrectionReservation(
            self.template.family,
            self.template.attempt_id,
            attempt,
            invalid_sha256,
            correction_id,
            directory,
            True,
        )

    def _notification_event(
        self, reservation: CorrectionReservation, status: str
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "correction_id": reservation.correction_id,
            "status": status,
        }

    def correction_notification_state(
        self, reservation: CorrectionReservation
    ) -> str:
        authoritative = [
            row for row in self._reservations()
            if row.family is reservation.family
            and row.attempt_id == reservation.attempt_id
            and row.attempt == reservation.attempt
            and row.invalid_sha256 == reservation.invalid_sha256
            and row.correction_id == reservation.correction_id
            and row.root == reservation.root
        ]
        if (
            reservation.family is not self.template.family
            or reservation.attempt_id != self.template.attempt_id
            or len(authoritative) != 1
        ):
            raise ArtifactRepairError("correction reservation is not authoritative")
        reserved = reservation.root / "notification-reserved.json"
        sent = reservation.root / "notification-sent.json"
        states = ((reserved, "reserved"), (sent, "sent"))
        present: set[str] = set()
        for path, status in states:
            if path.exists() or path.is_symlink():
                try:
                    if path.is_symlink() or not path.is_file():
                        raise ArtifactRepairError("correction notification receipt is invalid")
                    value = json.loads(path.read_bytes())
                except (OSError, json.JSONDecodeError) as exc:
                    raise ArtifactRepairError(
                        "correction notification receipt is unreadable"
                    ) from exc
                if value != self._notification_event(reservation, status):
                    raise ArtifactRepairError("correction notification receipt changed")
                present.add(status)
        if "sent" in present and "reserved" not in present:
            raise ArtifactRepairError("correction notification receipt order is invalid")
        return "sent" if "sent" in present else "reserved" if present else "unreserved"

    def deliver_correction(
        self,
        reservation: CorrectionReservation,
        message: str,
        sender: Callable[[str], object],
        *,
        fault_observer: Callable[[str], object] | None = None,
    ) -> bool:
        state = self.correction_notification_state(reservation)
        reserved = reservation.root / "notification-reserved.json"
        sent = reservation.root / "notification-sent.json"
        expected_sent = self._notification_event(reservation, "sent")
        if state == "sent":
            return False
        if state == "reserved":
            raise CorrectionNotificationUncertain(
                "correction notification effect is uncertain"
            )
        _write_once(
            reserved,
            self._notification_event(reservation, "reserved"),
        )
        if fault_observer is not None:
            fault_observer("notification-reserved")
        try:
            sender(message)
        except Exception as exc:
            raise CorrectionNotificationUncertain(
                "correction notification effect is uncertain"
            ) from exc
        _write_once(sent, expected_sent)
        return True


__all__ = (
    "ArtifactObservation", "ArtifactRepairError", "ArtifactRepairResult",
    "VERIFICATION_PUBLIC_DECISIONS",
    "ContractArtifactOwner",
    "CorrectionBudgetExhausted",
    "CorrectionNotificationUncertain",
    "CorrectionReservation",
    "build_verification_escalation",
    "observe_stable_artifact",
    "pipeline_step_contract_template",
    "publish_pipeline_step_contract",
    "publish_verification_escalation",
    "review_input_contract_template",
    "review_resolution_contract_template",
    "resolve_verification_escalation",
    "verification_escalation_contract_template",
    "verification_resolution_authorizes",
)
