"""Zero-effect capability handshake for cmux and provider routes."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .adapters.claude import ClaudeDriver, ClaudeDriverError
from .adapters.codex import CodexDriver, CodexDriverError
from .contracts import AttentionReason, CapabilityReport, RuntimeRoute


Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


def _provider_help(
    binary: str,
    runtime: str,
    required: tuple[str, ...],
    runner: Runner,
) -> tuple[int, str]:
    """Read provider help without accepting a pipe-truncated Claude response."""
    try:
        result = runner(
            [binary, "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return 1, ""
    output = (result.stdout + result.stderr)[:20_000]
    if result.returncode or all(token in output for token in required):
        return result.returncode, output
    if runtime != "claude":
        return result.returncode, output
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stream:
            file_result = runner(
                [binary, "--help"],
                text=True,
                stdout=stream,
                stderr=stream,
                check=False,
            )
            stream.seek(0)
            return file_result.returncode, stream.read(20_000)
    except (OSError, TypeError, ValueError):
        return result.returncode, output


def check(
    route: RuntimeRoute,
    *,
    callback_dir: Path,
    prompt_contract_version: int = 1,
    expected_routing_sha256: str | None = None,
    which: Which | None = None,
    runner: Runner | None = None,
) -> CapabilityReport:
    which = which or shutil.which
    runner = runner or subprocess.run
    capabilities: list[str] = []
    if (
        expected_routing_sha256 is not None
        and route.routing_sha256 != expected_routing_sha256
    ):
        return CapabilityReport(
            route, False, (), AttentionReason.CAPABILITY_MISMATCH
        )
    capabilities.append("routing:fingerprint")
    binaries: dict[str, str] = {}
    for name in ("cmux", route.runtime):
        resolved = which(name)
        if resolved:
            binaries[name] = resolved
            capabilities.append(f"binary:{name}")
        else:
            return CapabilityReport(
                route, False, tuple(capabilities), AttentionReason.RUNTIME_UNAVAILABLE
            )
    probes = (
        (
            ("new-split", "--help"),
            ("--surface", "--focus"),
            "cmux:anchored-split",
        ),
        (
            ("workspace", "create", "--help"),
            ("create [flags]", "close <workspace>"),
            "cmux:canonical-workspace-create",
        ),
        (
            ("new-workspace", "--help"),
            ("--window", "--focus"),
            "cmux:anchored-workspace",
        ),
        (
            ("workspace", "close", "--help"),
            ("close <workspace>", "--window"),
            "cmux:canonical-workspace-close",
        ),
        (
            ("identify", "--help"),
            ("--surface",),
            "cmux:identify-status",
        ),
        (
            ("surface", "resume", "--help"),
            (
                "resume get",
                "resume set",
                "resume show",
                "resume clear",
                "--surface",
                "--json",
            ),
            "cmux:typed-resume",
        ),
        (
            ("close-surface", "--help"),
            ("--surface",),
            "cmux:exact-close",
        ),
    )
    for args, required, capability in probes:
        try:
            result = runner(
                [binaries["cmux"], *args],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            result = subprocess.CompletedProcess(args, 1, "", "")
        output = (result.stdout + result.stderr)[:20_000]
        if result.returncode or not all(token in output for token in required):
            return CapabilityReport(
                route,
                False,
                tuple(capabilities),
                AttentionReason.CAPABILITY_MISMATCH,
            )
        capabilities.append(capability)
    provider_help = {
        "claude": ("--model", "--effort", "--permission-mode"),
        "codex": ("--model", "--config", "--sandbox", "--ask-for-approval"),
    }[route.runtime]
    returncode, output = _provider_help(
        binaries[route.runtime], route.runtime, provider_help, runner
    )
    if returncode or not all(token in output for token in provider_help):
        return CapabilityReport(
            route,
            False,
            tuple(capabilities),
            AttentionReason.CAPABILITY_MISMATCH,
        )
    try:
        if route.runtime == "claude":
            ClaudeDriver(Path(binaries["claude"])).command(route)
        else:
            CodexDriver(Path(binaries["codex"])).command(
                route,
                callback_pointer=(
                    callback_dir.resolve()
                    / ".harness-capability-outbox.json"
                ),
                session_root=callback_dir.resolve(),
            )
    except (ClaudeDriverError, CodexDriverError):
        return CapabilityReport(
            route,
            False,
            tuple(capabilities),
            AttentionReason.CAPABILITY_MISMATCH,
        )
    capabilities.append("provider:model-effort-profile")
    provider = (
        ClaudeDriver(Path(binaries["claude"]))
        if route.runtime == "claude"
        else CodexDriver(Path(binaries["codex"]))
    )
    auth_command = provider.auth_command(Path(binaries[route.runtime]))
    try:
        auth = runner(
            list(auth_command),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        auth = subprocess.CompletedProcess((), 1, "", "")
    if not provider.authenticated_subscription(
        auth.stdout, auth.stderr, auth.returncode
    ):
        return CapabilityReport(
            route,
            False,
            tuple(capabilities),
            AttentionReason.CAPABILITY_MISMATCH,
        )
    capabilities.extend(("provider:authenticated", "provider:subscription"))
    if prompt_contract_version != 1:
        return CapabilityReport(
            route, False, tuple(capabilities), AttentionReason.CAPABILITY_MISMATCH
        )
    writable = callback_dir.is_dir() and os.access(callback_dir, os.W_OK)
    callback_pointer = callback_dir.resolve() / ".harness-capability-outbox.json"
    if not writable or not provider.callback_writable(route, callback_pointer):
        return CapabilityReport(
            route, False, tuple(capabilities), AttentionReason.CAPABILITY_MISMATCH
        )
    capabilities.extend(
        (
            "cmux:exact-surface",
            "callback:writable",
            "callback:profile-writable",
            "prompt-contract:1",
        )
    )
    return CapabilityReport(route, True, tuple(capabilities))
