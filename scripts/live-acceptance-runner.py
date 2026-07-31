#!/usr/bin/env python3
"""Run or resume exactly four provider-backed harness acceptance cells."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
CELL_IDS = (
    "claude-lifecycle",
    "codex-lifecycle",
    "cross-runtime-composition",
    "deep-review",
)
NON_BEHAVIORAL_ROOTS = frozenset({"docs", "references", "wiki"})
NON_BEHAVIORAL_RUNTIME_PATHS = frozenset({".task-origin-session"})
BEHAVIORAL_DOCUMENTS = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "docs/runtime-capabilities.md",
        "docs/task-sessions.md",
    }
)
BEHAVIORAL_DOCUMENT_ROOTS = ("docs/skill-references/",)


CellDriver = Callable[..., dict[str, Any]]
ContractLoader = Callable[[Path], dict[str, Any]]
ReleasePreflight = Callable[..., dict[str, Any]]


class AcceptanceError(ValueError):
    pass


def bootstrap_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode or len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise AcceptanceError("cannot resolve release HEAD before loading live code")
    return value


def bootstrap_clean_head(root: Path) -> str:
    """Check the exact checkout before importing any repo-owned behavior."""
    commit_sha = bootstrap_head(root)
    changed: set[str] = set()
    for args in (
        ("diff", "--name-only", "-z", "HEAD", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AcceptanceError("cannot inspect release worktree before loading live code")
        changed.update(
            raw.decode("utf-8", "surrogateescape")
            for raw in result.stdout.split(b"\0")
            if raw
        )
    behavioral = sorted(
        relative
        for relative in changed
        if (
            Path(relative).parts
            and Path(relative).as_posix() not in NON_BEHAVIORAL_RUNTIME_PATHS
            and (
                Path(relative).as_posix() in BEHAVIORAL_DOCUMENTS
                or any(
                    Path(relative).as_posix().startswith(prefix)
                    for prefix in BEHAVIORAL_DOCUMENT_ROOTS
                )
                or (
                    Path(relative).parts[0] not in NON_BEHAVIORAL_ROOTS
                    and not (
                        len(Path(relative).parts) == 1
                        and Path(relative).suffix.casefold() == ".md"
                    )
                )
            )
        )
    )
    if behavioral:
        raise AcceptanceError("release evidence requires a clean HEAD before loading live code")
    if bootstrap_head(root) != commit_sha:
        raise AcceptanceError("release HEAD changed before loading live code")
    return commit_sha


def live_ports() -> tuple[
    type[ValueError],
    CellDriver,
    Callable[..., dict[str, Any]],
    Callable[..., dict[str, Any]],
    ReleasePreflight,
]:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from live_acceptance_driver import (
        LiveDriverError,
        preflight_release,
        run_cell,
        validate_cell_evidence,
        validate_preflight_evidence,
    )

    return (
        LiveDriverError,
        run_cell,
        validate_cell_evidence,
        validate_preflight_evidence,
        preflight_release,
    )


def release_ports() -> tuple[ContractLoader, Callable[[Path, Path], dict[str, Any]]]:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from release_acceptance_support import contract, validate_report

    return contract, validate_report


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_state(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if (
        value.get("schema_version") != 3
        or value.get("commit_sha") != expected["commit_sha"]
    ):
        return {
            "schema_version": 3,
            "commit_sha": expected["commit_sha"],
            "preflight": {},
            "cells": [],
            "failures": [],
        }
    return value


def _failure(
    cell_id: str,
    classification: str,
    *,
    attempt: int,
) -> dict[str, Any]:
    return {
        "cell_id": cell_id,
        "status": "failed",
        "classification": classification,
        "attempt": attempt,
    }


def execute_release(
    root: Path,
    release: dict[str, Any],
    *,
    state_path: Path,
    report_path: Path,
    selected: set[str],
    restart: bool,
    timeout: int,
    cell_driver: CellDriver | None = None,
    release_preflight: ReleasePreflight | None = None,
    verify_clean_head: bool = False,
    contract_loader: ContractLoader | None = None,
) -> dict[str, Any]:
    load_contract = contract_loader
    if verify_clean_head:
        bootstrap_sha = ""
        if load_contract is None:
            if root != ROOT:
                raise AcceptanceError(
                    "live acceptance code and root must resolve to the same checkout"
                )
            bootstrap_sha = bootstrap_clean_head(root)
            load_contract = release_ports()[0]
        if load_contract(root) != release:
            raise AcceptanceError("release contract changed before loading the live driver")
        if bootstrap_sha and release.get("commit_sha") != bootstrap_sha:
            raise AcceptanceError("release code is not bound to the bootstrap HEAD")
    (
        live_error,
        repo_cell_driver,
        validate_cell_evidence,
        validate_preflight_evidence,
        repo_release_preflight,
    ) = live_ports()
    selected_preflight = release_preflight or (
        repo_release_preflight if cell_driver is None else None
    )
    preflight_artifact: dict[str, Any]
    if selected_preflight is not None:
        preflight_artifact = validate_preflight_evidence(
            selected_preflight(root, release, timeout=timeout),
            commit_sha=str(release["commit_sha"]),
        )
    else:
        raise AcceptanceError("live acceptance requires a global route preflight")
    selected_driver = cell_driver or repo_cell_driver
    commit_sha = release["commit_sha"]
    state = (
        {
            "schema_version": 3,
            "commit_sha": commit_sha,
            "preflight": preflight_artifact,
            "cells": [],
            "failures": [],
        }
        if restart else read_state(state_path, release)
    )
    state["preflight"] = preflight_artifact
    completed = {
        row["cell_id"]: row
        for row in state.get("cells", [])
        if isinstance(row, dict) and row.get("cell_id") in CELL_IDS
    }
    expected = {row["cell_id"]: row for row in release["cells"]}
    raw_failures = state.get("failures", [])
    failures = [
        row
        for row in raw_failures
        if isinstance(row, dict)
        and row.get("cell_id") in CELL_IDS
        and row.get("status") == "failed"
        and row.get("classification")
        in {"runtime-contract", "mechanism-failure"}
        and type(row.get("attempt")) is int
        and int(row["attempt"]) >= 1
    ]
    if len(failures) != len(raw_failures) or len(failures) > 1:
        raise AcceptanceError("live state has an invalid failed-cell classification")
    effective_selected = set(selected)
    recovery_cell = ""
    if failures:
        recovery_cell = str(failures[0]["cell_id"])
        if recovery_cell not in selected:
            raise AcceptanceError(
                "resume must include the explicitly classified failed cell"
            )
        effective_selected = {recovery_cell}

    def persist(failed: list[dict[str, Any]]) -> dict[str, Any]:
        state["cells"] = [
            completed[key] for key in CELL_IDS if key in completed
        ]
        state["failures"] = failed
        atomic_json(state_path, state)
        report_value = {
            "schema_version": 3,
            "commit_sha": commit_sha,
            "preflight": preflight_artifact,
            "cells": state["cells"],
            "failures": failed,
        }
        atomic_json(report_path, report_value)
        return report_value

    for cell_id in CELL_IDS:
        if cell_id not in effective_selected:
            continue
        contract_cell = expected[cell_id]
        prior = completed.get(cell_id)
        if prior is not None:
            try:
                validate_cell_evidence(contract_cell, prior, commit_sha=commit_sha)
            except live_error:
                completed.pop(cell_id, None)
            else:
                continue
        if verify_clean_head and load_contract is not None and load_contract(root) != release:
            raise AcceptanceError("release contract changed before live cell execution")
        request = {**contract_cell, "commit_sha": commit_sha}
        try:
            value = selected_driver(root, request, timeout=timeout)
        # BaseException: an operator interrupt must leave the same durable
        # classification as any other incomplete cell, otherwise the leak is
        # invisible to a resume and the cell is silently skipped.
        except BaseException as exc:
            prior_attempt = (
                int(failures[0]["attempt"])
                if failures and failures[0]["cell_id"] == cell_id
                else 0
            )
            classification = (
                "runtime-contract"
                if isinstance(exc, live_error)
                else "mechanism-failure"
            )
            persist(
                [
                    _failure(
                        cell_id,
                        classification,
                        attempt=prior_attempt + 1,
                    )
                ]
            )
            raise
        if verify_clean_head and load_contract is not None and load_contract(root) != release:
            raise AcceptanceError("release contract changed during live cell execution")
        try:
            completed[cell_id] = validate_cell_evidence(
                contract_cell,
                value,
                commit_sha=commit_sha,
            )
        except live_error:
            prior_attempt = (
                int(failures[0]["attempt"])
                if failures and failures[0]["cell_id"] == cell_id
                else 0
            )
            persist(
                [
                    _failure(
                        cell_id,
                        "runtime-contract",
                        attempt=prior_attempt + 1,
                    )
                ]
            )
            raise
        failures = []
        persist([])
    return persist([])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, default=ROOT)
    run.add_argument("--cell", choices=CELL_IDS, action="append")
    run.add_argument("--timeout", type=int, default=1200)
    run.add_argument("--restart", action="store_true")
    run.add_argument("--state", type=Path)
    run.add_argument("--report", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--root", type=Path, default=ROOT)
    verify.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        if root != ROOT:
            raise AcceptanceError("live acceptance code and --root must resolve to the same checkout")
        bootstrap_sha = bootstrap_clean_head(root)
        contract, validate_report = release_ports()
        if args.command == "verify":
            value = validate_report(root, args.report or root / ".vault-meta/acceptance/latest-live.json")
            if value.get("commit_sha") != bootstrap_sha:
                raise AcceptanceError("verified evidence is not bound to the bootstrap HEAD")
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0
        release = contract(root)
        if release.get("commit_sha") != bootstrap_sha:
            raise AcceptanceError("release code is not bound to the bootstrap HEAD")
        selected = set(args.cell or CELL_IDS)
        state_path = args.state or root / ".vault-meta/acceptance/live-state.json"
        report_path = args.report or root / ".vault-meta/acceptance/latest-live.json"
        report = execute_release(
            root,
            release,
            state_path=state_path,
            report_path=report_path,
            selected=selected,
            restart=args.restart,
            timeout=args.timeout,
            verify_clean_head=True,
        )
        if {row["cell_id"] for row in report["cells"]} == set(CELL_IDS):
            validate_report(root, report_path)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except ValueError as exc:
        print(f"live-acceptance-runner: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
