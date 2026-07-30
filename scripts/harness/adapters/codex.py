"""Codex provider argv/profile adapter; execution belongs to ProcessAdapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import RuntimeRoute


class CodexDriverError(ValueError):
    pass


EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
CHECKPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class CodexDriver:
    binary: Path
    registered_models: frozenset[str] = frozenset()

    def command(
        self,
        route: RuntimeRoute,
        *,
        resume: str = "",
        callback_pointer: Path | None = None,
        product_root: Path | None = None,
        session_root: Path | None = None,
    ) -> tuple[str, ...]:
        del callback_pointer
        if route.runtime != "codex":
            raise CodexDriverError("Codex driver received a non-Codex route")
        if self.registered_models and route.model not in self.registered_models:
            raise CodexDriverError("Codex model is not registered")
        if route.effort not in EFFORTS:
            raise CodexDriverError("unsupported Codex effort")
        profiles = {
            "executor": ("workspace-write", "never"),
            "reviewer-readonly": ("read-only", "never"),
            "reviewer-callback": ("workspace-write", "never"),
            "research-safe": ("workspace-write", "never"),
            "research-unsafe": ("read-only", "never"),
            "prototype": ("workspace-write", "never"),
        }
        if route.profile not in profiles:
            raise CodexDriverError("unsupported Codex permission profile")
        sandbox, approval = profiles[route.profile]
        args = [str(self.binary), "--model", route.model]
        if route.profile == "executor" and product_root is not None:
            from cmux_agent_support import (
                SupervisorError,
                resolved_git_common_dir,
                task_codex_config_values,
                validated_cmux_socket_path,
            )

            product = product_root.expanduser().resolve()
            session = (
                session_root.expanduser().resolve()
                if session_root is not None
                else None
            )
            if (
                not product.is_dir()
                or session != product
            ):
                raise CodexDriverError(
                    "executor product and session roots must match exactly"
                )
            try:
                git_common = resolved_git_common_dir(product)
                cmux_socket = validated_cmux_socket_path()
            except (OSError, SupervisorError) as exc:
                raise CodexDriverError(
                    "executor Git/cmux capabilities are unavailable"
                ) from exc
            args.extend(
                [
                    "--cd",
                    str(product),
                    "--add-dir",
                    str(git_common),
                    "--sandbox",
                    sandbox,
                    "--ask-for-approval",
                    approval,
                ]
            )
            for value in task_codex_config_values(
                cmux_socket,
                route.effort,
            ):
                args.extend(["--config", value])
        else:
            args.extend(
                [
                    "--config",
                    f"model_reasoning_effort={route.effort}",
                    "--sandbox",
                    sandbox,
                    "--ask-for-approval",
                    approval,
                ]
            )
        if route.profile == "research-safe":
            if session_root is None or not session_root.is_absolute():
                raise CodexDriverError(
                    "safe research requires an absolute isolated scratch root"
                )
            args.extend(("--strict-config", "--cd", str(session_root)))
        if resume:
            if not CHECKPOINT.fullmatch(resume):
                raise CodexDriverError("Codex checkpoint is invalid")
            args.extend(["resume", resume])
        return tuple(args)

    def resume_command(
        self,
        route: RuntimeRoute,
        checkpoint: str,
        *,
        callback_pointer: Path | None = None,
        product_root: Path | None = None,
        session_root: Path | None = None,
    ) -> tuple[str, ...]:
        if not CHECKPOINT.fullmatch(checkpoint):
            raise CodexDriverError("Codex resume requires an exact checkpoint")
        return self.command(
            route,
            resume=checkpoint,
            callback_pointer=callback_pointer,
            product_root=product_root,
            session_root=session_root,
        )

    def callback_writable(
        self, route: RuntimeRoute, callback_pointer: Path
    ) -> bool:
        """Prove the fixed sandbox profile can write its owned callback."""

        if not callback_pointer.is_absolute():
            return False
        try:
            command = self.command(
                route,
                callback_pointer=callback_pointer,
                session_root=(
                    callback_pointer.parent
                    if route.profile == "research-safe"
                    else None
                ),
            )
        except CodexDriverError:
            return False
        return (
            "--sandbox" in command
            and "workspace-write" in command
            and route.profile
            in {"executor", "reviewer-callback", "research-safe", "prototype"}
        )

    @staticmethod
    def auth_command(binary: Path) -> tuple[str, ...]:
        return (str(binary), "login", "status")

    @staticmethod
    def authenticated_subscription(
        stdout: str, stderr: str, returncode: int
    ) -> bool:
        if returncode:
            return False
        status = (stdout + stderr).casefold()
        return "logged in" in status and "chatgpt" in status
