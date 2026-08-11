#!/usr/bin/env python3
"""Read-only live Harness dashboard and external cmux launcher."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.cmux import CmuxAdapter, CmuxError, UUID_RE
from harness.contracts import ID_RE
from harness.dashboard_projection import (
    ACTIVE,
    ATTENTION,
    HEALTHY,
    MAX_ISSUES,
    WAITING,
    DashboardProjection,
    IssueView,
    escalate,
    project,
    project_root,
)
from harness.dashboard_view import render
from harness.state_machine import TERMINAL
from harness.status_segment import LiveInventory, live_inventory


MIN_INTERVAL = 0.1
MAX_INTERVAL = 60.0
RECENT_CHOICES = (2, 3)
CLEAR = "\x1b[2J\x1b[H"
RESERVATION_STALE_SECONDS = 30.0


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


def _root_id_argument(value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "root must be one exact bounded operation identity"
        )
    return value


def _live_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-dashboard")
    parser.add_argument("--store", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--root",
        type=_root_id_argument,
        help="project exactly one root operation and its descendants",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="explicit diagnostic projection across every owner",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="render one unbounded compatibility snapshot",
    )
    parser.add_argument("--interval", type=_interval, default=1.0)
    parser.add_argument("--recent", type=int, choices=RECENT_CHOICES, default=3)
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable semantic ANSI colors",
    )
    return parser


def _open_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-dashboard open")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--surface", required=True)
    parser.add_argument("--root", type=_root_id_argument, required=True)
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
    observed_at: float | None = None,
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
                observed_at=observed_at,
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

    priority = {ACTIVE: 0, WAITING: 1, ATTENTION: 2}
    active = sorted(
        (item for item in programs if item[2].state not in TERMINAL),
        key=lambda item: (
            priority.get(item[2].classification, 3),
            -item[0],
            item[1],
            item[2].operation_id,
        ),
    )
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


def _aliases_caller(surface_id: str, caller_surface: str) -> bool:
    return surface_id.casefold() == caller_surface.casefold()


def _write_marker(path: Path, value: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        os.write(descriptor, payload.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _marker_lock(marker: Path) -> object:
    """Serialize one vault/workspace marker on the marker filesystem."""

    descriptor = os.open(marker.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_marker(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CmuxError("dashboard marker is invalid") from exc
    if not isinstance(value, dict):
        raise CmuxError("dashboard marker is invalid")
    return value


def _open_dashboard_unlocked(
    *,
    resolved_store: Path,
    root_id: str,
    caller_surface: str,
    cmux: CmuxAdapter,
    inventory: object,
    workspace_id: str,
    marker: Path,
    expected: dict[str, object],
    clock: Callable[[], float],
    crash_hook: Callable[[str], None] | None,
) -> OpenResult:
    """Open at most one external observer split for one vault/workspace pair."""
    marker_exists = marker.exists()
    if marker_exists:
        value = _read_marker(marker)
        if any(value.get(key) != item for key, item in expected.items()):
            raise CmuxError("dashboard marker identity does not match the caller")
        state = str(value.get("state") or "")
        surface_id = str(value.get("surface_id") or "")
        surface_key = surface_id.casefold()
        caller_alias = _aliases_caller(surface_id, caller_surface)
        live = (
            UUID_RE.fullmatch(surface_id) is not None
            and not caller_alias
            and surface_key not in inventory.ambiguous_surfaces
            and inventory.surface_workspaces.get(surface_key, "").casefold()
            == workspace_id.casefold()
        )
        if state == "ready" and live:
            return OpenResult(surface_id, workspace_id, True)
        if state == "starting":
            started_at = value.get("reserved_at")
            if (
                not isinstance(started_at, (int, float))
                or isinstance(started_at, bool)
            ):
                raise CmuxError("dashboard starting marker is invalid")
            if live and clock() - float(started_at) < RESERVATION_STALE_SECONDS:
                raise CmuxError(
                    "dashboard startup is incomplete on a live exact surface"
                )
            if live:
                cmux.close_exact(surface_id)
        if state == "reserved":
            reserved_at = value.get("reserved_at")
            if (
                not isinstance(reserved_at, (int, float))
                or isinstance(reserved_at, bool)
                or clock() - float(reserved_at) < RESERVATION_STALE_SECONDS
            ):
                raise CmuxError("dashboard marker was reserved concurrently")
        elif state not in {"ready", "starting", "retryable"}:
            raise CmuxError("dashboard marker state is invalid")

    reservation = {
        **expected,
        "state": "reserved",
        "surface_id": "",
        "reserved_at": clock(),
    }
    try:
        _write_marker(marker, reservation)
    except OSError as exc:
        raise CmuxError("dashboard marker reservation failed") from exc

    try:
        surface = cmux.open_split(caller_surface)
    except Exception:
        _write_marker(
            marker,
            {**expected, "state": "retryable", "surface_id": "", "reserved_at": 0},
        )
        raise
    caller_alias = _aliases_caller(surface.surface_id, caller_surface)
    invalid_placement = (
        not UUID_RE.fullmatch(surface.surface_id)
        or surface.workspace_id.casefold() != workspace_id.casefold()
    )
    if caller_alias or invalid_placement:
        if UUID_RE.fullmatch(surface.surface_id) and not caller_alias:
            try:
                cmux.close_exact(surface.surface_id)
            except CmuxError:
                pass
        _write_marker(
            marker,
            {**expected, "state": "retryable", "surface_id": "", "reserved_at": 0},
        )
        raise CmuxError(
            "dashboard split returned the caller surface"
            if caller_alias
            else "dashboard split returned an invalid placement"
        )
    _write_marker(
        marker,
        {
            **expected,
            "state": "starting",
            "surface_id": surface.surface_id,
            "reserved_at": clock(),
        },
    )
    if crash_hook is not None:
        crash_hook("starting-published")
    command = shlex.join(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--store",
            str(resolved_store),
            "--root",
            root_id,
            "--interval",
            "1",
            "--recent",
            "3",
        ]
    )
    try:
        cmux.send(surface.surface_id, command)
        cmux.send_key(surface.surface_id, "Enter")
        if crash_hook is not None:
            crash_hook("startup-delivered")
    except Exception:
        try:
            cmux.close_exact(surface.surface_id)
        finally:
            _write_marker(
                marker,
                {
                    **expected,
                    "state": "retryable",
                    "surface_id": "",
                    "reserved_at": 0,
                },
            )
        raise
    _write_marker(
        marker,
        {
            **expected,
            "state": "ready",
            "surface_id": surface.surface_id,
            "reserved_at": 0,
        },
    )
    return OpenResult(surface.surface_id, workspace_id, False)


def open_dashboard(
    *,
    vault: Path,
    store: Path,
    caller_surface: str,
    root: str,
    adapter: CmuxAdapter | None = None,
    marker_root: Path | None = None,
    clock: Callable[[], float] = time.time,
    crash_hook: Callable[[str], None] | None = None,
) -> OpenResult:
    """Serialize and open one root-scoped external observer split.

    The split identity is the vault plus the exact coordinator workspace plus
    one root operation id, so reopening one request reuses exactly one split
    and a second request owns a second split.
    """

    resolved_vault = vault.expanduser().resolve()
    resolved_store = store.expanduser().resolve()
    if not resolved_vault.is_dir() or not resolved_store.is_dir():
        raise CmuxError("dashboard vault and store must be existing directories")
    if not resolved_store.is_relative_to(resolved_vault):
        raise CmuxError("dashboard store must belong to the exact vault")
    if not UUID_RE.fullmatch(caller_surface):
        raise CmuxError("coordinator surface must be an exact UUID")
    if not ID_RE.fullmatch(root):
        raise CmuxError("dashboard root must be one exact operation identity")
    cmux = adapter or CmuxAdapter()
    markers = (marker_root or _marker_root()).expanduser().resolve()
    markers.mkdir(parents=True, exist_ok=True, mode=0o700)
    vault_digest = _digest(str(resolved_vault))
    with _marker_lock(markers / f"{vault_digest}.guard"):
        inventory = cmux.surface_workspaces()
        caller_key = caller_surface.casefold()
        if caller_key in inventory.ambiguous_surfaces:
            raise CmuxError("coordinator surface placement is ambiguous")
        workspace_id = inventory.surface_workspaces.get(caller_key, "")
        if not UUID_RE.fullmatch(workspace_id):
            raise CmuxError("coordinator surface has no exact workspace identity")
        marker_key = _digest(f"{vault_digest}\0{workspace_id.casefold()}\0{root}")
        marker = markers / f"{marker_key}.json"
        expected = {
            "schema_version": 3,
            "marker_key": marker_key,
            "vault_sha256": vault_digest,
            "store_sha256": _digest(str(resolved_store)),
            "workspace_id": workspace_id,
            "root_id": root,
        }
        return _open_dashboard_unlocked(
            resolved_store=resolved_store,
            root_id=root,
            caller_surface=caller_surface,
            cmux=cmux,
            inventory=inventory,
            workspace_id=workspace_id,
            marker=marker,
            expected=expected,
            clock=clock,
            crash_hook=crash_hook,
        )


def _open_main(argv: Sequence[str]) -> int:
    args = _open_parser().parse_args(argv)
    result = open_dashboard(
        vault=args.vault,
        store=args.store,
        caller_surface=args.surface,
        root=args.root,
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "reused" if result.reused else "created",
                "surface_id": result.surface_id,
                "workspace_id": result.workspace_id,
                "root": args.root,
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
    tty_probe: Callable[[], bool] | None = None,
    terminal_rows: Callable[[], int] | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ["open"]:
        return _open_main(values[1:])
    args = _live_parser().parse_args(values)
    emit = output or (lambda value: print(value, end=""))
    is_tty = (tty_probe or sys.stdout.isatty)()
    color = not args.no_color and is_tty
    row_probe = terminal_rows or (
        lambda: shutil.get_terminal_size(fallback=(80, 24)).lines
    )
    try:
        while True:
            observed_at = clock()
            inventory = _probe_inventory(inventory_probe)
            projection = (
                snapshot(
                    args.store,
                    recent=args.recent,
                    inventory=inventory,
                    observed_at=observed_at,
                )
                if args.all
                else project_root(
                    args.store,
                    args.root,
                    inventory=inventory,
                    surface_probe=(
                        "observed" if inventory is not None else "unavailable"
                    ),
                    observed_at=observed_at,
                )
            )
            rows = max(int(row_probe()), 1) if is_tty and not args.once else None
            text = render(
                projection,
                recent=args.recent,
                color=color,
                rows=rows,
                scope="owner" if args.all else "root",
            )
            emit(text if args.once else CLEAR + text)
            if args.once:
                return 0
            sleeper(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
