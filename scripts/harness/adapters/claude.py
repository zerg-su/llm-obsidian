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


def _review_submit_root(
    callback_pointer: Path | None, product_root: Path
) -> Path:
    if callback_pointer is not None:
        for parent in callback_pointer.parents:
            if parent.name == ".vault-meta":
                return parent.parent
    return product_root


EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CHECKPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _absolute_permission_path(path: Path) -> str:
    """Encode an absolute file path using Claude Code's `//` rule syntax."""

    if not path.is_absolute():
        raise ClaudeDriverError("Claude permission path must be absolute")
    return f"/{path.as_posix()}"


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
            relative_input = ""
            input_pointer: Path | None = None
            if callback_pointer is not None:
                if not callback_pointer.is_absolute():
                    raise ClaudeDriverError(
                        "review callback pointer must be absolute"
                    )
                input_pointer = callback_pointer.with_name(
                    ".review-input.json"
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
                        relative_input = input_pointer.relative_to(
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
                            "Write the final review-round JSON only to this "
                            f"exact input file: {input_pointer}. Pass this "
                            "absolute path verbatim to Edit or Write, then "
                            "run the exact review_submit.py command. Never "
                            f"hand-write the generated callback: {callback_pointer}"
                            + (
                                "; the input file's exact session-relative alias is "
                                f"{relative_input}"
                                if relative_input
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
                input_rule = _absolute_permission_path(input_pointer)
                args.extend(
                    (
                        f"Edit({input_rule})",
                        f"Write({input_rule})",
                    )
                )
                if relative_callback:
                    args.extend(
                        (
                            f"Edit({relative_input})",
                            f"Write({relative_input})",
                        )
                    )
            if product_root is not None:
                if not product_root.is_absolute():
                    raise ClaudeDriverError("review product root must be absolute")
                quoted = shlex.quote(str(product_root))
                inspect = shlex.join(
                    (
                        str(Path(sys.executable).resolve()),
                        str(product_root / "scripts" / "review-inspect.py"),
                        "--worktree",
                        str(product_root),
                    )
                )
                args.extend(
                    (
                        f"Bash(python3 {quoted}/tests/test_*.py)",
                        f"Bash(bash {quoted}/tests/test_*.sh)",
                        f"Bash(python3 {quoted}/scripts/lint-instructions.py)",
                        f"Bash(make -C {quoted} test)",
                        f"Bash({inspect}:*)",
                        (
                            f"Bash(python3 {quoted}/scripts/"
                            "check-skill-budget.py)"
                        ),
                        f"Bash(make -C {quoted} test-harness)",
                        f"Bash(make -C {quoted} test-model-routing)",
                    )
                )
                if callback_pointer is not None:
                    submit_root = _review_submit_root(
                        callback_pointer, product_root
                    )
                    submit = shlex.join(
                        (
                            str(Path(sys.executable).resolve()),
                            str(
                                submit_root
                                / "scripts"
                                / "harness"
                                / "review_submit.py"
                            ),
                            "--worktree",
                            str(product_root),
                            "--state-dir",
                            str(callback_pointer.parent),
                            "--input-file",
                            str(input_pointer),
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
            input_rule = _absolute_permission_path(
                callback_pointer.with_name(".review-input.json")
            )
            return (
                f"Edit({input_rule})" in command
                and f"Write({input_rule})" in command
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
