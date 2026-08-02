#!/usr/bin/env python3
"""Run one engineering-skill pressure case through a bounded Codex session.

The generic agent-evals facade grades the returned object.  This adapter keeps
expected assertion values out of the model prompt, supplies the exact local
skill contracts, and permits no product writes or interactive approvals.
"""

from __future__ import annotations

import argparse
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


class RunnerError(ValueError):
    """The pressure case or local runner boundary is invalid."""


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


def source_bundle(capability: str) -> str:
    sections = []
    for relative in SOURCES[capability]:
        path = ROOT / relative
        sections.append(f"## {relative}\n\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(sections)


def prompt_for(case: dict[str, Any]) -> str:
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
        f"{source_bundle(case['capability'])}\n"
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


def run(case: dict[str, Any], *, model: str, effort: str, timeout: float) -> dict[str, Any]:
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
            prompt_for(case),
        ]
        proc = subprocess.run(command, text=True, capture_output=True)
        if proc.returncode != 0:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="terra")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise RunnerError("timeout must be positive")
        model = load_config(ROOT).resolve_alias(args.model, "codex")["model"]
        case = load_case()
        result = run(case, model=model, effort=args.effort, timeout=args.timeout)
    except (RoutingError, RunnerError) as exc:
        print(f"engineering-eval-runner: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
