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
import wiki_summary_contract
import daily_contract
import task_contract


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
research = load("research-artifact-v1.schema.json")
source_props = research["properties"]["sources"]["items"]["properties"]
assert set(source_props["source_class"]["enum"]) == research_contract.SOURCE_CLASSES
daily = load("daily-evidence-v1.schema.json")
daily_item_props = daily["properties"]["items"]["items"]["properties"]
daily_session_props = daily["properties"]["session_map"]["items"]["properties"]
assert set(daily_item_props["kind"]["enum"]) == daily_contract.EVIDENCE_KINDS
assert set(daily_session_props["runtime"]["enum"]) == daily_contract.SESSION_RUNTIMES
print("OK   executable enums match schemas")

print("\nAll contract schema tests passed.")
