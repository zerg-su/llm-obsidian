#!/usr/bin/env python3
"""Run and verify immutable exact-HEAD release-gate evidence bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from verification_diagnostics import (
    FailureDiagnosticStore,
    VerificationDiagnosticError,
    canonical_attempt_id,
)
from verification_staging import (
    ExternalPublicationStaging,
    VerificationStagingError,
)
from rc3_attempt_ledger import AttemptLedgerError, AttemptLedgerStore


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
COVERAGE = re.compile(
    r"harness statement-line coverage: ([0-9]+(?:\.[0-9]+)?)% .* "
    r"\(([0-9]+)/([0-9]+) lines\)"
)
MATRIX_CASES = re.compile(
    r"transition completeness: ([0-9,]+) deterministic matrix cases"
)


class ReceiptError(RuntimeError):
    """A gate command or immutable receipt failed closed."""


@dataclass(frozen=True)
class GateCommand:
    command_id: str
    argv: tuple[str, ...]
    validator: str = "exit-zero"


@dataclass(frozen=True)
class GateProfile:
    name: str
    commands: tuple[GateCommand, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    {
                        "command_id": item.command_id,
                        "argv": list(item.argv),
                        "validator": item.validator,
                    }
                    for item in self.commands
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


COMMON_GATE = (
    GateCommand("full-tests", ("make", "test")),
    GateCommand("harness-coverage", ("make", "test-harness-coverage"), "coverage"),
    GateCommand(
        "release-acceptance",
        ("python3", "scripts/release-acceptance.py", "check"),
    ),
    GateCommand(
        "vault-validation", ("python3", "scripts/validate-vault.py", "--summary")
    ),
    GateCommand("code-quality", ("make", "test-code-quality")),
    GateCommand(
        "skill-audit",
        ("python3", "skills/improve-skills/scripts/audit_skills.py", "--strict"),
    ),
    GateCommand(
        "instruction-budget-adapter",
        (
            "make",
            "test-instruction-lint",
            "test-skill-budget",
            "test-codex-adapter",
        ),
    ),
    GateCommand(
        "codex-adapter", ("python3", "scripts/codex-adapter.py", "--check")
    ),
)


PROFILES = {
    "stability-gate": GateProfile(
        "stability-gate",
        (
            *COMMON_GATE,
            GateCommand(
                "mcp-sync-config",
                (
                    "scripts/mcp-gateway/mcp-gateway.sh",
                    "sync-config",
                    "--apply",
                ),
            ),
            GateCommand(
                "codex-mcp-sync",
                ("scripts/mcp-gateway/mcp-gateway.sh", "codex-sync", "--check"),
            ),
            GateCommand("diff-check", ("git", "diff", "--check")),
            GateCommand(
                "clean-status",
                ("git", "status", "--short"),
                "empty-output",
            ),
        ),
    ),
    "release-final": GateProfile(
        "release-final",
        (
            *COMMON_GATE,
            GateCommand(
                "split-skill-audit",
                (
                    "python3",
                    "skills/improve-skills/scripts/audit_skills.py",
                    "--verdicts",
                    "docs/acceptance/v2.6.5-split-activation-skill-verdicts.json",
                    "--scope",
                    "split",
                    "--strict",
                ),
            ),
            GateCommand(
                "mcp-sync-config",
                (
                    "scripts/mcp-gateway/mcp-gateway.sh",
                    "sync-config",
                    "--apply",
                ),
            ),
            GateCommand(
                "codex-mcp-sync",
                ("scripts/mcp-gateway/mcp-gateway.sh", "codex-sync", "--check"),
            ),
            GateCommand(
                "release-secret-check",
                (
                    "python3",
                    "scripts/release-secret-check.py",
                    "--root",
                    ".",
                    "--base",
                    "nearest-tag",
                ),
            ),
            GateCommand(
                "harness-status",
                ("python3", "scripts/harness-cli.py", "--json", "status"),
                "resource-free",
            ),
            GateCommand(
                "harness-doctor",
                ("python3", "scripts/harness-cli.py", "--json", "doctor"),
                "doctor-ok",
            ),
            GateCommand("diff-check", ("git", "diff", "--check")),
            GateCommand(
                "clean-status",
                ("git", "status", "--short"),
                "empty-output",
            ),
        ),
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode:
        raise ReceiptError(f"cannot resolve Git evidence: git {' '.join(argv)}")
    return value


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _observations(command: GateCommand, output: bytes) -> dict[str, object]:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError(f"{command.command_id} output is not UTF-8") from exc
    if command.validator == "exit-zero":
        return {}
    if command.validator == "empty-output":
        if text.strip():
            raise ReceiptError(f"{command.command_id} expected empty output")
        return {"empty": True}
    if command.validator == "resource-free":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReceiptError("harness status is not JSON") from exc
        if value != []:
            raise ReceiptError("harness status retains owned or unknown resources")
        return {"operation_count": 0, "resource_free": True}
    if command.validator == "doctor-ok":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReceiptError("harness doctor output is not JSON") from exc
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ReceiptError("harness doctor did not report ok")
        return {"status": "ok"}
    if command.validator == "coverage":
        coverage = COVERAGE.search(text)
        cases = MATRIX_CASES.search(text)
        if coverage is None or cases is None:
            raise ReceiptError("coverage output lacks its typed denominator")
        covered = int(coverage.group(2))
        executable = int(coverage.group(3))
        percent = float(coverage.group(1))
        if executable <= 0 or round(covered * 100 / executable, 2) != percent:
            raise ReceiptError("coverage percentage contradicts its denominator")
        return {
            "coverage_kind": "stdlib-trace-ast-statement-lines",
            "weighted_percent": percent,
            "covered_lines": covered,
            "executable_lines": executable,
            "transition_matrix_cases": int(cases.group(1).replace(",", "")),
        }
    raise ReceiptError(f"unknown gate validator: {command.validator}")


def execute_gate(
    profile: GateProfile,
    *,
    root: Path,
    output_dir: Path,
    diagnostic_root: Path,
    staging_root: Path,
    attempt_id: str,
    expected_head: str,
    execution_relation: str,
    attempt_ledger_root: Path | None = None,
) -> dict[str, object]:
    """Run every command before atomically publishing any evidence bytes."""

    root = root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if (
        not root.is_dir()
        or not output_dir.parent.is_dir()
        or output_dir.exists()
        or output_dir.is_symlink()
        or execution_relation not in {"exact-head-reconstruction", "release-candidate"}
    ):
        raise ReceiptError("gate execution paths or relation are invalid")
    head = _git(root, "rev-parse", "HEAD")
    if head != expected_head or GIT_SHA.fullmatch(head) is None:
        raise ReceiptError("gate subject HEAD changed before execution")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    try:
        attempt_id = canonical_attempt_id(attempt_id)
        git_roots = []
        for value in (
            _git(root, "rev-parse", "--git-dir"),
            _git(root, "rev-parse", "--git-common-dir"),
        ):
            path = Path(value)
            git_roots.append(path if path.is_absolute() else root / path)
        staging_store = ExternalPublicationStaging(
            staging_root,
            subject_root=root,
            git_roots=git_roots,
            publication_dir=output_dir,
        )
        diagnostics = FailureDiagnosticStore(
            diagnostic_root,
            attempt_id=attempt_id,
            subject_root=root,
            publication_dir=output_dir,
        )
        if (
            staging_store.root == diagnostics.root
            or staging_store.root in diagnostics.root.parents
            or diagnostics.root in staging_store.root.parents
        ):
            raise ReceiptError(
                "verification staging and diagnostics must be physically separate"
            )
    except (VerificationDiagnosticError, VerificationStagingError) as exc:
        raise ReceiptError(str(exc)) from exc
    started_at = _utc_now()
    runner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ledger = None
    if profile.name == "release-final":
        if attempt_ledger_root is None:
            raise ReceiptError("release-final requires an authoritative attempt ledger")
        ledger = AttemptLedgerStore(attempt_ledger_root)
        try:
            ledger.reserve(
                attempt_id=attempt_id,
                subject_head_sha=head,
                profile_sha256=profile.sha256,
                execution_relation=execution_relation,
                runner_sha256=runner_sha256,
            )
        except AttemptLedgerError as exc:
            raise ReceiptError(str(exc)) from exc
    staging = staging_store.create(attempt_id)
    commands: list[dict[str, object]] = []
    try:
        for index, command in enumerate(profile.commands, 1):
            log_name = f"{index:02d}-{command.command_id}.log"
            log_path = staging / log_name
            command_started = _utc_now()
            print(
                f"verification-receipt: {profile.name} {index}/{len(profile.commands)} "
                f"{command.command_id}",
                file=sys.stderr,
                flush=True,
            )
            with log_path.open("wb") as output:
                try:
                    result = subprocess.run(
                        list(command.argv),
                        cwd=root,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        check=False,
                        env={**os.environ, "PYTHONHASHSEED": "0"},
                    )
                    exit_code = result.returncode
                    failure_kind = "command-exit"
                except OSError:
                    output.write(
                        b"verification-receipt: command could not start\n"
                    )
                    exit_code = 127
                    failure_kind = "command-start"
                output.flush()
                os.fsync(output.fileno())
            command_finished = _utc_now()
            log_path.chmod(0o644)
            output_sha256, output_bytes = _file_sha256(log_path)
            if exit_code:
                try:
                    diagnostic = diagnostics.publish(
                        subject_head_sha=head,
                        subject_tree_sha=tree,
                        profile=profile.name,
                        profile_sha256=profile.sha256,
                        attempt_started_at=started_at,
                        command_index=index,
                        command_count=len(profile.commands),
                        command_id=command.command_id,
                        command_argv=command.argv,
                        command_text=shlex.join(command.argv),
                        command_started_at=command_started,
                        command_finished_at=command_finished,
                        exit_code=exit_code,
                        failure_kind=failure_kind,
                        output_path=log_path,
                        runner=Path(__file__).name,
                        runner_sha256=runner_sha256,
                    )
                except VerificationDiagnosticError as exc:
                    raise ReceiptError(
                        "failed command diagnostic could not be retained"
                    ) from exc
                raise ReceiptError(
                    f"{command.command_id} exited {exit_code}; diagnostic: "
                    f"{diagnostic}; no evidence was published"
                )
            try:
                observations = _observations(command, log_path.read_bytes())
            except ReceiptError as exc:
                try:
                    diagnostic = diagnostics.publish(
                        subject_head_sha=head,
                        subject_tree_sha=tree,
                        profile=profile.name,
                        profile_sha256=profile.sha256,
                        attempt_started_at=started_at,
                        command_index=index,
                        command_count=len(profile.commands),
                        command_id=command.command_id,
                        command_argv=command.argv,
                        command_text=shlex.join(command.argv),
                        command_started_at=command_started,
                        command_finished_at=command_finished,
                        exit_code=exit_code,
                        failure_kind="validator-rejected",
                        output_path=log_path,
                        runner=Path(__file__).name,
                        runner_sha256=runner_sha256,
                    )
                except VerificationDiagnosticError as diagnostic_exc:
                    raise ReceiptError(
                        "failed validator diagnostic could not be retained"
                    ) from diagnostic_exc
                raise ReceiptError(
                    f"{command.command_id} output was rejected; diagnostic: "
                    f"{diagnostic}; no evidence was published"
                ) from exc
            commands.append(
                {
                    "command_id": command.command_id,
                    "argv": list(command.argv),
                    "cwd": ".",
                    "exit_code": exit_code,
                    "started_at": command_started,
                    "finished_at": command_finished,
                    "output_pointer": log_name,
                    "output_sha256": output_sha256,
                    "output_bytes": output_bytes,
                    "observations": observations,
                }
            )
        if _git(root, "rev-parse", "HEAD") != head:
            raise ReceiptError("gate subject HEAD changed during execution")
        receipt = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "profile": profile.name,
            "profile_sha256": profile.sha256,
            "subject_head_sha": head,
            "subject_tree_sha": tree,
            "execution_relation": execution_relation,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "runner": Path(__file__).name,
            "runner_sha256": runner_sha256,
            "commands": commands,
            "status": "passed",
        }
        write_path = staging / "receipt.json"
        write_path.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        write_path.chmod(0o644)
        staging_store.publish(staging, output_dir)
        if ledger is not None:
            try:
                ledger.finalize(
                    attempt_id=attempt_id,
                    classification=(
                        "published"
                        if execution_relation == "release-candidate"
                        else "test-only"
                    ),
                    exit_status=0,
                    artifact_path=output_dir / "receipt.json",
                )
            except AttemptLedgerError as exc:
                raise ReceiptError("release attempt ledger finalization failed") from exc
        return receipt
    except BaseException as failure:
        staging_store.cleanup(staging)
        if ledger is not None:
            diagnostic = diagnostic_root / attempt_id / "diagnostic-receipt.json"
            if not diagnostic.is_file():
                diagnostic = ledger.root / "artifacts" / f"{attempt_id}.failure.json"
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "type": "release-attempt-failure",
                            "attempt_id": attempt_id,
                            "subject_head_sha": head,
                            "profile_sha256": profile.sha256,
                            "failure_type": type(failure).__name__,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
            try:
                ledger.finalize(
                    attempt_id=attempt_id,
                    classification=(
                        "test-only"
                        if execution_relation == "exact-head-reconstruction"
                        else "unpublished"
                    ),
                    exit_status=1,
                    artifact_path=diagnostic,
                )
            except AttemptLedgerError as exc:
                raise ReceiptError("release attempt ledger failure was not retained") from exc
        raise


def verify_receipt(
    receipt_path: Path,
    *,
    expected_head: str,
    expected_profile: str,
) -> dict[str, object]:
    if GIT_SHA.fullmatch(expected_head) is None:
        raise ReceiptError("expected verification subject HEAD is invalid")
    if expected_profile not in PROFILES:
        raise ReceiptError("expected verification profile is unknown")
    receipt_path = receipt_path.expanduser().resolve()
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ReceiptError("verification receipt is unavailable")
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError("verification receipt is invalid JSON") from exc
    expected = {
        "schema_version",
        "attempt_id",
        "profile",
        "profile_sha256",
        "subject_head_sha",
        "subject_tree_sha",
        "execution_relation",
        "started_at",
        "finished_at",
        "runner",
        "runner_sha256",
        "commands",
        "status",
    }
    if not isinstance(value, dict):
        raise ReceiptError("verification receipt schema is invalid")
    try:
        attempt_id = canonical_attempt_id(str(value.get("attempt_id") or ""))
    except VerificationDiagnosticError as exc:
        raise ReceiptError("verification receipt schema is invalid") from exc
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("status") != "passed"
        or attempt_id != value.get("attempt_id")
        or not SHA256.fullmatch(str(value.get("profile_sha256") or ""))
        or not GIT_SHA.fullmatch(str(value.get("subject_head_sha") or ""))
        or not GIT_SHA.fullmatch(str(value.get("subject_tree_sha") or ""))
    ):
        raise ReceiptError("verification receipt schema is invalid")
    profile = PROFILES.get(str(value.get("profile") or ""))
    if profile is None or value.get("profile_sha256") != profile.sha256:
        raise ReceiptError("verification profile identity changed")
    if value.get("subject_head_sha") != expected_head:
        raise ReceiptError("verification receipt subject HEAD is not exact")
    if value.get("profile") != expected_profile:
        raise ReceiptError("verification receipt profile is not exact")
    if value.get("runner") != Path(__file__).name or value.get(
        "runner_sha256"
    ) != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ReceiptError("verification runner identity changed")
    commands = value.get("commands")
    if not isinstance(commands, list) or len(commands) != len(profile.commands):
        raise ReceiptError("verification receipt has no commands")
    pointers: set[str] = set()
    for item, expected_command in zip(commands, profile.commands, strict=True):
        if (
            not isinstance(item, dict)
            or item.get("command_id") != expected_command.command_id
            or item.get("argv") != list(expected_command.argv)
        ):
            raise ReceiptError("verification command receipt is invalid")
        pointer = item.get("output_pointer")
        if (
            not isinstance(pointer, str)
            or Path(pointer).name != pointer
            or pointer in pointers
            or item.get("exit_code") != 0
        ):
            raise ReceiptError("verification output pointer is invalid")
        pointers.add(pointer)
        path = receipt_path.parent / pointer
        digest, size = _file_sha256(path)
        if digest != item.get("output_sha256") or size != item.get("output_bytes"):
            raise ReceiptError("verification output digest changed")
    return value


def die(message: str) -> NoReturn:
    print(f"verification-receipt: {message}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--profile", choices=sorted(PROFILES), required=True)
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--diagnostic-root", type=Path, required=True)
    run.add_argument("--staging-root", type=Path, required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--expected-head", required=True)
    run.add_argument(
        "--execution-relation",
        choices=("exact-head-reconstruction", "release-candidate"),
        required=True,
    )
    run.add_argument("--attempt-ledger-root", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--expected-head", required=True)
    verify.add_argument("--expected-profile", choices=sorted(PROFILES), required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            receipt = execute_gate(
                PROFILES[args.profile],
                root=args.root,
                output_dir=args.output_dir,
                diagnostic_root=args.diagnostic_root,
                staging_root=args.staging_root,
                attempt_id=args.attempt_id,
                expected_head=args.expected_head,
                execution_relation=args.execution_relation,
                attempt_ledger_root=args.attempt_ledger_root,
            )
            print(json.dumps(receipt, sort_keys=True))
        else:
            receipt = verify_receipt(
                args.receipt,
                expected_head=args.expected_head,
                expected_profile=args.expected_profile,
            )
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "valid",
                        "profile": receipt["profile"],
                        "attempt_id": receipt["attempt_id"],
                        "subject_head_sha": receipt["subject_head_sha"],
                    },
                    sort_keys=True,
                )
            )
        return 0
    except (
        OSError,
        ReceiptError,
        ValueError,
        VerificationDiagnosticError,
        VerificationStagingError,
    ) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
