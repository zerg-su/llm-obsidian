"""Claude provider argv/profile adapter; execution belongs to ProcessAdapter."""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from ..contracts import RuntimeRoute


class ClaudeDriverError(ValueError):
    pass


EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CHECKPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class ClaudeDriver:
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
        if route.runtime != "claude":
            raise ClaudeDriverError("Claude driver received a non-Claude route")
        if self.registered_models and route.model not in self.registered_models:
            raise ClaudeDriverError("Claude model is not registered")
        if route.effort not in EFFORTS:
            raise ClaudeDriverError("unsupported Claude effort")
        permissions = {
            "executor": "auto",
            "reviewer-readonly": "dontAsk",
            "reviewer-callback": "dontAsk",
            "research-safe": "dontAsk",
            "research-unsafe": "dontAsk",
            "prototype": "auto",
        }
        if route.profile not in permissions:
            raise ClaudeDriverError("unsupported Claude permission profile")
        args = [
            str(self.binary), "--model", route.model,
            "--effort", route.effort,
            "--permission-mode", permissions[route.profile],
        ]
        if route.profile == "reviewer-callback":
            relative_callback = ""
            if callback_pointer is not None:
                if not callback_pointer.is_absolute():
                    raise ClaudeDriverError(
                        "review callback pointer must be absolute"
                    )
                if session_root is not None:
                    if not session_root.is_absolute():
                        raise ClaudeDriverError(
                            "review session root must be absolute"
                        )
                    try:
                        relative_callback = callback_pointer.relative_to(
                            session_root
                        ).as_posix()
                    except ValueError as exc:
                        raise ClaudeDriverError(
                            "review callback pointer escapes session root"
                        ) from exc
                args.extend(
                    (
                        "--append-system-prompt",
                        (
                            "Write the final reviewer callback only to this "
                            f"exact file: {callback_pointer}. Pass this "
                            "absolute path verbatim to Edit or Write"
                            + (
                                "; its exact session-relative alias is "
                                f"{relative_callback}"
                                if relative_callback
                                else ""
                            )
                            + "; no other write location is allowed."
                        ),
                    )
                )
            args.extend(
                ["--tools", "Read,Glob,Grep,Edit,Write,Bash", "--allowedTools"]
            )
            args.extend(("Read", "Glob", "Grep"))
            if callback_pointer is not None:
                args.extend(
                    (
                        f"Edit({callback_pointer})",
                        f"Write({callback_pointer})",
                    )
                )
                if relative_callback:
                    args.extend(
                        (
                            f"Edit({relative_callback})",
                            f"Write({relative_callback})",
                        )
                    )
            if product_root is not None:
                if not product_root.is_absolute():
                    raise ClaudeDriverError("review product root must be absolute")
                quoted = shlex.quote(str(product_root))
                args.extend(
                    (
                        f"Bash(python3 {quoted}/tests/test_*.py)",
                        f"Bash(bash {quoted}/tests/test_*.sh)",
                        f"Bash(python3 {quoted}/scripts/lint-instructions.py)",
                        f"Bash(make -C {quoted} test)",
                        f"Bash(git -C {quoted} rev-parse HEAD)",
                        f"Bash(git -C {quoted} status --short)",
                        f"Bash(git -C {quoted} diff)",
                        (
                            f"Bash(git -C {quoted} --no-pager log "
                            "--oneline -20)"
                        ),
                        (
                            f"Bash(git -C {quoted} --no-pager show "
                            "--stat --oneline HEAD)"
                        ),
                        (
                            f"Bash(python3 {quoted}/scripts/"
                            "check-skill-budget.py)"
                        ),
                        f"Bash(make -C {quoted} test-harness)",
                        f"Bash(make -C {quoted} test-model-routing)",
                        f"Bash(git -C {quoted} diff --check)",
                    )
                )
                if callback_pointer is not None:
                    submit = shlex.join(
                        (
                            str(Path(sys.executable).resolve()),
                            str(
                                product_root
                                / "scripts"
                                / "harness"
                                / "review_submit.py"
                            ),
                            "--worktree",
                            str(product_root),
                            "--state-dir",
                            str(callback_pointer.parent),
                        )
                    )
                    args.append(f"Bash({submit})")
                args.extend(["--add-dir", str(product_root)])
        if resume:
            if not CHECKPOINT.fullmatch(resume):
                raise ClaudeDriverError("Claude checkpoint is invalid")
            args.extend(["--resume", resume])
        args.append("--")
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
            raise ClaudeDriverError("Claude resume requires an exact checkpoint")
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
        """Prove callback transport without widening a read-only profile."""

        if not callback_pointer.is_absolute():
            return False
        try:
            command = self.command(route, callback_pointer=callback_pointer)
        except ClaudeDriverError:
            return False
        if route.profile == "reviewer-callback":
            return (
                f"Edit({callback_pointer})" in command
                and f"Write({callback_pointer})" in command
            )
        return route.profile in {"executor", "prototype"}

    @staticmethod
    def auth_command(binary: Path) -> tuple[str, ...]:
        return (str(binary), "auth", "status")

    @staticmethod
    def authenticated_subscription(
        stdout: str, stderr: str, returncode: int
    ) -> bool:
        del stderr
        if returncode:
            return False
        try:
            status = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(status, dict)
            and status.get("loggedIn") is True
            and status.get("authMethod") == "claude.ai"
            and status.get("apiProvider") == "firstParty"
            and str(status.get("subscriptionType") or "").casefold()
            in {"pro", "max", "team", "enterprise"}
        )
