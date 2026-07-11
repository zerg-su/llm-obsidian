#!/usr/bin/env python3
"""Synchronize local MCP gateway/client JSON from one runtime port setting."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PORT_LINE = re.compile(r"^MCP_GATEWAY_PORT=([0-9]+)$")


def read_port(path: Path) -> int:
    if not path.is_file():
        raise ValueError(f"runtime config missing: {path}")
    values: list[int] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PORT_LINE.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{number}: expected MCP_GATEWAY_PORT=<integer>")
        values.append(int(match.group(1)))
    if len(values) != 1 or not 1 <= values[0] <= 65535:
        raise ValueError(f"{path}: expected exactly one port in range 1..65535")
    return values[0]


def local_url_port(value: str, port: int) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
            return value
        host = parsed.hostname
        return urlunsplit((parsed.scheme, f"{host}:{port}", parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return value


def rewrite_urls(value: object, port: int) -> object:
    if isinstance(value, dict):
        return {
            key: local_url_port(item, port) if key == "url" and isinstance(item, str) else rewrite_urls(item, port)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [rewrite_urls(item, port) for item in value]
    return value


def gateway_value(value: dict, port: int) -> dict:
    updated = rewrite_urls(value, port)
    assert isinstance(updated, dict)
    proxy = updated.setdefault("mcpProxy", {})
    if not isinstance(proxy, dict):
        raise ValueError("config.json: mcpProxy must be an object")
    proxy["baseURL"] = f"http://127.0.0.1:{port}"
    proxy["addr"] = f"127.0.0.1:{port}"
    return updated


def load_json_or_template(path: Path, template: Path) -> tuple[dict, bool]:
    source = path if path.is_file() else template
    if not source.is_file():
        raise ValueError(f"missing both {path} and template {template}")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{source}: top level must be an object")
    return value, not path.is_file()


def rendered(value: dict) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
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


def sync_one(path: Path, template: Path, transform, apply: bool, quiet: bool) -> bool:
    value, missing = load_json_or_template(path, template)
    wanted_value = transform(value)
    wanted = rendered(wanted_value)
    changed = missing or value != wanted_value
    if changed:
        if apply:
            atomic_write(path, wanted)
            if not quiet:
                print(f"write: {path}")
        elif not quiet:
            print(f"drift: {path}")
    return changed


def main() -> int:
    here = Path(__file__).resolve().parent
    root = here.parent.parent
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=root)
    parser.add_argument("--gateway-dir", type=Path, default=here)
    parser.add_argument("--runtime-env", type=Path)
    parser.add_argument("--gateway-only", action="store_true")
    parser.add_argument("--print-port", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    gateway = args.gateway_dir.resolve()
    repo = args.repo_root.resolve()
    runtime = (args.runtime_env or gateway / "runtime.env").resolve()
    try:
        port = read_port(runtime)
        if args.print_port:
            print(port)
            return 0
        apply = args.apply
        changed = sync_one(
            gateway / "config.json",
            gateway / "config.json.example",
            lambda value: gateway_value(value, port),
            apply,
            args.quiet,
        )
        if not args.gateway_only:
            changed = sync_one(
                repo / ".mcp.json",
                repo / ".mcp.json.example",
                lambda value: rewrite_urls(value, port),
                apply,
                args.quiet,
            ) or changed
        if changed and not apply:
            if not args.quiet:
                print("run mcp-gateway.sh sync-config --apply")
            return 1
        if not args.quiet:
            print("config-sync: synchronized" if apply and changed else "config-sync: no changes")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
