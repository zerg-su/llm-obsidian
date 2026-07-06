#!/usr/bin/env python3
"""Sync Codex MCP config from llm-obsidian MCP JSON files."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path


MARKER_BEGIN = "# BEGIN LLM-OBSIDIAN CODEX MCP (managed by scripts/mcp-gateway/codex-sync.py)"
MARKER_END = "# END LLM-OBSIDIAN CODEX MCP"

def mcp_source_path(repo_root: Path) -> Path:
    path = repo_root / ".mcp.json"
    if path.exists():
        return path
    example = repo_root / ".mcp.json.example"
    if example.exists():
        return example
    raise SystemExit(f"ERROR: neither {path} nor {example} exists")


def plugin_name(repo_root: Path) -> str:
    path = repo_root / ".claude-plugin" / "plugin.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: invalid {path}: {e}") from e
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return repo_root.name


def default_profile_name(repo_root: Path) -> str:
    return f"{plugin_name(repo_root)}-mcp"


def profile_paths(repo_root: Path) -> OrderedDict[str, Path]:
    out: OrderedDict[str, Path] = OrderedDict()
    profiles_dir = repo_root / ".mcp-profiles"
    if not profiles_dir.is_dir():
        return out
    prefix = plugin_name(repo_root)
    for path in sorted(profiles_dir.glob("*.json")):
        out[f"{prefix}-{path.stem}"] = path
    return out


def load_json_servers(path: Path) -> OrderedDict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise SystemExit(f"ERROR: {path} has no mcpServers object")
    out: OrderedDict[str, dict[str, str]] = OrderedDict()
    for name, cfg in servers.items():
        url = cfg.get("url")
        if not isinstance(url, str) or not url:
            raise SystemExit(f"ERROR: {path}: {name} has no url")
        out[name] = {"url": url}
    return out


def merged_servers(repo_root: Path, profile_path: Path | None = None) -> OrderedDict[str, dict[str, str]]:
    servers = load_json_servers(mcp_source_path(repo_root))
    if profile_path is not None:
        extra = load_json_servers(profile_path)
        for name, cfg in extra.items():
            servers[name] = cfg
    return servers


def all_managed_names(repo_root: Path) -> set[str]:
    names = set(load_json_servers(mcp_source_path(repo_root)))
    for path in profile_paths(repo_root).values():
        names.update(load_json_servers(path))
    return names


def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def managed_block(servers: OrderedDict[str, dict[str, str]]) -> str:
    lines = [
        MARKER_BEGIN,
        "# Source of truth: .mcp.json or .mcp.json.example plus .mcp-profiles/*.json.",
        "# Secrets stay in ~/.config/mcp-gateway/secrets.env behind the gateway.",
        "",
    ]
    for name, cfg in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        lines.append(f"url = {toml_quote(cfg['url'])}")
        lines.append("")
    lines.append(MARKER_END)
    lines.append("")
    return "\n".join(lines)


def strip_marked_block(text: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(MARKER_BEGIN)}.*?{re.escape(MARKER_END)}\n?",
        re.DOTALL,
    )
    return pattern.sub("\n", text)


def section_belongs_to_managed_server(section: str, names: set[str]) -> bool:
    prefix = "mcp_servers."
    if not section.startswith(prefix):
        return False
    rest = section[len(prefix) :]
    for name in names:
        if rest == name or rest.startswith(f"{name}."):
            return True
    return False


def strip_mcp_sections(text: str, names: set[str]) -> str:
    text = strip_marked_block(text)
    matches = list(re.finditer(r"(?m)^\[([^\]\n]+)\]\s*$", text))
    if not matches:
        return text

    pieces: list[str] = []
    cursor = 0
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        pieces.append(text[cursor:start])
        section = match.group(1)
        if not section_belongs_to_managed_server(section, names):
            pieces.append(text[start:end])
        cursor = end
    pieces.append(text[cursor:])

    cleaned = "".join(pieces)
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)
    return cleaned.strip() + "\n"


def render_repo_config(existing: str, repo_root: Path) -> str:
    names = all_managed_names(repo_root)
    base = strip_mcp_sections(existing, names).rstrip()
    block = managed_block(merged_servers(repo_root)).rstrip()
    return f"{base}\n\n{block}\n"


def render_global_config(existing: str, repo_root: Path) -> str:
    return strip_mcp_sections(existing, all_managed_names(repo_root))


def profile_text(repo_root: Path, profile_path: Path | None) -> str:
    servers = merged_servers(repo_root, profile_path)
    return (
        f"# {plugin_name(repo_root)} Codex MCP profile.\n"
        "# Generated by scripts/mcp-gateway/codex-sync.py; edit .mcp.json or .mcp-profiles/*.json instead.\n\n"
        + managed_block(servers)
    )


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def diff_text(path: Path, old: str, new: str) -> str:
    if old == new:
        return f"--- {path}\n+++ {path}\n(no changes)\n"
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def backup_name(path: Path) -> str:
    label = str(path.expanduser().resolve()).lstrip(os.sep)
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", label)


def write_atomic(path: Path, content: str) -> None:
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o777
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(content, encoding="utf-8")
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_with_backup(path: Path, content: str, backup_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    if path.exists():
        backup = backup_dir / backup_name(path)
        shutil.copy2(path, backup)
    write_atomic(path, content)


def desired_files(repo_root: Path, codex_home: Path) -> dict[Path, str]:
    repo_config = repo_root / ".codex" / "config.toml"
    global_config = codex_home / "config.toml"
    files = {
        repo_config: render_repo_config(read_file(repo_config), repo_root),
        global_config: render_global_config(read_file(global_config), repo_root),
        codex_home / f"{default_profile_name(repo_root)}.config.toml": profile_text(repo_root, None),
    }
    for profile_name, path in profile_paths(repo_root).items():
        files[codex_home / f"{profile_name}.config.toml"] = profile_text(repo_root, path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Codex MCP TOML config from repo MCP JSON files.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print diffs and exit non-zero if changes are needed")
    mode.add_argument("--apply", action="store_true", help="write files with backups")
    parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[2], type=Path)
    parser.add_argument("--codex-home", default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(), type=Path)
    args = parser.parse_args()

    if not args.check and not args.apply:
        args.check = True

    repo_root = args.repo_root.resolve()
    codex_home = args.codex_home.expanduser().resolve()
    files = desired_files(repo_root, codex_home)

    changed = []
    for path, new in files.items():
        old = read_file(path)
        if old != new:
            changed.append((path, old, new))

    if args.check:
        for path, old, new in changed:
            sys.stdout.write(diff_text(path, old, new))
            if not new.endswith("\n"):
                sys.stdout.write("\n")
        if not changed:
            print("codex-sync: no changes")
        return 1 if changed else 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = codex_home / "backups" / f"{plugin_name(repo_root)}-codex-sync-{stamp}"
    for path, _old, new in changed:
        write_with_backup(path, new, backup_dir)
        print(f"wrote {path}")
    if changed:
        print(f"backup dir: {backup_dir}")
    else:
        print("codex-sync: no changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
