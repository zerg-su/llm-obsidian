#!/usr/bin/env python3
"""Install pinned Obsidian Tasks assets without clobbering vault user state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ASSET_NAMES = ("main.js", "manifest.json", "styles.css")
SHA_RX = re.compile(r"[0-9a-f]{64}")


class SetupError(RuntimeError):
    pass


def json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"{label} JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


class Backups:
    def __init__(self, vault: Path):
        self.vault = vault
        self.root: Path | None = None

    def ensure(self) -> Path:
        if self.root is not None:
            return self.root
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        base = self.vault / ".obsidian-backups" / stamp
        candidate = base
        suffix = 1
        while candidate.exists():
            candidate = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        candidate.mkdir(parents=True)
        self.root = candidate
        return candidate

    def copy(self, source: Path, relative: Path) -> Path:
        target = self.ensure() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"backup: {target}")
        return target


def validate_lock(path: Path) -> tuple[str, str, dict[str, dict[str, str]]]:
    lock = json_object(path, "Tasks lock")
    plugin_id = lock.get("plugin_id")
    version = lock.get("version")
    assets = lock.get("assets")
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise SetupError("Tasks lock plugin_id must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise SetupError("Tasks lock version must be a non-empty string")
    if not isinstance(assets, dict) or set(assets) != set(ASSET_NAMES):
        raise SetupError(f"Tasks lock assets must be exactly: {', '.join(ASSET_NAMES)}")
    normalized: dict[str, dict[str, str]] = {}
    for name in ASSET_NAMES:
        item = assets[name]
        if not isinstance(item, dict):
            raise SetupError(f"Tasks lock assets.{name} must be an object")
        url = item.get("url")
        digest = item.get("sha256")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise SetupError(f"Tasks lock assets.{name}.url must use https")
        if not isinstance(digest, str) or SHA_RX.fullmatch(digest) is None:
            raise SetupError(f"Tasks lock assets.{name}.sha256 must be lowercase SHA-256")
        normalized[name] = {"url": url, "sha256": digest}
    return plugin_id, version, normalized


def validate_status_settings(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    custom = value.get("customStatuses")
    if not isinstance(custom, list):
        return False
    return any(isinstance(item, dict) and item.get("symbol") == ">" for item in custom)


def fetch_verified(plugin_parent: Path, assets: dict[str, dict[str, str]]) -> Path:
    temp = Path(tempfile.mkdtemp(prefix=".tasks-download.", dir=plugin_parent))
    try:
        for name in ASSET_NAMES:
            target = temp / name
            result = subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "--retry",
                    "2",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "120",
                    assets[name]["url"],
                    "-o",
                    str(target),
                ]
            )
            if result.returncode:
                raise SetupError(f"Tasks download failed for {name}; existing files were not changed")
            actual = sha256_file(target)
            if actual != assets[name]["sha256"]:
                raise SetupError(
                    f"Tasks checksum mismatch for {name} (got {actual}, expected {assets[name]['sha256']}); "
                    "existing files were not changed"
                )
        return temp
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def install_assets(
    plugin_dir: Path,
    assets: dict[str, dict[str, str]],
    version: str,
    backups: Backups,
    *,
    repair: bool,
) -> bool:
    present = {name: (plugin_dir / name).is_file() for name in ASSET_NAMES}
    actual = {
        name: sha256_file(plugin_dir / name)
        for name in ASSET_NAMES
        if present[name]
    }
    mismatched = {
        name: digest
        for name, digest in actual.items()
        if digest != assets[name]["sha256"]
    }
    missing = [name for name in ASSET_NAMES if not present[name]]
    if not mismatched and not missing:
        print(f"keep: {plugin_dir} (pinned Tasks {version})")
        return True
    if mismatched and not repair:
        details = ", ".join(f"{name}={digest}" for name, digest in sorted(mismatched.items()))
        print(
            f"WARN: preserving existing Tasks assets with checksum mismatch ({details}); "
            "run bin/setup-vault.sh --repair-tasks to back up and replace them",
            file=sys.stderr,
        )
        return False

    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    downloaded = fetch_verified(plugin_dir.parent, assets)
    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        for name in ASSET_NAMES:
            destination = plugin_dir / name
            if destination.exists() and repair:
                backups.copy(destination, Path("plugins") / plugin_dir.name / name)
            if not destination.exists() or repair:
                os.replace(downloaded / name, destination)
        print(f"installed: Obsidian Tasks {version} ({', '.join(ASSET_NAMES)})")
        return True
    finally:
        shutil.rmtree(downloaded, ignore_errors=True)


def merge_community_plugins(path: Path, plugin_id: str, backups: Backups) -> None:
    if path.exists():
        try:
            plugins = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupError(f"invalid community-plugins JSON {path}: {exc}") from exc
        if not isinstance(plugins, list) or any(not isinstance(item, str) for item in plugins):
            raise SetupError("community-plugins.json must be an array of strings")
    else:
        plugins = []
    if plugin_id in plugins:
        print(f"keep: {path} ({plugin_id} enabled)")
        return
    if path.exists():
        backups.copy(path, Path("community-plugins.json"))
    plugins.append(plugin_id)
    atomic_json(path, plugins)
    print(f"updated: {path} ({plugin_id} enabled)")


def merge_data(path: Path, defaults: dict[str, Any], backups: Backups) -> None:
    if not path.exists():
        atomic_json(path, defaults)
        print(f"created: {path} (LLM Obsidian Tasks defaults)")
        return
    current = json_object(path, "Tasks data")
    if "statusSettings" in current and not validate_status_settings(current["statusSettings"]):
        print(
            "WARN: existing Tasks statusSettings has no '>' Migrated/NON_TASK status; preserving it. "
            "The #agenda/migrated marker still excludes carried source items from reports.",
            file=sys.stderr,
        )
    missing = [key for key in defaults if key not in current]
    if not missing:
        print(f"keep: {path} (existing Tasks settings preserved)")
        return
    backups.copy(path, Path("plugins") / path.parent.name / "data.json")
    merged = dict(current)
    for key in missing:
        merged[key] = defaults[key]
    atomic_json(path, merged)
    print(f"updated: {path} (added absent top-level defaults: {', '.join(missing)})")


def install_snippet(source: Path, destination: Path) -> None:
    if destination.exists():
        print(f"keep: {destination}")
        return
    atomic_write(destination, source.read_bytes())
    print(f"created: {destination}")


def enable_fresh_snippet(appearance: Path, snippet_name: str) -> None:
    value = json_object(appearance, "appearance")
    enabled = value.get("enabledCssSnippets")
    if enabled is None:
        enabled = []
    if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
        raise SetupError("appearance.enabledCssSnippets must be an array of strings")
    if snippet_name in enabled:
        return
    updated = dict(value)
    updated["enabledCssSnippets"] = [*enabled, snippet_name]
    atomic_json(appearance, updated)
    print(f"updated: {appearance} (enabled {snippet_name}.css)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--defaults", required=True)
    parser.add_argument("--snippet", required=True)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    try:
        vault = Path(args.vault).resolve()
        obsidian = vault / ".obsidian"
        lock_path = Path(args.lock).resolve()
        defaults_path = Path(args.defaults).resolve()
        snippet_source = Path(args.snippet).resolve()
        plugin_id, version, assets = validate_lock(lock_path)
        defaults = json_object(defaults_path, "Tasks defaults")
        if not snippet_source.is_file():
            raise SetupError(f"Tasks CSS snippet is missing: {snippet_source}")

        # Validate user JSON before downloading so malformed state fails closed.
        community = obsidian / "community-plugins.json"
        if community.exists():
            try:
                raw_plugins = json.loads(community.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SetupError(f"invalid community-plugins JSON {community}: {exc}") from exc
            if not isinstance(raw_plugins, list) or any(not isinstance(item, str) for item in raw_plugins):
                raise SetupError("community-plugins.json must be an array of strings")
        appearance = obsidian / "appearance.json"
        if args.fresh:
            json_object(appearance, "appearance")
        data_path = obsidian / "plugins" / plugin_id / "data.json"
        if data_path.exists():
            json_object(data_path, "Tasks data")

        backups = Backups(vault)
        healthy = install_assets(
            obsidian / "plugins" / plugin_id,
            assets,
            version,
            backups,
            repair=args.repair,
        )
        merge_community_plugins(community, plugin_id, backups)
        merge_data(data_path, defaults, backups)
        install_snippet(snippet_source, obsidian / "snippets" / snippet_source.name)
        if args.fresh:
            enable_fresh_snippet(appearance, snippet_source.stem)
        elif not healthy:
            print("WARN: Tasks plugin assets were preserved; agenda CLI remains usable without the plugin.", file=sys.stderr)
        else:
            print(
                f"note: enable CSS snippet '{snippet_source.stem}' manually in existing vaults if desired",
                file=sys.stderr,
            )
        return 0
    except (SetupError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
