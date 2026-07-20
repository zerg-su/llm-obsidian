#!/usr/bin/env python3
"""Run release acceptance shards across bounded cmux workspaces."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from acceptance_fingerprints import (
    FingerprintError,
    cell_metadata,
    dirty_paths,
    environment_contract,
    generation_snapshot,
    is_non_behavioral_path,
    non_behavioral_paths,
    non_behavioral_prefixes,
    orchestration_dependencies,
    production_generations,
    read_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release-acceptance.py"
MAX_WORKSPACES = 5
MAX_JOBS_PER_WORKSPACE = 5
WORKSPACE_REF = re.compile(r"\bworkspace:\d+\b")
FORWARDED_ENV = (
    "LLM_OBSIDIAN_ACCEPTANCE_CLAUDE_MODEL",
    "LLM_OBSIDIAN_ACCEPTANCE_CODEX_MODEL",
    "LLM_OBSIDIAN_ACCEPTANCE_EFFORT",
    "LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT",
)


def load_release_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_acceptance_workspace", RELEASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load release-acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_release_module()


class WorkspaceAcceptanceError(RuntimeError):
    pass


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceAcceptanceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceAcceptanceError(f"{path} must contain an object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def validate_limits(workspaces: int, jobs_per_workspace: int) -> None:
    if not 1 <= workspaces <= MAX_WORKSPACES:
        raise WorkspaceAcceptanceError(
            f"acceptance workspaces must be between 1 and {MAX_WORKSPACES}"
        )
    if not 1 <= jobs_per_workspace <= MAX_JOBS_PER_WORKSPACE:
        raise WorkspaceAcceptanceError(
            "acceptance jobs per workspace must be between 1 and "
            f"{MAX_JOBS_PER_WORKSPACE}"
        )


def shard_environment(commit: str) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in FORWARDED_ENV if os.environ.get(key)
    }
    # The supervisor owns the tested revision. Pin it after reading user-facing
    # overrides so a long-running shard cannot silently follow a moving HEAD.
    environment["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] = commit
    return environment


def partition_skills(pending_rows: list[dict[str, Any]], workspaces: int) -> list[list[str]]:
    """Greedily balance pending cells while keeping both runtimes of a skill together."""

    if workspaces < 1:
        raise WorkspaceAcceptanceError("at least one workspace is required")
    weights: dict[str, int] = {}
    for row in pending_rows:
        skill = str(row["skill"])
        weights[skill] = weights.get(skill, 0) + 1
    if not weights:
        return []
    buckets: list[list[str]] = [[] for _ in range(min(workspaces, len(weights)))]
    loads = [0 for _ in buckets]
    for skill, weight in sorted(weights.items(), key=lambda item: (-item[1], item[0])):
        index = min(range(len(buckets)), key=lambda item: (loads[item], item))
        buckets[index].append(skill)
        loads[index] += weight
    return [sorted(bucket) for bucket in buckets]


def build_context(root: Path, phase: str, report: Path) -> dict[str, Any]:
    skills = release.load_spec(root / "evals/acceptance/skills.json", root)
    release.validate_scenario_coverage(root / "evals/acceptance/scenarios.json", skills)
    manifest = read_manifest(root)
    rows = release.matrix_rows(skills, phase)
    generations = production_generations(root, manifest)
    environment = environment_contract()
    metadata = {
        release.row_key(row): cell_metadata(
            root, manifest, row, environment=environment, generations=generations
        )
        for row in rows
    }
    commit = release.source_commit(root)
    allowed_dirty = non_behavioral_paths(manifest)
    allowed_prefixes = non_behavioral_prefixes(manifest)
    dirty = dirty_paths(root)
    if dirty is None:
        raise WorkspaceAcceptanceError("cannot inspect acceptance worktree state")
    behavioral_dirty = sorted(
        path for path in dirty if not is_non_behavioral_path(path, allowed_dirty, allowed_prefixes)
    )
    if behavioral_dirty:
        raise WorkspaceAcceptanceError(
            "workspace acceptance requires committed behavioral state; dirty paths: "
            + ", ".join(behavioral_dirty[:8])
        )
    orchestration = orchestration_dependencies(manifest)
    orchestration_version = int(manifest["orchestration_contract_version"])
    fingerprint = release.matrix_fingerprint(
        [{**row, "cell_fingerprint": metadata[release.row_key(row)]["cell_fingerprint"]}
         for row in rows]
    )
    prior = release.load_resume_results(
        report,
        rows,
        phase=phase,
        commit=commit,
        metadata=metadata,
        root=root,
        non_behavioral=allowed_dirty,
        non_behavioral_prefixes_=allowed_prefixes,
        orchestration=orchestration,
        orchestration_contract_version=orchestration_version,
        include_dirty=True,
    )
    return {
        "rows": rows,
        "metadata": metadata,
        "commit": commit,
        "fingerprint": fingerprint,
        "orchestration_version": orchestration_version,
        "prior": prior,
    }


def validate_shard_report(
    path: Path,
    *,
    context: dict[str, Any],
    phase: str,
    allowed_skills: set[str],
    base_keys: set[tuple[str, str, str, str, str]],
) -> list[dict[str, Any]]:
    raw = read_object(path)
    if (
        raw.get("schema_version") != 2
        or raw.get("phase") != phase
        or raw.get("source_commit") != context["commit"]
        or raw.get("matrix_fingerprint") != context["fingerprint"]
        or raw.get("orchestration_contract_version") != context["orchestration_version"]
        or not isinstance(raw.get("rows"), list)
    ):
        raise WorkspaceAcceptanceError(f"shard report contract mismatch: {path}")
    expected = {release.row_key(row): row for row in context["rows"]}
    seen: set[tuple[str, str, str, str, str]] = set()
    values: list[dict[str, Any]] = []
    for item in raw["rows"]:
        if not isinstance(item, dict):
            raise WorkspaceAcceptanceError(f"malformed shard row: {path}")
        key = release.row_key(item)
        if key in seen or key not in expected:
            raise WorkspaceAcceptanceError(f"duplicate or stale shard row: {path}")
        seen.add(key)
        if key not in base_keys and str(item.get("skill")) not in allowed_skills:
            raise WorkspaceAcceptanceError(f"shard emitted an unassigned skill: {path}")
        result = release.validate_result(expected[key], item)
        current = context["metadata"][key]
        provenance = item.get("provenance")
        if (
            item.get("cell_fingerprint") != current["cell_fingerprint"]
            or item.get("dependencies") != current["dependencies"]
            or item.get("generation") != current["generation"]
            or not isinstance(provenance, dict)
            or item.get("row_integrity_sha256")
            != release.integrity_sha256(result, current["cell_fingerprint"], provenance)
        ):
            raise WorkspaceAcceptanceError(f"invalid shard evidence: {path}")
        values.append(item)
    return values


def merge_shards(
    shard_reports: list[tuple[Path, set[str]]],
    *,
    context: dict[str, Any],
    phase: str,
) -> list[dict[str, Any]]:
    base = {release.row_key(row): row for row in context["prior"]}
    merged = dict(base)
    for path, skills in shard_reports:
        if not path.is_file():
            continue
        for item in validate_shard_report(
            path,
            context=context,
            phase=phase,
            allowed_skills=skills,
            base_keys=set(base),
        ):
            key = release.row_key(item)
            previous = merged.get(key)
            if previous is not None and (
                previous.get("row_integrity_sha256") != item.get("row_integrity_sha256")
            ):
                raise WorkspaceAcceptanceError(f"conflicting shard evidence for {key[1]}:{key[2]}")
            merged[key] = item
    return [merged[release.row_key(row)] for row in context["rows"] if release.row_key(row) in merged]


def checkpoint(report: Path, rows: list[dict[str, Any]], context: dict[str, Any], phase: str) -> None:
    release.write_json(
        release.report_payload(
            phase,
            rows,
            planned_total=len(context["rows"]),
            commit=context["commit"],
            fingerprint=context["fingerprint"],
            orchestration_contract_version=context["orchestration_version"],
        ),
        report,
        announce=False,
    )


def refresh_generation_snapshot(
    root: Path, report: Path, snapshot: dict[str, Any]
) -> bool:
    """Refresh the canonical model-generation baseline after one green merge."""

    acceptance_root = root.resolve() / ".vault-meta" / "acceptance"
    try:
        report.resolve().relative_to(acceptance_root)
    except ValueError:
        return False
    payload = read_object(report)
    summary = payload.get("summary")
    if (
        not isinstance(summary, dict)
        or summary.get("complete") is not True
        or summary.get("failed") != 0
    ):
        return False
    atomic_json(acceptance_root / "model-generations.json", snapshot)
    return True


def worker(config_path: Path) -> int:
    config = read_object(config_path)
    if config.get("schema_version") != 1:
        raise WorkspaceAcceptanceError("worker config must use schema_version 1")
    root = Path(str(config["root"])).resolve()
    report = Path(str(config["report"])).resolve()
    status = Path(str(config["status"])).resolve()
    skills = config.get("skills")
    if not isinstance(skills, list) or not skills or any(not isinstance(item, str) for item in skills):
        raise WorkspaceAcceptanceError("worker config requires bounded skills")
    command = [
        sys.executable,
        str(root / "scripts/release-acceptance.py"),
        "run",
        "--phase",
        str(config["phase"]),
        "--runner",
        str(config["runner"]),
        "--timeout",
        str(config["cell_timeout"]),
        "--jobs",
        str(config["jobs"]),
        "--report",
        str(report),
    ]
    for skill in skills:
        command.extend(["--skill", skill])
    env = dict(os.environ)
    forwarded = config.get("environment")
    if isinstance(forwarded, dict):
        for key in FORWARDED_ENV:
            value = forwarded.get(key)
            if isinstance(value, str) and value:
                env[key] = value
    result = subprocess.run(command, cwd=root, env=env, check=False)
    atomic_json(status, {"schema_version": 1, "exit_code": result.returncode})
    return result.returncode


def create_workspace(root: Path, name: str, command: list[str]) -> str:
    result = subprocess.run(
        [
            "cmux", "new-workspace", "--name", name, "--cwd", str(root),
            "--command", shlex.join(command), "--focus", "false",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    match = WORKSPACE_REF.search(output)
    if result.returncode != 0 or match is None:
        raise WorkspaceAcceptanceError(output or "cmux did not create an acceptance workspace")
    return match.group(0)


def close_workspace(workspace: str) -> None:
    subprocess.run(
        ["cmux", "close-workspace", "--workspace", workspace],
        text=True,
        capture_output=True,
        check=False,
    )


def supervise(args: argparse.Namespace) -> int:
    validate_limits(args.workspaces, args.jobs_per_workspace)
    if args.cell_timeout <= 0 or args.supervisor_timeout <= 0:
        raise WorkspaceAcceptanceError("acceptance workspace timeouts must be positive")
    root = args.root.resolve()
    report = args.report.resolve()
    context = build_context(root, args.phase, report)
    prior_keys = {release.row_key(row) for row in context["prior"]}
    pending = [row for row in context["rows"] if release.row_key(row) not in prior_keys]
    assignments = partition_skills(pending, args.workspaces)
    if not assignments:
        checkpoint(report, context["prior"], context, args.phase)
        refresh_generation_snapshot(
            root, report, generation_snapshot(root, read_manifest(root))
        )
        print("acceptance workspaces: matrix already complete")
        return 0

    run_dir = root / ".vault-meta/acceptance/workspace-runs" / str(uuid.uuid4())
    run_dir.mkdir(parents=True, exist_ok=False)
    environment = shard_environment(context["commit"])
    shards: list[dict[str, Any]] = []
    seed = release.report_payload(
        args.phase,
        context["prior"],
        planned_total=len(context["rows"]),
        commit=context["commit"],
        fingerprint=context["fingerprint"],
        orchestration_contract_version=context["orchestration_version"],
    )
    for index, skills in enumerate(assignments, start=1):
        shard_report = run_dir / f"shard-{index}.json"
        status = run_dir / f"shard-{index}.status.json"
        config = run_dir / f"shard-{index}.config.json"
        atomic_json(shard_report, seed)
        atomic_json(config, {
            "schema_version": 1,
            "root": str(root),
            "phase": args.phase,
            "runner": args.runner,
            "cell_timeout": args.cell_timeout,
            "jobs": args.jobs_per_workspace,
            "report": str(shard_report),
            "status": str(status),
            "skills": skills,
            "environment": environment,
        })
        shards.append({"skills": set(skills), "report": shard_report, "status": status})

    checkpoint(report, context["prior"], context, args.phase)
    created: list[str] = []
    closed: set[str] = set()
    last_completed = len(context["prior"])
    deadline = time.monotonic() + args.supervisor_timeout
    try:
        for index, shard in enumerate(shards, start=1):
            command = [sys.executable, str(Path(__file__).resolve()), "worker", "--config", str(run_dir / f"shard-{index}.config.json")]
            workspace = create_workspace(root, f"Acceptance {index}/{len(shards)} · max {args.jobs_per_workspace}", command)
            shard["workspace"] = workspace
            created.append(workspace)
        print(
            f"acceptance workspaces: {len(shards)} workspace(s) × "
            f"{args.jobs_per_workspace} jobs; {len(pending)} pending cells"
        )
        while True:
            reports = [(item["report"], item["skills"]) for item in shards]
            merged = merge_shards(reports, context=context, phase=args.phase)
            checkpoint(report, merged, context, args.phase)
            if len(merged) != last_completed:
                last_completed = len(merged)
                print(f"acceptance workspaces: checkpoint {last_completed}/{len(context['rows'])}")
            finished = 0
            for shard in shards:
                if shard["status"].is_file():
                    finished += 1
                    workspace = str(shard["workspace"])
                    if workspace not in closed:
                        close_workspace(workspace)
                        closed.add(workspace)
            if finished == len(shards):
                break
            if time.monotonic() >= deadline:
                raise WorkspaceAcceptanceError("acceptance workspace supervisor timed out")
            time.sleep(1)
        merged = merge_shards(
            [(item["report"], item["skills"]) for item in shards],
            context=context,
            phase=args.phase,
        )
        checkpoint(report, merged, context, args.phase)
        payload = read_object(report)
        summary = payload["summary"]
        if summary["complete"] and summary["failed"] == 0:
            refresh_generation_snapshot(
                root, report, generation_snapshot(root, read_manifest(root))
            )
        print(
            "acceptance workspaces: "
            f"completed={summary['completed']}/{summary['total']} failed={summary['failed']}"
        )
        return 0 if summary["complete"] and summary["failed"] == 0 else 1
    finally:
        for workspace in created:
            if workspace not in closed:
                close_workspace(workspace)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, default=ROOT)
    run.add_argument("--phase", choices=release.PHASES, required=True)
    run.add_argument("--runner", required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--workspaces", type=int, default=1)
    run.add_argument("--jobs-per-workspace", type=int, default=5)
    run.add_argument("--cell-timeout", type=float, default=3700.0)
    run.add_argument("--supervisor-timeout", type=float, default=14400.0)
    child = sub.add_parser("worker")
    child.add_argument("--config", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "worker":
            return worker(args.config)
        return supervise(args)
    except (
        WorkspaceAcceptanceError,
        release.AcceptanceError,
        FingerprintError,
        OSError,
        KeyError,
    ) as exc:
        print(f"acceptance workspaces: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("acceptance workspaces: interrupted after owned workspace cleanup", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
