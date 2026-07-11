#!/usr/bin/env python3
"""Generate Codex-native packaging for llm-obsidian.

The Claude plugin manifest remains the compatibility source of truth. This
script mirrors the parts Codex needs natively:

  - .codex-plugin/plugin.json
  - .agents/plugins/marketplace.json for repo-scoped Codex discovery

Run from repo root:
  python3 scripts/codex-adapter.py --check
  python3 scripts/codex-adapter.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class DesiredFile:
    path: Path
    text: str


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def load_claude_plugin(repo_root: Path) -> dict[str, Any]:
    path = repo_root / ".claude-plugin" / "plugin.json"
    if not path.exists():
        raise SystemExit(f"ERROR: missing {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"name", "version", "description"}
    missing = sorted(required - set(data))
    if missing:
        raise SystemExit(f"ERROR: {path} missing required keys: {', '.join(missing)}")
    return data


def codex_author(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {"name": raw.strip()}
    return None


def codex_version(repo_root: Path, source_version: str) -> str:
    """Keep an applied +codex cachebuster until the Claude base version changes."""

    base = source_version.split("+", 1)[0]
    path = repo_root / ".codex-plugin" / "plugin.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, AttributeError, json.JSONDecodeError):
        current = None
    if isinstance(current, str) and current.startswith(base + "+codex."):
        return current
    return source_version


def codex_plugin_text(repo_root: Path) -> str:
    src = load_claude_plugin(repo_root)
    plugin_name = src["name"]
    plugin = {
        "name": plugin_name,
        "version": codex_version(repo_root, str(src["version"])),
        "description": src["description"],
        "author": codex_author(src.get("author")),
        "homepage": src.get("homepage"),
        "repository": src.get("repository"),
        "license": src.get("license"),
        "keywords": [*src.get("keywords", []), "codex"],
        "skills": "./skills/",
        "interface": {
            "displayName": plugin_name,
            "shortDescription": "Obsidian LLM wiki workflows for Codex",
            "longDescription": (
                "A self-organizing Obsidian vault companion for Codex: ingest "
                "sources, query accumulated knowledge, file session notes, lint "
                "wiki health, and maintain DragonScale-style long-term memory."
            ),
            "developerName": "zerg-su",
            "category": "Productivity",
            "capabilities": ["Interactive", "Read", "Write"],
            "defaultPrompt": [
                "Ingest this source into the wiki",
                "What do we know about this topic?",
                "Save this session into the vault",
            ],
            "brandColor": "#5B6CFF",
            "screenshots": [],
        },
    }
    return json_text({k: v for k, v in plugin.items() if v is not None})


def marketplace_text(repo_root: Path) -> str:
    src = load_claude_plugin(repo_root)
    plugin_name = src["name"]
    payload = {
        "name": f"{plugin_name}-codex",
        "interface": {
            "displayName": f"{plugin_name} Codex",
        },
        "plugins": [
            {
                "name": plugin_name,
                "source": {
                    "source": "local",
                    "path": "./",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    return json_text(payload)


def desired_files(repo_root: Path) -> list[DesiredFile]:
    return [
        DesiredFile(repo_root / ".codex-plugin" / "plugin.json", codex_plugin_text(repo_root)),
        DesiredFile(repo_root / ".agents" / "plugins" / "marketplace.json", marketplace_text(repo_root)),
    ]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def changed_files(files: list[DesiredFile]) -> list[DesiredFile]:
    changed: list[DesiredFile] = []
    for item in files:
        try:
            current = item.path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != item.text:
            changed.append(item)
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Codex plugin adapter files.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (default: script parent repo)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check for drift without writing")
    mode.add_argument("--apply", action="store_true", help="Write generated files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.expanduser().resolve()
    files = desired_files(repo_root)
    changed = changed_files(files)
    if not changed:
        print("codex-adapter: no changes")
        return 0
    if not args.apply:
        for item in changed:
            print(f"codex-adapter: would update {item.path}")
        return 1
    for item in changed:
        write_atomic(item.path, item.text)
        print(f"codex-adapter: wrote {item.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
