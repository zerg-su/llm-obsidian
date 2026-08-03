"""Claude provider argv/profile adapter; execution belongs to ProcessAdapter."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import RuntimeRoute


class ClaudeDriverError(ValueError):
    pass


EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CHECKPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

REVIEW_CREDENTIAL_FILES = (
    "~/.aws",
    "~/.azure",
    "~/.codex",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.docker",
    "~/.gnupg",
    "~/.kube",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.ssh",
)
REVIEW_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "CODEX_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "NPM_TOKEN",
    "OPENAI_API_KEY",
    "PYPI_TOKEN",
    "SSH_AUTH_SOCK",
)


def _absolute_permission_path(path: Path) -> str:
    """Encode an absolute file path using Claude Code's `//` rule syntax."""

    if not path.is_absolute():
        raise ClaudeDriverError("Claude permission path must be absolute")
    return f"/{path.as_posix()}"


def _resolved_owned_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ClaudeDriverError(f"{label} must be absolute")
    resolved = path.resolve(strict=False)
    raw_current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        raw_current /= part
        if index == 1 and part in {"tmp", "var"}:
            continue
        if raw_current.is_symlink():
            raise ClaudeDriverError(f"{label} must not traverse a symlink")
    inspected = resolved
    current = Path(inspected.anchor)
    for part in inspected.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ClaudeDriverError(f"{label} must not traverse a symlink")
    if path != resolved and path.parts[1:2] not in {("tmp",), ("var",)}:
        raise ClaudeDriverError(f"{label} must use its canonical realpath")
    if resolved.exists() and resolved.stat().st_uid != os.getuid():
        raise ClaudeDriverError(f"{label} must be owned by the current user")
    return resolved


def _git_common_dir(product_root: Path) -> Path:
    dot_git = product_root / ".git"
    if dot_git.is_symlink():
        raise ClaudeDriverError("product Git metadata must not be a symlink")
    if dot_git.is_dir():
        git_dir = _resolved_owned_path(dot_git, "product Git metadata")
    elif dot_git.is_file():
        first = dot_git.read_text(encoding="utf-8").strip()
        if not first.startswith("gitdir: "):
            raise ClaudeDriverError("product Git metadata pointer is invalid")
        raw = Path(first.removeprefix("gitdir: "))
        git_dir = _resolved_owned_path(
            raw if raw.is_absolute() else product_root / raw,
            "product Git metadata",
        )
    else:
        return dot_git.resolve(strict=False)
    common_pointer = git_dir / "commondir"
    if common_pointer.is_symlink():
        raise ClaudeDriverError("product Git common metadata must not be a symlink")
    if common_pointer.is_file():
        raw_common = Path(common_pointer.read_text(encoding="utf-8").strip())
        common_candidate = (
            raw_common if raw_common.is_absolute() else git_dir / raw_common
        ).resolve(strict=False)
        return _resolved_owned_path(
            common_candidate,
            "product Git common metadata",
        )
    return git_dir


def review_test_directory(callback_pointer: Path) -> Path:
    return callback_pointer.parent / ".review-test-tmp"


def reviewer_sandbox_settings(
    *,
    callback_pointer: Path,
    product_root: Path,
    session_root: Path,
) -> dict[str, object]:
    """Compile one operation-owned native Claude Bash sandbox."""

    product = _resolved_owned_path(product_root, "review product root")
    session = _resolved_owned_path(session_root, "review session root")
    callback_parent = _resolved_owned_path(
        callback_pointer.parent, "review callback root"
    )
    test_tmp = _resolved_owned_path(
        review_test_directory(callback_pointer), "review test root"
    )
    if product == session or product in session.parents or session in product.parents:
        raise ClaudeDriverError("review scratch must be outside the product root")
    if product == callback_parent or product in callback_parent.parents:
        raise ClaudeDriverError("review callback root must be outside the product root")
    try:
        common = Path(os.path.commonpath((session, callback_parent)))
    except ValueError as exc:
        raise ClaudeDriverError("review scratch roots have no common owner") from exc
    if common == Path(common.anchor):
        raise ClaudeDriverError("review scratch roots are not operation-bounded")
    git_common = _git_common_dir(product)
    return {
        "autoMemoryEnabled": False,
        "disableAllHooks": True,
        "disableArtifact": True,
        "disableClaudeAiConnectors": True,
        "includeGitInstructions": False,
        "permissions": {
            "deny": [
                "Agent",
                "Skill",
                "WebFetch",
                "WebSearch",
            ],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "filesystem": {
                "allowWrite": [str(callback_parent), str(test_tmp)],
                "denyWrite": [str(product), str(git_common)],
            },
            "credentials": {
                "files": [
                    {"path": path, "mode": "deny"}
                    for path in REVIEW_CREDENTIAL_FILES
                ],
                "envVars": [
                    {"name": name, "mode": "deny"}
                    for name in REVIEW_CREDENTIAL_ENV
                ],
            },
            "network": {
                "allowedDomains": [],
                "strictAllowlist": True,
                "allowUnixSockets": [],
                "allowLocalBinding": False,
            },
        },
    }


def validate_reviewer_sandbox_command(
    argv: tuple[str, ...],
    *,
    callback_pointer: Path,
    product_root: Path,
    session_root: Path,
) -> None:
    """Fail closed if a persisted Claude reviewer command weakens its sandbox."""

    def one(flag: str) -> int:
        positions = [index for index, value in enumerate(argv) if value == flag]
        if len(positions) != 1:
            raise ClaudeDriverError(f"review sandbox must pin one {flag}")
        return positions[0]

    settings_index = one("--settings")
    sources_index = one("--setting-sources")
    allowed_index = one("--allowedTools")
    tools_index = one("--tools")
    add_dir_index = one("--add-dir")
    mcp_index = one("--mcp-config")
    for flag in (
        "--safe-mode",
        "--strict-mcp-config",
        "--no-chrome",
        "--disable-slash-commands",
    ):
        one(flag)
    if any(
        flag in argv
        for flag in (
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
            "--plugin-dir",
            "--plugin-url",
        )
    ):
        raise ClaudeDriverError("review sandbox command has an escape flag")
    try:
        observed_settings = json.loads(argv[settings_index + 1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ClaudeDriverError("review sandbox settings are invalid") from exc
    expected_settings = reviewer_sandbox_settings(
        callback_pointer=callback_pointer,
        product_root=product_root,
        session_root=session_root,
    )
    if observed_settings != expected_settings:
        raise ClaudeDriverError("review sandbox settings drifted")
    if (
        sources_index + 1 >= len(argv)
        or argv[sources_index + 1] != ""
        or tools_index + 1 >= len(argv)
        or argv[tools_index + 1] != "Read,Glob,Grep,Edit,Write,Bash"
        or add_dir_index + 1 >= len(argv)
        or Path(argv[add_dir_index + 1]).resolve() != product_root.resolve()
        or mcp_index + 1 >= len(argv)
        or argv[mcp_index + 1] != '{"mcpServers":{}}'
    ):
        raise ClaudeDriverError("review sandbox command identity drifted")
    input_pointer = callback_pointer.with_name(".review-input.json")
    allowed = [
        "Read",
        "Glob",
        "Grep",
        f"Edit({_absolute_permission_path(input_pointer)})",
        f"Write({_absolute_permission_path(input_pointer)})",
    ]
    try:
        relative_input = input_pointer.relative_to(session_root).as_posix()
    except ValueError:
        pass
    else:
        allowed.extend((f"Edit({relative_input})", f"Write({relative_input})"))
    allowed.append("Bash")
    if tuple(argv[allowed_index + 1 : settings_index]) != tuple(allowed):
        raise ClaudeDriverError("review sandbox tool surface drifted")


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
                if callback_pointer is None or session_root is None:
                    raise ClaudeDriverError(
                        "review sandbox requires callback and session roots"
                    )
                settings = reviewer_sandbox_settings(
                    callback_pointer=callback_pointer,
                    product_root=product_root,
                    session_root=session_root,
                )
                args.extend(
                    (
                        "Bash",
                        "--settings",
                        json.dumps(settings, sort_keys=True, separators=(",", ":")),
                        "--setting-sources",
                        "",
                        "--safe-mode",
                        "--strict-mcp-config",
                        "--mcp-config",
                        '{"mcpServers":{}}',
                        "--no-chrome",
                        "--disable-slash-commands",
                    )
                )
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
