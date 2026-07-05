#!/usr/bin/env python3
"""Pin / update package versions of MCP gateway children.

Two child kinds:
- PyPI servers: installed via `uv tool install` (no uvx wrappers, which saves
  a ~27MB nanny process per child), binaries in ~/.local/bin; pins and
  constraints live in tools.json next to config.json — never edit versions
  by hand.
- npm servers (npx children): pinned in the args array of config.json.

Usage: update-pins.py <config.json> [--check] [--yes] [--pin-only] [--sync] [name ...]
  (no args)   check every package, propose pin/update per package
  name ...    limit to these server names or package names
  --check     report only, write nothing
  --yes       apply without prompting (required when stdin is not a TTY)
  --pin-only  only pin @latest/versionless npx packages, never bump pinned
  --sync      ensure every tools.json entry is installed at its pinned version
              (bootstrap on a fresh machine / after cleaning uv tools; idempotent)

Exit codes: 0 = no changes, 10 = pins changed (caller should restart+smoke).
"""
import json
import os
import subprocess
import sys
import urllib.request

TIMEOUT = 15
NPX_VALUE_FLAGS = {"-p", "--package"}


def find_npx_spec_index(args):
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg in NPX_VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return i
    return None


def split_spec(spec):
    """'pkg@1.2.3' -> ('pkg', '1.2.3'); scoped npm names keep leading @."""
    idx = spec.rfind("@")
    if idx > 0:
        return spec[:idx], spec[idx + 1:]
    return spec, None


def registry_latest(kind, name):
    if kind == "pypi":
        url = f"https://pypi.org/pypi/{name}/json"
        key = lambda d: d["info"]["version"]
    else:
        url = f"https://registry.npmjs.org/{name}/latest"
        key = lambda d: d["version"]
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return key(json.loads(resp.read()))


def uv_install(pkg, meta, version):
    cmd = ["uv", "tool", "install", f"{pkg}=={version}", "--force"]
    for c in meta.get("with", []):
        cmd += ["--with", c]
    if meta.get("python"):
        cmd += ["--python", meta["python"]]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"uv tool install failed: {res.stderr.strip()[:300]}")


def load_tools(tools_path):
    data = json.load(open(tools_path))
    return data, {k: v for k, v in data.items() if not k.startswith("_")}


def confirm(prompt_text, assume_yes, interactive):
    if assume_yes:
        return True
    if not interactive:
        print("        skipped (no TTY; re-run with --yes to apply)", file=sys.stderr)
        return False
    return input(prompt_text).strip().lower() in ("y", "yes")


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    config_path = argv[0]
    flags = {a for a in argv[1:] if a.startswith("--")}
    names = [a for a in argv[1:] if not a.startswith("--")]
    check_only = "--check" in flags
    assume_yes = "--yes" in flags
    pin_only = "--pin-only" in flags
    interactive = sys.stdin.isatty()

    tools_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "tools.json")
    tools_data, tools = load_tools(tools_path)
    cfg = json.load(open(config_path))
    servers = cfg["mcpServers"]

    if "--sync" in flags:
        for pkg, meta in tools.items():
            uv_install(pkg, meta, meta["version"])
            print(f"sync: {pkg}=={meta['version']} installed")
        return

    # PyPI-пакеты: серверы, чей command = ~/.local/bin/<entrypoint>
    pkg_servers = {}
    for srv, s in servers.items():
        base = os.path.basename(s.get("command", ""))
        for pkg, meta in tools.items():
            if base == meta["entrypoint"]:
                pkg_servers.setdefault(pkg, []).append(srv)

    # npm-пакеты: npx-дети, пин в args
    npx_packages = {}
    for srv, s in servers.items():
        if s.get("command") != "npx":
            continue
        idx = find_npx_spec_index(s.get("args", []))
        if idx is None:
            print(f"WARN {srv}: package spec not found in args, skipping", file=sys.stderr)
            continue
        pkg, ver = split_spec(s["args"][idx])
        npx_packages.setdefault(pkg, []).append((srv, idx, ver))

    if names:
        known = set(tools) | set(npx_packages)
        for srvs in pkg_servers.values():
            known.update(srvs)
        for entries in npx_packages.values():
            known.update(srv for srv, _, _ in entries)
        unknown = set(names) - known
        if unknown:
            sys.exit(f"unknown name(s): {', '.join(sorted(unknown))}")

    def matches(pkg, srv_list):
        if not names:
            return True
        wanted = set(names)
        return pkg in wanted or bool(wanted & set(srv_list))

    changed = False

    # --- PyPI через uv tool ---
    for pkg, meta in sorted(tools.items()):
        srvs = pkg_servers.get(pkg, [])
        if not matches(pkg, srvs):
            continue
        current = meta["version"]
        try:
            latest = registry_latest("pypi", pkg)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"WARN {pkg}: registry lookup failed ({exc}), skipping", file=sys.stderr)
            continue
        if current == latest:
            print(f"OK      {pkg}: {latest} (up to date; {', '.join(srvs)})")
            continue
        if pin_only:
            print(f"PINNED  {pkg}: {current} (latest {latest}; --pin-only, not touching)")
            continue
        print(f"PROPOSE {pkg}: {current} -> {latest} [update] (servers: {', '.join(srvs)})")
        if check_only:
            continue
        if not confirm(f"        apply {pkg} {current} -> {latest}? [y/N] ", assume_yes, interactive):
            print("        skipped")
            continue
        uv_install(pkg, meta, latest)
        meta["version"] = latest
        changed = True
        print(f"        installed {pkg}=={latest}")

    # --- npm через config.json args ---
    config_changed = False
    for pkg, entries in sorted(npx_packages.items()):
        srvs = [srv for srv, _, _ in entries]
        if not matches(pkg, srvs):
            continue
        current = {ver or "(unpinned)" for _, _, ver in entries}
        cur_label = "/".join(sorted(current))
        is_pinned = current not in ({"latest"}, {"(unpinned)"})
        try:
            latest = registry_latest("npm", pkg)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"WARN {pkg}: registry lookup failed ({exc}), skipping", file=sys.stderr)
            continue
        if current == {latest}:
            print(f"OK      {pkg}: {latest} (up to date; {', '.join(srvs)})")
            continue
        if pin_only and is_pinned:
            print(f"PINNED  {pkg}: {cur_label} (latest {latest}; --pin-only, not touching)")
            continue
        action = "pin" if not is_pinned else "update"
        print(f"PROPOSE {pkg}: {cur_label} -> {latest} [{action}] (servers: {', '.join(srvs)})")
        if check_only:
            continue
        if not confirm(f"        apply {pkg} {cur_label} -> {latest}? [y/N] ", assume_yes, interactive):
            print("        skipped")
            continue
        for srv, idx, _ in entries:
            servers[srv]["args"][idx] = f"{pkg}@{latest}"
        changed = config_changed = True
        print(f"        pinned {pkg}@{latest}")

    if not changed:
        sys.exit(0)
    tools_data.update(tools)
    with open(tools_path, "w") as f:
        json.dump(tools_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if config_changed:
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print("--- pins updated")
    sys.exit(10)


if __name__ == "__main__":
    main()
