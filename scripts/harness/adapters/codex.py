"""Codex provider argv/profile adapter; execution belongs to ProcessAdapter."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..contracts import RuntimeRoute
from ..ephemeral_provider import (
    AuthPreflightResult,
    AuthProbeCommand,
    EphemeralCommand,
    EphemeralProcessResult,
    EphemeralProviderError,
    EphemeralRunResult,
    EphemeralRunSpec,
    normalized_run_result,
    validate_output_instance,
)


class CodexDriverError(ValueError):
    pass


EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
CHECKPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
REVIEWER_CONFIG = (
    "sandbox_workspace_write.exclude_slash_tmp=true",
    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    "sandbox_workspace_write.network_access=false",
    "sandbox_workspace_write.writable_roots=[]",
    "shell_environment_policy.ignore_default_excludes=false",
)
_EPHEMERAL_ENV_ALLOWLIST = frozenset(
    {"HOME", "LANG", "LC_ALL", "LOGNAME", "PATH", "SHELL", "TERM", "TMPDIR", "TZ", "USER"}
)
_EPHEMERAL_CONFIG = (
    "features.web_search=false",
    "sandbox_workspace_write.network_access=false",
    "shell_environment_policy.ignore_default_excludes=false",
)


def validate_reviewer_sandbox_command(
    argv: tuple[str, ...],
    *,
    callback_pointer: Path,
    product_root: Path,
    session_root: Path,
) -> None:
    """Fail closed if a persisted Codex reviewer command weakens isolation."""

    def one(flag: str) -> int:
        positions = [index for index, value in enumerate(argv) if value == flag]
        if len(positions) != 1:
            raise CodexDriverError(f"review sandbox must pin one {flag}")
        return positions[0]

    one("--strict-config")
    one("--model")
    cd_index = one("--cd")
    sandbox_index = one("--sandbox")
    approval_index = one("--ask-for-approval")
    try:
        callback_root = callback_pointer.parent.resolve(strict=False)
        session = session_root.resolve()
        product = product_root.resolve()
        observed_root = Path(argv[cd_index + 1]).resolve(strict=False)
        callback_root.relative_to(session)
        sandbox = argv[sandbox_index + 1]
        approval = argv[approval_index + 1]
    except (IndexError, OSError, ValueError) as exc:
        raise CodexDriverError("review sandbox root is invalid") from exc
    if (
        observed_root != callback_root
        or observed_root == product
        or product in observed_root.parents
        or session == product
        or sandbox != "workspace-write"
        or approval != "never"
    ):
        raise CodexDriverError("review sandbox command identity drifted")
    config_values: list[str] = []
    for index, value in enumerate(argv):
        if value != "--config":
            continue
        if index + 1 >= len(argv):
            raise CodexDriverError("review sandbox config is invalid")
        config_values.append(argv[index + 1])
    reasoning = [
        value
        for value in config_values
        if value.startswith("model_reasoning_effort=")
    ]
    if (
        len(reasoning) != 1
        or config_values != [reasoning[0], *REVIEWER_CONFIG]
    ):
        raise CodexDriverError("review sandbox config expands authority")

    option_argv = argv[1:]
    forbidden_exact = {
        "--add-dir",
        "--approval-policy",
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto",
        "-a",
        "-c",
        "-C",
        "-s",
    }
    forbidden_equals = (
        "--add-dir=",
        "--approval-policy=",
        "--cd=",
        "--config=",
        "--sandbox=",
        "-a=",
        "-c=",
        "-C=",
        "-s=",
    )
    if any(
        value in forbidden_exact
        or value.startswith(forbidden_equals)
        or value == "danger-full-access"
        for value in option_argv
    ):
        raise CodexDriverError("review sandbox command has an escape flag")


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
        if route.profile == "reviewer-callback":
            if callback_pointer is None or not callback_pointer.is_absolute():
                raise CodexDriverError(
                    "review callback requires an absolute lane root"
                )
            if session_root is None or not session_root.is_absolute():
                raise CodexDriverError(
                    "review callback requires an absolute session root"
                )
            session_input = session_root.expanduser()
            callback_parent = callback_pointer.parent.expanduser()
            session_lexical = Path(os.path.abspath(session_input))
            callback_lexical = Path(os.path.abspath(callback_parent))
            try:
                relative = callback_lexical.relative_to(session_lexical)
            except ValueError as exc:
                raise CodexDriverError(
                    "review callback lane escapes its session root"
                ) from exc
            cursor = session_lexical
            for part in relative.parts:
                if part == "..":
                    raise CodexDriverError(
                        "review callback lane escapes its session root"
                    )
                cursor /= part
                if cursor.is_symlink():
                    raise CodexDriverError(
                        "review callback lane root must not contain symlinks"
                    )
            session = session_input.resolve()
            callback_root = callback_parent.resolve(strict=False)
            try:
                callback_root.relative_to(session)
            except ValueError as exc:
                raise CodexDriverError(
                    "review callback lane escapes its session root"
                ) from exc
            args.append("--strict-config")
            for value in REVIEWER_CONFIG:
                args.extend(("--config", value))
            args.extend(("--cd", str(callback_root)))
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
                    if route.profile in {"reviewer-callback", "research-safe"}
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
        if (
            type(returncode) is not int
            or returncode != 0
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or stderr.strip()
        ):
            return False
        status = " ".join(stdout.split()).casefold()
        return bool(
            re.fullmatch(r"logged in (?:using|with|to) chatgpt[.!]?", status)
        )


def _codex_ephemeral_environment(
    env: Mapping[str, str], runtime_home: Path
) -> dict[str, str]:
    """Use an isolated Codex home and a minimal credential-free child env."""

    child = {
        key: value
        for key, value in env.items()
        if key in _EPHEMERAL_ENV_ALLOWLIST and isinstance(value, str)
    }
    child["CODEX_HOME"] = str(runtime_home)
    return child


def _codex_failure_disposition(stderr: bytes) -> str:
    status = stderr.decode("utf-8", errors="replace").casefold()
    if any(marker in status for marker in ("usage limit", "rate limit", "quota")):
        return "usage-exhausted"
    if any(marker in status for marker in ("not logged", "authentication", "unauthorized")):
        return "auth-expired"
    if any(marker in status for marker in ("permission denied", "policy denied", "sandbox denied")):
        return "policy-denied"
    return "transport-failed"


@dataclass(frozen=True)
class CodexEphemeralAdapter:
    """ChatGPT-backed Codex exec compiler and JSONL/result normalizer."""

    binary: Path
    logical_provider: str = "openai"
    transport: str = "codex-exec"

    def __post_init__(self) -> None:
        if not self.binary.is_absolute():
            raise EphemeralProviderError("Codex ephemeral binary must be absolute")

    def compile(
        self, spec: EphemeralRunSpec, *, env: Mapping[str, str]
    ) -> EphemeralCommand:
        if spec.logical_provider != self.logical_provider:
            raise EphemeralProviderError("Codex adapter received another provider")
        args = [
            str(self.binary),
            "--model",
            spec.model,
            "--config",
            f"model_reasoning_effort={spec.effort}",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--strict-config",
        ]
        for value in _EPHEMERAL_CONFIG:
            args.extend(("--config", value))
        args.extend(
            (
                "--cd",
                str(spec.cwd),
                "exec",
                "--ephemeral",
                "--json",
                "--output-schema",
                str(spec.output_schema),
                "--output-last-message",
                str(spec.result_path),
                "-",
            )
        )
        return EphemeralCommand(
            tuple(args),
            spec.context_packet.read_bytes(),
            _codex_ephemeral_environment(env, spec.runtime_home),
            self.transport,
        )

    def auth_command(
        self, spec: EphemeralRunSpec, *, env: Mapping[str, str]
    ) -> AuthProbeCommand:
        if spec.logical_provider != self.logical_provider:
            raise EphemeralProviderError("Codex auth probe provider changed")
        return AuthProbeCommand(
            CodexDriver.auth_command(self.binary),
            _codex_ephemeral_environment(env, spec.runtime_home),
        )

    def preflight(
        self,
        spec: EphemeralRunSpec,
        *,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> AuthPreflightResult:
        if spec.logical_provider != self.logical_provider:
            raise EphemeralProviderError("Codex auth probe provider changed")
        ready = CodexDriver.authenticated_subscription(stdout, stderr, returncode)
        return AuthPreflightResult(
            spec.logical_provider,
            spec.auth_profile,
            "ready" if ready else "billing-profile-unverified",
            "chatgpt-login-ready" if ready else "billing-profile-unverified",
            ready,
        )

    def normalize(
        self, spec: EphemeralRunSpec, process: EphemeralProcessResult
    ) -> EphemeralRunResult:
        if spec.logical_provider != self.logical_provider:
            raise EphemeralProviderError("Codex result provider changed")
        if process.timed_out:
            return normalized_run_result(
                spec,
                process,
                transport=self.transport,
                disposition="timeout",
                gap_reason="timeout",
            )
        if process.returncode:
            return normalized_run_result(
                spec,
                process,
                transport=self.transport,
                disposition=_codex_failure_disposition(process.stderr),
            )
        try:
            lines = process.stdout.decode("utf-8").splitlines()
            stream = [json.loads(line) for line in lines if line.strip()]
            result = json.loads(process.result_bytes)
            valid = (
                bool(stream)
                and all(
                    isinstance(item, dict)
                    and isinstance(item.get("type"), str)
                    and bool(item["type"])
                    for item in stream
                )
                and isinstance(result, dict)
                and validate_output_instance(result, spec.schema)
            )
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            valid = False
            result = None
        if not valid:
            return normalized_run_result(
                spec,
                process,
                transport=self.transport,
                disposition="schema-invalid",
                gap_reason="schema-invalid",
            )
        return normalized_run_result(
            spec,
            process,
            transport=self.transport,
            disposition="succeeded",
            result=result,
        )
