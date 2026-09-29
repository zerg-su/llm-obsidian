#!/usr/bin/env python3
"""Keep local, private snapshots of the Codex CLI file-based login.

Snapshots preserve the local cache; they cannot revive a token revoked by
``codex logout`` or the service. Use ``switch`` before logging into another
account so the first account's cache is not explicitly revoked.
"""

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
from datetime import datetime


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def read_auth(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read()
    finally:
        os.close(fd)
    if not isinstance(json.loads(data), dict):
        raise ValueError(f"Not a Codex auth JSON object: {path}")
    return data


def backup_dir(home: Path) -> Path:
    directory = home / "auth-backups"
    if directory.is_symlink():
        raise ValueError(f"Refusing symlinked backup directory: {directory}")
    directory.mkdir(mode=0o700, exist_ok=True)
    if not directory.is_dir():
        raise ValueError(f"Not a backup directory: {directory}")
    directory.chmod(0o700)
    return directory


def backup(home: Path) -> Path:
    data = read_auth(home / "auth.json")
    directory = backup_dir(home)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for suffix in range(1000):
        name = f"auth-{stamp}.json" if suffix == 0 else f"auth-{stamp}-{suffix}.json"
        target = directory / name
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if read_auth(target) != data:
                raise OSError("Backup verification failed")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target
    raise FileExistsError("Too many backups with the same timestamp")


def snapshots(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("auth-*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)


def restore(home: Path, name: str) -> tuple[Path, Path | None]:
    directory = backup_dir(home)
    if name == "latest":
        available = snapshots(directory)
        if not available:
            raise FileNotFoundError("No Codex auth backups found")
        source = available[0]
    else:
        if Path(name).name != name or not name.startswith("auth-") or not name.endswith(".json"):
            raise ValueError("Use a filename from the list command")
        source = directory / name
    data = read_auth(source)
    current = home / "auth.json"
    previous = None
    if current.exists():
        if read_auth(current) == data:
            return source, None
        previous = backup(home)
    fd, temp_name = tempfile.mkstemp(prefix=".auth-restore-", dir=home)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, current)
    finally:
        temp.unlink(missing_ok=True)
    return source, previous


def switch(home: Path) -> Path:
    """Save the active file and clear it locally without calling codex logout."""
    saved = backup(home)
    (home / "auth.json").unlink()
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: backup; switch; list; restore auth-YYYY-MM-DD_HH-MM-SS.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup", help="save the current auth.json with a timestamp")
    sub.add_parser("switch", help="back up and clear local auth.json before signing into another account")
    sub.add_parser("list", help="show available backup filenames")
    restore_parser = sub.add_parser("restore", help="restore a listed backup; first back up current auth")
    restore_parser.add_argument("snapshot", help="filename from list, or latest")
    args = parser.parse_args()
    home = codex_home()
    try:
        if args.command == "backup":
            print(f"Saved: {backup(home)}")
        elif args.command == "switch":
            print(f"Saved: {switch(home)}")
            print("Local login cleared. Run `codex login` for the other account; do not run `codex logout`.")
        elif args.command == "list":
            for path in snapshots(backup_dir(home)):
                print(path.name)
        else:
            source, previous = restore(home, args.snapshot)
            if previous:
                print(f"Previous login saved: {previous}")
            print(f"Restored: {source}")
            print("This restores the local cache only; revoked or expired tokens require `codex login`.")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"codex-auth-backup: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
