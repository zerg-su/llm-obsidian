#!/usr/bin/env python3
"""Execute one Architecture Workflow pressure case through registered Codex."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib_sanitize import residual_credential_kinds, sanitize  # noqa: E402
from model_routing import RoutingError, load_config  # noqa: E402


TIMEOUT = ROOT / "scripts" / "with-timeout"
CURRENT_SESSION = ROOT / "scripts" / "current-session-id.sh"
SOURCES = {
    "architecture": (
        "skills/architecture/SKILL.md",
        "docs/skill-references/architecture-artifacts.md",
    ),
    "decompose": (
        "skills/decompose/SKILL.md",
        "docs/skill-references/architecture-artifacts.md",
    ),
    "implementation-plan": (
        "skills/implementation-plan/SKILL.md",
        "docs/skill-references/engineering-quality-contract.md",
        "docs/skill-references/architecture-artifacts.md",
    ),
}
PRESSURE_EVIDENCE_ROOT = (
    ROOT / "docs" / "acceptance" / "evidence" / "architecture-workflow-v1"
)
PRESSURE_CASE_ID = re.compile(r"[a-s]\Z")
SUBJECT_HEAD = re.compile(r"[0-9a-f]{40}\Z")
CHILD_SESSION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
COORDINATOR_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
FIXTURE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
EFFECT_MODES = frozenset({"read-only", "zero-effect", "fixture-mutation"})
FIXTURE_MARKER = ".architecture-pressure-fixture.json"
FIXTURE_PARENT_MARKER = ".architecture-pressure-parent.json"
APP_SERVER_EPERM = (
    "failed to initialize in-process app-server client: "
    "Operation not permitted (os error 1)"
)


class RunnerError(ValueError):
    """The case or the release-owned execution boundary is invalid."""


class TransportInitializationFailure(RunnerError):
    """A typed transport failure known to precede any model result."""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        super().__init__(json.dumps(record, sort_keys=True))


def transport_failure_record(
    *, returncode: int, stderr: str, typed_case_output_present: bool
) -> dict[str, Any] | None:
    """Classify only the exact output-free nested Codex EPERM signature."""

    if (
        returncode == 0
        or stderr.strip() not in {APP_SERVER_EPERM, f"Error: {APP_SERVER_EPERM}"}
        or typed_case_output_present
    ):
        return None
    return {
        "schema_version": 1,
        "type": "architecture-pressure-transport-failure",
        "failure_class": "pre-model-transport-initialization",
        "code": "nested-codex-app-server-eperm",
        "accepted_case_receipts": 0,
        "completed_model_results": 0,
        "typed_case_output_present": False,
    }


def load_case() -> dict[str, Any]:
    """Read and validate exactly one release case from stdin."""

    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise RunnerError("input must be one JSON object") from exc
    if not isinstance(value, dict):
        raise RunnerError("input must be one JSON object")
    if value.get("schema_version") != 1:
        raise RunnerError("unsupported case schema_version")
    capability = value.get("capability")
    if capability not in SOURCES:
        raise RunnerError(f"unsupported capability: {capability!r}")
    if not isinstance(value.get("id"), str) or not isinstance(value.get("input"), dict):
        raise RunnerError("case id and input are required")
    assertions = value.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise RunnerError("case assertions are required for response-field derivation")
    artifact_fields(value)
    return value


def artifact_fields(case: dict[str, Any]) -> tuple[str, ...]:
    """Derive the flat response fields without revealing expected values."""

    fields: set[str] = set()
    for assertion in case["assertions"]:
        if not isinstance(assertion, dict) or assertion.get("kind") != "equals":
            raise RunnerError("pressure assertions must be equality objects")
        path = assertion.get("path")
        if not isinstance(path, str) or not path.startswith("artifacts."):
            raise RunnerError("pressure assertion must target artifacts.<field>")
        parts = path.split(".")
        if len(parts) != 2 or not parts[1]:
            raise RunnerError("pressure artifact assertions must be flat")
        fields.add(parts[1])
    return tuple(sorted(fields))


def grade_case(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Grade the equality-only contract outside the provider transcript."""

    failures: list[str] = []
    artifacts = result.get("artifacts") if isinstance(result, dict) else None
    if not isinstance(artifacts, dict):
        return ["artifacts: missing"]
    for assertion in case["assertions"]:
        field = assertion["path"].split(".", 1)[1]
        expected = assertion.get("value")
        if field not in artifacts:
            failures.append(f"artifacts.{field}: missing")
            continue
        actual = artifacts[field]
        if type(actual) is not type(expected) or actual != expected:
            failures.append(
                f"artifacts.{field}: expected {expected!r}, got {actual!r}"
            )
    return failures


def source_bundle(capability: str) -> str:
    """Read the exact governing sources for one carrier."""

    sections: list[str] = []
    for relative in SOURCES[capability]:
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RunnerError(f"governing source is unavailable: {relative}") from exc
        sections.append(f"## {relative}\n\n{text.strip()}")
    return "\n\n".join(sections)


def prompt_for(case: dict[str, Any]) -> str:
    """Compile the bounded prompt without grading expectations."""

    fields = artifact_fields(case)
    vocabulary = case.get("response_contract", {})
    if not isinstance(vocabulary, dict) or not set(vocabulary) <= set(fields):
        raise RunnerError("response vocabulary does not match artifact fields")
    vocabulary_text = (
        "\nStable response vocabulary (choices are unordered; select independently):\n"
        f"{json.dumps(vocabulary, ensure_ascii=False, indent=2)}\n"
        if vocabulary
        else ""
    )
    return (
        "You are evaluating one repository engineering-skill pressure scenario.\n"
        "Use only the supplied local skill contracts as the behavioral authority.\n"
        "Do not inspect the repository, call tools, launch a workflow, or infer expected "
        "answers from a grader. Judge the scenario on its merits.\n"
        "Return one JSON object with a concise string `output` and an `artifacts` object.\n"
        f"The artifacts object must contain exactly these fields: {', '.join(fields)}.\n"
        "Artifact values describe the action you recommend after applying the contracts, "
        "not the rejected proposal or the scenario's initial state.\n"
        "Use JSON booleans for yes/no judgments and short stable strings for decisions.\n\n"
        f"Case id: {case['id']}\n"
        f"Capability: {case['capability']}\n"
        "Scenario input:\n"
        f"{json.dumps(case['input'], ensure_ascii=False, indent=2)}\n"
        f"{vocabulary_text}\n"
        "Authoritative local contracts:\n\n"
        f"{source_bundle(case['capability'])}\n"
    )


def output_schema(case: dict[str, Any]) -> dict[str, Any]:
    """Build the strict provider result schema from declared artifact types."""

    field_types: dict[str, str] = {}
    for assertion in case["assertions"]:
        field = assertion["path"].split(".", 1)[1]
        expected = assertion.get("value")
        if type(expected) is bool:
            field_types[field] = "boolean"
        elif type(expected) is int:
            field_types[field] = "integer"
        elif isinstance(expected, (int, float)):
            field_types[field] = "number"
        else:
            field_types[field] = "string"
    vocabulary = case.get("response_contract", {})
    properties = {field: {"type": field_types[field]} for field in sorted(field_types)}
    for field, choices in vocabulary.items():
        if not isinstance(choices, list) or not choices:
            raise RunnerError("response vocabulary choices must be non-empty lists")
        properties[field]["enum"] = choices
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["output", "artifacts"],
        "properties": {
            "output": {"type": "string"},
            "artifacts": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(field_types),
                "properties": properties,
            },
        },
    }


def pressure_metadata(case: dict[str, Any]) -> dict[str, Any]:
    """Validate release authority that remains outside the model prompt."""

    value = case.get("pressure")
    required = {
        "case_id",
        "expected_carrier",
        "effect_mode",
        "fixture_vault",
        "assertions",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RunnerError("Architecture Workflow pressure metadata is invalid")
    case_id = value["case_id"]
    effect_mode = value["effect_mode"]
    fixture_id = value["fixture_vault"]
    assertions = value["assertions"]
    if (
        not isinstance(case_id, str)
        or PRESSURE_CASE_ID.fullmatch(case_id) is None
        or case.get("id") != f"architecture-workflow.{case_id}"
        or not isinstance(value["expected_carrier"], str)
        or not value["expected_carrier"].strip()
        or effect_mode not in EFFECT_MODES
        or not isinstance(assertions, list)
        or not assertions
        or any(not isinstance(item, str) or not item.strip() for item in assertions)
    ):
        raise RunnerError("Architecture Workflow pressure metadata is invalid")
    if effect_mode == "read-only":
        if fixture_id is not None:
            raise RunnerError("read-only pressure case cannot name a fixture vault")
    elif not isinstance(fixture_id, str) or FIXTURE_ID.fullmatch(fixture_id) is None:
        raise RunnerError("fixture pressure case requires a normalized fixture id")
    return value


def _read_fixture_marker(path: Path, expected: dict[str, object]) -> None:
    if path.is_symlink() or not path.is_file():
        raise RunnerError("pressure fixture marker is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("pressure fixture marker is unavailable") from exc
    if value != expected:
        raise RunnerError("pressure fixture marker identity changed")


def _fixture_root(
    path: Path | None, metadata: dict[str, Any], subject_head: str
) -> Path | None:
    effect_mode = metadata["effect_mode"]
    if effect_mode == "read-only":
        if path is not None:
            raise RunnerError("read-only pressure case cannot receive a fixture root")
        return None
    if path is None or not path.is_absolute() or path.is_symlink():
        raise RunnerError("fixture pressure case requires an absolute real directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RunnerError("fixture pressure root is unavailable") from exc
    if not resolved.is_dir() or resolved == ROOT or ROOT in resolved.parents:
        raise RunnerError("pressure fixture must be disposable and outside the product vault")
    temporary_roots = {Path(tempfile.gettempdir()).resolve()}
    for candidate in (Path("/tmp"), Path("/private/tmp")):
        if candidate.is_dir():
            temporary_roots.add(candidate.resolve())
    parent_prefix = f"architecture-workflow-v1-{subject_head}"
    case_id = metadata["case_id"]
    fixture_id = metadata["fixture_vault"]
    if (
        resolved.parent.parent not in temporary_roots
        or not resolved.parent.name.startswith(parent_prefix)
        or resolved.name != case_id
    ):
        raise RunnerError("pressure fixture is outside its dedicated temporary parent")
    _read_fixture_marker(
        resolved.parent / FIXTURE_PARENT_MARKER,
        {
            "schema_version": 1,
            "type": "architecture-pressure-parent",
            "subject_head": subject_head,
        },
    )
    _read_fixture_marker(
        resolved / FIXTURE_MARKER,
        {
            "schema_version": 1,
            "type": "architecture-pressure-fixture",
            "subject_head": subject_head,
            "case_id": case_id,
            "fixture_id": fixture_id,
        },
    )
    return resolved


def directory_state_manifest(root: Path) -> list[dict[str, object]]:
    """Describe a complete fixture without following symlinks."""

    manifest: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RunnerError("pressure fixture contains a symlink")
        if path.is_dir():
            manifest.append({"path": relative, "kind": "directory"})
            continue
        if not path.is_file():
            raise RunnerError("pressure fixture contains an unsupported filesystem entry")
        data = path.read_bytes()
        manifest.append(
            {
                "path": relative,
                "kind": "file",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return manifest


def manifest_sha256(manifest: list[dict[str, object]]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def repository_state_sha256() -> str:
    """Hash tracked deltas and untracked product paths, excluding ignored state."""

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        tracked = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--", "."],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        untracked_raw = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise RunnerError("cannot measure product worktree state") from exc
    digest = hashlib.sha256()
    digest.update(head.encode("ascii") + b"\0" + tracked + b"\0")
    for raw in sorted(item for item in untracked_raw.split(b"\0") if item):
        try:
            path = ROOT / raw.decode("utf-8")
            if path.is_symlink():
                data = os.readlink(path).encode("utf-8")
                kind = b"L"
            elif path.is_file():
                data = path.read_bytes()
                kind = b"F"
            elif path.is_dir():
                data = b""
                kind = b"D"
            else:
                data = b""
                kind = b"?"
        except (OSError, UnicodeDecodeError) as exc:
            raise RunnerError("cannot measure untracked product state") from exc
        digest.update(kind + b"\0" + raw + b"\0" + data + b"\0")
    return digest.hexdigest()


def pressure_prompt_for(case: dict[str, Any]) -> str:
    """Compile the case prompt with its exact effect boundary."""

    metadata = pressure_metadata(case)
    prompt = prompt_for(case)
    if metadata["effect_mode"] == "read-only":
        boundary = "This is a read-only pressure case. Do not call tools or mutate local state."
    else:
        boundary = (
            "The working directory is a disposable fixture vault. You may inspect it and "
            "use local tools only as the supplied contracts and scenario authorize. Do not "
            "access the network or any path outside the fixture. A zero-effect case must "
            "leave the fixture unchanged; a fixture-mutation case must keep every effect "
            "inside the fixture."
        )
    return prompt.replace(
        "Do not inspect the repository, call tools, launch a workflow, or infer expected "
        "answers from a grader. Judge the scenario on its merits.",
        f"{boundary} Do not infer expected answers from a grader. Judge the scenario on its merits.",
        1,
    )


def validate_child_session_id(value: object) -> str:
    """Require the canonical child identity emitted by Codex thread.started."""

    if not isinstance(value, str) or CHILD_SESSION_ID.fullmatch(value) is None:
        raise RunnerError("Codex pressure result lacks a canonical child session identity")
    return value


def current_coordinator_session_id() -> str:
    """Read the executing coordinator identity through the registered helper."""

    try:
        value = subprocess.run(
            [str(CURRENT_SESSION)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RunnerError("cannot capture executing coordinator session") from exc
    if (
        value == "unknown"
        or COORDINATOR_SESSION_ID.fullmatch(value) is None
    ):
        raise RunnerError("executing coordinator session is unavailable")
    return value


def _pressure_provider_run(
    case: dict[str, Any],
    *,
    model: str,
    effort: str,
    timeout: float,
    cwd: Path,
    sandbox: str,
    skip_git_repo_check: bool,
) -> tuple[dict[str, Any], str, str]:
    """Run one fresh Codex child and return result, prompt, and child identity."""

    codex = shutil.which("codex")
    if codex is None:
        raise RunnerError("codex executable is unavailable")
    prompt = pressure_prompt_for(case)
    with tempfile.TemporaryDirectory(prefix="architecture-pressure.") as raw:
        scratch = Path(raw)
        schema_path = scratch / "result.schema.json"
        output_path = scratch / "result.json"
        schema_path.write_text(
            json.dumps(output_schema(case), sort_keys=True) + "\n", encoding="utf-8"
        )
        command = [
            str(TIMEOUT),
            str(timeout),
            codex,
            "--ask-for-approval",
            "never",
            "exec",
        ]
        if skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.extend(
            [
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                sandbox,
                "--config",
                "sandbox_workspace_write.network_access=false",
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{effort}"',
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(cwd),
                "-",
            ]
        )
        proc = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if proc.returncode != 0:
            typed_output_present = output_path.is_file() and output_path.stat().st_size > 0
            failure = transport_failure_record(
                returncode=proc.returncode,
                stderr=proc.stderr,
                typed_case_output_present=typed_output_present,
            )
            if failure is not None:
                raise TransportInitializationFailure(failure)
            detail_lines = proc.stderr.strip().splitlines()[-12:]
            detail = " | ".join(line.strip() for line in detail_lines if line.strip())
            raise RunnerError(
                f"Codex pressure run exited {proc.returncode}: "
                f"{(detail or 'no diagnostic')[:1200]}"
            )
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
            events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError("Codex pressure run returned invalid JSON") from exc
    sessions = [
        event.get("thread_id")
        for event in events
        if isinstance(event, dict) and event.get("type") == "thread.started"
    ]
    if len(sessions) != 1:
        raise RunnerError("Codex pressure result lacks one child session identity")
    session_id = validate_child_session_id(sessions[0])
    if (
        not isinstance(result, dict)
        or set(result) != {"output", "artifacts"}
        or not isinstance(result["output"], str)
        or not result["output"].strip()
        or not isinstance(result["artifacts"], dict)
        or set(result["artifacts"]) != set(artifact_fields(case))
    ):
        raise RunnerError("Codex pressure result violates the bounded result contract")
    return result, prompt, session_id


def _sanitize_transcript(text: str, fixture_root: Path | None) -> str:
    sanitized = text.replace(str(ROOT), "<task-worktree>")
    if fixture_root is not None:
        sanitized = sanitized.replace(str(fixture_root), "<fixture-vault>")
    sanitized = re.sub(
        r"/(?:Users|private/tmp|private/var/folders)/[^\s`\"']+",
        "<redacted-path>",
        sanitized,
    )
    sanitized, _ = sanitize(sanitized)
    if residual := residual_credential_kinds(sanitized):
        raise RunnerError(
            "pressure transcript retains credential-shaped content: "
            + ", ".join(sorted(residual))
        )
    return sanitized


def _create_evidence(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise RunnerError(f"pressure evidence already exists: {path.name}")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RunnerError(f"cannot publish pressure evidence: {path.name}") from exc


def execute_pressure_case(
    case: dict[str, Any],
    *,
    route_alias: str,
    effort: str,
    timeout: float,
    subject_head: str,
    fixture_root: Path | None,
    evidence_root: Path = PRESSURE_EVIDENCE_ROOT,
) -> dict[str, Any]:
    """Execute and exclusively publish one Architecture Workflow record."""
    metadata = pressure_metadata(case)
    if SUBJECT_HEAD.fullmatch(subject_head) is None:
        raise RunnerError("pressure subject_head must be a full Git object id")
    config = load_config(ROOT)
    aliases = config.data.get("model_aliases", {})
    if route_alias not in aliases:
        raise RunnerError("pressure route must use a registered routing alias")
    route = config.resolve_alias(route_alias, "codex")
    coordinator_session_id = current_coordinator_session_id()
    fixture = _fixture_root(fixture_root, metadata, subject_head)
    if fixture is None:
        pre_manifest = None
        pre_state = repository_state_sha256()
    else:
        pre_manifest = directory_state_manifest(fixture)
        pre_state = manifest_sha256(pre_manifest)
    result, prompt, session_id = _pressure_provider_run(
        case,
        model=route["model"],
        effort=effort,
        timeout=timeout,
        cwd=ROOT if fixture is None else fixture,
        sandbox="read-only" if fixture is None else "workspace-write",
        skip_git_repo_check=fixture is not None,
    )
    if fixture is None:
        post_manifest = None
        post_state = repository_state_sha256()
    else:
        post_manifest = directory_state_manifest(fixture)
        post_state = manifest_sha256(post_manifest)
    equal_state = pre_state == post_state
    if metadata["effect_mode"] in {"read-only", "zero-effect"} and not equal_state:
        raise RunnerError("zero-effect pressure case mutated measured state")
    if metadata["effect_mode"] == "fixture-mutation" and equal_state:
        raise RunnerError("fixture-mutation pressure case produced no measured effect")
    failures = grade_case(case, result)
    if failures:
        raise RunnerError("pressure result failed assertions: " + "; ".join(failures))
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    transcript = _sanitize_transcript(
        "\n".join(
            (
                f"# Architecture Workflow pressure case {metadata['case_id'].upper()}",
                "",
                f"Session: `{session_id}`",
                f"Coordinator session: `{coordinator_session_id}`",
                f"Route alias: `{route_alias}`",
                f"Runtime/model/effort: `codex` / `{route['model']}` / `{effort}`",
                "",
                "## Prompt",
                "",
                "```text",
                prompt,
                "```",
                "",
                "## Response",
                "",
                "```json",
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            )
        ),
        fixture,
    ).encode("utf-8")
    case_id = metadata["case_id"]
    transcript_path = evidence_root / "pressure" / f"{case_id}.md"
    record_path = evidence_root / "pressure" / f"{case_id}.json"
    canonical_transcript_path = (
        f"docs/acceptance/evidence/architecture-workflow-v1/pressure/{case_id}.md"
    )
    record = {
        "schema_version": 1,
        "case_id": case_id,
        "prompt": prompt,
        "expected_carrier": metadata["expected_carrier"],
        "observed_outcome": result["output"],
        "verdict": "pass",
        "subject_head": subject_head,
        "effect_mode": metadata["effect_mode"],
        "fixture_vault": metadata["fixture_vault"],
        "pre_state_manifest": pre_manifest,
        "post_state_manifest": post_manifest,
        "pre_state_sha256": pre_state,
        "post_state_sha256": post_state,
        "execution_provenance": {
            "session_id": session_id,
            "coordinator_session_id": coordinator_session_id,
            "timestamp": timestamp,
            "route": {
                "alias": route_alias,
                "runtime": route["runtime"],
                "model": route["model"],
                "effort": effort,
                "routing_sha256": config.fingerprint,
            },
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "transcript_path": canonical_transcript_path,
            "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
            "harness_operation_id": None,
            "harness_receipt_id": None,
        },
        "assertions": metadata["assertions"],
    }
    record_text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    sanitized_record, _ = sanitize(record_text)
    if sanitized_record != record_text or residual_credential_kinds(record_text):
        raise RunnerError("pressure record contains credential-shaped content")
    record_bytes = record_text.encode("utf-8")
    _create_evidence(transcript_path, transcript)
    try:
        _create_evidence(record_path, record_bytes)
    except RunnerError:
        transcript_path.unlink(missing_ok=True)
        raise
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--architecture-pressure",
        action="store_true",
        help="run and publish exactly one Architecture Workflow pressure case",
    )
    parser.add_argument("--model", default="terra")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--subject-head")
    parser.add_argument("--fixture-vault", type=Path)
    args = parser.parse_args()
    try:
        if not args.architecture_pressure:
            raise RunnerError("--architecture-pressure is required")
        if args.timeout <= 0:
            raise RunnerError("timeout must be positive")
        if not args.subject_head:
            raise RunnerError("Architecture Workflow pressure requires --subject-head")
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if args.subject_head != current_head:
            raise RunnerError("pressure subject_head is not the current product HEAD")
        result = execute_pressure_case(
            load_case(),
            route_alias=args.model,
            effort=args.effort,
            timeout=args.timeout,
            subject_head=args.subject_head,
            fixture_root=args.fixture_vault,
        )
    except (RoutingError, RunnerError, subprocess.CalledProcessError) as exc:
        print(f"architecture-workflow-pressure: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
