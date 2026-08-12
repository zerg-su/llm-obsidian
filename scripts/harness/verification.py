"""HEAD-bound verification profiles and bounded evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Mapping, Sequence

from .contracts import (
    ContractError,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    VerificationEvidence,
    to_dict,
)
from .store import OperationStore, StoreError
from .verification_attempt import (
    VerificationAttempt,
    VerificationAttemptError,
    pipeline_verify_effect_id,
    pipeline_verify_identity,
)


class VerificationError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class VerificationProfile:
    name: str
    commands: tuple[str, ...]
    sha256: str


def compose_commands(
    profile: VerificationProfile, extra_commands: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Append bounded model checks without replacing the configured gate."""

    if len(extra_commands) > 16 or not all(
        isinstance(command, str)
        and command.strip()
        and "\0" not in command
        for command in extra_commands
    ):
        raise VerificationError("invalid appended verification commands")
    return profile.commands + tuple(command.strip() for command in extra_commands)


def load_profiles(path: Path | str) -> dict[str, VerificationProfile]:
    path = Path(path)
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("profiles"), dict):
        raise VerificationError("unsupported verification profile schema")
    result: dict[str, VerificationProfile] = {}
    for name, item in value["profiles"].items():
        commands = item.get("commands") if isinstance(item, dict) else None
        if not isinstance(commands, list) or not commands or not all(isinstance(row, str) and row for row in commands):
            raise VerificationError(f"invalid verification profile: {name}")
        canonical = json.dumps(commands, separators=(",", ":")).encode()
        result[name] = VerificationProfile(name, tuple(commands), hashlib.sha256(canonical).hexdigest())
    return result


def run_profile(
    profile: VerificationProfile,
    *,
    root: Path,
    evidence_dir: Path,
    runner: Runner = subprocess.run,
    max_output_bytes: int = 131_072,
    extra_commands: tuple[str, ...] = (),
    pointer_root: Path | None = None,
) -> list[VerificationEvidence]:
    pointer_root = (pointer_root or root).resolve()
    head_result = runner(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    if head_result.returncode:
        raise VerificationError("cannot resolve verification HEAD")
    head = head_result.stdout.strip()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence: list[VerificationEvidence] = []
    commands = compose_commands(profile, extra_commands)
    for index, command in enumerate(commands):
        command_id = f"{profile.name}-{index + 1}"
        started = time.time()
        result = runner(shlex.split(command), cwd=root, text=True, capture_output=True, check=False)
        finished = time.time()
        bounded = (result.stdout + result.stderr).encode()[:max_output_bytes]
        pointer = evidence_dir / f"{command_id}.log"
        pointer.write_bytes(bounded)
        item = VerificationEvidence(
            profile.name,
            profile.sha256,
            head,
            command_id,
            ".",
            result.returncode,
            str(started),
            str(finished),
            pointer.relative_to(pointer_root).as_posix(),
            hashlib.sha256(bounded).hexdigest(),
            len(bounded),
            2,
        )
        evidence.append(item)
        if result.returncode:
            break
    summary = evidence_dir / f"{profile.name}.json"
    summary.write_text(json.dumps([to_dict(row) for row in evidence], sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def valid_for(evidence: VerificationEvidence, *, head: str, profile: VerificationProfile) -> bool:
    return (
        evidence.schema_version == 2
        and bool(evidence.output_sha256)
        and evidence.exit_code == 0
        and evidence.head_sha == head
        and evidence.profile == profile.name
        and evidence.profile_sha256 == profile.sha256
    )


def output_binding_valid(
    evidence: VerificationEvidence, *, pointer_root: Path
) -> bool:
    """Validate that v2 evidence still names the exact persisted output bytes."""

    if evidence.schema_version != 2 or not evidence.output_sha256:
        return False
    root = pointer_root.resolve()
    raw = root / evidence.output_pointer
    candidate = raw.resolve()
    if root not in candidate.parents or raw.is_symlink():
        return False
    current = root
    for part in Path(evidence.output_pointer).parts[:-1]:
        current /= part
        if current.is_symlink():
            return False
    try:
        payload = candidate.read_bytes()
    except OSError:
        return False
    return (
        candidate.is_file()
        and len(payload) == evidence.output_bytes
        and hashlib.sha256(payload).hexdigest() == evidence.output_sha256
    )


RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "parent_operation_id",
        "lane_id",
        "run_id",
        "definition_sha256",
        "step_id",
        "head_sha",
        "input_sha256",
        "profile",
        "profile_sha256",
        "effect_id",
        "status",
        "evidence",
        "verification_attempt",
        "verification_attempt_sha256",
    }
)
MAX_AUTHORITY_EVIDENCE = 100


class VerificationAuthorityError(ValueError):
    """Durable schema-v2 verification authority is absent or conflicting."""


def _authority_path_is_safe(path: Path) -> bool:
    original = path.expanduser()
    if not original.is_absolute() or ".." in original.parts:
        return False
    current = Path(original.anchor)
    try:
        for part in original.parts[1:]:
            current /= part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _authority_attempt(value: Mapping[str, object]) -> VerificationAttempt:
    if value.get("schema_version") != 2:
        raise VerificationAuthorityError(
            "verification authority requires schema version 2"
        )
    try:
        attempt = VerificationAttempt.from_dict(
            value.get("verification_attempt")
        )
    except VerificationAttemptError as exc:
        raise VerificationAuthorityError(
            "verification attempt identity is invalid"
        ) from exc
    if value.get("verification_attempt_sha256") != attempt.sha256:
        raise VerificationAuthorityError(
            "verification attempt digest is invalid"
        )
    return attempt


def _authority_evidence(
    value: Mapping[str, object],
    *,
    runtime_root: Path,
    profile: str,
    profile_sha256: str,
    head_sha: str,
    status: str,
    expected_command_ids: Sequence[str] | None,
) -> tuple[VerificationEvidence, ...]:
    rows = value.get("evidence")
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) > MAX_AUTHORITY_EVIDENCE
    ):
        raise VerificationAuthorityError("verification evidence is invalid")
    evidence: list[VerificationEvidence] = []
    for row in rows:
        try:
            typed = (
                VerificationEvidence(**row)
                if isinstance(row, dict)
                else None
            )
        except (ContractError, TypeError):
            typed = None
        if (
            typed is None
            or typed.schema_version != 2
            or typed.profile != profile
            or typed.profile_sha256 != profile_sha256
            or typed.head_sha != head_sha
            or not _authority_path_is_safe(
                runtime_root / typed.output_pointer
            )
            or not output_binding_valid(typed, pointer_root=runtime_root)
        ):
            raise VerificationAuthorityError(
                "verification evidence is invalid"
            )
        evidence.append(typed)
    command_ids = tuple(item.command_id for item in evidence)
    exit_codes = tuple(item.exit_code for item in evidence)
    if (
        len(set(command_ids)) != len(command_ids)
        or (status == "complete" and not all(code == 0 for code in exit_codes))
        or (status == "failed" and exit_codes[-1] == 0)
        or (
            expected_command_ids is not None
            and (
                command_ids != tuple(expected_command_ids)[: len(command_ids)]
                or (
                    status == "complete"
                    and len(command_ids) != len(expected_command_ids)
                )
            )
        )
    ):
        raise VerificationAuthorityError(
            "verification outcome authority is invalid"
        )
    return tuple(evidence)


@dataclass(frozen=True)
class VerificationAuthority:
    """Validated immutable value behind every verification receipt caller."""

    SCHEMA_VERSION = 2

    parent: OperationRecord
    child: OperationRecord
    definition_sha256: str
    input_sha256: str
    head_sha: str
    profile: str
    profile_sha256: str
    effect_id: str
    status: str
    attempt: VerificationAttempt
    evidence: tuple[VerificationEvidence, ...]

    @property
    def operation_id(self) -> str:
        return self.child.spec.operation_id

    @property
    def lane_id(self) -> str:
        return self.child.lane_id

    @property
    def run_id(self) -> str:
        return self.child.run_id

    @property
    def command_ids(self) -> tuple[str, ...]:
        return tuple(item.command_id for item in self.evidence)

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "parent_operation_id": self.parent.spec.operation_id,
            "lane_id": self.lane_id,
            "run_id": self.run_id,
            "definition_sha256": self.definition_sha256,
            "step_id": "verify",
            "head_sha": self.head_sha,
            "input_sha256": self.input_sha256,
            "profile": self.profile,
            "profile_sha256": self.profile_sha256,
            "effect_id": self.effect_id,
            "status": self.status,
            "evidence": [to_dict(item) for item in self.evidence],
            "verification_attempt": self.attempt.as_dict(),
            "verification_attempt_sha256": self.attempt.sha256,
        }

    @classmethod
    def attempt_from(cls, value: object) -> VerificationAttempt:
        if not isinstance(value, Mapping):
            raise VerificationAuthorityError(
                "verification authority requires schema version 2"
            )
        return _authority_attempt(value)

    @classmethod
    def validate(
        cls,
        value: object,
        *,
        store: OperationStore,
        parent: OperationRecord,
        runtime_root: Path,
        receipt_path: Path | None = None,
        expected_definition_sha256: str | None = None,
        expected_profile: str | None = None,
        expected_profile_sha256: str | None = None,
        expected_head_sha: str | None = None,
        allowed_statuses: Collection[str] = ("complete", "failed"),
        expected_command_ids: Sequence[str] | None = None,
        child_states: Collection[str] | None = None,
        require_released: bool = False,
        require_effect_succeeded: bool = False,
    ) -> "VerificationAuthority":
        if not isinstance(value, dict) or value.get("schema_version") != 2:
            raise VerificationAuthorityError(
                "verification authority requires schema version 2"
            )
        if set(value) != RECEIPT_FIELDS:
            raise VerificationAuthorityError(
                "verification receipt fields are invalid"
            )
        attempt = _authority_attempt(value)
        definition_sha256 = str(value.get("definition_sha256") or "")
        input_sha256 = str(value.get("input_sha256") or "")
        head_sha = str(value.get("head_sha") or "")
        profile = str(value.get("profile") or "")
        profile_sha256 = str(value.get("profile_sha256") or "")
        status = str(value.get("status") or "")
        if (
            value.get("parent_operation_id") != parent.spec.operation_id
            or value.get("step_id") != "verify"
            or not re.fullmatch(r"[0-9a-f]{64}", definition_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", input_sha256)
            or not re.fullmatch(r"[0-9a-f]{40,64}", head_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", profile_sha256)
            or status not in set(allowed_statuses)
            or attempt.parent_operation_id != parent.spec.operation_id
            or attempt.profile != profile
            or attempt.profile_sha256 != profile_sha256
            or attempt.exact_head_sha != head_sha
            or (
                expected_definition_sha256 is not None
                and definition_sha256 != expected_definition_sha256
            )
            or (expected_profile is not None and profile != expected_profile)
            or (
                expected_profile_sha256 is not None
                and profile_sha256 != expected_profile_sha256
            )
            or (expected_head_sha is not None and head_sha != expected_head_sha)
        ):
            raise VerificationAuthorityError(
                "verification receipt contract identity is invalid"
            )
        try:
            stored_parent = store.read(
                parent.spec.owner_id, parent.spec.operation_id
            )
            expected_spec, expected_lane, expected_run = (
                pipeline_verify_identity(
                    parent.spec,
                    definition_sha256=definition_sha256,
                    input_sha256=input_sha256,
                    profile=profile,
                    attempt_index=attempt.attempt_index,
                )
            )
            child = store.read(parent.spec.owner_id, expected_spec.operation_id)
            effect_id = pipeline_verify_effect_id(
                input_sha256, attempt.attempt_index
            )
        except (StoreError, VerificationAttemptError) as exc:
            raise VerificationAuthorityError(
                "verification operation authority is unavailable"
            ) from exc
        if (
            stored_parent != parent
            or value.get("operation_id") != expected_spec.operation_id
            or value.get("lane_id") != expected_lane
            or value.get("run_id") != expected_run
            or value.get("effect_id") != effect_id
            or child.spec != expected_spec
            or child.lane_id != expected_lane
            or child.run_id != expected_run
            or (
                child_states is not None
                and child.state not in set(child_states)
            )
            or (require_released and child.resources != OwnedResources())
            or (require_released and bool(child.pending_effect))
            or (
                require_effect_succeeded
                and (
                    child.effect_id != effect_id
                    or child.effect_outcome != EffectOutcome.SUCCEEDED
                )
            )
        ):
            raise VerificationAuthorityError(
                "verification operation authority changed"
            )
        runtime_root = runtime_root.resolve()
        if receipt_path is not None:
            expected_path = (
                runtime_root
                / "pipeline-verification"
                / expected_spec.operation_id
                / "receipt.json"
            ).resolve()
            if (
                not _authority_path_is_safe(receipt_path)
                or receipt_path.is_symlink()
                or not receipt_path.is_file()
                or receipt_path.resolve() != expected_path
            ):
                raise VerificationAuthorityError(
                    "verification receipt pointer is invalid"
                )
        evidence = _authority_evidence(
            value,
            runtime_root=runtime_root,
            profile=profile,
            profile_sha256=profile_sha256,
            head_sha=head_sha,
            status=status,
            expected_command_ids=expected_command_ids,
        )
        return cls(
            parent=parent,
            child=child,
            definition_sha256=definition_sha256,
            input_sha256=input_sha256,
            head_sha=head_sha,
            profile=profile,
            profile_sha256=profile_sha256,
            effect_id=effect_id,
            status=status,
            attempt=attempt,
            evidence=evidence,
        )

    @classmethod
    def load(
        cls, receipt_path: Path, **kwargs: object
    ) -> "VerificationAuthority":
        if (
            not _authority_path_is_safe(receipt_path)
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            raise VerificationAuthorityError(
                "verification receipt is unavailable"
            )
        try:
            raw = receipt_path.read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationAuthorityError(
                "verification receipt is invalid"
            ) from exc
        if not raw or len(raw) > 1_000_000:
            raise VerificationAuthorityError(
                "verification receipt is invalid"
            )
        return cls.validate(value, receipt_path=receipt_path, **kwargs)

    @classmethod
    def issue(
        cls,
        *,
        store: OperationStore,
        parent: OperationRecord,
        runtime_root: Path,
        definition_sha256: str,
        input_sha256: str,
        profile: str,
        profile_sha256: str,
        attempt: VerificationAttempt,
        evidence: Sequence[VerificationEvidence],
        expected_command_ids: Sequence[str],
    ) -> "VerificationAuthority":
        try:
            expected_spec, lane_id, run_id = pipeline_verify_identity(
                parent.spec,
                definition_sha256=definition_sha256,
                input_sha256=input_sha256,
                profile=profile,
                attempt_index=attempt.attempt_index,
            )
            effect_id = pipeline_verify_effect_id(
                input_sha256, attempt.attempt_index
            )
        except VerificationAttemptError as exc:
            raise VerificationAuthorityError(str(exc)) from exc
        value = {
            "schema_version": cls.SCHEMA_VERSION,
            "operation_id": expected_spec.operation_id,
            "parent_operation_id": parent.spec.operation_id,
            "lane_id": lane_id,
            "run_id": run_id,
            "definition_sha256": definition_sha256,
            "step_id": "verify",
            "head_sha": attempt.exact_head_sha,
            "input_sha256": input_sha256,
            "profile": profile,
            "profile_sha256": profile_sha256,
            "effect_id": effect_id,
            "status": (
                "complete"
                if all(item.exit_code == 0 for item in evidence)
                else "failed"
            ),
            "evidence": [to_dict(item) for item in evidence],
            "verification_attempt": attempt.as_dict(),
            "verification_attempt_sha256": attempt.sha256,
        }
        return cls.validate(
            value,
            store=store,
            parent=parent,
            runtime_root=runtime_root,
            expected_definition_sha256=definition_sha256,
            expected_profile=profile,
            expected_profile_sha256=profile_sha256,
            expected_head_sha=attempt.exact_head_sha,
            expected_command_ids=expected_command_ids,
        )
