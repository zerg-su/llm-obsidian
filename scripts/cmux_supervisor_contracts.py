"""Command specs and trusted runtime identities for the legacy cmux supervisor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, NoReturn

from cmux_agent_support import (
    CODEX_EFFORTS,
    DEFAULT_CODEX_EFFORT,
    ROUTING_CONFIG as _ROUTING_CONFIG,
    SupervisorError,
    codex_automation_service_tier_config,
    codex_effort_config,
    resolved_git_common_dir,
    task_codex_config_values,
    validated_cmux_socket_path,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SPEC_FILES = {"task": ".task-agent-command.json", "reviewer": ".review-agent-command.json"}
PROMPT_FILES = {"task": ".task-prompt.md", "reviewer": ".review-prompt.md"}
ALLOWED_ENV = {
    "CODEX_HOME",
    "TMPDIR",
    "CMUX_SOCKET_PATH",
    "DCG_CONFIG",
    "PATH",
    "LLM_OBSIDIAN_PROJECT_ROOT",
    "LLM_OBSIDIAN_SESSION_ROLE",
}
REVIEW_RELAY_FILE = ".review-relay.json"
REVIEW_OUTBOX_FILE = ".review-outbox.json"
REVIEW_RELAY_POLL_SECONDS = 0.25
REVIEW_RELAY_TIMEOUT_SECONDS = 15
WORKSPACE_TRUST_POLL_SECONDS = 0.5
ARMED_EXIT_POLL_SECONDS = 0.25
# A cold runtime may paint its native workspace-trust dialog long after launch.
# Thirty minutes covers slow subscription/auth startup without polling the
# surface for the full lifetime of a long-running agent.
WORKSPACE_TRUST_TIMEOUT_SECONDS = 30 * 60
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
CODEX_FORBIDDEN_OPTIONS = {
    "--full-auto",
    "--dangerously-bypass-approvals-and-sandbox",
    "--sandbox",
    "--ask-for-approval",
    "--approval-policy",
    "--config",
    "-C",
}
CLAUDE_REVIEW_TOOL_SURFACE = "Read,Glob,Grep,Write,Bash"
CLAUDE_REVIEW_BASE_ALLOWED_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "Edit(./.review-outbox.json)",
    "Write(./.review-outbox.json)",
    # Repository test entrypoints are executable code, but reviewers already
    # need to run changed tests to verify a task. These end-anchored patterns
    # deny the observed pipe/redirect/wrapper forms, but the embedded wildcard
    # is not an argv parser: a trailing token that also ends in .py/.sh may
    # still match. The prompt therefore requires the exact no-argument form.
    "Bash(python3 tests/test_*.py)",
    "Bash(bash tests/test_*.sh)",
    "Bash(python3 scripts/lint-instructions.py)",
    "Bash(bash scripts/dcg-test-suite.sh)",
    "Bash(make test)",
    "Bash(cmux --help)",
    "Bash(cmux notify --help)",
    "Bash(cmux read-screen --help)",
    "Bash(cmux top --help)",
)


def claude_review_allowed_tools(
    worktree: Path,
    *,
    base_branch: str = "",
) -> tuple[str, ...]:
    """Return a reviewer allowlist pinned to one exact product worktree."""
    root = worktree.expanduser().resolve()

    def command_pattern(relative: str) -> str:
        placeholder = "__LLM_OBSIDIAN_REVIEW_FILE__"
        rendered = shlex.quote(str(root / relative.replace("*", placeholder)))
        return rendered.replace(placeholder, "*")

    cwd_git_commands = [
        ["git", "status", "--porcelain=v1"],
        ["git", "status", "--short"],
        ["git", "diff"],
        ["git", "diff", "--stat"],
        ["git", "log", "--oneline", "-10"],
        ["git", "show", "--stat", "HEAD"],
    ]
    exact_git_commands = list(cwd_git_commands)
    if base_branch:
        revision_range = f"{base_branch}...HEAD"
        exact_git_commands.extend([
            ["git", "diff", revision_range],
            ["git", "diff", revision_range, "--stat"],
            ["git", "log", "--oneline", revision_range],
        ])
    anchored_git_commands = [
        ["git", "-C", str(root), *command[1:]] for command in exact_git_commands
    ]
    dynamic = [
        f"Bash(python3 {command_pattern('tests/test_*.py')})",
        f"Bash(bash {command_pattern('tests/test_*.sh')})",
        f"Bash(python3 {shlex.quote(str(root / 'scripts' / 'lint-instructions.py'))})",
        f"Bash(bash {shlex.quote(str(root / 'scripts' / 'dcg-test-suite.sh'))})",
        *(f"Bash({shlex.join(command)})" for command in exact_git_commands),
        *(f"Bash({shlex.join(command)})" for command in anchored_git_commands),
    ]
    return CLAUDE_REVIEW_BASE_ALLOWED_TOOLS + tuple(dynamic)

RUNTIME_COMMANDS = ("python3", "git", "bash", "make", "uv", "brew", "cmux", "codex", "claude")
RUNTIME_DIRS = (
    Path.home() / ".local" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
)


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SupervisorError(f"{path} must contain an object")
    return value


def atomic_tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp.{os.getpid()}")


def write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = atomic_tmp_path(path)
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def exact_spec_path(worktree: Path, kind: str, raw: str = "") -> Path:
    expected = (worktree / SPEC_FILES[kind]).resolve()
    candidate = Path(raw).expanduser() if raw else expected
    if not candidate.is_absolute():
        candidate = worktree / candidate
    candidate = candidate.resolve()
    if candidate != expected:
        raise SupervisorError(f"{kind} command spec must be {expected}")
    return candidate


def write_agent_spec(
    worktree: Path,
    kind: str,
    runtime: str,
    argv: list[str],
    prompt_file: str,
    env: dict[str, str] | None = None,
) -> Path:
    path = exact_spec_path(worktree, kind)
    spec_env = dict(env or {})
    spec_env["PATH"] = trusted_runtime_path()
    payload = {
        "version": 1,
        "kind": kind,
        "runtime": runtime,
        "argv": argv,
        "prompt_file": prompt_file,
        "env": spec_env,
    }
    validate_spec_shape(payload, kind)
    write_json(path, payload)
    return path


def trusted_runtime_path() -> str:
    """Return a stable, owner/root-controlled tool path for background agents."""
    candidates: list[Path] = [Path(sys.executable).resolve().parent]
    candidates.extend(
        Path(item).expanduser()
        for item in os.environ.get("PATH", "").split(os.pathsep)
        if item
    )
    for command in RUNTIME_COMMANDS:
        resolved = shutil.which(command)
        if resolved:
            selected = Path(resolved).expanduser()
            # Preserve the directory selected by the caller before adding
            # generic prefixes. Also add the symlink target directory for
            # tools whose runtime assets live beside the real executable.
            candidates.extend((selected.parent, selected.resolve().parent))
    candidates.extend(RUNTIME_DIRS)
    candidates.extend(
        sorted(
            (Path.home() / ".local/share/llm-obsidian/docling").glob("*/venv/bin"),
            reverse=True,
        )
    )
    candidates.extend(
        sorted((Path.home() / ".local/share/uv/python").glob("*/bin"), reverse=True)
    )

    accepted: list[str] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            directory = candidate.expanduser().resolve()
        except OSError:
            continue
        if (
            directory in seen
            or not runtime_directory_is_stable(directory)
            or not runtime_directory_is_trusted(directory)
        ):
            continue
        seen.add(directory)
        accepted.append(str(directory))
    if not accepted:
        raise SupervisorError("no trusted runtime directories are available")
    return os.pathsep.join(accepted)


def runtime_directory_is_trusted(directory: Path) -> bool:
    try:
        stat = directory.stat()
    except OSError:
        return False
    if not directory.is_dir() or stat.st_uid not in {0, os.getuid()} or stat.st_mode & 0o002:
        return False
    # Homebrew's prefix is commonly user-owned and group-writable on macOS.
    # The owner already controls it. Root-owned directories remain stricter
    # because another privileged group must not inject a command.
    return stat.st_uid != 0 or not stat.st_mode & 0o020


def runtime_directory_is_stable(directory: Path) -> bool:
    """Reject cmux's per-session CLI shims from durable agent specs."""
    return "cmux-cli-shims" not in directory.parts


def trusted_claude_wrapper(surface: str, env: dict[str, str] | None = None) -> Path | None:
    """Return cmux's ephemeral Claude wrapper only for this exact live surface.

    The wrapper stays out of durable command specs, but using the surface-bound
    copy at execution time lets cmux publish Claude's native session checkpoint.
    A missing or mismatched wrapper falls back to the already validated PATH.
    """
    values = os.environ if env is None else env
    raw = str(values.get("CMUX_CLAUDE_WRAPPER_SHIM") or "").strip()
    raw_root = str(values.get("CMUX_CLAUDE_WRAPPER_SHIM_ROOT") or "").strip()
    bound_surface = str(values.get("CMUX_SURFACE_ID") or "").strip()
    if not raw or not raw_root or bound_surface != surface:
        return None
    candidate = Path(raw).expanduser()
    root = Path(raw_root).expanduser()
    try:
        candidate = candidate.resolve()
        root = root.resolve()
        stat = candidate.stat()
    except OSError:
        return None
    if (
        candidate.name != "claude"
        or candidate.parent != root
        or root.name != surface
        or "cmux-cli-shims" not in root.parts
        or not candidate.is_file()
        or not os.access(candidate, os.X_OK)
        or stat.st_uid != os.getuid()
        or stat.st_mode & 0o022
    ):
        return None
    return candidate


def validated_caller_identity(payload: object, surface: str) -> dict[str, str]:
    """Return the explicit cmux target identity, never the focused surface."""
    if not isinstance(payload, dict):
        raise SupervisorError("cmux identify returned a non-object payload")
    caller = payload.get("caller")
    if not isinstance(caller, dict):
        raise SupervisorError("cmux identify returned no caller identity")
    surface_id = str(caller.get("surface_id") or "").strip()
    surface_ref = str(caller.get("surface_ref") or "").strip()
    if surface_id.casefold() != surface.casefold():
        raise SupervisorError("caller surface identity mismatch")
    if not re.fullmatch(r"surface:\d+", surface_ref):
        raise SupervisorError("cmux identify returned an invalid caller surface ref")
    return {"surface_id": surface_id, "surface_ref": surface_ref}


def identify_caller(surface: str) -> dict[str, str]:
    """Resolve one exact surface through cmux's caller-preserving JSON output."""
    cmux = shutil.which("cmux", path=trusted_runtime_path())
    if not cmux:
        raise SupervisorError("cmux is unavailable")
    result = subprocess.run(
        [cmux, "--id-format", "both", "identify", "--surface", surface],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SupervisorError(f"cmux identify failed: {detail[:300]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SupervisorError("cmux identify returned invalid JSON") from exc
    return validated_caller_identity(payload, surface)


def validate_trusted_runtime_path(raw: str, runtime: str) -> None:
    entries = raw.split(os.pathsep) if raw else []
    if not entries or len(entries) > 128 or len(entries) != len(set(entries)):
        raise SupervisorError("agent runtime PATH is empty, oversized, or duplicated")
    for entry in entries:
        candidate = Path(entry).expanduser()
        if not candidate.is_absolute() or candidate.resolve() != candidate:
            raise SupervisorError("agent runtime PATH must use canonical absolute directories")
        if not runtime_directory_is_stable(candidate) or not runtime_directory_is_trusted(candidate):
            raise SupervisorError(f"agent runtime PATH contains an untrusted directory: {candidate}")
    for command in (runtime, "python3", "git", "bash", "cmux"):
        if shutil.which(command, path=raw) is None:
            raise SupervisorError(f"agent runtime PATH cannot resolve required command: {command}")


def task_dcg_config(meta: dict[str, Any] | None = None) -> Path:
    root = SCRIPT_DIR.parent
    if meta is not None and meta.get("version") in {3, 4}:
        raw = str(meta.get("vault_root") or "").strip()
        candidate = Path(raw).expanduser() if raw else Path()
        if not raw or not candidate.is_absolute():
            raise SupervisorError("v3 task metadata has no absolute coordinator vault root")
        root = candidate.resolve()
    path = (root / "config" / "dcg" / "task.toml").resolve()
    try:
        stat = path.stat()
    except OSError as exc:
        raise SupervisorError(f"task DCG profile is unavailable: {path}") from exc
    if not path.is_file() or stat.st_uid not in {0, os.getuid()} or stat.st_mode & 0o022:
        raise SupervisorError(f"task DCG profile is not trusted: {path}")
    return path


def validate_spec_shape(spec: dict[str, Any], kind: str) -> None:
    if set(spec) != {"version", "kind", "runtime", "argv", "prompt_file", "env"}:
        raise SupervisorError("agent command spec has unexpected or missing fields")
    if spec.get("version") != 1 or spec.get("kind") != kind:
        raise SupervisorError("agent command spec version/kind mismatch")
    runtime = spec.get("runtime")
    if runtime not in {"claude", "codex"}:
        raise SupervisorError("agent command runtime must be claude or codex")
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > 64:
        raise SupervisorError("agent command argv must contain 1..64 arguments")
    if any(not isinstance(item, str) or not item or "\0" in item for item in argv):
        raise SupervisorError("agent command argv contains an invalid argument")
    if argv[0] != runtime:
        raise SupervisorError("agent executable must match the declared runtime")
    if spec.get("prompt_file") != PROMPT_FILES[kind]:
        raise SupervisorError("agent command prompt file is not the canonical handoff")
    env = spec.get("env")
    if not isinstance(env, dict) or not set(env) <= ALLOWED_ENV:
        raise SupervisorError("agent command environment contains unsupported keys")
    if any(not isinstance(value, str) or not value or "\0" in value for value in env.values()):
        raise SupervisorError("agent command environment contains an invalid value")
