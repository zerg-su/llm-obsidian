#!/usr/bin/env python3
"""Hermetic tests for the dynamic release acceptance contract."""

from __future__ import annotations

import ast
import hashlib
import json
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release-acceptance.py"
WORKSPACE_SCRIPT = ROOT / "scripts" / "acceptance-workspace-supervisor.py"
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_fingerprints import cell_metadata, read_manifest


module_spec = importlib.util.spec_from_file_location("release_acceptance_test", SCRIPT)
assert module_spec is not None and module_spec.loader is not None
release_acceptance = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(release_acceptance)

workspace_spec = importlib.util.spec_from_file_location(
    "acceptance_workspace_test", WORKSPACE_SCRIPT
)
assert workspace_spec is not None and workspace_spec.loader is not None
acceptance_workspaces = importlib.util.module_from_spec(workspace_spec)
workspace_spec.loader.exec_module(acceptance_workspaces)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


result = run("check")
check("dynamic coverage", result.returncode == 0 and "runtimes" in result.stdout, result.stderr)

acceptance_workspaces.validate_limits(5, 5)
for workspaces, jobs in ((6, 5), (5, 6), (0, 5), (5, 0)):
    try:
        acceptance_workspaces.validate_limits(workspaces, jobs)
    except acceptance_workspaces.WorkspaceAcceptanceError:
        pass
    else:
        check("workspace acceptance limits are code-owned", False)
check("workspace acceptance limits are code-owned", True)

timeout_args = acceptance_workspaces.parser().parse_args([
    "run", "--phase", "final", "--runner", "fixture", "--report", "fixture.json",
    "--cell-timeout", "0",
])
try:
    acceptance_workspaces.supervise(timeout_args)
except acceptance_workspaces.WorkspaceAcceptanceError as exc:
    check("workspace acceptance rejects non-positive timeouts", "positive" in str(exc))
else:
    check("workspace acceptance rejects non-positive timeouts", False)

partition_fixture = [
    {"skill": skill, "runtime": runtime}
    for skill in ("a", "b", "c", "d", "e", "f")
    for runtime in (("claude", "codex") if skill in {"a", "b", "c"} else ("claude",))
]
partitions = acceptance_workspaces.partition_skills(partition_fixture, 5)
flattened = [skill for partition in partitions for skill in partition]
weights = {
    skill: sum(1 for row in partition_fixture if row["skill"] == skill)
    for skill in flattened
}
loads = [sum(weights[skill] for skill in partition) for partition in partitions]
check(
    "workspace shards assign each skill once and balance pending cells",
    len(partitions) == 5
    and sorted(flattened) == ["a", "b", "c", "d", "e", "f"]
    and len(flattened) == len(set(flattened))
    and max(loads) - min(loads) <= 1,
)
check(
    "workspace supervisor forwards the code-owned effort override",
    "LLM_OBSIDIAN_ACCEPTANCE_EFFORT" in acceptance_workspaces.FORWARDED_ENV,
)

with tempfile.TemporaryDirectory(prefix="acceptance-workspace-merge.") as raw:
    tmp = Path(raw)
    merge_row = {
        "schema_version": 1,
        "phase": "final",
        "skill": "fixture",
        "runtime": "codex",
        "scenario": "workspace-shard",
        "expected": "bounded",
    }
    merge_metadata = {
        "cell_fingerprint": "f" * 64,
        "dependencies": ["skills/fixture/SKILL.md"],
        "generation": "codex:5.6",
    }
    merge_result = {
        **merge_row,
        "verdict": "pass",
        "model": "gpt-5.6-terra",
        "effort": "medium",
        "actual": "bounded",
        "cleanup": "clean",
        "evidence": "proof",
    }
    merge_commit = "a" * 40
    merge_fingerprint = "b" * 64
    decorated = release_acceptance.decorate_result(
        merge_result, merge_metadata, commit=merge_commit
    )
    merge_context = {
        "rows": [merge_row],
        "metadata": {release_acceptance.row_key(merge_row): merge_metadata},
        "commit": merge_commit,
        "fingerprint": merge_fingerprint,
        "orchestration_version": 1,
        "prior": [],
    }
    shard_report = tmp / "shard.json"
    release_acceptance.write_json(
        release_acceptance.report_payload(
            "final",
            [decorated],
            planned_total=1,
            commit=merge_commit,
            fingerprint=merge_fingerprint,
            orchestration_contract_version=1,
        ),
        shard_report,
        announce=False,
    )
    merged = acceptance_workspaces.merge_shards(
        [(shard_report, {"fixture"})], context=merge_context, phase="final"
    )
    check(
        "workspace shard merge validates operation evidence",
        len(merged) == 1 and merged[0]["row_integrity_sha256"] == decorated["row_integrity_sha256"],
    )
    try:
        acceptance_workspaces.merge_shards(
            [(shard_report, {"another-skill"})], context=merge_context, phase="final"
        )
    except acceptance_workspaces.WorkspaceAcceptanceError as exc:
        check("workspace shard cannot emit unassigned skills", "unassigned" in str(exc))
    else:
        check("workspace shard cannot emit unassigned skills", False)

    conflicting = dict(decorated)
    conflicting["evidence"] = "different proof"
    conflicting["row_integrity_sha256"] = release_acceptance.integrity_sha256(
        conflicting, merge_metadata["cell_fingerprint"], conflicting["provenance"]
    )
    conflict_report = tmp / "conflict.json"
    release_acceptance.write_json(
        release_acceptance.report_payload(
            "final",
            [conflicting],
            planned_total=1,
            commit=merge_commit,
            fingerprint=merge_fingerprint,
            orchestration_contract_version=1,
        ),
        conflict_report,
        announce=False,
    )
    try:
        acceptance_workspaces.merge_shards(
            [(shard_report, {"fixture"}), (conflict_report, {"fixture"})],
            context=merge_context,
            phase="final",
        )
    except acceptance_workspaces.WorkspaceAcceptanceError as exc:
        check("workspace shard conflicts fail closed", "conflicting" in str(exc))
    else:
        check("workspace shard conflicts fail closed", False)

with tempfile.TemporaryDirectory(prefix="release-acceptance-test.") as raw:
    tmp = Path(raw)
    unsafe_manifest = tmp / "unsafe-acceptance-cells.toml"
    unsafe_manifest.write_text(
        (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8").replace(
            '"CHANGELOG.md"', '"../CHANGELOG.md"', 1
        ),
        encoding="utf-8",
    )
    result = run("--manifest", str(unsafe_manifest), "check")
    check(
        "non-behavioral paths reject parent traversal",
        result.returncode == 3 and "unsafe acceptance dependency" in result.stderr,
        result.stderr,
    )
    broadened_exact_manifest = tmp / "broadened-exact-acceptance-cells.toml"
    broadened_exact_manifest.write_text(
        (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8").replace(
            '  "CHANGELOG.md",',
            '  "CHANGELOG.md",\n  "scripts/live-acceptance-runner.py",',
            1,
        ),
        encoding="utf-8",
    )
    result = run("--manifest", str(broadened_exact_manifest), "check")
    check(
        "non-behavioral exact paths are code-owned",
        result.returncode == 3 and "code-owned allowlist" in result.stderr,
        result.stderr,
    )
    unsafe_prefix_manifest = tmp / "unsafe-prefix-acceptance-cells.toml"
    unsafe_prefix_manifest.write_text(
        (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8").replace(
            'non_behavioral_prefixes = ["tests/"]',
            'non_behavioral_prefixes = ["skills/"]',
            1,
        ),
        encoding="utf-8",
    )
    result = run("--manifest", str(unsafe_prefix_manifest), "check")
    check(
        "non-behavioral prefixes are restricted to tests",
        result.returncode == 3 and "only tests/" in result.stderr,
        result.stderr,
    )
    dead_generation_route_manifest = tmp / "dead-generation-route-acceptance-cells.toml"
    dead_generation_route_manifest.write_text(
        (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8").replace(
            'include = ["runtimes.codex", "runtimes.claude"]',
            'include = ["runtimes.codex", "runtimes.claude"]\nexclude = ["roles.review.codex"]',
            1,
        ),
        encoding="utf-8",
    )
    result = run("--manifest", str(dead_generation_route_manifest), "check")
    check(
        "generation routes reject dead or unknown configuration",
        result.returncode == 3 and "only the two production runtime defaults" in result.stderr,
        result.stderr,
    )
    matrix = tmp / "matrix.json"
    result = run("matrix", "--phase", "baseline", "--output", str(matrix))
    data = json.loads(matrix.read_text(encoding="utf-8"))
    skills = sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md"))
    check("matrix exit", result.returncode == 0, result.stderr)
    check("matrix complete", len(data["rows"]) == len(skills) * 2)
    check("matrix runtime parity", {row["runtime"] for row in data["rows"]} == {"claude", "codex"})
    check(
        "gitignore and shared support affect every live cell",
        all(
            ".gitignore" in row["dependencies"]
            and "scripts/cmux_agent_support.py" in row["dependencies"]
            and "scripts/lifecycle_telemetry.py" in row["dependencies"]
            and "scripts/task_contract.py" in row["dependencies"]
            and "scripts/turn_telemetry.py" in row["dependencies"]
            for row in data["rows"]
        ),
    )
    check(
        "acceptance orchestration is absent from cell behavior fingerprints",
        all(
            "scripts/release-acceptance.py" not in row["dependencies"]
            and "scripts/acceptance_fingerprints.py" not in row["dependencies"]
            and "config/acceptance-cells.toml" not in row["dependencies"]
            for row in data["rows"]
        ),
    )
    lifecycle_scenarios = {"cmux-lifecycle", "dispatch-review-reap"}
    check(
        "supervisor and lifecycle invalidate only their scenarios",
        all(
            ("scripts/cmux_agent_supervisor.py" in row["dependencies"])
            == (row["scenario"] in lifecycle_scenarios)
            and ("scripts/cmux_surface_lifecycle.py" in row["dependencies"])
            == (row["scenario"] in lifecycle_scenarios)
            for row in data["rows"]
        ),
    )
    daily_agent_dependencies = {
        ".claude-plugin/plugin.json",
        ".codex/agents/daily-summarizer.toml",
        "agents/daily-summarizer.md",
    }
    check(
        "bounded daily agents invalidate only daily-summary cells",
        all(
            daily_agent_dependencies.issubset(set(row["dependencies"]))
            == (row["scenario"] == "daily-summary")
            for row in data["rows"]
        ),
    )

    # A path declared by any cell is no longer protected by unknown-path
    # invalidation. Therefore every cell must include the complete local import
    # closure of each Python dependency it declares. Modules declared nowhere
    # intentionally remain fail-closed as unknown product paths.
    declared_by_any_cell = {
        rel for row in data["rows"] for rel in row["dependencies"]
    }

    def declared_local_imports(rel: str) -> set[str]:
        source = ROOT / rel
        if source.suffix != ".py" or not source.is_file():
            return set()
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=rel)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".", 1)[0])
        imported: set[str] = set()
        for name in names:
            candidates = {
                candidate
                for candidate in declared_by_any_cell
                if Path(candidate).suffix == ".py" and Path(candidate).stem == name
            }
            sibling = (source.parent / f"{name}.py")
            if sibling.is_file():
                candidates.add(sibling.relative_to(ROOT).as_posix())
            scripts_module = ROOT / "scripts" / f"{name}.py"
            if scripts_module.is_file():
                candidates.add(scripts_module.relative_to(ROOT).as_posix())
            imported.update(candidates & declared_by_any_cell)
        return imported

    incomplete_closures: list[str] = []
    for row in data["rows"]:
        dependencies = set(row["dependencies"])
        pending = list(dependencies)
        visited: set[str] = set()
        while pending:
            importer = pending.pop()
            if importer in visited:
                continue
            visited.add(importer)
            for imported in declared_local_imports(importer):
                if imported not in dependencies:
                    incomplete_closures.append(
                        f"{row['skill']}/{row['runtime']}: {importer} -> {imported}"
                    )
                elif imported not in visited:
                    pending.append(imported)
    check(
        "declared Python dependencies contain their transitive local import closure",
        not incomplete_closures,
        "\n".join(sorted(incomplete_closures)),
    )

    fragment_root = tmp / "fragment-root"
    (fragment_root / "evals/acceptance").mkdir(parents=True)
    (fragment_root / "skills/close").mkdir(parents=True)
    (fragment_root / "skills/clarify").mkdir(parents=True)
    for rel in ("evals/acceptance/skills.json", "evals/acceptance/scenarios.json"):
        (fragment_root / rel).write_bytes((ROOT / rel).read_bytes())
    (fragment_root / "skills/close/SKILL.md").write_text("# Close\n", encoding="utf-8")
    (fragment_root / "skills/clarify/SKILL.md").write_text("# Clarify\n", encoding="utf-8")
    fragment_manifest = read_manifest(ROOT)
    fixed_environment = {"os": "test", "os_release": "1", "architecture": "test", "cmux": "1", "claude": "1", "codex": "1"}
    fixed_generations = {
        "claude": {"model": "opus", "generation": "claude:opus-4.8"},
        "codex": {"model": "gpt-5.6-sol", "generation": "codex:5.6"},
    }
    close_row = next(row for row in data["rows"] if row["skill"] == "close" and row["runtime"] == "claude")
    clarify_row = next(row for row in data["rows"] if row["skill"] == "clarify" and row["runtime"] == "claude")
    close_before = cell_metadata(fragment_root, fragment_manifest, close_row, environment=fixed_environment, generations=fixed_generations)
    clarify_before = cell_metadata(fragment_root, fragment_manifest, clarify_row, environment=fixed_environment, generations=fixed_generations)
    fragment_skills_path = fragment_root / "evals/acceptance/skills.json"
    fragment_skills = json.loads(fragment_skills_path.read_text(encoding="utf-8"))
    fragment_skills["skills"]["close"]["fixture"] += " changed"
    fragment_skills_path.write_text(json.dumps(fragment_skills), encoding="utf-8")
    close_after = cell_metadata(fragment_root, fragment_manifest, close_row, environment=fixed_environment, generations=fixed_generations)
    clarify_after = cell_metadata(fragment_root, fragment_manifest, clarify_row, environment=fixed_environment, generations=fixed_generations)
    check(
        "shared registry hashes only the exact cell fragment",
        close_before["cell_fingerprint"] != close_after["cell_fingerprint"]
        and clarify_before["cell_fingerprint"] == clarify_after["cell_fingerprint"],
    )

    spec = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
    missing = tmp / "missing.json"
    missing_data = json.loads(json.dumps(spec))
    missing_data["skills"].pop(skills[0])
    missing.write_text(json.dumps(missing_data), encoding="utf-8")
    result = run("--spec", str(missing), "check")
    check("missing skill rejected", result.returncode == 3 and "coverage mismatch" in result.stderr)

    stale = tmp / "stale.json"
    stale_data = json.loads(json.dumps(spec))
    stale_data["skills"]["stale-skill"] = {
        "scenario": "stale",
        "expected": "Never exists.",
        "fixture": "Run the impossible stale fixture.",
    }
    stale.write_text(json.dumps(stale_data), encoding="utf-8")
    result = run("--spec", str(stale), "check")
    check("stale skill rejected", result.returncode == 3 and "coverage mismatch" in result.stderr)

    no_fixture = tmp / "no-fixture.json"
    no_fixture_data = json.loads(json.dumps(spec))
    no_fixture_data["skills"][skills[0]].pop("fixture")
    no_fixture.write_text(json.dumps(no_fixture_data), encoding="utf-8")
    result = run("--spec", str(no_fixture), "check")
    check("missing live fixture rejected", result.returncode == 3 and "invalid live fixture" in result.stderr)

    scenarios = json.loads((ROOT / "evals/acceptance/scenarios.json").read_text(encoding="utf-8"))
    missing_scenario = tmp / "missing-scenario.json"
    missing_scenario_data = json.loads(json.dumps(scenarios))
    missing_scenario_data["scenarios"].pop(next(iter(missing_scenario_data["scenarios"])))
    missing_scenario.write_text(json.dumps(missing_scenario_data), encoding="utf-8")
    result = run("--scenarios", str(missing_scenario), "check")
    check("missing scenario rejected", result.returncode == 3 and "scenario coverage mismatch" in result.stderr)

    runner = tmp / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high',"
        "'actual':'bounded pass','cleanup':'none','evidence':'fixture'}))\n",
        encoding="utf-8",
    )
    checkpoint_rows = [
        {
            "phase": "final", "skill": name, "runtime": "claude",
            "scenario": "checkpoint", "expected": "bounded",
        }
        for name in ("first", "later-prior")
    ]
    later_prior = {
        **checkpoint_rows[1], "verdict": "pass", "model": "fixture",
        "effort": "high", "actual": "prior", "cleanup": "ok", "evidence": "ok",
    }
    checkpoints: list[list[dict[str, object]]] = []
    checkpoint_result = release_acceptance.run_matrix(
        checkpoint_rows,
        [sys.executable, str(runner)],
        5,
        prior=[later_prior],
        checkpoint=lambda value: checkpoints.append(value),
    )
    check(
        "checkpoint preserves valid rows later in matrix order",
        len(checkpoints) == 1
        and [row["skill"] for row in checkpoints[0]] == ["first", "later-prior"]
        and [row["skill"] for row in checkpoint_result] == ["first", "later-prior"],
    )
    parallel_markers = tmp / "parallel-markers"
    parallel_runner = tmp / "parallel-runner.py"
    parallel_runner.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "row=json.load(sys.stdin); root=Path(sys.argv[1]); root.mkdir(exist_ok=True)\n"
        "(root / row['skill']).write_text('started\\n')\n"
        "deadline=time.monotonic()+3\n"
        "while time.monotonic()<deadline and len(list(root.glob('*'))) < 5: time.sleep(0.02)\n"
        "if len(list(root.glob('*'))) < 5: raise SystemExit(9)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'medium',"
        "'actual':'parallel','cleanup':'ok','evidence':'five started'}))\n",
        encoding="utf-8",
    )
    parallel_rows = [
        {
            "schema_version": 1, "phase": "final", "skill": f"parallel-{index}", "runtime": "claude",
            "scenario": "parallel", "expected": "bounded",
        }
        for index in range(5)
    ]
    parallel_checkpoints: list[list[dict[str, object]]] = []
    parallel_result = release_acceptance.run_matrix(
        parallel_rows,
        [sys.executable, str(parallel_runner), str(parallel_markers)],
        5,
        checkpoint=lambda value: parallel_checkpoints.append(value),
        jobs=5,
    )
    check(
        "five acceptance workers execute concurrently with canonical checkpoints",
        len(parallel_result) == 5
        and all(item["verdict"] == "pass" for item in parallel_result)
        and len(parallel_checkpoints) == 5
        and [item["skill"] for item in parallel_checkpoints[-1]]
        == [item["skill"] for item in parallel_rows],
    )
    try:
        release_acceptance.run_matrix([], [sys.executable, str(runner)], 5, jobs=6)
    except release_acceptance.AcceptanceError as exc:
        check("parallel worker limit is bounded at five per workspace", "between 1 and 5" in str(exc))
    else:
        check("parallel worker limit is bounded at five per workspace", False)
    report = tmp / "report.json"
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("green report", result.returncode == 0 and payload["summary"]["failed"] == 0, result.stderr)
    check("schema-2 evidence", payload["schema_version"] == 2)
    check(
        "rows carry content-addressed proof",
        all(
            row["cell_fingerprint"] and row["dependencies"] and row["generation"]
            and row["row_integrity_sha256"] and row["reason"] == "executed"
            for row in payload["rows"]
        ),
    )
    check("expected retained", all(row["expected"] for row in payload["rows"]))
    check("duration measured", all(row["duration_seconds"] is not None for row in payload["rows"]))
    check("completed report is checkpoint-compatible", payload["summary"]["complete"] is True and payload["summary"]["pending"] == 0)

    runner.write_text("raise SystemExit(99)\n", encoding="utf-8")
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    check("matching completed report resumes without rerunning cells", result.returncode == 0, result.stderr)
    payload = json.loads(report.read_text(encoding="utf-8"))
    check(
        "reuse reason and evidence age are visible",
        all(row["reason"] == "reused-identical" and row["evidence_age_seconds"] >= 0 for row in payload["rows"]),
    )

    payload["rows"][0]["row_integrity_sha256"] = "0" * 64
    report.write_text(json.dumps(payload), encoding="utf-8")
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check(
        "tampered row reruns while intact rows reuse",
        result.returncode == 1
        and payload["summary"]["verdicts"]["blocked"] == 1
        and sum(row["reason"] == "reused-identical" for row in payload["rows"]) == len(payload["rows"]) - 1,
        result.stderr,
    )

    stale_report = json.loads(report.read_text(encoding="utf-8"))
    stale_report["source_commit"] = "0" * 40
    report.write_text(json.dumps(stale_report), encoding="utf-8")
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check(
        "unknown prior commit reruns instead of guessing",
        result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] == len(payload["rows"]),
        result.stderr,
    )

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("evidence-free pass blocks", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] > 0)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'n-a','decision':''}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("invalid n-a blocks", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] > 0)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'n-a','decision':'approved architecture boundary'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("approved n-a is neutral", result.returncode == 0 and payload["summary"]["accepted"] == len(payload["rows"]))

    result = run(
        "run", "--restart", "--phase", "final", "--runner", str(tmp / "missing-runner"),
        "--timeout", "1", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("missing runner blocks every row", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] == len(payload["rows"]))

    selected_report = tmp / "selected-report.json"
    runner.write_text(
        "import json,sys\nrow=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high','actual':'ok','cleanup':'ok','evidence':'ok'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--phase", "final", "--skill", skills[0],
        "--runner", f"{sys.executable} {runner}", "--timeout", "5", "--report", str(selected_report),
    )
    selected_payload = json.loads(selected_report.read_text(encoding="utf-8"))
    check(
        "skill selection executes two cells and stays visibly partial",
        result.returncode == 0
        and selected_payload["summary"]["completed"] == 2
        and selected_payload["summary"]["pending"] == len(skills) * 2 - 2
        and {row["skill"] for row in selected_payload["rows"]} == {skills[0]},
        result.stderr,
    )
    result = run(
        "run", "--restart", "--phase", "final", "--skill", skills[0],
        "--runner", f"{sys.executable} {runner}", "--timeout", "5", "--report", str(selected_report),
    )
    check("restart cannot silently become partial", result.returncode == 3 and "full matrix" in result.stderr)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high',"
        "'actual':'token=abcdefghijk','cleanup':'none','evidence':'fixture'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("credential text sanitized", result.returncode == 0 and all("abcdefghijk" not in row["actual"] for row in payload["rows"]))

    interrupt_marker = tmp / "runner-interrupted"
    interrupt_runner = tmp / "interrupt-runner.py"
    interrupt_runner.write_text(
        "import json,signal,sys,time\n"
        "from pathlib import Path\n"
        "marker=Path(sys.argv[1]); row=json.load(sys.stdin)\n"
        "token=row['skill']+'-'+row['runtime']\n"
        "started=marker.with_name(marker.name+'.'+token+'.started')\n"
        "cleaned=marker.with_name(marker.name+'.'+token+'.cleaned')\n"
        "def stop(_signum,_frame): cleaned.write_text('cleanup-complete\\n'); raise SystemExit(130)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "started.write_text('started\\n')\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    interrupted = subprocess.Popen(
        [
            sys.executable, str(SCRIPT), "run", "--restart", "--phase", "final",
            "--runner", f"{sys.executable} {interrupt_runner} {interrupt_marker}",
            "--timeout", "60", "--jobs", "5", "--report", str(report),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(list(tmp.glob("runner-interrupted.*.started"))) < 5:
        time.sleep(0.05)
    started_markers = list(tmp.glob("runner-interrupted.*.started"))
    check("five interrupt fixtures start", len(started_markers) == 5)
    os.kill(interrupted.pid, signal.SIGINT)
    _stdout, interrupt_stderr = interrupted.communicate(timeout=15)
    cleaned_markers = list(tmp.glob("runner-interrupted.*.cleaned"))
    check("matrix interrupt reaches every active runner cleanup", len(cleaned_markers) == len(started_markers) == 5)
    check("matrix interrupt exits without traceback", interrupted.returncode == 130 and "Traceback" not in interrupt_stderr)

    incremental = tmp / "incremental"
    (incremental / "skills/demo-a").mkdir(parents=True)
    (incremental / "skills/demo-b").mkdir(parents=True)
    (incremental / "config").mkdir()
    (incremental / "evals").mkdir()
    (incremental / "scripts").mkdir()
    (incremental / "tests").mkdir()
    (incremental / ".gitignore").write_text(".vault-meta/\n", encoding="utf-8")
    (incremental / "skills/demo-a/SKILL.md").write_text("# Demo A\n", encoding="utf-8")
    (incremental / "skills/demo-b/SKILL.md").write_text("# Demo B\n", encoding="utf-8")
    (incremental / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (incremental / "config/model-routing.toml").write_text(
        (ROOT / "config/model-routing.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    manifest = incremental / "config/acceptance-cells.toml"
    (incremental / "scripts/acceptance_fingerprints.py").write_text("# orchestration\n", encoding="utf-8")
    (incremental / "scripts/acceptance-workspace-supervisor.py").write_text("# orchestration\n", encoding="utf-8")
    (incremental / "scripts/live-acceptance-runner.py").write_text("# orchestration\n", encoding="utf-8")
    (incremental / "scripts/release-acceptance.py").write_text("# orchestration\n", encoding="utf-8")
    manifest.write_text(
        "schema_version = 1\nrunner_contract_version = 2\norchestration_contract_version = 1\n"
        "non_behavioral_paths = [\"CHANGELOG.md\"]\n"
        "non_behavioral_prefixes = [\"tests/\"]\n"
        "orchestration_dependencies = [\"config/acceptance-cells.toml\", \"scripts/acceptance_fingerprints.py\", \"scripts/acceptance-workspace-supervisor.py\", \"scripts/live-acceptance-runner.py\", \"scripts/release-acceptance.py\"]\n"
        "global_dependencies = [\".gitignore\", \"config/model-routing.toml\"]\n"
        "[model_generations]\n\"gpt-5.6-sol\" = \"codex:5.6\"\n"
        "opus = \"claude:opus-4.8\"\nfable = \"claude:fable\"\n"
        "[generation_routes]\ninclude = [\"runtimes.codex\", \"runtimes.claude\"]\n"
        "[scenarios.demo]\ndependencies = []\n",
        encoding="utf-8",
    )
    mini_spec = incremental / "evals/skills.json"
    mini_spec.write_text(json.dumps({
        "schema_version": 1,
        "skills": {
            name: {"scenario": "demo", "expected": "bounded", "fixture": "bounded fixture"}
            for name in ("demo-a", "demo-b")
        },
    }), encoding="utf-8")
    mini_scenarios = incremental / "evals/scenarios.json"
    mini_scenarios.write_text(json.dumps({"schema_version": 1, "scenarios": {"demo": {}}}), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=incremental, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=incremental, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=incremental, check=True)
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=incremental, check=True)
    invocation_log = tmp / "incremental-invocations.jsonl"
    incremental_runner = tmp / "incremental-runner.py"
    incremental_runner.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "row=json.load(sys.stdin)\n"
        "with Path(sys.argv[1]).open('a') as f: f.write(json.dumps([row['skill'],row['runtime']])+'\\n')\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high','actual':'ok','cleanup':'ok','evidence':'ok'}))\n",
        encoding="utf-8",
    )
    incremental_report = incremental / ".vault-meta/acceptance/latest-live.json"

    def incremental_run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(SCRIPT), "--root", str(incremental), "--spec", str(mini_spec),
            "--scenarios", str(mini_scenarios), "--manifest", str(manifest), "run",
            "--phase", "final", "--runner", f"{sys.executable} {incremental_runner} {invocation_log}",
            "--timeout", "5", "--report", str(incremental_report),
        ], text=True, capture_output=True, check=False)

    result = incremental_run()
    check("incremental fixture starts green", result.returncode == 0, result.stderr)
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "skills/demo-a/SKILL.md").write_text("# Demo A\n\nchanged\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "change one cell"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "cross-commit reuse reruns only affected skill cells",
        result.returncode == 0 and {item[0] for item in calls} == {"demo-a"} and len(calls) == 2,
        result.stderr,
    )
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "CHANGELOG.md").write_text("# Changelog\n\nRelease metadata only.\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "release metadata"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "exact non-behavioral path reuses every unchanged cell",
        result.returncode == 0 and calls == [],
        result.stderr,
    )
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "CHANGELOG.md").write_text("# Changelog\n\nUncommitted metadata only.\n", encoding="utf-8")
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "dirty exact non-behavioral path still reuses valid cells",
        result.returncode == 0 and calls == [],
        result.stderr,
    )
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "uncommitted metadata becomes committed"], cwd=incremental, check=True)
    invocation_log.write_text("", encoding="utf-8")
    test_only = incremental / "tests/test_only.py"
    test_only.write_text("# no product behavior\n", encoding="utf-8")
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "dirty tests prefix is non-behavioral",
        result.returncode == 0 and calls == [],
        result.stderr,
    )
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "test-only change"], cwd=incremental, check=True)
    invocation_log.write_text("", encoding="utf-8")
    orchestration_script = incremental / "scripts/release-acceptance.py"
    orchestration_script.write_text("# orchestration-only change\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "orchestration-only change"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "compatible orchestration change reuses live evidence",
        result.returncode == 0 and calls == [],
        result.stderr,
    )
    legacy_report = json.loads(incremental_report.read_text(encoding="utf-8"))
    for row in legacy_report["rows"]:
        row["dependencies"] = sorted(
            set(row["dependencies"]) | {"scripts/live-acceptance-runner.py"}
        )
        row["cell_fingerprint"] = hashlib.sha256(
            (row["cell_fingerprint"] + ":legacy-global-runner").encode("utf-8")
        ).hexdigest()
        row["row_integrity_sha256"] = release_acceptance.integrity_sha256(
            row, row["cell_fingerprint"], row["provenance"]
        )
    incremental_report.write_text(json.dumps(legacy_report), encoding="utf-8")
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "scripts/live-acceptance-runner.py").write_text(
        "# compatible orchestration migration\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "move runner out of cell behavior"],
        cwd=incremental, check=True,
    )
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    migrated = json.loads(incremental_report.read_text(encoding="utf-8"))
    check(
        "legacy global runner dependency migrates without rerunning unaffected cells",
        result.returncode == 0
        and calls == []
        and all(
            row["reason"] == "reused-compatible-orchestration-migration"
            for row in migrated["rows"]
        ),
        result.stderr,
    )
    report_data = json.loads(incremental_report.read_text(encoding="utf-8"))
    report_data["orchestration_contract_version"] = 999
    incremental_report.write_text(json.dumps(report_data), encoding="utf-8")
    result = incremental_run()
    check(
        "incompatible orchestration version fails closed",
        result.returncode == 3 and "incompatible orchestration contract" in result.stderr,
        result.stderr,
    )
    report_data["orchestration_contract_version"] = 1
    incremental_report.write_text(json.dumps(report_data), encoding="utf-8")
    invocation_log.write_text("", encoding="utf-8")
    dirty_unknown = incremental / "dirty-unknown.txt"
    dirty_unknown.write_text("not committed\n", encoding="utf-8")
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "same-commit dirty unknown path blocks without recording evidence",
        result.returncode == 3 and calls == [] and "committed behavioral state" in result.stderr,
        result.stderr,
    )
    dirty_unknown.unlink()
    (incremental / "skills/demo-b/SKILL.md").write_text("# Demo B\n\nchanged after evidence\n", encoding="utf-8")
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "dirty declared dependency blocks before testing committed HEAD",
        result.returncode == 3 and calls == [],
        result.stderr,
    )
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "commit previously dirty dependency"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "committed formerly dirty dependency reruns its exact cells",
        result.returncode == 0 and {item[0] for item in calls} == {"demo-b"} and len(calls) == 2,
        result.stderr,
    )
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "unknown.txt").write_text("unknown dependency\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "unknown dependency"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check("unknown changed path reruns every cell", result.returncode == 0 and len(calls) == 4, result.stderr)

print("\nAll release acceptance tests passed.")
