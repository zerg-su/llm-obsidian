#!/usr/bin/env python3
"""Verify pinned upstream skill snapshots without executing snapshot code."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


SNAPSHOT_ROOT = Path(__file__).resolve().parent
MANIFEST = SNAPSHOT_ROOT / "manifest.json"


def inspect_tree(root: Path) -> tuple[int, int, str]:
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    invalid = [path for path in entries if path.is_symlink() or not (path.is_dir() or path.is_file())]
    if invalid:
        names = ", ".join(path.relative_to(root).as_posix() for path in invalid)
        raise ValueError(f"unsupported snapshot entries: {names}")

    files = [path for path in entries if path.is_file()]
    digest = hashlib.sha256()
    byte_count = 0
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        byte_count += len(content)
    return len(files), byte_count, digest.hexdigest()


def verify_manifest(manifest_path: Path) -> tuple[list[str], list[str]]:
    manifest_path = manifest_path.resolve()
    snapshot_root = manifest_path.parent
    repo_root = snapshot_root.parents[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    successes: list[str] = []
    failures: list[str] = []
    for name, expected in sorted(manifest["sources"].items()):
        root = (repo_root / expected["local_path"]).resolve()
        try:
            root.relative_to(snapshot_root)
        except ValueError:
            failures.append(f"{name}: local_path escapes {snapshot_root}")
            continue
        if root.parent != snapshot_root or not root.is_dir():
            failures.append(f"{name}: snapshot directory is missing or not a direct child")
            continue

        try:
            files, byte_count, digest = inspect_tree(root)
        except ValueError as exc:
            failures.append(f"{name}: {exc}")
            continue
        actual = {"files": files, "bytes": byte_count, "tree_sha256": digest}
        drift = [
            f"{field}={actual[field]!r} (expected {expected.get(field)!r})"
            for field in actual
            if actual[field] != expected.get(field)
        ]
        if drift:
            failures.append(f"{name}: " + ", ".join(drift))
        else:
            successes.append(f"{name}: {files} files, {byte_count} bytes, {digest}")
    return successes, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    try:
        successes, failures = verify_manifest(args.manifest)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"FAIL manifest: {exc}", file=sys.stderr)
        return 1

    for success in successes:
        print(f"OK   {success}")
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print(f"\nAll {len(successes)} upstream snapshots match manifest.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
