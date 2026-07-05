#!/usr/bin/env python3
"""Smoke-check every MCP gateway endpoint: initialize + tools/list.

Usage: smoke.py <config.json> <port> [server-name[,server-name...]]
       smoke.py <config.json> <port> --routes
Exit 0 if all checked endpoints answered tools/list, 1 otherwise.

--routes: cheap mode for `status` — not an MCP handshake but a route
registration check: mcp-proxy registers /<name>/mcp only once the child
came up, so 404 = child dead / never started, any other answer = alive.
"""
import json
import sys
import urllib.request
import urllib.error

TIMEOUT = 60  # first call may wait for a cold uvx/npx child
ROUTE_TIMEOUT = 3


def parse_body(resp):
    ctype = resp.headers.get("Content-Type", "")
    raw = resp.read().decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    return json.loads(data)
        raise ValueError("no data frame in SSE response")
    return json.loads(raw) if raw.strip() else None


def post(url, payload, session=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    return parse_body(resp), resp.headers.get("Mcp-Session-Id")


def handshake(url):
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp-gateway-smoke", "version": "1.0"},
        },
    }
    body, session = post(url, init)
    if body and "error" in body:
        raise RuntimeError(f"initialize error: {body['error']}")
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


def call_tool(url, session, name, arguments, rpc_id=3):
    body, _ = post(url, {
        "jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, session)
    if not body or "result" not in body:
        raise RuntimeError(f"tools/call {name} bad reply: {body}")
    result = body["result"]
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise RuntimeError(f"{name}: {text[:300]}")
    return text


def check(name, port):
    url = f"http://127.0.0.1:{port}/{name}/mcp"
    session = handshake(url)
    body, _ = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    if not body or "result" not in body:
        raise RuntimeError(f"tools/list bad reply: {body}")
    return len(body["result"].get("tools", []))


def route_registered(name, port):
    """True if the route is registered (child connected), False on 404."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{name}/mcp",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=ROUTE_TIMEOUT)
        return True
    except urllib.error.HTTPError as exc:
        return exc.code != 404


def routes_report(config_path, port):
    servers = list(json.load(open(config_path))["mcpServers"])
    down = set()
    for name in servers:
        try:
            if not route_registered(name, port):
                down.add(name)
        except (urllib.error.URLError, OSError):
            sys.exit(f"gateway unreachable on 127.0.0.1:{port} — service down?")
    summary = f"routes: {len(servers) - len(down)}/{len(servers)} active"
    if down:
        summary += f", {len(down)} DOWN (marked '!')"
    print(summary)
    width = max(len(s) for s in servers) + 3
    cols = max(1, 96 // width)
    for i in range(0, len(servers), cols):
        row = servers[i:i + cols]
        print("  " + "".join(
            (("!" if s in down else " ") + s).ljust(width) for s in row
        ).rstrip())
    sys.exit(1 if down else 0)


def main():
    config_path, port = sys.argv[1], sys.argv[2]
    only = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    if only == "--routes":
        routes_report(config_path, port)
    servers = list(json.load(open(config_path))["mcpServers"])
    if only:
        wanted = only.split(",")
        unknown = [s for s in wanted if s not in servers]
        if unknown:
            sys.exit(f"unknown server: {','.join(unknown)}")
        servers = [s for s in servers if s in wanted]
    failed = []
    for name in servers:
        try:
            n = check(name, port)
            print(f"OK   {name}: {n} tools")
        except Exception as exc:  # noqa: BLE001 - report and continue
            msg = str(exc).replace("\n", " ")[:200]
            print(f"FAIL {name}: {msg}")
            failed.append(name)
    print(f"--- {len(servers) - len(failed)}/{len(servers)} ok")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
