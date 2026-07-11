#!/usr/bin/env python3
"""Lock MCP tool inventories and schema hashes without storing descriptions."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_LOCK = HERE / "mcp-schema.lock.json"
PROTOCOL = "2025-03-26"


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_tools(tools: Any) -> dict[str, Any]:
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    rows = []
    seen = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
            raise ValueError("every tool needs a non-empty name")
        name = tool["name"]
        if name in seen:
            raise ValueError(f"duplicate tool name {name}")
        seen.add(name)
        row = {
            "name": name,
            "description_sha256": digest(tool.get("description", "")),
            "input_schema_sha256": digest(tool.get("inputSchema", {})),
        }
        if "outputSchema" in tool:
            row["output_schema_sha256"] = digest(tool["outputSchema"])
        rows.append(row)
    rows.sort(key=lambda row: row["name"])
    return {"tool_count": len(rows), "schema_sha256": digest(rows), "tools": rows}


def snapshot_payload(servers: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "protocol_version": PROTOCOL,
        "servers": {name: normalize_tools(value.get("tools")) for name, value in sorted(servers.items())},
    }


def load_snapshot(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("snapshot must contain a servers object")
    return snapshot_payload(servers)


def load_smoke_module():
    spec = importlib.util.spec_from_file_location("mcp_gateway_smoke_for_lock", HERE / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def live_payload(config: Path, port: int) -> dict[str, Any]:
    smoke = load_smoke_module()
    names = list(json.loads(config.read_text(encoding="utf-8"))["mcpServers"])
    servers = {}
    for name in names:
        url = f"http://127.0.0.1:{port}/{name}/mcp"
        session = smoke.handshake(url)
        body, _ = smoke.post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        if not body or "result" not in body:
            raise ValueError(f"{name}: tools/list bad reply")
        servers[name] = {"tools": body["result"].get("tools", [])}
    return snapshot_payload(servers)


def changed_tools(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changes = []
    old_servers = old.get("servers", {}) if isinstance(old, dict) else {}
    new_servers = new.get("servers", {})
    for server in sorted(set(old_servers) | set(new_servers)):
        before = {row["name"]: row for row in old_servers.get(server, {}).get("tools", [])}
        after = {row["name"]: row for row in new_servers.get(server, {}).get("tools", [])}
        for name in sorted(set(before) | set(after)):
            if name not in before:
                changes.append(f"{server}/{name}: added")
            elif name not in after:
                changes.append(f"{server}/{name}: removed")
            elif before[name] != after[name]:
                changes.append(f"{server}/{name}: schema changed")
    return changes


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--snapshot", type=Path, help="offline tools/list fixture")
    args = parser.parse_args()
    if not args.apply:
        args.check = True
    try:
        current = load_snapshot(args.snapshot) if args.snapshot else live_payload(args.config, args.port)
        previous = json.loads(args.lock.read_text(encoding="utf-8")) if args.lock.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mcp-schema-lock: {exc}", file=sys.stderr)
        return 3
    changes = changed_tools(previous, current)
    if args.apply:
        atomic_write(args.lock, current)
        print(f"wrote {args.lock} ({sum(v['tool_count'] for v in current['servers'].values())} tools)")
        return 0
    if previous != current:
        print("MCP_SCHEMA_DRIFT", file=sys.stderr)
        for change in changes or ["lock metadata changed"]:
            print(f"  {change}", file=sys.stderr)
        return 1
    print("mcp schema lock: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
