#!/usr/bin/env python3
"""Build and validate one governed SplitManifest preview without child effects."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import ContractError, SHA256_RE
from harness.split_contracts import manifest_to_dict, preview_from_dict
from harness.split_validation import validate_manifest


def _request_text(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def _current_parent(value: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "plan_sha256",
        "outcome_contract_sha256",
    }:
        raise ContractError("current_parent fields changed")
    plan = value.get("plan_sha256")
    outcome = value.get("outcome_contract_sha256")
    if (
        not isinstance(plan, str)
        or not SHA256_RE.fullmatch(plan)
        or not isinstance(outcome, str)
        or not SHA256_RE.fullmatch(outcome)
    ):
        raise ContractError("current_parent requires exact lowercase sha256 values")
    return plan, outcome


def preview_payload(request: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    preview = preview_from_dict(request)
    plan_sha256, outcome_sha256 = _current_parent(request.get("current_parent", {}))
    pipelines = request.get("registered_pipelines")
    if (
        not isinstance(pipelines, list)
        or not pipelines
        or len(set(pipelines)) != len(pipelines)
        or any(not isinstance(item, str) or not item for item in pipelines)
    ):
        raise ContractError("registered_pipelines must be a non-empty unique list")
    result = validate_manifest(
        preview.manifest,
        current_plan_sha256=plan_sha256,
        current_outcome_contract_sha256=outcome_sha256,
        registered_pipelines=pipelines,
    )
    payload = {
        "schema_version": 1,
        "mode": "preview",
        "effects": preview.effect_counts,
        "validation": {
            "accepted": result.accepted,
            "issues": [
                {"code": issue.code, "message": issue.message}
                for issue in result.issues
            ],
        },
        "manifest": manifest_to_dict(preview.manifest),
    }
    return payload, 0 if result.accepted else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "request",
        help="exact preview request JSON path, or - for stdin",
    )
    args = parser.parse_args()
    try:
        request = json.loads(_request_text(args.request))
        if not isinstance(request, dict):
            raise ContractError("split preview request must be a JSON object")
        payload, exit_code = preview_payload(request)
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        payload = {
            "schema_version": 1,
            "mode": "preview",
            "effects": {
                "dispatches": 0,
                "provider_calls": 0,
                "surfaces_created": 0,
                "worktrees_created": 0,
            },
            "validation": {
                "accepted": False,
                "issues": [{"code": "contract-invalid", "message": str(exc)}],
            },
            "manifest": None,
        }
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
