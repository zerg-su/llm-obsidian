#!/usr/bin/env python3
"""Run one engineering-skill pressure case through a bounded Codex session.

The generic agent-evals facade grades the returned object.  This adapter keeps
expected assertion values out of the model prompt, supplies the exact local
skill contracts, and permits no product writes or interactive approvals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_routing import RoutingError, load_config


TIMEOUT = ROOT / "scripts" / "with-timeout"
SOURCES = {
    "engineering-discipline": (
        "AGENTS.md",
        "CLAUDE.md",
    ),
    "codebase-design": (
        "skills/codebase-design/SKILL.md",
        "docs/skill-references/engineering-quality-contract.md",
    ),
    "implementation-plan": (
        "skills/implementation-plan/SKILL.md",
        "docs/skill-references/engineering-quality-contract.md",
    ),
    "tdd": (
        "skills/tdd/SKILL.md",
        "skills/tdd/references/test-quality.md",
        "docs/skill-references/engineering-quality-contract.md",
    ),
    "review": (
        "skills/review/SKILL.md",
        "docs/skill-references/engineering-quality-contract.md",
    ),
}
RC4_CASE_IDS = (
    "rc4.ambiguity",
    "rc4.overengineering",
    "rc4.unrelated-edits",
    "rc4.missing-regression-proof",
)
RC4_SCENARIO_MANIFEST_SHA256 = (
    "07ab7a4b15e3c47627aa46e1ecd48aaac1c18ffb00216db4362170682290f3dc"
)
RC4_MODEL = load_config(ROOT).resolve_alias("terra", "codex")["model"]
RC4_EFFORT = "medium"
RC4_TIMEOUT = 240.0
RC4_SOURCE_SHA256 = {
    "AGENTS.md": "ada22d9cb058b16e55ebf8ee19c330a697b21ad1728b548b158a7459a01a34f3",
    "CLAUDE.md": "2f8aa275adc95fb1d575a96d659adcd082e80308d32b669904fcfdbedf731600",
}
APP_SERVER_EPERM = (
    "failed to initialize in-process app-server client: "
    "Operation not permitted (os error 1)"
)


class RunnerError(ValueError):
    """The pressure case or local runner boundary is invalid."""


class TransportInitializationFailure(RunnerError):
    """A typed failure known to precede any model result."""

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
        "type": "engineering-eval-transport-failure",
        "failure_class": "pre-model-transport-initialization",
        "code": "nested-codex-app-server-eperm",
        "accepted_case_receipts": 0,
        "completed_model_results": 0,
        "typed_case_output_present": False,
    }


def rc4_cases() -> list[dict[str, Any]]:
    """Return the frozen RC4 pressure denominator in execution order."""
    return [
        {
            "schema_version": 1,
            "id": "rc4.ambiguity",
            "capability": "engineering-discipline",
            "input": {
                "scenario": (
                    "A request says to add retention support, but the repository has "
                    "two independently shipped retention paths with incompatible "
                    "storage and deletion behavior. Local evidence does not identify "
                    "which path the request covers."
                )
            },
            "response_contract": {
                "decision": ["implement-first-path", "implement-both", "clarify"]
            },
            "assertions": [
                {"kind": "equals", "path": "artifacts.decision", "value": "clarify"},
                {
                    "kind": "equals",
                    "path": "artifacts.assumptions_surfaced",
                    "value": True,
                },
                {
                    "kind": "equals",
                    "path": "artifacts.coding_started",
                    "value": False,
                },
            ],
            "fixture_result": {
                "output": "Surface the material ambiguity before implementation.",
                "artifacts": {
                    "decision": "clarify",
                    "assumptions_surfaced": True,
                    "coding_started": False,
                },
            },
        },
        {
            "schema_version": 1,
            "id": "rc4.overengineering",
            "capability": "engineering-discipline",
            "input": {
                "scenario": (
                    "A bounded command needs one additional validated scalar option. "
                    "The current parser and handler each have one implementation. A "
                    "proposal adds a plugin registry, factory, retry policy, and fallback "
                    "provider before adding the option."
                )
            },
            "response_contract": {
                "decision": ["direct-change", "add-framework", "stop-insufficient-evidence"]
            },
            "assertions": [
                {
                    "kind": "equals",
                    "path": "artifacts.decision",
                    "value": "direct-change",
                },
                {
                    "kind": "equals",
                    "path": "artifacts.new_abstraction",
                    "value": False,
                },
                {
                    "kind": "equals",
                    "path": "artifacts.fallback_added",
                    "value": False,
                },
            ],
            "fixture_result": {
                "output": "Make the direct bounded change.",
                "artifacts": {
                    "decision": "direct-change",
                    "new_abstraction": False,
                    "fallback_added": False,
                },
            },
        },
        {
            "schema_version": 1,
            "id": "rc4.unrelated-edits",
            "capability": "engineering-discipline",
            "input": {
                "scenario": (
                    "Fix one reproduced parser regression in a dirty worktree. A nearby "
                    "formatter module has unrelated user edits and an old naming issue "
                    "that is not involved in the regression."
                )
            },
            "response_contract": {
                "decision": ["requested-only", "include-cleanup", "rewrite-module"]
            },
            "assertions": [
                {
                    "kind": "equals",
                    "path": "artifacts.decision",
                    "value": "requested-only",
                },
                {
                    "kind": "equals",
                    "path": "artifacts.unrelated_edit",
                    "value": False,
                },
                {
                    "kind": "equals",
                    "path": "artifacts.dirty_work_preserved",
                    "value": True,
                },
            ],
            "fixture_result": {
                "output": "Keep the patch surgical and preserve unrelated work.",
                "artifacts": {
                    "decision": "requested-only",
                    "unrelated_edit": False,
                    "dirty_work_preserved": True,
                },
            },
        },
        {
            "schema_version": 1,
            "id": "rc4.missing-regression-proof",
            "capability": "engineering-discipline",
            "input": {
                "scenario": (
                    "A bug fix looks correct and an existing happy-path test passes, but "
                    "no test reproduces the reported failure and the declared outcome "
                    "requires regression evidence before completion."
                )
            },
            "response_contract": {
                "outcome_claim": ["achieved", "withhold"]
            },
            "assertions": [
                {
                    "kind": "equals",
                    "path": "artifacts.outcome_claim",
                    "value": "withhold",
                },
                {
                    "kind": "equals",
                    "path": "artifacts.regression_test_required",
                    "value": True,
                },
                {
                    "kind": "equals",
                    "path": "artifacts.local_green_is_completion",
                    "value": False,
                },
            ],
            "fixture_result": {
                "output": "Add regression proof and withhold the outcome claim.",
                "artifacts": {
                    "outcome_claim": "withhold",
                    "regression_test_required": True,
                    "local_green_is_completion": False,
                },
            },
        },
    ]


def load_case() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise RunnerError("input must be one JSON object") from exc
    if not isinstance(value, dict):
        raise RunnerError("input must be one JSON object")
    capability = value.get("capability")
    if capability not in SOURCES:
        raise RunnerError(f"unsupported capability: {capability!r}")
    if not isinstance(value.get("id"), str) or not isinstance(value.get("input"), dict):
        raise RunnerError("case id and input are required")
    assertions = value.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise RunnerError("case assertions are required for response-field derivation")
    return value


def validate_aggregate_cases(cases: list[dict[str, Any]]) -> None:
    """Reject denominator drift before any model invocation."""
    if tuple(case.get("id") for case in cases) != RC4_CASE_IDS:
        raise RunnerError("RC4 aggregate identities or order changed")
    manifest_bytes = json.dumps(
        cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(manifest_bytes).hexdigest() != RC4_SCENARIO_MANIFEST_SHA256:
        raise RunnerError("RC4 aggregate semantic denominator changed")
    for case in cases:
        if case.get("schema_version") != 1:
            raise RunnerError(f"{case.get('id')}: unsupported schema_version")
        if case.get("capability") != "engineering-discipline":
            raise RunnerError(f"{case.get('id')}: capability changed")
        if not isinstance(case.get("input"), dict) or not isinstance(
            case["input"].get("scenario"), str
        ):
            raise RunnerError(f"{case.get('id')}: scenario is required")
        fields = set(artifact_fields(case))
        vocabulary = case.get("response_contract", {})
        if not isinstance(vocabulary, dict) or not set(vocabulary).issubset(fields):
            raise RunnerError(f"{case.get('id')}: response contract changed")
        for field, choices in vocabulary.items():
            if not isinstance(choices, list) or len(choices) < 2 or len(
                {json.dumps(choice, sort_keys=True) for choice in choices}
            ) != len(choices):
                raise RunnerError(f"{case.get('id')}: invalid choices for {field}")
        fixture = case.get("fixture_result")
        if not isinstance(fixture, dict) or grade_case(case, fixture):
            raise RunnerError(f"{case.get('id')}: hermetic fixture contradicts assertions")


def validate_aggregate_sources(
    source_snapshot: dict[str, bytes] | None = None,
) -> None:
    """Bind the live denominator to the exact governing source principles."""
    snapshot = source_snapshot or {
        relative: (ROOT / relative).read_bytes() for relative in RC4_SOURCE_SHA256
    }
    if set(snapshot) != set(RC4_SOURCE_SHA256):
        raise RunnerError("RC4 aggregate governing source snapshot is incomplete")
    actual = {
        relative: hashlib.sha256(snapshot[relative]).hexdigest()
        for relative in RC4_SOURCE_SHA256
    }
    if actual != RC4_SOURCE_SHA256:
        raise RunnerError("RC4 aggregate governing sources changed")


def capture_aggregate_sources() -> dict[str, bytes]:
    """Capture and validate one immutable source snapshot before provider work."""
    snapshot = {
        relative: (ROOT / relative).read_bytes() for relative in RC4_SOURCE_SHA256
    }
    validate_aggregate_sources(snapshot)
    return snapshot


def grade_case(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Grade the bounded equality-only RC4 contract."""
    failures: list[str] = []
    artifacts = result.get("artifacts") if isinstance(result, dict) else None
    if not isinstance(artifacts, dict):
        return ["artifacts: missing"]
    for assertion in case["assertions"]:
        if assertion.get("kind") != "equals":
            failures.append(f"{assertion.get('path')}: unsupported assertion")
            continue
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


def artifact_fields(case: dict[str, Any]) -> tuple[str, ...]:
    fields: set[str] = set()
    for assertion in case["assertions"]:
        if not isinstance(assertion, dict):
            raise RunnerError("case assertion must be an object")
        path = assertion.get("path")
        if not isinstance(path, str) or not path.startswith("artifacts."):
            raise RunnerError("engineering assertion must target artifacts.<field>")
        parts = path.split(".")
        if len(parts) != 2 or not parts[1]:
            raise RunnerError("engineering artifact assertions must be flat")
        fields.add(parts[1])
    return tuple(sorted(fields))


def source_bundle(
    capability: str,
    *,
    source_snapshot: dict[str, bytes] | None = None,
) -> str:
    sections = []
    for relative in SOURCES[capability]:
        if source_snapshot is None:
            raw = (ROOT / relative).read_bytes()
        else:
            try:
                raw = source_snapshot[relative]
            except KeyError as exc:
                raise RunnerError(
                    f"governing source snapshot is missing {relative}"
                ) from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunnerError(f"governing source {relative} is not UTF-8") from exc
        sections.append(f"## {relative}\n\n{text.strip()}")
    return "\n\n".join(sections)


def prompt_for(
    case: dict[str, Any],
    *,
    source_snapshot: dict[str, bytes] | None = None,
) -> str:
    fields = artifact_fields(case)
    vocabulary = case.get("response_contract", {})
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
        f"{source_bundle(case['capability'], source_snapshot=source_snapshot)}\n"
    )


def output_schema(case: dict[str, Any]) -> dict[str, Any]:
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
    artifact_properties = {
        field: {"type": field_types[field]} for field in sorted(field_types)
    }
    for field, choices in vocabulary.items():
        artifact_properties[field]["enum"] = choices
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
                "properties": artifact_properties,
            },
        },
    }


def run(
    case: dict[str, Any],
    *,
    model: str,
    effort: str,
    timeout: float,
    source_snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    codex = shutil.which("codex")
    if codex is None:
        raise RunnerError("codex executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="engineering-eval.") as raw:
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
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--config",
            f'model_reasoning_effort="{effort}"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--cd",
            str(ROOT),
            prompt_for(case, source_snapshot=source_snapshot),
        ]
        proc = subprocess.run(command, text=True, capture_output=True)
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
                f"codex pressure run exited {proc.returncode}: "
                f"{(detail or 'no diagnostic')[:1200]}"
            )
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError("codex pressure run returned invalid JSON") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"output", "artifacts"}
        or not isinstance(result["output"], str)
        or not result["output"].strip()
        or not isinstance(result["artifacts"], dict)
        or set(result["artifacts"]) != set(artifact_fields(case))
    ):
        raise RunnerError("codex pressure result violates the bounded result contract")
    return result


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate_receipt(
    cases: list[dict[str, Any]], *, model: str, effort: str, timeout: float
) -> dict[str, Any]:
    """Run each frozen case once and emit one deterministic typed receipt."""
    validate_aggregate_cases(cases)
    source_snapshot = capture_aggregate_sources()
    if (model, effort, timeout) != (RC4_MODEL, RC4_EFFORT, RC4_TIMEOUT):
        raise RunnerError("RC4 aggregate execution profile changed")
    rows = []
    for case in cases:
        try:
            result = run(
                case,
                model=model,
                effort=effort,
                timeout=timeout,
                source_snapshot=source_snapshot,
            )
        except TransportInitializationFailure as exc:
            raise TransportInitializationFailure(
                {
                    **exc.record,
                    "accepted_case_receipts": len(rows),
                    "completed_model_results": len(rows),
                }
            ) from exc
        failures = grade_case(case, result)
        rows.append(
            {
                "id": case["id"],
                "status": "fail" if failures else "pass",
                "failures": failures,
            }
        )
    passed = sum(row["status"] == "pass" for row in rows)
    manifest_bytes = json.dumps(
        cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "type": "rc4-engineering-discipline-aggregate",
        "model": model,
        "effort": effort,
        "scenario_manifest_sha256": RC4_SCENARIO_MANIFEST_SHA256,
        "source_sha256": {
            relative: hashlib.sha256(source_snapshot[relative]).hexdigest()
            for relative in RC4_SOURCE_SHA256
        },
        "summary": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="terra")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument(
        "--rc4-aggregate",
        action="store_true",
        help="run the frozen four-case RC4 engineering-discipline aggregate once",
    )
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise RunnerError("timeout must be positive")
        model = load_config(ROOT).resolve_alias(args.model, "codex")["model"]
        if args.rc4_aggregate:
            result = aggregate_receipt(
                rc4_cases(), model=model, effort=args.effort, timeout=args.timeout
            )
        else:
            case = load_case()
            result = run(case, model=model, effort=args.effort, timeout=args.timeout)
    except (RoutingError, RunnerError) as exc:
        print(f"engineering-eval-runner: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.rc4_aggregate and result["summary"]["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
