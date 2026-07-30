"""Bounded disposable prototype operation contract."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..context import ContextBuilder, ContextInput
from ..contracts import ContextPacketManifest
from ..git_ops import GitAdapter


@dataclass(frozen=True)
class PrototypeRequest:
    operation_id: str
    question: str
    success_criterion: str
    run_command: tuple[str, ...]
    worktree: str
    base: str = "HEAD"
    branch: str = ""

    def __post_init__(self) -> None:
        if not self.question.strip() or not self.success_criterion.strip():
            raise ValueError("prototype requires one question and success criterion")
        if not self.run_command or len(self.run_command) > 32:
            raise ValueError("prototype requires one bounded run command")
        if any(not value or "\0" in value for value in self.run_command):
            raise ValueError("prototype run command is invalid")
        if not self.worktree.startswith("/"):
            raise ValueError("prototype worktree must be an exact absolute path")

    @property
    def exact_branch(self) -> str:
        return self.branch or f"prototype/{self.operation_id}"


@dataclass(frozen=True)
class PrototypePrepared:
    request: PrototypeRequest
    context: ContextPacketManifest


@dataclass(frozen=True)
class PrototypeEvidence:
    operation_id: str
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


def prepare_prototype(
    request: PrototypeRequest,
    git: GitAdapter,
    context: ContextBuilder,
    *,
    references: tuple[ContextInput, ...] = (),
) -> PrototypePrepared:
    """Create the exact disposable worktree and its minimal typed handoff."""

    git.create_worktree(
        Path(request.worktree), request.exact_branch, request.base
    )
    builtins = (
        ContextInput(
            "question",
            "prototype-request",
            (
                f"Question: {request.question}\n"
                f"Success: {request.success_criterion}\n"
            ).encode(),
            role="task",
        ),
        ContextInput(
            "permissions",
            "prototype-policy",
            b"Owned disposable worktree only; no push or production promotion.\n",
            role="permissions",
        ),
        ContextInput(
            "verification",
            "prototype-success-criterion",
            (request.success_criterion + "\n").encode(),
            role="verification",
        ),
    )
    manifest = context.build(
        request.operation_id,
        builtins + references,
        metadata={
            "base": request.base,
            "branch": request.exact_branch,
            "workflow": "prototype",
        },
    )
    return PrototypePrepared(request, manifest)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_prototype(
    request: PrototypeRequest,
    *,
    runner: Runner = subprocess.run,
    max_output_bytes: int = 16_384,
) -> PrototypeEvidence:
    """Run exactly the approved command once and retain bounded evidence."""

    if max_output_bytes <= 0:
        raise ValueError("prototype output budget must be positive")
    result = runner(
        list(request.run_command),
        cwd=Path(request.worktree),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    encoded = (stdout + stderr).encode("utf-8", errors="replace")
    truncated = len(encoded) > max_output_bytes
    if truncated:
        remaining = max_output_bytes
        stdout_bytes = stdout.encode("utf-8", errors="replace")[:remaining]
        remaining -= len(stdout_bytes)
        stderr_bytes = stderr.encode("utf-8", errors="replace")[:remaining]
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
    return PrototypeEvidence(
        request.operation_id,
        request.run_command,
        result.returncode,
        stdout,
        stderr,
        truncated,
    )


def capture_decision(
    request: PrototypeRequest,
    evidence: PrototypeEvidence,
    decision: str,
    rationale: str,
    output: Path,
) -> Path:
    """Durably capture the bounded answer before disposable cleanup."""

    if evidence.operation_id != request.operation_id:
        raise ValueError("prototype evidence belongs to another operation")
    if decision not in {"adopt", "reject", "inconclusive"}:
        raise ValueError("prototype decision must be adopt, reject, or inconclusive")
    if not rationale.strip():
        raise ValueError("prototype decision requires a rationale")
    payload = {
        "schema_version": 1,
        "operation_id": request.operation_id,
        "question": request.question,
        "success_criterion": request.success_criterion,
        "command": list(evidence.command),
        "exit_code": evidence.exit_code,
        "truncated": evidence.truncated,
        "decision": decision,
        "rationale": rationale.strip(),
    }
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def cleanup_prototype(
    request: PrototypeRequest,
    git: GitAdapter,
    *,
    decision_path: Path,
) -> None:
    """Remove only the proven owned worktree after a durable decision."""

    if not decision_path.expanduser().resolve().is_file():
        raise ValueError("prototype cleanup requires a durable decision")
    worktree = Path(request.worktree).expanduser().resolve()
    decision = decision_path.expanduser().resolve()
    if decision == worktree or worktree in decision.parents:
        raise ValueError("prototype decision must live outside disposable worktree")
    git.cleanup_owned_worktree(worktree, discard=True)
