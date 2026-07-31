"""HEAD-bound verification profiles and bounded evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .contracts import ContractError, VerificationEvidence, to_dict


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
        )
        evidence.append(item)
        if result.returncode:
            break
    summary = evidence_dir / f"{profile.name}.json"
    summary.write_text(json.dumps([to_dict(row) for row in evidence], sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def valid_for(evidence: VerificationEvidence, *, head: str, profile: VerificationProfile) -> bool:
    return (
        evidence.exit_code == 0
        and evidence.head_sha == head
        and evidence.profile == profile.name
        and evidence.profile_sha256 == profile.sha256
    )
