"""Exact prompt adapters; unknown or near-match prompts never receive input."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptDecision:
    recognized: bool
    family: str = ""
    keys: tuple[str, ...] = ()
    interactive: bool = False

    @property
    def key(self) -> str:
        """Compatibility view for one-key prompt consumers."""

        return self.keys[0] if len(self.keys) == 1 else ""


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _dialog_region(
    screen: str,
    footers: tuple[str, ...],
    *,
    max_lines: int = 256,
    max_footer_lines: int = 48,
) -> str:
    """Return a bounded native dialog only when its footer ends the screen."""

    lines = screen.splitlines()
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return ""
    end = nonempty[-1]
    expected = {_compact(footer).casefold() for footer in footers}
    for start in range(max(0, end - max_footer_lines + 1), end + 1):
        fragments = [line.strip() for line in lines[start : end + 1]]
        if (
            not all(fragments)
            or any(re.search(r"""["'`\\]""", fragment) for fragment in fragments)
            or _compact("".join(fragments)).casefold() not in expected
        ):
            continue
        return "\n".join(lines[max(0, start - max_lines + 1) : end + 1])
    return ""


def _has(region: str, markers: tuple[str, ...]) -> bool:
    compact = _compact(region).casefold()
    return all(_compact(marker).casefold() in compact for marker in markers)


def _unknown_interactive(screen: str) -> bool:
    """Detect a bounded native choice without guessing how to answer it."""

    lines = screen.splitlines()
    nonempty = [line.strip() for line in lines if line.strip()]
    if len(nonempty) < 3:
        return False
    tail = nonempty[-24:]
    footer = tail[-1]
    if (
        re.search(r"""["'`\\]""", footer)
        or not re.fullmatch(
            r"(?i)(?:press\s+)?(?:enter|return)\b.{0,48}",
            footer,
        )
    ):
        return False
    return sum(
        bool(re.match(r"^(?:[>›]\s*)?[1-9][.)]\s+\S", line))
        for line in tail[:-1]
    ) >= 2


def classify(runtime: str, screen: str, *, closure_armed: bool = False) -> PromptDecision:
    if runtime == "claude":
        region = _dialog_region(
            screen, ("Enter to confirm", "Enter to confirm · Esc to cancel")
        )
        trust_region = _dialog_region(
            screen,
            ("Enter to confirm", "Enter to confirm · Esc to cancel"),
        )
        trust = (
            "Accessing workspace:",
            "Quick safety check: Is this a project you created or one you trust?",
            "Yes, I trust this",
        )
        if trust_region and _has(trust_region, trust):
            return PromptDecision(True, "workspace-trust", ("Enter",), True)
        mcp = (
            "New MCP server found in this project:",
            "MCP servers may execute code or access system resources.",
            "1. Use this MCP server",
            "2. Use this and all future MCP servers in this project",
            "3. Continue without using this MCP server",
        )
        if region and _has(region, mcp):
            return PromptDecision(
                True,
                "project-mcp-trust",
                ("Tab", "Tab", "Enter"),
                True,
            )
        first_run = (
            "Choose the text style that looks best with your terminal",
            "1. Dark mode",
            "2. Light mode",
        )
        if region and _has(region, first_run):
            return PromptDecision(True, "first-run-style", ("Enter",), True)
        auto_mode_onboarding = (
            "Set up auto mode for your environment?",
            "1. Set it up",
            "2. Not now",
            "3. Don't show again",
        )
        if region and _has(region, auto_mode_onboarding):
            # This is provider onboarding, not task authority. Dismiss it for
            # this session without choosing an environment or suppressing the
            # user's future reminder.
            return PromptDecision(
                True,
                "auto-mode-onboarding-dismiss",
                ("Esc",),
                True,
            )
        auto_mode_wizard = (
            "How would you describe the code you work on with Claude?",
            "Personal / hobby projects",
            "Work / enterprise (private repos, sensitive data)",
        )
        if region and _has(region, auto_mode_wizard):
            return PromptDecision(
                True,
                "auto-mode-wizard-cancel",
                ("Esc",),
                True,
            )
        background = (
            "Background work is running",
            "The following will stop when you exit:",
            "1. Exit anyway",
            "2. Move to background and exit",
            "3. Stay",
            "Enter to confirm",
        )
        if closure_armed and region and _has(region, background):
            return PromptDecision(True, "background-exit", ("Enter",), True)
    elif runtime == "codex":
        region = _dialog_region(
            screen,
            (
                "Press enter",
                "Press enter to continue",
                "Press ent",
                "Press enter to confirm or esc to go back",
            ),
        )
        trust = (
            "Do you trust the contents of this directory?",
            "Yes, continue",
            "No, quit",
        )
        if region and _has(region, trust):
            return PromptDecision(True, "workspace-trust", ("Enter",), True)
        first_run = (
            "Choose the text style that looks best with your terminal",
            "1. Dark mode",
            "2. Light mode",
        )
        if region and _has(region, first_run):
            return PromptDecision(True, "first-run-style", ("Enter",), True)
        update_available = (
            "Update available!",
            "1. Update now",
            "2. Skip",
            "3. Skip until next version",
            "Press enter to continue",
        )
        if region and _has(region, update_available):
            # Installation remains user-owned. Skip this invocation without
            # suppressing future update reminders.
            return PromptDecision(
                True,
                "update-skip-current",
                ("down", "Enter"),
                True,
            )
        rate_limit_switch = (
            "Approaching rate limits",
            "Switch to",
            "for lower credit usage?",
            "› 1. Switch to",
            "2. Keep current model",
            "3. Keep current model (never show again)",
            "Press enter to confirm or esc to go back",
        )
        if region and _has(region, rate_limit_switch):
            # The bound review route is authoritative. Select the temporary
            # "keep" option, but leave the user's future reminder setting alone.
            return PromptDecision(
                True,
                "rate-limit-keep-current",
                ("down", "Enter"),
                True,
            )
    if runtime in {"claude", "codex"} and _unknown_interactive(screen):
        return PromptDecision(False, interactive=True)
    return PromptDecision(False)
