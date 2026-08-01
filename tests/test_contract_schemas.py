#!/usr/bin/env python3
"""Keep published JSON Schemas aligned with executable contract enums."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import research_contract
import review_contract
import review_resolution
import wiki_summary_contract
import daily_contract
import task_contract
from harness.custom_pipelines import CUSTOM_SPEC_VERSION, REVIEW_MODES


def load(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


for path in sorted((ROOT / "schemas").glob("*.schema.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["$schema"].endswith("2020-12/schema")
    assert data["type"] == "object"
    print(f"OK   parses {path.name}")

summary = load("wiki-summary-v1.schema.json")
assert set(summary["properties"]["type"]["enum"]) == wiki_summary_contract.TYPES
review = load("review-v1.schema.json")
assert set(review["properties"]["verdict"]["enum"]) == review_contract.VERDICTS
assert set(review["properties"]["mode"]["enum"]) == review_contract.MODES
task_meta = load("task-meta-v2.schema.json")
assert task_meta["properties"]["vault_root"] == {"type": "string", "minLength": 1}
task_types = task_meta["properties"]["reap_policy"]["properties"]["allowed_types"]["items"]["enum"]
assert set(task_types) == task_contract.SUMMARY_TYPES
assert task_meta["properties"]["forbidden_actions"]["const"] == task_contract.FORBIDDEN_ACTIONS
watchdog_props = task_meta["properties"]["watchdog_policy"]["properties"]
assert watchdog_props["poll_seconds"]["minimum"] == 5
assert watchdog_props["alert_after_seconds"]["maximum"] == 14400
task_meta_v3 = load("task-meta-v3.schema.json")
assert task_meta_v3["properties"]["version"] == {"const": 3}
assert {"project_id", "task_id"} <= set(task_meta_v3["required"])
assert task_meta_v3["properties"]["forbidden_actions"]["const"] == task_contract.FORBIDDEN_ACTIONS
assert task_meta_v3["additionalProperties"] is False
review_v3 = task_meta_v3["properties"]["review_policy"]
assert review_v3["additionalProperties"] is False
assert set(review_v3["required"]) == {
    "mode",
    "cross_model",
    "runtime",
    "model",
    "effort",
    "max_verify_iterations",
    "verification_profile",
    "verification_profile_sha256",
    "auto_resolve_severities",
    "escalate_severities",
}
assert set(review_v3["properties"]["mode"]["enum"]) == task_contract.REVIEW_MODES
assert review_v3["allOf"] == [
    {
        "if": {"properties": {"mode": {"const": "simple"}}},
        "then": {"properties": {"max_verify_iterations": {"const": 1}}},
    },
    {
        "if": {"properties": {"mode": {"const": "deep"}}},
        "then": {"properties": {"max_verify_iterations": {"const": 2}}},
    },
    {
        "if": {"properties": {"mode": {"const": "skip"}}},
        "then": {
            "properties": {
                "cross_model": {"const": False},
                "runtime": {"const": ""},
                "model": {"const": ""},
                "effort": {"const": ""},
                "max_verify_iterations": {"const": 0},
            }
        },
    },
]
task_meta_v4 = load("task-meta-v4.schema.json")
assert task_meta_v4["properties"]["version"] == {"const": 4}
assert {"project_id", "task_id"} <= set(task_meta_v4["required"])
assert task_meta_v4["properties"]["forbidden_actions"]["const"] == task_contract.FORBIDDEN_ACTIONS
assert task_meta_v4["additionalProperties"] is False
review_v4 = task_meta_v4["properties"]["review_policy"]
assert review_v4["additionalProperties"] is False
assert set(review_v4["required"]) == task_contract.REVIEW_POLICY_V4_FIELDS
assert "auto_resolve_severities" not in review_v4["properties"]
assert "escalate_severities" not in review_v4["properties"]
assert set(review_v4["properties"]["mode"]["enum"]) == task_contract.REVIEW_MODES
assert review_v4["allOf"] == review_v3["allOf"]
review_resolution_v1 = load("review-resolution-v1.schema.json")
resolution_items = review_resolution_v1["$defs"]["resolution"]
assert set(resolution_items["properties"]["disposition"]["enum"]) == (
    review_resolution.DISPOSITIONS
)
assert "attempted" not in resolution_items["properties"]["disposition"]["enum"]
pipeline_spec = load("pipeline-spec-v1.schema.json")
assert pipeline_spec["properties"]["schema_version"] == {"const": CUSTOM_SPEC_VERSION}
assert set(pipeline_spec["properties"]["review_mode"]["enum"]) == REVIEW_MODES
assert pipeline_spec["additionalProperties"] is False
assert pipeline_spec["properties"]["steps"]["maxItems"] == 8
assert pipeline_spec["properties"]["context_pointers"]["items"] == {
    "$ref": "#/$defs/contextPointer"
}
research = load("research-artifact-v2.schema.json")
source_props = research["properties"]["sources"]["items"]["properties"]
assert set(source_props["source_class"]["enum"]) == research_contract.SOURCE_CLASSES
assert "clean_markdown" not in source_props
assert source_props["content_path"]["pattern"] == "^sources/[^/]+\\.md$"
research_result = load("research-result-v2.schema.json")
result_props = research_result["properties"]["artifact"]["properties"]
assert result_props["kind"]["const"] == "cited-markdown"
assert result_props["path"]["const"] == "answer.md"
daily = load("daily-evidence-v1.schema.json")
daily_item_props = daily["properties"]["items"]["items"]["properties"]
daily_session_props = daily["properties"]["session_map"]["items"]["properties"]
assert set(daily_item_props["kind"]["enum"]) == daily_contract.EVIDENCE_KINDS
assert set(daily_session_props["runtime"]["enum"]) == daily_contract.SESSION_RUNTIMES
print("OK   executable enums match schemas")

print("\nAll contract schema tests passed.")
