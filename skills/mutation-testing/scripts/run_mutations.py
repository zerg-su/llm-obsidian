#!/usr/bin/env python3
"""Run a bounded semantic-mutation plan in copies of the current Git worktree.

This isolates ordinary file edits, not hostile commands or external effects.
Exit zero means evidence collected with green controls, not tests adequate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts or path == Path("."):
        raise ValueError(f"unsafe relative path: {value!r}")
    return path


def environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in {"PYTHONPATH", "NODE_PATH"}}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git(source: Path, *argv: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(source), "-c", "core.fsmonitor=false", *argv],
        env=environment(), stderr=subprocess.PIPE,
    )


def file_names(source: Path, includes: list[str]) -> set[str]:
    names = {os.fsdecode(name) for name in git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0") if name}
    for value in includes:
        path = source / relative(value)
        if path.is_symlink() or path.is_file():
            names.add(path.relative_to(source).as_posix())
        elif path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                for name in [*dirs, *files]:
                    item = Path(root) / name
                    if name == ".git":
                        raise ValueError("include contains Git metadata")
                    if item.is_symlink() or not item.is_dir():
                        names.add(item.relative_to(source).as_posix())
        else:
            raise ValueError(f"missing include path: {value}")
    return names


def excluded(name: str, excludes: list[str]) -> bool:
    return any(name == value or name.startswith(value + "/") for value in excludes)


def inventory(source: Path, includes: list[str], max_bytes: int, excludes: list[str]) -> dict:
    stage = git(source, "ls-files", "--stage", "-z")
    if any(row.startswith(b"160000 ") and not excluded(os.fsdecode(row.split(b"\t", 1)[1]), excludes)
           for row in stage.split(b"\0")):
        raise ValueError("submodules need a separately isolated module snapshot")
    if git(source, "ls-files", "--unmerged", "-z"):
        raise ValueError("resolve the existing index conflict before mutation testing")
    files, total = {}, 0
    for name in sorted(file_names(source, includes)):
        if excluded(name, excludes):
            continue
        path = source / relative(name)
        # Reject traversal through a symlink directory, even for explicit includes.
        if any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents):
            raise ValueError(f"symlink parent: {name}")
        if not path.exists() and not path.is_symlink():
            continue  # staged or unstaged deletion is part of the current state
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            target = path.resolve()
            if not target.is_relative_to(source) or ".git" in target.relative_to(source).parts:
                raise ValueError(f"external or Git metadata symlink: {name}")
            files[name] = {"link": os.readlink(path), "target": target.relative_to(source).as_posix()}
        elif stat.S_ISREG(mode):
            total += path.stat().st_size
            if total > max_bytes:
                raise ValueError("snapshot exceeds --max-bytes; narrow source module or explicitly raise limit")
            files[name] = {"sha256": digest(path.read_bytes()), "mode": stat.S_IMODE(mode)}
        else:
            raise ValueError(f"unsupported file type: {name}")
    for name, entry in files.items():
        if "link" in entry:
            target = entry["target"]
            if target not in files and not any(key.startswith(target + "/") for key in files):
                raise ValueError(f"symlink target excluded from snapshot: {name}; include its dependency")
    index = Path(os.fsdecode(git(source, "rev-parse", "--git-path", "index")).strip())
    if not index.is_absolute():
        index = source / index
    return {"files": files, "includes": includes, "excludes": excludes,
            "index_sha256": digest(index.read_bytes()) if index.exists() else None,
            "stage_sha256": digest(stage), "head": git(source, "rev-parse", "HEAD").decode().strip()}


def capture(source: Path, target: Path, state: dict) -> None:
    target.mkdir()
    for name, entry in state["files"].items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        if "link" in entry:
            # Rebase absolute internal links to this copy, never to the source.
            output.symlink_to(os.path.relpath(target / entry["target"], output.parent))
        else:
            shutil.copy2(source / name, output)
            if digest(output.read_bytes()) != entry["sha256"]:
                raise ValueError(f"source changed during capture: {name}")


def validate_plan(plan: dict, base: Path) -> None:
    if plan.get("schema_version") != 1:
        raise ValueError("plan schema_version must be 1")
    command = plan.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x and "\0" not in x for x in command):
        raise ValueError("command must be a nonempty argv array")
    timeout = plan.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    mutations = plan.get("mutations")
    if not isinstance(mutations, list) or not 1 <= len(mutations) <= 8:
        raise ValueError("use 1–8 mutations per bounded plan")
    ids = {"baseline", "control"}
    for item in mutations:
        name = item.get("id", "")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) or name in ids:
            raise ValueError("mutation id must be unique and filesystem-safe")
        ids.add(name)
        path = base / relative(item["path"])
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
            raise ValueError("mutation path must be a regular file in the snapshot")
        for key in ("before", "after", "reason", "expected_failure"):
            if not isinstance(item.get(key), str) or (key != "after" and not item[key]):
                raise ValueError(f"mutation requires {key}")
        if item["before"] == item["after"] or path.read_bytes().count(item["before"].encode()) != 1:
            raise ValueError("mutation before must match exactly once and change content")


def terminate_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def verify(base: Path, output: Path, plan: dict, item: dict | None, name: str) -> dict:
    work = output / "work"
    shutil.copytree(base, work, symlinks=True)
    process = None
    try:
        if item:
            path = work / item["path"]
            path.write_bytes(path.read_bytes().replace(item["before"].encode(), item["after"].encode(), 1))
        env = environment()
        scratch = output / "tmp" / name
        scratch.mkdir(parents=True)
        env["TMPDIR"] = str(scratch)
        with (output / f"{name}.log").open("wb") as log:
            process = subprocess.Popen(plan["command"], cwd=work, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=plan.get("timeout_seconds", 60))
                status = "passed" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                status, code = "timeout", None
        return {"id": name, "status": status, "exit_code": code, "log": f"{name}.log",
                "cwd": str(work), "command": plan["command"],
                "mutated_path": item["path"] if item else None,
                "artifact_sha256": digest((work / item["path"]).read_bytes()) if item else None}
    finally:
        if process is not None:
            terminate_group(process)
        shutil.rmtree(work)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def execute(source: Path, output: Path, plan: dict, includes: list[str], max_bytes: int, excludes: list[str]) -> dict:
    original = inventory(source, includes, max_bytes, excludes)
    base = output / "snapshot"
    report = {"schema_version": 1, "status": "incomplete", "runs": [], "source_unchanged": None,
              "runner_sha256": digest(Path(__file__).read_bytes()),
              "plan_sha256": digest(json.dumps(plan, sort_keys=True).encode()),
              "source_manifest_sha256": digest(json.dumps(original, sort_keys=True).encode()),
              "classification": "Agent must inspect behavioral failure evidence; exit codes are observations only."}
    write_json(output / "source-manifest.json", original)
    write_json(output / "plan.json", plan)
    try:
        capture(source, base, original)
        if inventory(source, includes, max_bytes, excludes) != original:
            raise ValueError("source changed during capture")
        validate_plan(plan, base)
        baseline = verify(base, output, plan, None, "baseline")
        report["runs"].append(baseline)
        if baseline["status"] != "passed":
            report["status"] = "baseline-failed"
        else:
            for item in plan["mutations"]:
                report["active_mutation"] = item["id"]
                write_json(output / "report.json", report)
                report["runs"].append(verify(base, output, plan, item, item["id"]))
            report.pop("active_mutation", None)
            control = verify(base, output, plan, None, "control")
            report["runs"].append(control)
            report["status"] = "collected" if control["status"] == "passed" else "control-failed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    finally:
        try:
            report["source_unchanged"] = inventory(source, includes, max_bytes, excludes) == original
        except (OSError, ValueError, subprocess.CalledProcessError):
            report["source_unchanged"] = False
        if not report["source_unchanged"]:
            report["status"] = "source-drift"
        write_json(output / "report.json", report)
    return report


def interrupted(*_args) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="new directory outside source; default: private temporary directory")
    parser.add_argument("--include", action="append", default=[], help="explicit ignored dependency path to copy")
    parser.add_argument("--exclude", action="append", default=[], help="explicit unrelated path omitted from snapshot and content checks")
    parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        source = args.source.resolve(strict=True)
        root = Path(os.fsdecode(git(source, "rev-parse", "--show-toplevel")).strip()).resolve()
        if source != root:
            raise ValueError("source must be the Git worktree root")
        plan = json.loads(args.plan.read_text())
        if args.output:
            output = args.output.resolve()
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError("output must be outside and disjoint from source")
            output.mkdir(mode=0o700, parents=True, exist_ok=False)
        else:
            output = Path(tempfile.mkdtemp(prefix="semantic-mutation-")).resolve()
            if output.is_relative_to(source):
                output.rmdir()
                raise ValueError("temporary directory is inside source; choose --output outside it")
        print(str(output), flush=True)
        excludes = [relative(value).as_posix() for value in args.exclude]
        report = execute(source, output, plan, args.include, args.max_bytes, excludes)
        print(json.dumps(report))
        return 0 if report["status"] == "collected" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"mutation-workspace: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
