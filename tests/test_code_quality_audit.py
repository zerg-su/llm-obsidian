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

assert audit.effective_baseline_path(scan=None, baseline=None) == (
    ROOT / "config" / "code-quality-baseline.json"
)
assert audit.effective_baseline_path(
    scan=ROOT / "scripts" / "harness", baseline=None
) is None

owned = audit.owned_source_paths(ROOT)
assert ROOT / "scripts" / "code-quality-audit.py" in owned
assert ROOT / "hooks" / "run-hook.py" in owned
assert ROOT / "scripts" / "review-runner.py" in owned
assert ROOT / "scripts" / "live-acceptance-runner.py" in owned
assert ROOT / "skills" / "tdd" / "agents" / "openai.yaml" not in owned
assert (
    ROOT / "evals" / "paired-v2.6.0" / "fix" / "test_label_normalizer.py"
    not in owned
)
assert not any("references/upstream-skills" in str(path) for path in owned)
assert {
    path.relative_to(ROOT).parts[0] for path in owned
} == set(audit.OWNED_PYTHON_ROOTS)


with tempfile.TemporaryDirectory(prefix="code-quality-owned.") as raw:
    root = Path(raw)
    included = {
        root / ".claude" / "hooks" / "capture.py",
        root / "evals" / "case" / "runner.py",
        root / "hooks" / "run.py",
        root / "prototypes" / "spike.py",
        root / "scripts" / "tool.py",
        root / "skills" / "demo" / "scripts" / "helper.py",
    }
    excluded = {
        root / "evals" / "case" / "test_runner.py",
        root / "hooks" / "run_test.py",
        root / "scripts" / "tests" / "helper.py",
        root / "scripts" / "conftest.py",
        root / "skills" / "demo" / "references" / "pinned.py",
        root / "references" / "upstream-skills" / "verify.py",
    }
    for path in included | excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
    first = audit.owned_source_paths(root)
    second = audit.owned_source_paths(root)
    assert first == tuple(sorted(included))
    assert second == first


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


with tempfile.TemporaryDirectory(prefix="rc1-authority-audit.") as raw:
    root = Path(raw)
    incident = root / "scripts" / "task_review_flow.py"
    recovery = (
        root / "scripts" / "harness" / "workflows" / "review_gate_recovery.py"
    )
    quiet = root / "scripts" / "harness" / "provider_events.py"
    for path in (incident, recovery, quiet):
        path.parent.mkdir(parents=True, exist_ok=True)
    incident.write_text(
        "OPERATION_ID = '75ff063d-d388-46a7-915d-0eed20392da4'\n"
        "DECISION = 'Classified as an eligible repository-owned incident'\n"
        "STORE = '/private/tmp/llm-obsidian-265-simulator/state'\n",
        encoding="utf-8",
    )
    recovery.write_text(
        "def restart_for_boundary():\n"
        "    return {'awaiting_resolution': True}\n",
        encoding="utf-8",
    )
    quiet.write_text("def observe():\n    return 'read-only'\n", encoding="utf-8")

    authority = audit.audit_rc1_active_authority(root)
    assert authority["schema_version"] == 1
    assert authority["production_loc"] == 7
    assert authority["production_files"] == [
        {
            "path": "scripts/harness/provider_events.py",
            "loc": 2,
        },
        {
            "path": "scripts/harness/workflows/review_gate_recovery.py",
            "loc": 2,
        },
        {
            "path": "scripts/task_review_flow.py",
            "loc": 3,
        },
    ]
    assert [item["symbol"] for item in authority["writable_authorities"]] == [
        "restart_for_boundary"
    ]
    assert {item["kind"] for item in authority["incident_literals"]} == {
        "decision-prose",
        "operation-uuid",
        "operator-local-path",
    }
    assert all(
        "value" not in item for item in authority["incident_literals"]
    ), "audit output must remain content-free"

print("code quality audit unit contracts passed")
