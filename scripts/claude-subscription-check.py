#!/usr/bin/env python3
"""Fail closed unless Claude Code is using a first-party paid subscription."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ALLOWED_AUTH_METHOD = "claude.ai"
ALLOWED_PROVIDER = "firstParty"
ALLOWED_SUBSCRIPTIONS = {"pro", "max", "team", "enterprise"}
BLOCKING_ENV = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


class SubscriptionCheckError(RuntimeError):
    """The active Claude credential is not an approved subscription login."""


def resolve_claude(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser()
    else:
        discovered = shutil.which("claude")
        fallback = Path.home() / ".local" / "bin" / "claude"
        discovered_path = Path(discovered) if discovered else None
        # cmux injects a surface-local command shim. It is correct for opening
        # the interactive TUI, but its `auth status` can report the shim's
        # environment instead of the native Claude credential state.
        if discovered_path and "cmux-cli-shims" not in discovered_path.parts:
            candidate = discovered_path
        elif fallback.is_file():
            candidate = fallback
        else:
            candidate = discovered_path or fallback
    if not candidate.is_file():
        raise SubscriptionCheckError("Claude CLI was not found")
    return candidate


def active_blocker(env: dict[str, str]) -> str | None:
    return next((name for name in BLOCKING_ENV if env.get(name)), None)


def load_auth_status(claude: Path, *, timeout: float) -> dict:
    try:
        result = subprocess.run(
            [str(claude), "auth", "status"],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubscriptionCheckError("Claude auth status was unavailable") from exc
    if result.returncode != 0:
        raise SubscriptionCheckError("Claude is not logged in")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SubscriptionCheckError("Claude auth status was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SubscriptionCheckError("Claude auth status was not an object")
    return payload


def validate_subscription(payload: dict) -> dict[str, object]:
    auth_method = str(payload.get("authMethod") or "")
    provider = str(payload.get("apiProvider") or "")
    subscription = str(payload.get("subscriptionType") or "").casefold()
    if payload.get("loggedIn") is not True:
        raise SubscriptionCheckError("Claude is not logged in")
    if auth_method != ALLOWED_AUTH_METHOD:
        raise SubscriptionCheckError("active Claude auth is not a Claude.ai subscription")
    if provider != ALLOWED_PROVIDER:
        raise SubscriptionCheckError("active Claude provider is not first-party")
    if subscription not in ALLOWED_SUBSCRIPTIONS:
        raise SubscriptionCheckError("active Claude account has no supported subscription")
    return {
        "schema_version": 1,
        "status": "ok",
        "auth_method": auth_method,
        "api_provider": provider,
        "subscription_type": subscription,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=8.0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 < args.timeout <= 30:
        print("claude-subscription-check: invalid timeout", file=sys.stderr)
        return 2
    try:
        blocker = active_blocker(os.environ)
        if blocker:
            raise SubscriptionCheckError(
                f"{blocker} is set; strict subscription policy treats any non-empty value, "
                "including 0/false, as an ambiguous override"
            )
        claude = resolve_claude(args.claude_bin)
        response = validate_subscription(load_auth_status(claude, timeout=args.timeout))
        print(json.dumps(response, sort_keys=True))
        return 0
    except SubscriptionCheckError as exc:
        print(
            f"claude-subscription-check: {exc}. "
            "Use /status, remove API/provider overrides, and sign in with /login.",
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
