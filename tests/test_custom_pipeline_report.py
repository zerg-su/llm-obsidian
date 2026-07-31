#!/usr/bin/env python3
"""Content-free promotion reporting for repeated custom definitions."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "scripts" / "custom-pipeline-report.py"
spec = importlib.util.spec_from_file_location("custom_pipeline_report", path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(definition: str, terminal: str, attention: str = "none") -> dict:
    return {
        "op": "compiled-pipeline",
        "identifiers": {
            "compiler_outcome": "custom-resolved",
            "definition_sha": definition,
            "terminal_category": terminal,
            "attention_category": attention,
        },
    }


with tempfile.TemporaryDirectory(prefix="custom-report.") as raw:
    events = Path(raw) / "pipeline-events.jsonl"
    rows = [
        event("a" * 64, "complete"),
        event("a" * 64, "complete"),
        event("a" * 64, "complete"),
        event("b" * 64, "complete"),
        event("b" * 64, "attention", "callback-timeout"),
        {"op": "compiled-pipeline", "identifiers": {"intent": "private"}},
    ]
    events.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    result = module.report(events)
    assert result["promotion_candidate_count"] == 1
    assert result["fingerprint_count"] == 2
    assert result["pipelines"][0] == {
        "definition_sha256": "a" * 64,
        "completed_runs": 3,
        "attention_runs": 0,
        "promotion_candidate": True,
    }
    assert "private" not in json.dumps(result)

print("All custom pipeline report tests passed.")
