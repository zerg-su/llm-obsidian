#!/usr/bin/env python3
"""Explicit, sanitized Claude-memory backup with a fail-closed secret scan.

The backup is disabled unless CLAUDE_MEMORY_DIR is set or the local
.vault-meta/memory-backup.json explicitly enables a source. No project or
sibling-vault paths are guessed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

VAULT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = VAULT_ROOT / ".claude-memory"
CONFIG_PATH = Path(
    os.environ.get("MEMORY_BACKUP_CONFIG", str(VAULT_ROOT / ".vault-meta" / "memory-backup.json"))
).expanduser()
ALWAYS_EXCLUDE_NAMES = {"MEMORY.md"}
WORK_MEMORY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmygames\b",
        r"\bwhalekit\b",
        r"\bjira\b",
        r"\bdevops\b",
        r"\bmattermost\b",
        r"\bteamcity\b",
        r"\bargocd\b",
        r"\bansible\b",
        r"\bterraform\b",
        r"\bterragrunt\b",
        r"\bkubernetes\b",
        r"\bkubectl\b",
        r"\bk8s\b",
        r"\baws\b",
        r"\biam\b",
        r"\bec2\b",
        r"\bebs\b",
        r"\beks\b",
        r"\bs3\b",
        r"\bacm\b",
        r"\bvpc\b",
        r"\broute53\b",
        r"\bopensearch\b",
        r"\bvictoriametrics\b",
        r"\bloki\b",
        r"\bredpanda\b",
        r"\brabbitmq\b",
        r"\bmongo(?:db)?\b",
        r"\bnginx\b",
        r"\bcert-manager\b",
        r"\bbuild-?agent\b",
        r"\bsvn\b",
        r"\bgitlab\.whalekit\b",
        r"\btoolbox(?:-dev|-prod)?\b",
        r"\bzbs\b",
        r"\blts\b",
        r"\bhunt(?:ing)?\b",
        r"\bserbia\b",
        r"\balkon\b",
        r"\bmgsupport\b",
        r"\bDEVOPS-\d+\b",
        r"\bMNT-\d+\b",
        r"team[_-]?docs",
        r"\bpromote\b",
        r"/promote",
        r"push[_-]?key",
        r"id_zerg_su",
        r"perfplay",
        r"project_alkon",
        r"cross-border",
        r"ruproxy",
        r"\bwlk\b",
        r"wlk/",
        r"~/Projects/MyGames",
        r"whalekit\.games",
        r"\.my\.games\b",
    )
]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_sanitize import residual_credential_kinds, sanitize  # noqa: E402


class ConfigurationError(ValueError):
    pass


def explicit_source() -> Tuple[bool, Optional[Path], str]:
    if "CLAUDE_MEMORY_DIR" in os.environ:
        raw = os.environ["CLAUDE_MEMORY_DIR"].strip()
        if not raw:
            raise ConfigurationError("CLAUDE_MEMORY_DIR is set but empty")
        return True, Path(raw).expanduser().resolve(), "environment"

    if not CONFIG_PATH.is_file():
        return False, None, "not configured"
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read {CONFIG_PATH}: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
        raise ConfigurationError(f"{CONFIG_PATH}: enabled must be true or false")
    if not config["enabled"]:
        return False, None, "config disabled"
    raw = config.get("source")
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(f"{CONFIG_PATH}: enabled backup requires a non-empty source")
    source = Path(raw.strip()).expanduser()
    if not source.is_absolute():
        source = VAULT_ROOT / source
    return True, source.resolve(), "config"


def newest_mtime(directory: Path) -> float:
    files = list(directory.glob("*.md"))
    return max((item.stat().st_mtime for item in files), default=0.0)


def memory_entry_allowed(name: str, text: str) -> bool:
    if name in ALWAYS_EXCLUDE_NAMES:
        return False
    haystack = f"{name}\n{text}"
    return not any(pattern.search(haystack) for pattern in WORK_MEMORY_PATTERNS)


def source_snapshot(source_dir: Path) -> tuple[dict[str, str], int, int, int]:
    snapshot: dict[str, str] = {}
    source_count = skipped = redacted_total = 0
    for source in sorted(source_dir.glob("*.md")):
        source_count += 1
        text = source.read_text(encoding="utf-8", errors="replace")
        if not memory_entry_allowed(source.name, text):
            skipped += 1
            continue
        clean, redactions = sanitize(text)
        snapshot[source.name] = clean
        redacted_total += redactions
    return snapshot, source_count, skipped, redacted_total


def residual_issues(snapshot: dict[str, str], scope: str) -> list[str]:
    issues: list[str] = []
    for name, text in sorted(snapshot.items()):
        kinds = residual_credential_kinds(text)
        if kinds:
            issues.append(f"{scope}/{name}: {','.join(kinds)}")
    return issues


def existing_backup_snapshot() -> dict[str, str]:
    if not BACKUP_DIR.is_dir():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(BACKUP_DIR.glob("*.md"))
    }


def existing_backup_scan() -> dict[str, str]:
    if not BACKUP_DIR.is_dir():
        return {}
    return {
        path.relative_to(BACKUP_DIR).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(BACKUP_DIR.rglob("*"))
        if path.is_file()
    }


def changed_names(snapshot: dict[str, str], backup: dict[str, str]) -> set[str]:
    return {name for name, clean in snapshot.items() if name in backup and backup[name] != clean}


def atomic_write_text(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def disabled_message(reason: str) -> str:
    return (
        "memory-backup: disabled "
        f"({reason}; set CLAUDE_MEMORY_DIR or copy config/memory-backup.example.json "
        "to .vault-meta/memory-backup.json)"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="exit 1 when the enabled backup is stale")
    parser.add_argument("--status", action="store_true", help="print configuration status as JSON")
    args = parser.parse_args()

    try:
        enabled, source_dir, reason = explicit_source()
    except ConfigurationError as exc:
        if args.status:
            print(json.dumps({"enabled": False, "source": None, "reason": "invalid", "error": str(exc)}))
        else:
            print(f"memory-backup: configuration error: {exc}", file=sys.stderr)
        return 2

    if args.status:
        print(
            json.dumps(
                {
                    "enabled": enabled,
                    "source": str(source_dir) if source_dir else None,
                    "source_exists": bool(source_dir and source_dir.is_dir()),
                    "reason": reason,
                    "config": str(CONFIG_PATH),
                },
                sort_keys=True,
            )
        )
        return 0
    if not enabled or source_dir is None:
        print(disabled_message(reason))
        return 0
    if not source_dir.is_dir():
        print(f"memory dir not found: {source_dir}", file=sys.stderr)
        return 2
    if source_dir == BACKUP_DIR or BACKUP_DIR in source_dir.parents:
        print("memory-backup: source cannot be .claude-memory or its child", file=sys.stderr)
        return 2

    try:
        snapshot, source_count, skipped, redacted_total = source_snapshot(source_dir)
        backup = existing_backup_snapshot()
        backup_scan = existing_backup_scan()
    except OSError as exc:
        print(f"memory-backup: read failed: {exc}", file=sys.stderr)
        return 2

    # All candidate output and every existing durable file are scanned before
    # mkdir/write/prune. A finding leaves the backup byte-for-byte untouched.
    issues = residual_issues(snapshot, "source") + residual_issues(backup_scan, "backup")
    if issues:
        print(
            "memory-backup: credential scan blocked mutation (" + "; ".join(issues) + ")",
            file=sys.stderr,
        )
        return 3

    src_names = set(snapshot)
    dst_names = set(backup)
    missing = src_names - dst_names
    changed = changed_names(snapshot, backup)
    extra = dst_names - src_names
    if args.check:
        src_mtime = newest_mtime(source_dir)
        dst_mtime = newest_mtime(BACKUP_DIR) if BACKUP_DIR.is_dir() else 0.0
        stale = bool(missing or changed or extra)
        print(
            f"backup {'STALE' if stale else 'fresh'} "
            f"(source={source_count} included={len(src_names)} skipped={skipped} "
            f"src={src_mtime:.0f} dst={dst_mtime:.0f} missing={len(missing)} "
            f"changed={len(changed)} extra={len(extra)})"
        )
        return 1 if stale else 0

    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        for name in sorted(missing | changed):
            atomic_write_text(BACKUP_DIR / name, snapshot[name])
        for name in sorted(extra):
            (BACKUP_DIR / name).unlink()
    except OSError as exc:
        print(f"memory-backup: write failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"memory-backup: {source_count} source files, {len(src_names)} included, "
        f"{skipped} skipped, {len(missing | changed)} updated, {len(extra)} pruned, "
        f"{redacted_total} redactions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
