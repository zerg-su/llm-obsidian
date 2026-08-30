#!/usr/bin/env python3
"""Resolve and explicitly register the state-owning LLM Obsidian coordinator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn, Sequence


class CoordinatorError(ValueError):
    pass


@dataclass(frozen=True)
class CoordinatorResolution:
    root: Path
    source: str


def default_registry_path(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    xdg = str(values.get("XDG_CONFIG_HOME") or "")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "llm-obsidian" / "coordinators-v1.json"


def _plugin_cache(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return any(
        parts[index] == "plugins" and parts[index + 1] == "cache"
        for index in range(len(parts) - 1)
    )


def validate_coordinator(path: Path | str, *, label: str = "coordinator") -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise CoordinatorError(f"{label} root must be an absolute non-symlink path")
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise CoordinatorError(f"{label} coordinator root is unavailable") from exc
    if _plugin_cache(root):
        raise CoordinatorError(f"{label} coordinator root cannot be a plugin cache")
    required = (
        root / "wiki",
        root / "scripts/task-review-runner.py",
        root / "skills/review/SKILL.md",
        root / "config/model-routing.toml",
        root / "config/verification-profiles.toml",
    )
    if not root.is_dir() or any(not item.exists() or item.is_symlink() for item in required):
        raise CoordinatorError(f"{label} coordinator root is not a verified LLM Obsidian vault")
    if not required[0].is_dir() or any(not item.is_file() for item in required[1:]):
        raise CoordinatorError(f"{label} coordinator markers are invalid")
    return root


def _ancestor_candidates(cwd: Path) -> list[Path]:
    current = cwd.expanduser().resolve()
    if current.is_file():
        current = current.parent
    matches: list[Path] = []
    for candidate in (current, *current.parents):
        if (
            (candidate / "wiki").is_dir()
            and (candidate / "scripts/task-review-runner.py").is_file()
            and (candidate / "skills/review/SKILL.md").is_file()
        ):
            matches.append(candidate)
    return matches


def _registry_root(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise CoordinatorError("coordinator registry is unavailable or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError("coordinator registry is malformed") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "default_vault_root"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("default_vault_root"), str)
    ):
        raise CoordinatorError("coordinator registry fields are not exact")
    return validate_coordinator(
        payload["default_vault_root"], label="registry"
    )


def resolve_coordinator(
    *,
    explicit: Path | str | None = None,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    registry_path: Path | None = None,
) -> CoordinatorResolution:
    values = os.environ if environment is None else environment
    if explicit is not None and str(explicit):
        return CoordinatorResolution(
            validate_coordinator(explicit, label="explicit"), "explicit"
        )
    environment_root = str(values.get("LLM_OBSIDIAN_PROJECT_ROOT") or "")
    if environment_root:
        return CoordinatorResolution(
            validate_coordinator(environment_root, label="environment"),
            "environment",
        )
    ancestors = _ancestor_candidates(cwd or Path.cwd())
    if len(ancestors) > 1:
        raise CoordinatorError("coordinator ancestor resolution is ambiguous")
    if ancestors:
        return CoordinatorResolution(
            validate_coordinator(ancestors[0], label="ancestor"), "ancestor"
        )
    registry = registry_path or default_registry_path(values)
    if registry.exists() or registry.is_symlink():
        return CoordinatorResolution(_registry_root(registry), "registry")
    raise CoordinatorError(
        "no verified LLM Obsidian coordinator; pass --vault-root or register one"
    )


def register_coordinator(
    root: Path | str, *, registry_path: Path | None = None
) -> dict[str, object]:
    validated = validate_coordinator(root, label="registered")
    path = registry_path or default_registry_path()
    path = path.expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise CoordinatorError("coordinator registry path is unsafe")
    payload: dict[str, object] = {
        "schema_version": 1,
        "default_vault_root": str(validated),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        raise CoordinatorError("coordinator registry write failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def die(message: str) -> NoReturn:
    print(f"review-coordinator: {message}", file=sys.stderr)
    raise SystemExit(3)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--vault-root", type=Path)
    resolve.add_argument("--cwd", type=Path, default=Path.cwd())
    resolve.add_argument("--registry", type=Path)
    register = sub.add_parser("register")
    register.add_argument("--vault-root", type=Path, required=True)
    register.add_argument("--registry", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            payload = register_coordinator(
                args.vault_root, registry_path=args.registry
            )
            result = {**payload, "source": "registered"}
        else:
            resolution = resolve_coordinator(
                explicit=args.vault_root,
                cwd=args.cwd,
                registry_path=args.registry,
            )
            result = {
                "schema_version": 1,
                "vault_root": str(resolution.root),
                "source": resolution.source,
            }
    except CoordinatorError as exc:
        die(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
