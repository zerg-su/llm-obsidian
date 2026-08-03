"""Provider launch, environment, prompt, and failure-containment policy."""

from __future__ import annotations

import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Mapping

from .adapters.process import ProcessAdapter, ProcessError, ProcessHandle
from .adapters.claude import (
    ClaudeDriverError,
    review_test_directory,
    validate_reviewer_sandbox_command,
)
from .contracts import AttentionReason
from .prompts import PromptDecision, classify
from .runtime_worker_contracts import (
    IDENTIFIER,
    SURFACE_UUID,
    RuntimeWorkerError,
)
from .state_machine import TERMINAL
from .store import OperationStore


RESEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def provider_argv(
    spec: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Select cmux's ephemeral wrapper only inside its exact owned surface."""

    argv = tuple(spec.get("argv") or ())
    runtime = str(spec.get("runtime") or "")
    surface_id = str(spec.get("surface_id") or "")
    values = os.environ if env is None else env
    runtime_interpreter = spec.get("runtime_interpreter")
    product_root = spec.get("product_root")
    if (
        runtime == "claude"
        and spec.get("callback_mode") == "envelope"
        and isinstance(product_root, Path)
    ):
        try:
            validate_reviewer_sandbox_command(
                argv,
                callback_pointer=spec["callback_pointer"],
                product_root=product_root,
                session_root=spec["cwd"],
            )
        except (ClaudeDriverError, KeyError, TypeError) as exc:
            raise RuntimeWorkerError(
                "Claude reviewer sandbox identity is invalid"
            ) from exc
    pinned_interpreter = (
        runtime_interpreter
        if isinstance(runtime_interpreter, Path)
        else None
    )
    if (
        not argv
        or runtime not in {"claude", "codex"}
        or not SURFACE_UUID.fullmatch(surface_id)
    ):
        return argv
    if spec.get("callback_mode") in {"research-fetch", "research-synth"}:
        return _pin_env_shebang(argv, values, pinned_interpreter)
    prefix = f"CMUX_{runtime.upper()}_WRAPPER_SHIM"
    raw_wrapper = str(values.get(prefix) or "").strip()
    raw_root = str(values.get(f"{prefix}_ROOT") or "").strip()
    if (
        not raw_wrapper
        or not raw_root
        or str(values.get("CMUX_SURFACE_ID") or "").casefold()
        != surface_id.casefold()
    ):
        return _pin_env_shebang(argv, values, pinned_interpreter)
    candidate = Path(raw_wrapper).expanduser()
    root = Path(raw_root).expanduser()
    try:
        if candidate.is_symlink() or root.is_symlink():
            return _pin_env_shebang(argv, values, pinned_interpreter)
        candidate = candidate.resolve()
        root = root.resolve()
        candidate_stat = candidate.stat()
        root_stat = root.stat()
    except OSError:
        return _pin_env_shebang(argv, values, pinned_interpreter)
    if (
        candidate.name != runtime
        or candidate.parent != root
        or root.name.casefold() != surface_id.casefold()
        or "cmux-cli-shims" not in root.parts
        or not candidate.is_file()
        or not root.is_dir()
        or not os.access(candidate, os.X_OK)
        or candidate_stat.st_uid != os.getuid()
        or root_stat.st_uid != os.getuid()
        or candidate_stat.st_mode & 0o022
        or root_stat.st_mode & 0o022
    ):
        return _pin_env_shebang(argv, values, pinned_interpreter)
    return _pin_env_shebang(
        (str(candidate), *argv[1:]), values, pinned_interpreter
    )


def _pin_env_shebang(
    argv: tuple[str, ...],
    env: Mapping[str, str],
    pinned_interpreter: Path | None = None,
) -> tuple[str, ...]:
    """Resolve one env shebang before protected runtimes sanitize PATH."""

    if not argv:
        return argv
    try:
        with Path(argv[0]).open("rb") as handle:
            first_line = handle.readline(256)
    except OSError:
        return argv
    match = re.fullmatch(
        rb"#![ \t]*/usr/bin/env[ \t]+([A-Za-z0-9._+-]+)[ \t]*\r?\n?",
        first_line,
    )
    if match is None:
        return argv
    interpreter_name = match.group(1).decode("ascii")
    if pinned_interpreter is not None:
        resolved = (
            pinned_interpreter
            if pinned_interpreter.name == interpreter_name
            else None
        )
    else:
        interpreter = shutil.which(interpreter_name, path=env.get("PATH"))
        resolved = (
            Path(interpreter).expanduser().resolve()
            if interpreter
            else None
        )
    if (
        resolved is None
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        return argv
    return (str(resolved), argv[0], *argv[1:])


def provider_resume_argv(
    argv: tuple[str, ...], runtime: str, checkpoint: str
) -> tuple[str, ...]:
    """Bind one provider restart to the exact previously captured session."""

    if not argv or runtime not in {"claude", "codex"}:
        raise RuntimeWorkerError("provider resume runtime is invalid")
    if not IDENTIFIER.fullmatch(checkpoint):
        raise RuntimeWorkerError("provider restart requires an exact checkpoint")
    if runtime == "claude":
        try:
            separator = len(argv) - 1 - tuple(reversed(argv)).index("--")
        except ValueError as exc:
            raise RuntimeWorkerError(
                "Claude provider command lacks the prompt separator"
            ) from exc
        return (
            *argv[:separator],
            "--resume",
            checkpoint,
            "--",
        )
    if len(argv) < 2:
        raise RuntimeWorkerError("Codex provider command lacks its prompt")
    return (*argv[:-1], "resume", checkpoint)


def provider_environment(
    spec: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a fresh environment, isolating protected research from the caller."""

    values = os.environ if env is None else env
    if (
        spec.get("runtime") in {"claude", "codex"}
        and spec.get("callback_mode") == "envelope"
        and isinstance(spec.get("product_root"), Path)
    ):
        callback = spec.get("callback_pointer")
        if not isinstance(callback, Path):
            raise RuntimeWorkerError("review callback root is unavailable")
        temporary = review_test_directory(callback).resolve(strict=False)
        if temporary.is_symlink():
            raise RuntimeWorkerError("review test root must not be a symlink")
        temporary.mkdir(mode=0o700, parents=False, exist_ok=True)
        temporary.chmod(0o700)
        if (
            temporary.resolve() != temporary
            or temporary.stat().st_uid != os.getuid()
            or temporary.stat().st_mode & 0o077
        ):
            raise RuntimeWorkerError("review test root ownership is invalid")
        reviewer = dict(values)
        reviewer["TMPDIR"] = str(temporary)
        if spec.get("runtime") == "claude":
            reviewer["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
            reviewer["DISABLE_AUTOUPDATER"] = "1"
        return reviewer
    if spec.get("callback_mode") not in {
        "research-fetch",
        "research-synth",
    }:
        return dict(values)
    runtime_home = spec.get("runtime_home")
    if not isinstance(runtime_home, Path):
        raise RuntimeWorkerError("research runtime home is unavailable")
    temporary = runtime_home / "tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    temporary.chmod(0o700)
    shell = "/bin/zsh" if Path("/bin/zsh").is_file() else "/bin/sh"
    return {
        "CODEX_HOME": str(runtime_home),
        "HOME": str(runtime_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": RESEARCH_PATH,
        "SHELL": shell,
        "TERM": "xterm-256color",
        "TMPDIR": str(temporary),
        "TZ": "UTC",
    }


def automate_prompt(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    runtime: str,
    surface_id: str,
    screen: str,
    cmux_adapter: object,
    *,
    closure_armed: bool = False,
) -> PromptDecision:
    """Apply only an exact prompt decision; unknown choices become durable."""

    decision = classify(runtime, screen, closure_armed=closure_armed)
    record = store.read(owner_id, operation_id)
    if record.state in TERMINAL or record.state == "attention-required":
        return decision
    if decision.recognized:
        try:
            for key in decision.keys:
                cmux_adapter.send_key(surface_id, key)
        except Exception:
            current = store.read(owner_id, operation_id)
            if current.state not in TERMINAL and current.state != "attention-required":
                store.transition(
                    owner_id,
                    operation_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
        return decision
    if decision.interactive:
        try:
            store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.PROMPT_UNKNOWN,
            )
        except Exception:
            pass
    return decision


def _reap_child(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() <= deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited == pid:
            return True
        time.sleep(0.02)
    return False


def _contain_provider_start_failure(
    process: ProcessAdapter, handle: ProcessHandle
) -> None:
    try:
        process.signal_owned_child_group(
            handle.process_group,
            handle.process_identity,
            signal.SIGTERM,
        )
    except ProcessError:
        pass
    if _reap_child(handle.pid, 0.5):
        return
    try:
        process.signal_owned_child_group(
            handle.process_group,
            handle.process_identity,
            signal.SIGKILL,
        )
    except ProcessError:
        pass
    _reap_child(handle.pid, 0.5)
