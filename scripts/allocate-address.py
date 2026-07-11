#!/usr/bin/env python3
"""Portable, atomic creation-order address allocation using stdlib fcntl."""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path
from typing import Optional

from vault_schema import ADDRESS_RX, FrontmatterError, parse_frontmatter, split_frontmatter


ROOT = Path(__file__).resolve().parents[1]
META = ROOT / ".vault-meta"
WIKI = ROOT / "wiki"
COUNTER = META / "address-counter.txt"
LOCK = META / ".address.lock"


def atomic_write(path: Path, value: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def scan_highest() -> int:
    highest = 0
    if not WIKI.is_dir():
        return highest
    for path in WIKI.rglob("*.md"):
        block = split_frontmatter(path.read_text(encoding="utf-8"))
        if block is None:
            continue
        try:
            raw = parse_frontmatter(block).get("address")
        except FrontmatterError:
            continue
        match = ADDRESS_RX.fullmatch(str(raw or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def read_counter() -> int:
    if not COUNTER.exists():
        recovered = scan_highest() + 1
        atomic_write(COUNTER, f"{recovered}\n")
        print(
            f"INFO: counter file missing; recovered from vault scan, set to {recovered}",
            file=sys.stderr,
        )
    raw = COUNTER.read_text(encoding="utf-8").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(raw)
    return int(raw)


def existing_counter() -> Optional[int]:
    """Return a valid persisted counter, or None when rebuild must recover it."""
    try:
        raw = COUNTER.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit() or int(raw) < 1:
        return None
    return int(raw)


def acquire(lock_file: object, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "allocate"
    if mode not in {"allocate", "--peek", "--rebuild"}:
        print(f"ERR: unknown mode: {mode}", file=sys.stderr)
        print(f"Usage: {Path(sys.argv[0]).name} [allocate|--peek|--rebuild]", file=sys.stderr)
        return 3
    try:
        META.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("ERR: cannot create .vault-meta/", file=sys.stderr)
        return 2

    try:
        with LOCK.open("a+", encoding="utf-8") as lock_file:
            if not acquire(lock_file):
                print("ERR: could not acquire address allocator lock within 5s", file=sys.stderr)
                return 1
            if mode == "--rebuild":
                floor = scan_highest() + 1
                current = existing_counter()
                next_value = max(current or 0, floor)
                atomic_write(COUNTER, f"{next_value}\n")
                action = "preserved" if current is not None and current > floor else "rebuilt"
                print(f"Counter {action}: next = {next_value}")
                return 0
            try:
                current = read_counter()
            except (OSError, ValueError) as exc:
                raw = exc.args[0] if exc.args else ""
                print(f"ERR: counter file content is not a positive integer: {raw}", file=sys.stderr)
                return 3
            if mode == "--peek":
                print(current)
                return 0
            atomic_write(COUNTER, f"{current + 1}\n")
            print(f"c-{current:06d}")
            return 0
    except OSError as exc:
        print(f"ERR: address allocator I/O failure: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
