#!/usr/bin/env python3
"""Read-only live Harness dashboard and external cmux launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.cmux import CmuxAdapter, CmuxError, UUID_RE
from harness.dashboard_projection import (
    HEALTHY,
    MAX_ISSUES,
    DashboardProjection,
    IssueView,
    escalate,
    project,
)
from harness.dashboard_view import render
from harness.state_machine import TERMINAL
from harness.status_segment import LiveInventory, live_inventory


MIN_INTERVAL = 0.1
MAX_INTERVAL = 60.0
RECENT_CHOICES = (2, 3)
CLEAR = "\x1b[2J\x1b[H"


@dataclass(frozen=True)
class OpenResult:
    surface_id: str
    workspace_id: str
    reused: bool


def _interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if not MIN_INTERVAL <= parsed <= MAX_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"interval must be between {MIN_INTERVAL:g} and {MAX_INTERVAL:g} seconds"
        )
    return parsed


def _live_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-dashboard")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=_interval, default=1.0)
    parser.add_argument("--recent", type=int, choices=RECENT_CHOICES, default=3)
    parser.add_argument("--no-color", action="store_true")
    return parser


def _open_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-dashboard open")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--surface", required=True)
    return parser


def _probe_inventory(
    inventory_probe: Callable[..., LiveInventory | None] | None,
) -> LiveInventory | None:
    binary = os.environ.get("CMUX_BUNDLED_CLI_PATH") or "cmux"
    try:
        if inventory_probe is not None:
            return inventory_probe(binary=binary)
        if shutil.which(binary):
            return live_inventory(binary=binary)
    except Exception:
        return None
    return None


def _record_mtime(store: Path, owner: str, operation_id: str) -> int:
    path = store / "owners" / owner / "operations" / f"{operation_id}.json"
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def snapshot(
    store: Path,
    *,
    recent: int,
    inventory: LiveInventory | None,
) -> DashboardProjection:
    """Collect every owner once, retaining all active and recent terminal roots."""

    root = store.expanduser().resolve()
    owners_root = root / "owners"
    owners = (
        sorted(path.name for path in owners_root.iterdir() if path.is_dir())
        if owners_root.is_dir()
        else []
    )
    programs: list[tuple[int, str, object]] = []
    issues: list[IssueView] = []
    classification = HEALTHY
    dropped_programs = 0
    dropped_issues = 0
    for owner in owners:
        try:
            owner_view = project(
                root,
                owner,
                inventory=inventory,
                surface_probe="observed" if inventory is not None else "unavailable",
            )
        except (OSError, ValueError):
            issues.append(
                IssueView(
                    "owner-records-invalid",
                    owner,
                    "owner records could not be projected",
                    "request-coordinator-classification",
                )
            )
            classification = escalate(
                classification, "request-coordinator-classification"
            )
            continue
        classification = escalate(classification, owner_view.classification)
        dropped_programs += int(owner_view.truncated.get("programs", 0))
        dropped_issues += int(owner_view.truncated.get("issues", 0))
        issues.extend(owner_view.issues)
        programs.extend(
            (
                _record_mtime(root, owner, program.operation_id),
                owner,
                program,
            )
            for program in owner_view.programs
        )

    active = [item for item in programs if item[2].state not in TERMINAL]
    terminal = sorted(
        (item for item in programs if item[2].state in TERMINAL),
        key=lambda item: (-item[0], item[1], item[2].operation_id),
    )
    selected = active + terminal[:recent]
    dropped_programs += max(len(terminal) - recent, 0)

    unique: list[IssueView] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        identity = (issue.code, issue.operation_id)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(issue)
    dropped_issues += max(len(unique) - MAX_ISSUES, 0)
    return DashboardProjection(
        "all",
        classification,
        "observed" if inventory is not None else "unavailable",
        tuple(item[2] for item in selected),
        tuple(unique[:MAX_ISSUES]),
        {"programs": dropped_programs, "issues": dropped_issues},
    )


def _marker_root() -> Path:
    return Path(tempfile.gettempdir()) / f"llm-obsidian-dashboard-{os.getuid()}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_marker(path: Path, value: dict[str, object], *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        os.write(descriptor, payload.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_marker(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CmuxError("dashboard marker is invalid") from exc
    if not isinstance(value, dict):
        raise CmuxError("dashboard marker is invalid")
    return value


def open_dashboard(
    *,
    vault: Path,
    store: Path,
    caller_surface: str,
    adapter: CmuxAdapter | None = None,
    marker_root: Path | None = None,
) -> OpenResult:
    """Open at most one external observer split for one vault/workspace pair."""

    resolved_vault = vault.expanduser().resolve()
    resolved_store = store.expanduser().resolve()
    if not resolved_vault.is_dir() or not resolved_store.is_dir():
        raise CmuxError("dashboard vault and store must be existing directories")
    if not resolved_store.is_relative_to(resolved_vault):
        raise CmuxError("dashboard store must belong to the exact vault")
    if not UUID_RE.fullmatch(caller_surface):
        raise CmuxError("coordinator surface must be an exact UUID")

    cmux = adapter or CmuxAdapter()
    inventory = cmux.surface_workspaces()
    caller_key = caller_surface.casefold()
    if caller_key in inventory.ambiguous_surfaces:
        raise CmuxError("coordinator surface placement is ambiguous")
    workspace_id = inventory.surface_workspaces.get(caller_key, "")
    if not UUID_RE.fullmatch(workspace_id):
        raise CmuxError("coordinator surface has no exact workspace identity")

    vault_digest = _digest(str(resolved_vault))
    store_digest = _digest(str(resolved_store))
    marker_key = _digest(f"{vault_digest}\0{workspace_id.casefold()}")
    markers = (marker_root or _marker_root()).expanduser().resolve()
    markers.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = markers / f"{marker_key}.json"
    expected = {
        "schema_version": 1,
        "marker_key": marker_key,
        "vault_sha256": vault_digest,
        "store_sha256": store_digest,
        "workspace_id": workspace_id,
    }
    if marker.exists():
        value = _read_marker(marker)
        if any(value.get(key) != item for key, item in expected.items()):
            raise CmuxError("dashboard marker identity does not match the caller")
        surface_id = str(value.get("surface_id") or "")
        if value.get("state") != "ready" or not UUID_RE.fullmatch(surface_id):
            raise CmuxError("dashboard marker is reserved but not ready")
        surface_key = surface_id.casefold()
        if (
            surface_key in inventory.ambiguous_surfaces
            or inventory.surface_workspaces.get(surface_key, "").casefold()
            != workspace_id.casefold()
        ):
            raise CmuxError("dashboard surface identity is missing or ambiguous")
        return OpenResult(surface_id, workspace_id, True)

    reservation = {**expected, "state": "reserved", "surface_id": ""}
    try:
        _write_marker(marker, reservation, exclusive=True)
    except FileExistsError:
        raise CmuxError("dashboard marker was reserved concurrently") from None

    surface = cmux.open_split(caller_surface)
    if (
        not UUID_RE.fullmatch(surface.surface_id)
        or surface.workspace_id.casefold() != workspace_id.casefold()
    ):
        raise CmuxError("dashboard split returned an invalid placement")
    _write_marker(
        marker,
        {**expected, "state": "ready", "surface_id": surface.surface_id},
        exclusive=False,
    )
    command = shlex.join(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--store",
            str(resolved_store),
            "--interval",
            "1",
            "--recent",
            "3",
        ]
    )
    cmux.send(surface.surface_id, command)
    cmux.send_key(surface.surface_id, "Enter")
    return OpenResult(surface.surface_id, workspace_id, False)


def _open_main(argv: Sequence[str]) -> int:
    args = _open_parser().parse_args(argv)
    result = open_dashboard(
        vault=args.vault,
        store=args.store,
        caller_surface=args.surface,
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "reused" if result.reused else "created",
                "surface_id": result.surface_id,
                "workspace_id": result.workspace_id,
            },
            sort_keys=True,
        )
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    inventory_probe: Callable[..., LiveInventory | None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] | None = None,
) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ["open"]:
        return _open_main(values[1:])
    args = _live_parser().parse_args(values)
    emit = output or (lambda value: print(value, end=""))
    color = not args.no_color and sys.stdout.isatty()
    try:
        while True:
            inventory = _probe_inventory(inventory_probe)
            projection = snapshot(args.store, recent=args.recent, inventory=inventory)
            text = render(projection, recent=args.recent, color=color)
            emit(text if args.once else CLEAR + text)
            if args.once:
                return 0
            sleeper(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
