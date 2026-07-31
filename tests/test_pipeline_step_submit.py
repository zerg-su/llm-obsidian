#!/usr/bin/env python3
"""Hermetic checks for the fixed engineering/fix callback submitter."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pipeline-step-submit.py"
REQUEST = ".task-pipeline-step-request.json"
OUTBOX = ".task-pipeline-step-callback.json"
failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"ok - {name}")
    else:
        failures.append(name)
        print(f"not ok - {name}: {detail}")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(value: str) -> str:
    return sha_bytes(value.encode())


def git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run(worktree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--worktree", str(worktree)],
        text=True,
        capture_output=True,
        check=False,
    )


def request(
    worktree: Path,
    *,
    step_id: str = "reproduce",
    iteration: int = 0,
    verification_sha256: str = "",
    result_pointer: str = ".task-pipeline-step-result.json",
    output_pointer: str = ".task-pipeline-step-output.md",
) -> dict[str, object]:
    input_schema, output_schema = {
        "reproduce": ("approved-plan/v1", "reproduction/v1"),
        "root-cause": ("reproduction/v1", "diagnosis/v1"),
        "regression-test": ("diagnosis/v1", "regression-test/v1"),
        "minimal-fix": ("regression-test/v1", "implementation-result/v1"),
    }[step_id]
    return {
        "schema_version": 1,
        "operation_id": (
            f"fix-parent-{step_id}-{iteration}-abcdef123456"
        ),
        "run_id": sha(f"run:{step_id}")[:32],
        "parent_operation_id": "fix-parent",
        "lane_id": "fix-lane",
        "definition_sha256": sha("definition"),
        "step_id": step_id,
        "iteration": iteration,
        "input_schema": input_schema,
        "input_sha256": sha(f"input:{step_id}"),
        "input_head_sha": git(worktree, "rev-parse", "HEAD"),
        "prior_receipt_sha256": (
            "" if step_id == "reproduce" else sha("prior-receipt")
        ),
        "verification_sha256": verification_sha256,
        "output_schema": output_schema,
        "result_pointer": result_pointer,
        "output_pointer": output_pointer,
    }


def prepare(
    worktree: Path,
    raw_request: dict[str, object],
    *,
    status: str = "complete",
    output: bytes = b"bounded phase evidence\n",
    declared_output_sha256: str | None = None,
    declared_head_sha: str | None = None,
) -> None:
    write_json(worktree / REQUEST, raw_request)
    output_path = worktree / str(raw_request["output_pointer"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output)
    write_json(
        worktree / str(raw_request["result_pointer"]),
        {
            "schema_version": 1,
            "status": status,
            "output_sha256": (
                declared_output_sha256
                if declared_output_sha256 is not None
                else sha_bytes(output)
            ),
            "head_sha": (
                declared_head_sha
                if declared_head_sha is not None
                else git(worktree, "rev-parse", "HEAD")
            ),
        },
    )


def reset_transport(worktree: Path) -> None:
    for path in (
        worktree / REQUEST,
        worktree / OUTBOX,
        worktree / ".task-pipeline-step-result.json",
        worktree / ".task-pipeline-step-output.md",
    ):
        if path.is_symlink() or path.is_file():
            path.unlink()


with tempfile.TemporaryDirectory(prefix="pipeline-step-submit.") as raw:
    worktree = Path(raw) / "generic-target"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "test@example.invalid")
    git(worktree, "config", "user.name", "Pipeline Test")
    (worktree / "README.md").write_text("fixture\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "fixture")

    raw_request = request(worktree)
    prepare(worktree, raw_request)
    before = {
        path.relative_to(worktree).as_posix()
        for path in worktree.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    result = run(worktree)
    after = {
        path.relative_to(worktree).as_posix()
        for path in worktree.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    callback = json.loads((worktree / OUTBOX).read_text(encoding="utf-8"))
    payload = callback["payload"]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    check(
        "fixed request produces one exact CallbackEnvelope in the fixed outbox",
        result.returncode == 0
        and after - before == {OUTBOX}
        and callback["schema_version"] == 1
        and callback["operation_id"] == raw_request["operation_id"]
        and callback["run_id"] == raw_request["run_id"]
        and callback["kind"] == "result"
        and callback["payload_sha256"] == sha_bytes(encoded)
        and callback["callback_id"]
        == "result-" + callback["payload_sha256"][:24],
        (result.stderr, callback, after - before),
    )
    check(
        "callback binds every immutable phase input and observed output",
        payload
        == {
            "schema_version": 1,
            "parent_operation_id": raw_request["parent_operation_id"],
            "definition_sha256": raw_request["definition_sha256"],
            "step_id": raw_request["step_id"],
            "iteration": raw_request["iteration"],
            "input_schema": raw_request["input_schema"],
            "input_sha256": raw_request["input_sha256"],
            "input_head_sha": raw_request["input_head_sha"],
            "prior_receipt_sha256": raw_request[
                "prior_receipt_sha256"
            ],
            "verification_sha256": "",
            "output_schema": raw_request["output_schema"],
            "output_pointer": raw_request["output_pointer"],
            "output_sha256": sha_bytes(b"bounded phase evidence\n"),
            "head_sha": git(worktree, "rev-parse", "HEAD"),
            "status": "complete",
        },
        payload,
    )

    original_outbox = (worktree / OUTBOX).read_bytes()
    second = run(worktree)
    check(
        "submitter never overwrites an existing callback outbox",
        second.returncode != 0
        and (worktree / OUTBOX).read_bytes() == original_outbox
        and "already exists" in second.stderr,
        second.stderr,
    )

    reset_transport(worktree)
    retry_request = request(
        worktree,
        step_id="root-cause",
        iteration=1,
        verification_sha256=sha("verification-packet"),
    )
    prepare(worktree, retry_request)
    retry_result = run(worktree)
    retry_callback = json.loads(
        (worktree / OUTBOX).read_text(encoding="utf-8")
    )
    check(
        "retry callback preserves its exact verification packet binding",
        retry_result.returncode == 0
        and retry_callback["payload"]["verification_sha256"]
        == retry_request["verification_sha256"]
        and retry_callback["payload"]["iteration"] == 1
        and retry_callback["payload"]["step_id"] == "root-cause",
        (retry_result.stderr, retry_callback),
    )

    reset_transport(worktree)
    missing_retry_verification = request(
        worktree,
        step_id="root-cause",
        iteration=1,
    )
    prepare(worktree, missing_retry_verification)
    missing_verification_result = run(worktree)
    check(
        "retry request without a verification packet fails closed",
        missing_verification_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "verification_sha256" in missing_verification_result.stderr,
        missing_verification_result.stderr,
    )

    reset_transport(worktree)
    first_pass_verification = request(
        worktree,
        verification_sha256=sha("unexpected-verification"),
    )
    prepare(worktree, first_pass_verification)
    first_pass_verification_result = run(worktree)
    check(
        "initial pass rejects a retry verification binding",
        first_pass_verification_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "verification_sha256" in first_pass_verification_result.stderr,
        first_pass_verification_result.stderr,
    )

    reset_transport(worktree)
    stale = request(worktree)
    prepare(worktree, stale, declared_head_sha="f" * 40)
    stale_result = run(worktree)
    check(
        "declared result HEAD must equal the exact current Git HEAD",
        stale_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "HEAD" in stale_result.stderr,
        stale_result.stderr,
    )

    reset_transport(worktree)
    bad_hash = request(worktree)
    prepare(worktree, bad_hash, declared_output_sha256=sha("wrong"))
    bad_hash_result = run(worktree)
    check(
        "declared output digest must equal the exact regular output file",
        bad_hash_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "digest" in bad_hash_result.stderr,
        bad_hash_result.stderr,
    )

    reset_transport(worktree)
    escaped = request(worktree, result_pointer="../result.json")
    write_json(worktree / REQUEST, escaped)
    escaped_result = run(worktree)
    check(
        "request and output pointers cannot escape the generic target repo",
        escaped_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "owner-relative" in escaped_result.stderr,
        escaped_result.stderr,
    )

    reset_transport(worktree)
    symlinked = request(worktree)
    write_json(worktree / REQUEST, symlinked)
    real_output = worktree / "real-output.md"
    real_output.write_text("evidence\n", encoding="utf-8")
    (worktree / str(symlinked["output_pointer"])).symlink_to(real_output)
    write_json(
        worktree / str(symlinked["result_pointer"]),
        {
            "schema_version": 1,
            "status": "complete",
            "output_sha256": sha_bytes(real_output.read_bytes()),
            "head_sha": git(worktree, "rev-parse", "HEAD"),
        },
    )
    symlink_result = run(worktree)
    check(
        "symlinked result or output evidence fails closed",
        symlink_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "symlink" in symlink_result.stderr,
        symlink_result.stderr,
    )
    (worktree / str(symlinked["output_pointer"])).unlink()
    real_output.unlink()

    reset_transport(worktree)
    cannot = request(worktree)
    prepare(worktree, cannot, status="cannot-reproduce")
    cannot_result = run(worktree)
    cannot_callback = json.loads(
        (worktree / OUTBOX).read_text(encoding="utf-8")
    )
    check(
        "reproduce may submit the typed cannot-reproduce outcome",
        cannot_result.returncode == 0
        and cannot_callback["payload"]["status"] == "cannot-reproduce",
        (cannot_result.stderr, cannot_callback),
    )

    reset_transport(worktree)
    wrong_phase = request(worktree, step_id="root-cause")
    prepare(worktree, wrong_phase, status="cannot-reproduce")
    wrong_phase_result = run(worktree)
    check(
        "cannot-reproduce is rejected for every later phase",
        wrong_phase_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "cannot-reproduce" in wrong_phase_result.stderr,
        wrong_phase_result.stderr,
    )

    reset_transport(worktree)
    unknown = request(worktree)
    unknown["extra"] = "drift"
    prepare(worktree, unknown)
    unknown_result = run(worktree)
    check(
        "unknown request fields fail closed",
        unknown_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "keys" in unknown_result.stderr,
        unknown_result.stderr,
    )

    reset_transport(worktree)
    custom_output = b"custom evidence\n"
    custom_request = {
        "schema_version": 1,
        "workflow_kind": "custom",
        "operation_id": "custom-step-0",
        "run_id": sha("custom-run")[:32],
        "parent_operation_id": "custom-parent",
        "lane_id": "custom-lane",
        "definition_sha256": sha("custom-definition"),
        "step_id": "design",
        "visit": 0,
        "input_schema": "approved-plan/v1",
        "input_sha256": sha("custom-input"),
        "input_head_sha": git(worktree, "rev-parse", "HEAD"),
        "prior_receipt_sha256": "",
        "output_schema": "approved-plan/v1",
        "allowed_outcomes": ["complete", "revise"],
        "result_pointer": ".task-pipeline/custom/00-design-result.json",
        "output_pointer": ".task-pipeline/custom/00-design-output.md",
    }
    custom_output_path = worktree / str(custom_request["output_pointer"])
    custom_result_path = worktree / str(custom_request["result_pointer"])
    custom_output_path.parent.mkdir(parents=True, exist_ok=True)
    custom_output_path.write_bytes(custom_output)
    write_json(
        custom_result_path,
        {
            "schema_version": 1,
            "status": "complete",
            "outcome": "revise",
            "output_sha256": sha_bytes(custom_output),
            "head_sha": git(worktree, "rev-parse", "HEAD"),
        },
    )
    write_json(worktree / REQUEST, custom_request)
    custom_result = run(worktree)
    custom_callback = json.loads((worktree / OUTBOX).read_text(encoding="utf-8"))
    check(
        "custom request publishes only a declared typed decision",
        custom_result.returncode == 0
        and custom_callback["payload"]["step_id"] == "design"
        and custom_callback["payload"]["visit"] == 0
        and custom_callback["payload"]["outcome"] == "revise"
        and "status" not in custom_callback["payload"],
        (custom_result.stderr, custom_callback),
    )

    (worktree / OUTBOX).unlink()
    invalid_custom = dict(custom_request)
    write_json(worktree / REQUEST, invalid_custom)
    invalid_result_payload = json.loads(custom_result_path.read_text(encoding="utf-8"))
    invalid_result_payload["outcome"] = "invented"
    write_json(custom_result_path, invalid_result_payload)
    invalid_custom_result = run(worktree)
    check(
        "custom outcome outside the frozen request fails closed",
        invalid_custom_result.returncode != 0
        and not (worktree / OUTBOX).exists()
        and "not allowed" in invalid_custom_result.stderr,
        invalid_custom_result.stderr,
    )


if failures:
    raise SystemExit(f"{len(failures)} pipeline submit test(s) failed")
