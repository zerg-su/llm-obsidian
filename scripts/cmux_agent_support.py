#!/usr/bin/env python3
"""Small shared primitives used by cmux agents and live acceptance."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from model_routing import RoutingError, load_config as load_routing_config


try:
    ROUTING_CONFIG = load_routing_config(Path(__file__).resolve().parents[1])
except RoutingError:  # Hermetic consumers may copy only the support module.
    ROUTING_CONFIG = load_routing_config()

DEFAULT_CODEX_EFFORT = ROUTING_CONFIG.runtime_default("codex")["effort"]
CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}


class SupervisorError(RuntimeError):
    pass


def validated_cmux_socket_path() -> Path:
    raw = os.environ.get("CMUX_SOCKET_PATH") or os.environ.get("CMUX_SOCKET")
    if raw and ("\n" in raw or "\0" in raw):
        raise SupervisorError("cmux socket path is malformed")
    path = (Path(raw).expanduser() if raw else Path.home() / ".local/state/cmux/cmux.sock").resolve()
    try:
        stat = path.stat()
        available = path.is_socket()
    except OSError:
        available = False
        stat = None
    if not available or stat is None or stat.st_uid != os.getuid():
        raise SupervisorError(f"cmux socket is unavailable or not user-owned: {path}")
    return path


def codex_effort_config(effort: str) -> str:
    if effort not in CODEX_EFFORTS:
        raise SupervisorError(f"Codex effort must be one of {sorted(CODEX_EFFORTS)}")
    return f'model_reasoning_effort="{effort}"'


def codex_automation_service_tier_config() -> str:
    """Keep repo-spawned Codex runs off user-only Fast/priority service."""
    return 'service_tier="default"'


def task_codex_config_values(
    cmux_socket: Path, effort: str = DEFAULT_CODEX_EFFORT
) -> list[str]:
    socket_rule = json.dumps(str(cmux_socket), ensure_ascii=False)
    return [
        codex_automation_service_tier_config(),
        "sandbox_workspace_write.network_access=true",
        "features.network_proxy.enabled=true",
        'features.network_proxy.domains={ "localhost" = "allow", "127.0.0.1" = "allow", "::1" = "allow" }',
        f"features.network_proxy.unix_sockets={{ {socket_rule} = \"allow\" }}",
        "features.network_proxy.allow_local_binding=true",
        "features.network_proxy.allow_upstream_proxy=false",
        "features.network_proxy.dangerously_allow_all_unix_sockets=false",
        "features.network_proxy.dangerously_allow_non_loopback_proxy=false",
        "features.network_proxy.enable_socks5=false",
        "features.network_proxy.enable_socks5_udp=false",
        codex_effort_config(effort),
    ]


def resolved_git_common_dir(worktree: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw or "\n" in raw or "\0" in raw:
        raise SupervisorError("cannot resolve the task Git metadata root")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = worktree / candidate
    common = candidate.resolve()
    if not common.is_dir() or common.stat().st_uid != os.getuid():
        raise SupervisorError("task Git metadata root is missing or not owned by the current user")
    return common
