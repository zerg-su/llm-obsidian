#!/usr/bin/env python3
"""Unit contracts for code-quality cohesion signals."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "code_quality_audit", ROOT / "scripts" / "code-quality-audit.py"
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


with tempfile.TemporaryDirectory(prefix="code-quality-audit.") as raw:
    root = Path(raw)
    package = root / "scripts" / "harness"
    package.mkdir(parents=True)
    cohesive = package / "cohesive.py"
    cohesive.write_text(
        "def decide(value):\n"
        "    if value:\n"
        "        return 'yes'\n"
        "    return 'no'\n",
        encoding="utf-8",
    )
    rows = audit.inspect_tree(package)
    errors, warnings = audit.classify(rows)
    assert not errors and not warnings
    assert rows[0].functions[0].branch_points == 1

    giant = package / "giant.py"
    giant.write_text(
        "def run(value):\n" + "    value += 1\n" * audit.FUNCTION_HARD_LINES,
        encoding="utf-8",
    )
    errors, _warnings = audit.classify(audit.inspect_tree(package))
    assert any("giant.py:1:run" in error and "hard limit" in error for error in errors)
    signals = audit.blocking_signals(audit.inspect_tree(package))
    baseline_path = root / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hotspots": {
                    identity: {
                        "max_value": value,
                        "owner": "test-owner",
                        "evidence": "test fixture",
                    }
                    for identity, value in signals.items()
                },
            }
        ),
        encoding="utf-8",
    )
    baseline = audit.load_baseline(baseline_path)
    assert audit.ratchet_failures(signals, baseline) == []
    identity = next(iter(signals))
    assert "blocker grew" in audit.ratchet_failures(
        {**signals, identity: signals[identity] + 1}, baseline
    )[0]
    assert "new unowned blocker" in audit.ratchet_failures(
        {**signals, "file-lines:scripts/harness/new.py": 1001}, baseline
    )[0]
    assert "stale blocker baseline" in audit.ratchet_failures({}, baseline)[0]

print("code quality audit unit contracts passed")
