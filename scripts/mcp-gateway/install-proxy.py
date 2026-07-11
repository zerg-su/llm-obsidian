#!/usr/bin/env python3
"""Install the exact mcp-proxy artifact recorded in mcp-proxy.lock.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Optional


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_name = {"darwin": "darwin", "linux": "linux"}.get(system)
    arch = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }.get(machine)
    if not os_name or not arch:
        raise ValueError(f"unsupported mcp-proxy platform: {system}/{machine}")
    return f"{os_name}-{arch}"


def load_lock(path: Path, key: str) -> tuple[dict, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    assets = data.get("assets")
    if not isinstance(version, str) or not version:
        raise ValueError("lock file needs a non-empty version")
    if not isinstance(assets, dict) or key not in assets:
        raise ValueError(f"lock file has no asset for {key}")
    asset = assets[key]
    if not isinstance(asset, dict):
        raise ValueError(f"invalid asset entry for {key}")
    url = asset.get("url")
    checksum = asset.get("sha256")
    if not isinstance(url, str) or not url.startswith(("https://", "file://")):
        raise ValueError(f"asset URL for {key} must use https or file")
    if not isinstance(checksum, str) or len(checksum) != 64:
        raise ValueError(f"asset SHA-256 for {key} is invalid")
    int(checksum, 16)
    return data, asset


def marker_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".install.json")


def expected_marker(lock: dict, asset: dict, key: str, binary_hash: str) -> dict:
    return {
        "version": lock["version"],
        "platform": key,
        "url": asset["url"],
        "archive_sha256": asset["sha256"],
        "binary_sha256": binary_hash,
    }


def installed_matches(dest: Path, lock: dict, asset: dict, key: str) -> bool:
    marker = marker_path(dest)
    if not dest.is_file() or not os.access(dest, os.X_OK) or not marker.is_file():
        return False
    try:
        actual = json.loads(marker.read_text(encoding="utf-8"))
        return actual == expected_marker(lock, asset, key, sha256(dest))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "llm-obsidian-bootstrap"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as out:
        while block := response.read(1024 * 1024):
            out.write(block)


def safe_binary_member(archive: tarfile.TarFile) -> tarfile.TarInfo:
    candidates: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"archive links are not allowed: {member.name}")
        if member.isfile() and path.name in {"mcp-proxy", "mcp_proxy"}:
            candidates.append(member)
    if len(candidates) != 1:
        raise ValueError(f"archive must contain exactly one mcp-proxy binary; found {len(candidates)}")
    return candidates[0]


def atomic_json(path: Path, value: dict, mode: int = 0o644) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def install(dest: Path, lock: dict, asset: dict, key: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcp-proxy-install.") as tmp_dir:
        tmp = Path(tmp_dir)
        archive_path = tmp / "mcp-proxy.tar.gz"
        download(asset["url"], archive_path)
        actual_archive_hash = sha256(archive_path)
        if actual_archive_hash != asset["sha256"]:
            raise ValueError(
                f"mcp-proxy archive checksum mismatch: {actual_archive_hash} != {asset['sha256']}"
            )

        candidate = tmp / "mcp-proxy"
        with tarfile.open(archive_path, "r:gz") as archive:
            member = safe_binary_member(archive)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("could not read mcp-proxy binary from archive")
            with candidate.open("wb") as out:
                while block := source.read(1024 * 1024):
                    out.write(block)
        candidate.chmod(0o755)
        version_output = subprocess.check_output(
            [str(candidate), "-version"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        if lock["version"] not in version_output:
            raise ValueError(
                f"downloaded binary reports {version_output!r}, expected {lock['version']}"
            )

        binary_hash = sha256(candidate)
        fd, staged_name = tempfile.mkstemp(prefix=dest.name + ".tmp.", dir=dest.parent)
        staged = Path(staged_name)
        try:
            with os.fdopen(fd, "wb") as out, candidate.open("rb") as source:
                while block := source.read(1024 * 1024):
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            staged.chmod(0o755)
            os.replace(staged, dest)
        finally:
            staged.unlink(missing_ok=True)
        atomic_json(marker_path(dest), expected_marker(lock, asset, key, binary_hash))
        print(f"installed mcp-proxy {lock['version']} ({key}) -> {dest}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--platform", help="test/override key such as darwin-arm64")
    parser.add_argument("--plan", action="store_true", help="describe without downloading or writing")
    parser.add_argument("--check", action="store_true", help="verify installed binary + provenance marker")
    args = parser.parse_args()

    try:
        key = args.platform or platform_key()
        lock, asset = load_lock(args.lock, key)
        dest = args.dest.expanduser().resolve()
        if args.plan:
            state = "current" if installed_matches(dest, lock, asset, key) else "install required"
            print(f"mcp-proxy {lock['version']} {key}: {state}; {asset['url']} -> {dest}")
            return 0
        if args.check:
            if installed_matches(dest, lock, asset, key):
                print(f"OK mcp-proxy {lock['version']} ({key})")
                return 0
            print(f"DRIFT mcp-proxy {lock['version']} ({key}): reinstall required", file=sys.stderr)
            return 1
        if installed_matches(dest, lock, asset, key):
            print(f"keep: mcp-proxy {lock['version']} ({key}) at {dest}")
            return 0
        install(dest, lock, asset, key)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
