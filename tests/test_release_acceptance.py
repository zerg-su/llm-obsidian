#!/usr/bin/env python3
"""Hermetic tests for the dynamic release acceptance contract."""

from __future__ import annotations

import ast
import hashlib
import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release-acceptance.py"
WORKSPACE_SCRIPT = ROOT / "scripts" / "acceptance-workspace-supervisor.py"
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_fingerprints import (
    behavior_fragment_hashes,
    cell_dependency_hashes,
    cell_metadata,
    command_version,
    generation_snapshot,
    live_runner_behavior_sha256,
    launch_generations,
    read_manifest,
    runtime_script_references,
    verify_dependency_lock,
)
from acceptance_dependencies import (
    _dynamic_repo_prefixes,
    closure as dependency_closure,
    read_lock,
)
from acceptance.sandbox import acceptance_seed_sha256


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
    values = list(args)
    if "run" in values and "--jobs" not in values:
        values.extend(["--jobs", "5"])
    return subprocess.run([sys.executable, str(SCRIPT), *values], text=True, capture_output=True)


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


result = run("check")
check("dynamic coverage", result.returncode == 0 and "runtimes" in result.stdout, result.stderr)

acceptance_workspaces.validate_limits(10, 5)
for workspaces, jobs in ((11, 5), (10, 6), (0, 5), (5, 0)):
    try:
        acceptance_workspaces.validate_limits(workspaces, jobs)
    except acceptance_workspaces.WorkspaceAcceptanceError:
        pass
    else:
        check("workspace acceptance limits are code-owned", False)
check("workspace acceptance limits are code-owned", True)

workspace_defaults = acceptance_workspaces.parser().parse_args([
    "run", "--phase", "final", "--runner", "fixture", "--report", "fixture.json",
])
check(
    "workspace acceptance defaults to two by five with exact UUID ownership",
    workspace_defaults.workspaces == 2
    and workspace_defaults.jobs_per_workspace == 5
    and acceptance_workspaces.WORKSPACE_ID.fullmatch("12345678-1234-1234-1234-123456789abc") is not None,
)

workspace_uuid = "12345678-1234-1234-1234-123456789abc"
workspace_calls: list[list[str]] = []
real_workspace_run = acceptance_workspaces.subprocess.run


def fake_workspace_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    workspace_calls.append(command)
    if command[3:5] == ["workspace", "create"]:
        return subprocess.CompletedProcess(command, 0, stdout="OK workspace:7\n", stderr="")
    if command[3:5] == ["workspace", "list"]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "workspaces": [
                    {"ref": "workspace:7", "id": workspace_uuid},
                    {"ref": "workspace:8", "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                ]
            }),
            stderr="",
        )
    raise AssertionError(f"unexpected cmux call: {command}")


acceptance_workspaces.subprocess.run = fake_workspace_run
try:
    resolved_workspace = acceptance_workspaces.create_workspace(
        ROOT, "Acceptance test", ["python3", "worker.py"]
    )
finally:
    acceptance_workspaces.subprocess.run = real_workspace_run
check(
    "workspace creation resolves current cmux short ref to exact UUID",
    resolved_workspace == workspace_uuid
    and workspace_calls[0][3:5] == ["workspace", "create"]
    and workspace_calls[1] == [
        "cmux", "--id-format", "both", "workspace", "list", "--json"
    ],
)

cleanup_calls: list[list[str]] = []


def fake_unresolvable_workspace_run(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    cleanup_calls.append(command)
    if command[3:5] == ["workspace", "create"]:
        return subprocess.CompletedProcess(command, 0, stdout="OK workspace:9\n", stderr="")
    if command[3:5] == ["workspace", "list"]:
        return subprocess.CompletedProcess(command, 0, stdout='{"workspaces": []}', stderr="")
    if command[:3] == ["cmux", "workspace", "close"]:
        return subprocess.CompletedProcess(command, 0, stdout="OK workspace:9\n", stderr="")
    raise AssertionError(f"unexpected cmux call: {command}")


acceptance_workspaces.subprocess.run = fake_unresolvable_workspace_run
try:
    try:
        acceptance_workspaces.create_workspace(ROOT, "Acceptance test", ["python3", "worker.py"])
    except acceptance_workspaces.WorkspaceAcceptanceError:
        pass
    else:
        check("unresolved created workspace is contained", False)
finally:
    acceptance_workspaces.subprocess.run = real_workspace_run
check(
    "unresolved created workspace is contained",
    cleanup_calls[-1] == ["cmux", "workspace", "close", "workspace:9"],
)

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
check(
    "workspace supervisor forwards its code-owned source pin",
    "LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT" in acceptance_workspaces.FORWARDED_ENV,
)
old_pin = os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT")
os.environ["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] = "f" * 40
try:
    shard_env = acceptance_workspaces.shard_environment("a" * 40)
finally:
    if old_pin is None:
        os.environ.pop("LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT", None)
    else:
        os.environ["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] = old_pin
check(
    "workspace supervisor cannot inherit a stale external source pin",
    shard_env["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] == "a" * 40,
)

with tempfile.TemporaryDirectory(prefix="acceptance-stable-cli.") as raw:
    command_root = Path(raw)
    shim_dir = command_root / "cmux-cli-shims" / "surface"
    native_dir = command_root / "native"
    shim_dir.mkdir(parents=True)
    native_dir.mkdir()
    shim = shim_dir / "claude"
    native = native_dir / "claude"
    shim.write_text("#!/bin/sh\necho shim-should-not-run\n", encoding="utf-8")
    native.write_text("#!/bin/sh\necho '2.1.206 (Claude Code)'\n", encoding="utf-8")
    shim.chmod(0o755)
    native.chmod(0o755)
    old_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join((str(shim_dir), str(native_dir)))
    try:
        stable_claude_version = command_version("claude")
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path
    check(
        "acceptance environment ignores ephemeral cmux CLI shims",
        stable_claude_version == "2.1.206 (Claude Code)",
    )

with tempfile.TemporaryDirectory(prefix="acceptance-generation-snapshot.") as raw:
    snapshot_root = Path(raw)
    snapshot_report = snapshot_root / ".vault-meta/acceptance/latest-live.json"
    snapshot_report.parent.mkdir(parents=True)
    snapshot_report.write_text(
        json.dumps({"summary": {"complete": True, "failed": 0}}),
        encoding="utf-8",
    )
    snapshot = {
        "schema_version": 1,
        "generations": {"claude": "claude:opus-4.8", "codex": "codex:5.6"},
    }
    refreshed = acceptance_workspaces.refresh_generation_snapshot(
        snapshot_root, snapshot_report, snapshot
    )
    check(
        "green sharded report refreshes model generations",
        refreshed
        and json.loads(
            (snapshot_report.parent / "model-generations.json").read_text(
                encoding="utf-8"
            )
        )
        == snapshot,
    )
    snapshot_report.write_text(
        json.dumps({"summary": {"complete": True, "failed": 1}}),
        encoding="utf-8",
    )
    check(
        "failed sharded report cannot refresh model generations",
        not acceptance_workspaces.refresh_generation_snapshot(
            snapshot_root, snapshot_report, snapshot
        ),
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
        "launch_model": "gpt-5.6-terra",
        "evidence_epoch": 3,
        "environment_sha256": "e" * 64,
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
        "evidence_epoch": 3,
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
        result.returncode == 3 and "only the two runtime defaults" in result.stderr,
        result.stderr,
    )
    invalid_environment_scope_manifest = tmp / "invalid-environment-scope.toml"
    invalid_environment_scope_manifest.write_text(
        (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8").replace(
            "environment_scope_version = 2",
            "environment_scope_version = true",
            1,
        ),
        encoding="utf-8",
    )
    result = run("--manifest", str(invalid_environment_scope_manifest), "check")
    check(
        "environment scope rejects booleans",
        result.returncode == 3 and "environment scope" in result.stderr,
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
        "behavioral runner is present while orchestration is absent from cell dependencies",
        all(
            "scripts/release-acceptance.py" not in row["dependencies"]
            and "scripts/acceptance_fingerprints.py" not in row["dependencies"]
            and "config/acceptance-cells.toml" not in row["dependencies"]
            and "config/acceptance-dependencies.lock.json" not in row["dependencies"]
            and "scripts/live-acceptance-runner.py" in row["dependencies"]
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
    runner_source = (ROOT / "scripts/acceptance/skill_adapters.py").read_text(encoding="utf-8")
    review_source = runner_source.replace(
        "never fabricate a finding to force a verify round",
        "never invent a finding to force a verify round",
    )
    review_row = next(
        row for row in data["rows"]
        if row["skill"] == "review-dispatch" and row["runtime"] == "codex"
    )
    ordinary_row = next(
        row for row in data["rows"]
        if row["skill"] == "clarify" and row["runtime"] == "codex"
    )
    check(
        "live review behavior invalidates only its scoped cells",
        review_source != runner_source
        and live_runner_behavior_sha256(ROOT, review_row, source_text=runner_source)
        != live_runner_behavior_sha256(ROOT, review_row, source_text=review_source)
        and live_runner_behavior_sha256(ROOT, ordinary_row, source_text=runner_source)
        == live_runner_behavior_sha256(ROOT, ordinary_row, source_text=review_source),
    )
    launcher_source = (ROOT / "scripts/acceptance/launchers.py").read_text(encoding="utf-8")
    common_source = launcher_source.replace(
        "OUTBOX_STABLE_SECONDS = 1.0", "OUTBOX_STABLE_SECONDS = 1.1"
    )
    check(
        "common live behavior invalidates every cell",
        common_source != launcher_source
        and live_runner_behavior_sha256(ROOT, review_row, source_text=launcher_source)
        != live_runner_behavior_sha256(ROOT, review_row, source_text=common_source)
        and live_runner_behavior_sha256(ROOT, ordinary_row, source_text=launcher_source)
        != live_runner_behavior_sha256(ROOT, ordinary_row, source_text=common_source),
    )

    dependency_lock = verify_dependency_lock(ROOT, read_manifest(ROOT))
    check(
        "generated code/data/registration dependency lock is current",
        dependency_lock == read_lock(ROOT),
    )
    check(
        "dynamic repo paths are detected and exactly declared fail-closed",
        _dynamic_repo_prefixes(ast.parse('path = ROOT / "scripts" / runtime_name')) == {"scripts/"}
        and dependency_lock["dynamic_dependency_prefixes"]
        == {"scripts/acceptance/contracts.py": ["skills/"]},
    )
    check(
        "every cell carries registration and adapter binding surfaces",
        all(
            {"hooks/hooks.json", ".claude/skill-rules.json", ".codex/config.toml"}
            .issubset(set(row["dependencies"]))
            and "scripts/acceptance/adapters.py" in row["dependencies"]
            and behavior_fragment_hashes(
                ROOT, read_manifest(ROOT), row["skill"], row["scenario"]
            )
            for row in data["rows"]
        ),
    )
    required_reap_edges = {
        "scripts/allocate-address.sh",
        "scripts/archive_task_reviews.py",
        "scripts/current-session-id.sh",
        "scripts/parse-wiki-summary.py",
        "scripts/reindex.py",
        "scripts/validate-vault.py",
        "scripts/vault-write.py",
    }
    check(
        "runtime edge detector covers the reviewed reap and callback subprocesses",
        required_reap_edges.issubset(set(dependency_closure(
            dependency_lock, ["scripts/reap-runner.py"]
        )))
        and "skills/review-send/scripts/send_review.py" in runtime_script_references(
            ROOT, "scripts/cmux_agent_supervisor.py"
        )
        and all(
            "skills/reap-send/scripts/send_reap.py" in row["dependencies"]
            for row in data["rows"]
            if row["scenario"] == "dispatch-review-reap"
        ),
    )

    fragment_root = tmp / "fragment-root"
    (fragment_root / "evals/acceptance").mkdir(parents=True)
    (fragment_root / "skills/close").mkdir(parents=True)
    (fragment_root / "skills/clarify").mkdir(parents=True)
    (fragment_root / "scripts").mkdir(parents=True)
    for rel in ("evals/acceptance/skills.json", "evals/acceptance/scenarios.json"):
        (fragment_root / rel).write_bytes((ROOT / rel).read_bytes())
    (fragment_root / "skills/close/SKILL.md").write_text("# Close\n", encoding="utf-8")
    (fragment_root / "skills/clarify/SKILL.md").write_text("# Clarify\n", encoding="utf-8")
    (fragment_root / "scripts/live-acceptance-runner.py").write_bytes(
        (ROOT / "scripts/live-acceptance-runner.py").read_bytes()
    )
    fragment_manifest = read_manifest(ROOT)
    fixed_environment = {"os": "test", "os_release": "1", "architecture": "test", "cmux": "1", "claude": "1", "codex": "1"}
    fixed_generations = {
        "claude": {"model": "opus", "generation": "claude:opus-4.8"},
        "codex": {"model": "gpt-5.6-sol", "generation": "codex:5.6"},
    }
    seed_copy = tmp / "acceptance-seed"
    shutil.copytree(ROOT / "evals" / "acceptance" / "seed", seed_copy)
    seed_before = acceptance_seed_sha256(seed_copy)
    seed_metadata_before = {
        release_acceptance.row_key(row): cell_metadata(
            ROOT, fragment_manifest, row,
            environment=fixed_environment, generations=fixed_generations,
            dependencies_override=[], include_live_runner_behavior=False,
            seed_root=seed_copy,
        )
        for row in data["rows"]
    }
    seed_target = seed_copy / ".vault-meta" / "last-fold-count.txt"
    seed_original = seed_target.read_bytes()
    seed_target.write_bytes(seed_original + b"1\n")
    seed_after = acceptance_seed_sha256(seed_copy)
    seed_metadata_after = {
        release_acceptance.row_key(row): cell_metadata(
            ROOT, fragment_manifest, row,
            environment=fixed_environment, generations=fixed_generations,
            dependencies_override=[], include_live_runner_behavior=False,
            seed_root=seed_copy,
        )
        for row in data["rows"]
    }
    seed_report = tmp / "seed-evidence.json"
    seed_rows = []
    for row in data["rows"]:
        result_row = {
            **row, "verdict": "pass", "model": "fixture", "effort": "medium",
            "actual": "ok", "cleanup": "ok", "evidence": "seed fixture",
        }
        seed_rows.append(release_acceptance.decorate_result(
            result_row, seed_metadata_before[release_acceptance.row_key(row)],
            commit="1" * 40,
        ))
    seed_report.write_text(json.dumps({
        "schema_version": 3, "evidence_epoch": 3,
        "phase": "baseline", "rows": seed_rows,
    }), encoding="utf-8")
    seed_reused = release_acceptance.load_resume_results(
        seed_report, data["rows"], phase="baseline", commit="2" * 40,
        metadata=seed_metadata_after, evidence_epoch=3,
    )
    seed_target.write_bytes(seed_original)
    seed_metadata_restored = {
        release_acceptance.row_key(row): cell_metadata(
            ROOT, fragment_manifest, row,
            environment=fixed_environment, generations=fixed_generations,
            dependencies_override=[], include_live_runner_behavior=False,
            seed_root=seed_copy,
        )
        for row in data["rows"]
    }
    check(
        "one canonical seed byte invalidates every cell and blocks stale reuse",
        seed_before != seed_after
        and not seed_reused
        and all(
            seed_metadata_before[key]["cell_fingerprint"]
            != seed_metadata_after[key]["cell_fingerprint"]
            and seed_metadata_before[key]["cell_fingerprint"]
            == seed_metadata_restored[key]["cell_fingerprint"]
            for key in seed_metadata_before
        ),
    )

    adapters_source = (ROOT / "scripts/acceptance/adapters.py").read_text(encoding="utf-8")
    rebound_adapters_source = adapters_source.replace(
        "dispatch_acceptance_fixture, dispatch_acceptance_proof,",
        "dispatch_acceptance_fixture, dispatch_acceptance_fixture as dispatch_acceptance_proof,",
    )
    check(
        "adapter re-export changes invalidate the common behavioral ABI",
        rebound_adapters_source != adapters_source
        and live_runner_behavior_sha256(ROOT, review_row, source_text=adapters_source)
        != live_runner_behavior_sha256(ROOT, review_row, source_text=rebound_adapters_source)
        and live_runner_behavior_sha256(ROOT, ordinary_row, source_text=adapters_source)
        != live_runner_behavior_sha256(ROOT, ordinary_row, source_text=rebound_adapters_source),
    )
    imported_source = runner_source.replace(
        "from .scenario_adapters import is_disposable_bookkeeping",
        "from .scenario_adapters import sandbox_cleanup_proof as is_disposable_bookkeeping",
    )
    check(
        "fragment hashes include module-level import bindings",
        imported_source != runner_source
        and behavior_fragment_hashes(
            ROOT, fragment_manifest, review_row["skill"], review_row["scenario"],
            source_overrides={"scripts/acceptance/skill_adapters.py": runner_source},
        )
        != behavior_fragment_hashes(
            ROOT, fragment_manifest, review_row["skill"], review_row["scenario"],
            source_overrides={"scripts/acceptance/skill_adapters.py": imported_source},
        )
        and behavior_fragment_hashes(
            ROOT, fragment_manifest, ordinary_row["skill"], ordinary_row["scenario"],
            source_overrides={"scripts/acceptance/skill_adapters.py": runner_source},
        )
        == behavior_fragment_hashes(
            ROOT, fragment_manifest, ordinary_row["skill"], ordinary_row["scenario"],
            source_overrides={"scripts/acceptance/skill_adapters.py": imported_source},
        ),
    )

    routing_root = tmp / "routing-root"
    (routing_root / "config").mkdir(parents=True)
    (routing_root / "scripts" / "acceptance").mkdir(parents=True)
    for name in ("scenario_adapters.py", "skill_adapters.py"):
        shutil.copy2(
            ROOT / "scripts" / "acceptance" / name,
            routing_root / "scripts" / "acceptance" / name,
        )
    routing_seed = routing_root / "seed"
    shutil.copytree(ROOT / "evals" / "acceptance" / "seed", routing_seed)
    routing_path = routing_root / "config" / "model-routing.toml"
    routing_source = (ROOT / "config" / "model-routing.toml").read_text(encoding="utf-8")
    routing_path.write_text(routing_source, encoding="utf-8")
    routing_row = review_row
    routing_base = cell_metadata(
        routing_root, fragment_manifest, routing_row,
        environment=fixed_environment, generations=fixed_generations,
        dependencies_override=["config/model-routing.toml"],
        include_live_runner_behavior=False, seed_root=routing_seed,
    )
    head, tail = routing_source.rsplit('model = "gpt-5.6-sol"', 1)
    routing_path.write_text(head + 'model = "gpt-5.6-terra"' + tail, encoding="utf-8")
    routing_same_generation = cell_metadata(
        routing_root, fragment_manifest, routing_row,
        environment=fixed_environment, generations=fixed_generations,
        dependencies_override=["config/model-routing.toml"],
        include_live_runner_behavior=False, seed_root=routing_seed,
    )
    routing_path.write_text(
        routing_source.replace('model = "fable"', 'model = "sonnet"'), encoding="utf-8"
    )
    routing_cross_generation = cell_metadata(
        routing_root, fragment_manifest, routing_row,
        environment=fixed_environment, generations=fixed_generations,
        dependencies_override=["config/model-routing.toml"],
        include_live_runner_behavior=False, seed_root=routing_seed,
    )
    check(
        "routing hashes ignore same-generation aliases but retain generation changes",
        routing_base["cell_fingerprint"] == routing_same_generation["cell_fingerprint"]
        and routing_base["cell_fingerprint"] != routing_cross_generation["cell_fingerprint"],
    )
    changed_claude_environment = {**fixed_environment, "claude": "2"}
    scoped_codex_row = next(
        row for row in data["rows"]
        if row["skill"] == "agenda" and row["runtime"] == "codex"
    )
    scoped_claude_row = next(
        row for row in data["rows"]
        if row["skill"] == "agenda" and row["runtime"] == "claude"
    )
    cross_runtime_row = next(
        row for row in data["rows"]
        if row["skill"] == "dispatch" and row["runtime"] == "codex"
    )
    terra_generations = {
        **fixed_generations,
        "codex": {"model": "gpt-5.6-terra", "generation": "codex:5.6"},
    }
    sol_metadata = cell_metadata(
        ROOT, fragment_manifest, scoped_codex_row,
        environment=fixed_environment, generations=fixed_generations,
    )
    terra_metadata = cell_metadata(
        ROOT, fragment_manifest, scoped_codex_row,
        environment=fixed_environment, generations=terra_generations,
    )
    check(
        "model aliases within one major generation share behavior but retain exact launch identity",
        sol_metadata["cell_fingerprint"] == terra_metadata["cell_fingerprint"]
        and sol_metadata["launch_model"] == "gpt-5.6-sol"
        and terra_metadata["launch_model"] == "gpt-5.6-terra",
    )
    resolved_test_routes = launch_generations(
        ROOT, fragment_manifest,
        overrides={"claude": "sonnet", "codex": "gpt-5.6-terra"},
    )
    check(
        "fingerprints resolve the actual acceptance launch overrides",
        resolved_test_routes == {
            "claude": {"model": "sonnet", "generation": "claude:sonnet"},
            "codex": {"model": "gpt-5.6-terra", "generation": "codex:5.6"},
        },
    )
    original_acceptance_codex = os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_CODEX_MODEL")
    os.environ["LLM_OBSIDIAN_ACCEPTANCE_CODEX_MODEL"] = "gpt-5.6-terra"
    try:
        production_snapshot = generation_snapshot(ROOT, fragment_manifest)
    finally:
        if original_acceptance_codex is None:
            os.environ.pop("LLM_OBSIDIAN_ACCEPTANCE_CODEX_MODEL", None)
        else:
            os.environ["LLM_OBSIDIAN_ACCEPTANCE_CODEX_MODEL"] = original_acceptance_codex
    check(
        "production generation drift scan ignores cheaper acceptance aliases",
        set(production_snapshot) == {"schema_version", "generations"}
        and production_snapshot["generations"]["codex"] == "codex:5.6",
    )
    scoped_before = {
        release_acceptance.row_key(row): cell_metadata(
            ROOT, fragment_manifest, row,
            environment=fixed_environment, generations=fixed_generations,
        )
        for row in (scoped_codex_row, scoped_claude_row, cross_runtime_row)
    }
    scoped_after = {
        release_acceptance.row_key(row): cell_metadata(
            ROOT, fragment_manifest, row,
            environment=changed_claude_environment, generations=fixed_generations,
        )
        for row in (scoped_codex_row, scoped_claude_row, cross_runtime_row)
    }
    check(
        "runtime environment invalidation is cell-scoped",
        scoped_before[release_acceptance.row_key(scoped_codex_row)]["cell_fingerprint"]
        == scoped_after[release_acceptance.row_key(scoped_codex_row)]["cell_fingerprint"]
        and scoped_before[release_acceptance.row_key(scoped_claude_row)]["cell_fingerprint"]
        != scoped_after[release_acceptance.row_key(scoped_claude_row)]["cell_fingerprint"]
        and scoped_before[release_acceptance.row_key(cross_runtime_row)]["cell_fingerprint"]
        != scoped_after[release_acceptance.row_key(cross_runtime_row)]["cell_fingerprint"],
    )
    patch_environment = {
        **fixed_environment,
        "claude": "2.1.205 (Claude Code)",
        "codex": "codex-cli 0.144.6",
    }
    patch_environment_after = {
        **patch_environment,
        "claude": "2.1.206 (Claude Code)",
        "codex": "codex-cli 0.144.9",
    }
    minor_environment_after = {
        **patch_environment_after,
        "claude": "2.2.0 (Claude Code)",
    }
    patch_before = cell_metadata(
        ROOT, fragment_manifest, cross_runtime_row,
        environment=patch_environment, generations=fixed_generations,
    )
    patch_after = cell_metadata(
        ROOT, fragment_manifest, cross_runtime_row,
        environment=patch_environment_after, generations=fixed_generations,
    )
    minor_after = cell_metadata(
        ROOT, fragment_manifest, cross_runtime_row,
        environment=minor_environment_after, generations=fixed_generations,
    )
    check(
        "runtime patch releases share one acceptance compatibility line",
        patch_before["cell_fingerprint"] == patch_after["cell_fingerprint"]
        and patch_before["cell_fingerprint"] != minor_after["cell_fingerprint"],
    )

    migration_report = tmp / "old-evidence-epoch.json"
    migration_report.write_text(
        json.dumps({"schema_version": 2, "phase": "baseline", "rows": []}),
        encoding="utf-8",
    )
    try:
        release_acceptance.load_resume_results(
            migration_report,
            [scoped_codex_row],
            phase="baseline",
            commit=release_acceptance.source_commit(ROOT),
            metadata={release_acceptance.row_key(scoped_codex_row): scoped_before[release_acceptance.row_key(scoped_codex_row)]},
            evidence_epoch=3,
        )
    except release_acceptance.AcceptanceError as exc:
        check("v2.1.1 evidence cannot migrate into the new epoch", "evidence epoch" in str(exc))
    else:
        check("v2.1.1 evidence cannot migrate into the new epoch", False)

    close_row = next(row for row in data["rows"] if row["skill"] == "close" and row["runtime"] == "claude")
    clarify_row = next(row for row in data["rows"] if row["skill"] == "clarify" and row["runtime"] == "claude")
    close_before = cell_dependency_hashes(
        fragment_root, ["evals/acceptance/skills.json"], skill="close", scenario=close_row["scenario"]
    )
    clarify_before = cell_dependency_hashes(
        fragment_root, ["evals/acceptance/skills.json"], skill="clarify", scenario=clarify_row["scenario"]
    )
    fragment_skills_path = fragment_root / "evals/acceptance/skills.json"
    fragment_skills = json.loads(fragment_skills_path.read_text(encoding="utf-8"))
    fragment_skills["skills"]["close"]["fixture"] += " changed"
    fragment_skills_path.write_text(json.dumps(fragment_skills), encoding="utf-8")
    close_after = cell_dependency_hashes(
        fragment_root, ["evals/acceptance/skills.json"], skill="close", scenario=close_row["scenario"]
    )
    clarify_after = cell_dependency_hashes(
        fragment_root, ["evals/acceptance/skills.json"], skill="clarify", scenario=clarify_row["scenario"]
    )
    check(
        "shared registry hashes only the exact cell fragment",
        close_before != close_after and clarify_before == clarify_after,
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
    check(
        "schema-3 evidence epoch",
        payload["schema_version"] == 3 and payload["evidence_epoch"] == 3,
    )
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
        "source commit identity does not invalidate matching semantic evidence",
        result.returncode == 1
        and payload["summary"]["verdicts"]["blocked"] == 1
        and sum(row["reason"] == "reused-identical" for row in payload["rows"])
        == len(payload["rows"]) - 1,
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
    timeout_row = {
        "schema_version": 1, "phase": "final", "skill": "timeout-fixture",
        "runtime": "codex", "scenario": "timeout", "expected": "bounded",
    }
    timeout_result = release_acceptance.run_matrix(
        [timeout_row],
        [sys.executable, str(interrupt_runner), str(interrupt_marker)],
        0.2,
    )
    check(
        "inactivity timeout reaches the exact active runner cleanup",
        timeout_result[0]["verdict"] == "blocked"
        and len(list(tmp.glob("runner-interrupted.*.cleaned"))) == 1,
    )

    retry_counter = tmp / "retry-counter.txt"
    retry_runner = tmp / "retry-runner.py"
    retry_runner.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "row=json.load(sys.stdin); p=Path(sys.argv[1]); n=int(p.read_text() if p.exists() else '0')+1; p.write_text(str(n))\n"
        "if n < 3: print(json.dumps({**row,'verdict':'blocked','defect':'capacity','failure_kind':'agent-capacity'}))\n"
        "else: print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'medium','actual':'ok','cleanup':'ok','evidence':'ok'}))\n",
        encoding="utf-8",
    )
    retry_result = release_acceptance.run_matrix(
        [timeout_row], [sys.executable, str(retry_runner), str(retry_counter)], 2
    )
    check(
        "closed typed transient retries stop after the third successful attempt",
        retry_result[0]["verdict"] == "pass"
        and retry_result[0]["attempts"] == 3
        and retry_result[0]["retry_count"] == 2,
    )

    heartbeat_runner = tmp / "heartbeat-runner.py"
    heartbeat_runner.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\nrow=json.load(sys.stdin); p=Path(os.environ['LLM_OBSIDIAN_ACCEPTANCE_HEARTBEAT'])\n"
        "for n in range(6): p.write_text(json.dumps({'schema_version':1,'stage':'model-wait','monotonic_ms':n})); time.sleep(0.1)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'medium','actual':'ok','cleanup':'ok','evidence':'ok'}))\n",
        encoding="utf-8",
    )
    heartbeat_result = release_acceptance.run_matrix(
        [timeout_row], [sys.executable, str(heartbeat_runner)], 0.2
    )
    check(
        "content-free heartbeat resets inactivity without extending a dead runner",
        heartbeat_result[0]["verdict"] == "pass"
        and heartbeat_result[0]["attempts"] == 1,
    )

    fail_runner = tmp / "product-fail-runner.py"
    fail_runner.write_text(
        "import json,sys\nrow=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'fail','model':'fixture','effort':'medium','actual':'bad','cleanup':'ok','evidence':'proof','defect':'product assertion'}))\n",
        encoding="utf-8",
    )
    fail_result = release_acceptance.run_matrix(
        [timeout_row], [sys.executable, str(fail_runner)], 2
    )
    check(
        "product failures are never retried",
        fail_result[0]["verdict"] == "fail" and fail_result[0]["attempts"] == 1,
    )

    incremental = tmp / "incremental"
    (incremental / "skills/demo-a").mkdir(parents=True)
    (incremental / "skills/demo-b").mkdir(parents=True)
    (incremental / "config").mkdir()
    (incremental / "evals").mkdir()
    (incremental / "evals" / "acceptance" / "seed").mkdir(parents=True)
    (incremental / "evals" / "acceptance" / "seed" / "fixture.txt").write_text(
        "canonical seed\n", encoding="utf-8"
    )
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
    (incremental / "scripts/acceptance_dependencies.py").write_text("# orchestration\n", encoding="utf-8")
    (incremental / "scripts/acceptance_fingerprints.py").write_text("# orchestration\n", encoding="utf-8")
    (incremental / "scripts/acceptance-workspace-supervisor.py").write_text("# orchestration\n", encoding="utf-8")
    (incremental / "scripts/live-acceptance-runner.py").write_bytes(
        (ROOT / "scripts/live-acceptance-runner.py").read_bytes()
    )
    (incremental / "scripts/release-acceptance.py").write_text("# orchestration\n", encoding="utf-8")
    manifest.write_text(
        "schema_version = 1\nrunner_contract_version = 3\norchestration_contract_version = 2\n"
        "environment_scope_version = 2\nevidence_epoch = 3\n"
        "non_behavioral_paths = [\"CHANGELOG.md\"]\n"
        "non_behavioral_prefixes = [\"tests/\"]\n"
        "orchestration_dependencies = [\"config/acceptance-cells.toml\", \"config/acceptance-dependencies.lock.json\", \"scripts/acceptance_dependencies.py\", \"scripts/acceptance_fingerprints.py\", \"scripts/acceptance-workspace-supervisor.py\", \"scripts/release-acceptance.py\"]\n"
        "behavioral_abi_dependencies = [\"scripts/live-acceptance-runner.py\"]\n"
        "behavioral_abi_fragments = []\nregistration_dependencies = []\n"
        "global_dependencies = [\".gitignore\", \"config/model-routing.toml\"]\n"
        "[model_generations]\n\"gpt-5.6-sol\" = \"codex:5.6\"\n\"gpt-5.6-terra\" = \"codex:5.6\"\n"
        "opus = \"claude:opus-4.8\"\nfable = \"claude:fable\"\nsonnet = \"claude:sonnet\"\n"
        "[generation_routes]\ninclude = [\"runtimes.codex\", \"runtimes.claude\"]\n"
        "[scenarios.demo]\ndependencies = []\n"
        "[skills.demo-a]\ndependencies = []\n"
        "[skills.demo-b]\ndependencies = []\n",
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
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/acceptance_dependencies.py"), "--root", str(incremental), "--write"],
        check=True, capture_output=True, text=True,
    )
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
            "--timeout", "5", "--jobs", "4", "--report", str(incremental_report),
        ], text=True, capture_output=True, check=False)

    result = incremental_run()
    check("incremental fixture starts green", result.returncode == 0, result.stderr)
    invocation_log.write_text("", encoding="utf-8")
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "identical semantic fingerprints reuse without rerunning",
        result.returncode == 0 and calls == [],
        result.stderr,
    )
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "skills/demo-a/SKILL.md").write_text("# Demo A\n\nchanged\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "change one cell"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "changed skill dependency reruns only affected skill cells",
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
    compatible_report = json.loads(incremental_report.read_text(encoding="utf-8"))
    mismatched_environment_report = json.loads(json.dumps(compatible_report))
    for row in mismatched_environment_report["rows"]:
        row["provenance"]["environment_sha256"] = "0" * 64
        row["row_integrity_sha256"] = release_acceptance.integrity_sha256(
            row, row["cell_fingerprint"], row["provenance"]
        )
    incremental_report.write_text(
        json.dumps(mismatched_environment_report), encoding="utf-8"
    )
    invocation_log.write_text("", encoding="utf-8")
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "evidence provenance mismatch reruns every cell",
        result.returncode == 0 and len(calls) == 4,
        result.stderr,
    )
    incremental_report.write_text(json.dumps(compatible_report), encoding="utf-8")
    invocation_log.write_text("", encoding="utf-8")
    live_runner = incremental / "scripts/live-acceptance-runner.py"
    live_runner.write_text(live_runner.read_text(encoding="utf-8") + "\n# behavioral ABI change\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "change live acceptance behavior"],
        cwd=incremental, check=True,
    )
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "live runner behavior change reruns every cell",
        result.returncode == 0 and len(calls) == 4,
        result.stderr,
    )
    report_data = json.loads(incremental_report.read_text(encoding="utf-8"))
    report_data["evidence_epoch"] = 999
    incremental_report.write_text(json.dumps(report_data), encoding="utf-8")
    result = incremental_run()
    check(
        "incompatible evidence epoch fails closed",
        result.returncode == 3 and "evidence epoch" in result.stderr,
        result.stderr,
    )
    report_data["evidence_epoch"] = 3
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
    check(
        "committed unknown data path does not invalidate semantic evidence",
        result.returncode == 0 and calls == [],
        result.stderr,
    )

print("\nAll release acceptance tests passed.")
