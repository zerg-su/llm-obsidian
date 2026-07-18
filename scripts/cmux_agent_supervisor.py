#!/usr/bin/env python3
"""Run one interactive cmux agent with a watchdog and post-exit lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NoReturn

from lifecycle_telemetry import emit_lifecycle_event, nonnegative_int, read_object
from model_routing import RoutingError, load_config as load_routing_config, resolve as resolve_model_route, session_from_meta
from task_contract import ContractError, normalize, read_json as read_task_json


SCRIPT_DIR = Path(__file__).resolve().parent
SPEC_FILES = {"task": ".task-agent-command.json", "reviewer": ".review-agent-command.json"}
PROMPT_FILES = {"task": ".task-prompt.md", "reviewer": ".review-prompt.md"}
ALLOWED_ENV = {"CODEX_HOME", "TMPDIR", "CMUX_SOCKET_PATH", "DCG_CONFIG", "PATH"}
REVIEW_RELAY_FILE = ".review-relay.json"
REVIEW_OUTBOX_FILE = ".review-outbox.json"
REVIEW_RELAY_POLL_SECONDS = 0.25
REVIEW_RELAY_TIMEOUT_SECONDS = 15
try:
    _ROUTING_CONFIG = load_routing_config(Path(__file__).resolve().parents[1])
except RoutingError:  # Hermetic consumers may copy only the supervisor.
    _ROUTING_CONFIG = load_routing_config()
DEFAULT_CODEX_MODEL = _ROUTING_CONFIG.runtime_default("codex")["model"]
DEFAULT_CODEX_EFFORT = _ROUTING_CONFIG.runtime_default("codex")["effort"]
DEFAULT_CLAUDE_MODEL = _ROUTING_CONFIG.runtime_default("claude")["model"]
DEFAULT_CLAUDE_EFFORT = _ROUTING_CONFIG.runtime_default("claude")["effort"]
CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
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
CLAUDE_REVIEW_ALLOWED_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "Edit(./.review-outbox.json)",
    "Bash(git diff *)",
    "Bash(git status *)",
    "Bash(git log *)",
    "Bash(git show *)",
    # Repository test entrypoints are executable code, but reviewers already
    # need to run changed tests to verify a task. These end-anchored patterns
    # deny the observed pipe/redirect/wrapper forms, but the embedded wildcard
    # is not an argv parser: a trailing token that also ends in .py/.sh may
    # still match. The prompt therefore requires the exact no-argument form.
    "Bash(python3 tests/test_*.py)",
    "Bash(bash tests/test_*.sh)",
    "Bash(python3 scripts/lint-instructions.py)",
    "Bash(python3 scripts/document-normalize.py check *)",
    "Bash(bash scripts/dcg-test-suite.sh)",
    "Bash(make test)",
    "Bash(cmux --help)",
    "Bash(cmux notify --help)",
    "Bash(cmux read-screen --help)",
    "Bash(cmux top --help)",
    "Bash(python3 *send_review.py submit *)",
)

RUNTIME_COMMANDS = ("python3", "git", "bash", "make", "uv", "brew", "cmux", "codex", "claude")
RUNTIME_DIRS = (
    Path.home() / ".local" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
)


class SupervisorError(RuntimeError):
    pass


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


def task_dcg_config() -> Path:
    path = (SCRIPT_DIR.parent / "config" / "dcg" / "task.toml").resolve()
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


def option_value(argv: list[str], flag: str) -> str | None:
    values = option_values(argv, flag)
    if not values:
        return None
    if len(values) != 1:
        raise SupervisorError(f"agent command must contain at most one {flag}")
    return values[0]


def option_values(argv: list[str], flag: str) -> list[str]:
    positions = [index for index, item in enumerate(argv) if item == flag or item.startswith(f"{flag}=")]
    values: list[str] = []
    for index in positions:
        if argv[index] != flag or index + 1 >= len(argv):
            raise SupervisorError(f"agent command must pass {flag} as a separate option")
        values.append(argv[index + 1])
    return values


def require_option(argv: list[str], flag: str, expected: str) -> None:
    if option_value(argv, flag) != expected:
        raise SupervisorError(f"agent command must pin {flag} {expected}")


def validated_cmux_socket_path() -> Path:
    raw = os.environ.get("CMUX_SOCKET_PATH") or os.environ.get("CMUX_SOCKET")
    if raw and ("\n" in raw or "\0" in raw):
        raise SupervisorError("cmux socket path is malformed")
    path = (Path(raw).expanduser() if raw else Path.home() / ".local/state/cmux/cmux.sock").resolve()
    try:
        stat = path.stat()
        available = path.is_socket()
    except OSError:
        available = False
        stat = None
    if not available or stat is None or stat.st_uid != os.getuid():
        raise SupervisorError(f"cmux socket is unavailable or not user-owned: {path}")
    return path


def codex_effort_config(effort: str) -> str:
    if effort not in CODEX_EFFORTS:
        raise SupervisorError(f"Codex effort must be one of {sorted(CODEX_EFFORTS)}")
    return f'model_reasoning_effort="{effort}"'


def task_codex_config_values(cmux_socket: Path, effort: str = DEFAULT_CODEX_EFFORT) -> list[str]:
    socket_rule = json.dumps(str(cmux_socket), ensure_ascii=False)
    return [
        "sandbox_workspace_write.network_access=true",
        "features.network_proxy.enabled=true",
        'features.network_proxy.domains={ "localhost" = "allow", "127.0.0.1" = "allow", "::1" = "allow" }',
        f"features.network_proxy.unix_sockets={{ {socket_rule} = \"allow\" }}",
        "features.network_proxy.allow_local_binding=true",
        "features.network_proxy.allow_upstream_proxy=false",
        "features.network_proxy.dangerously_allow_all_unix_sockets=false",
        "features.network_proxy.dangerously_allow_non_loopback_proxy=false",
        "features.network_proxy.enable_socks5=false",
        "features.network_proxy.enable_socks5_udp=false",
        codex_effort_config(effort),
    ]


def reviewer_codex_config_values(effort: str = "") -> list[str]:
    values = [
        'web_search="disabled"',
        "sandbox_workspace_write.network_access=true",
        "features.network_proxy.enabled=true",
        'features.network_proxy.domains={ "localhost" = "allow", "127.0.0.1" = "allow", "::1" = "allow" }',
        "features.network_proxy.unix_sockets={}",
        "features.network_proxy.allow_local_binding=true",
        "features.network_proxy.allow_upstream_proxy=false",
        "features.network_proxy.dangerously_allow_all_unix_sockets=false",
        "features.network_proxy.dangerously_allow_non_loopback_proxy=false",
        "features.network_proxy.enable_socks5=false",
        "features.network_proxy.enable_socks5_udp=false",
    ]
    if effort:
        values.append(codex_effort_config(effort))
    return values


def append_task_codex_network_policy(argv: list[str], cmux_socket: Path, effort: str) -> None:
    for value in task_codex_config_values(cmux_socket, effort):
        argv.extend(["-c", value])


def validate_reviewer_safety(
    argv: list[str],
    runtime: str,
    reviewer_model: str,
    reviewer_effort: str = "",
) -> None:
    require_option(argv, "--model", reviewer_model)
    if runtime == "codex":
        require_option(argv, "-s", "workspace-write")
        require_option(argv, "-a", "never")
        require_option(argv, "--disable", "hooks")
        if option_values(argv, "-c") != reviewer_codex_config_values(reviewer_effort):
            raise SupervisorError("Codex reviewer command has an unexpected network policy")
        if "--add-dir" in argv:
            raise SupervisorError("Codex reviewer command must not request additional writable roots")
        if any(item in CODEX_FORBIDDEN_OPTIONS for item in argv) or "danger-full-access" in argv:
            raise SupervisorError("Codex reviewer command weakens the isolated scratch boundary")
        return

    require_option(argv, "--permission-mode", "dontAsk")
    require_option(argv, "--tools", CLAUDE_REVIEW_TOOL_SURFACE)
    if reviewer_effort:
        require_option(argv, "--effort", reviewer_effort)
    if "--dangerously-skip-permissions" in argv:
        raise SupervisorError("Claude reviewer command bypasses permissions")
    allowed_positions = [index for index, item in enumerate(argv) if item == "--allowedTools"]
    model_positions = [index for index, item in enumerate(argv) if item == "--model"]
    if len(allowed_positions) != 1 or len(model_positions) != 1:
        raise SupervisorError("Claude reviewer command must pin allowed tools and model")
    allowed_index, model_index = allowed_positions[0], model_positions[0]
    if allowed_index >= model_index:
        raise SupervisorError("Claude reviewer allowed tools are malformed")
    if tuple(argv[allowed_index + 1:model_index]) != CLAUDE_REVIEW_ALLOWED_TOOLS:
        raise SupervisorError("Claude reviewer command has an unexpected permission allowlist")


def validate_task_safety(
    argv: list[str],
    runtime: str,
    interaction_policy: str,
    git_common_dir: Path | None = None,
    cmux_socket: Path | None = None,
    model: str = "",
    effort: str = "high",
) -> None:
    require_option(argv, "--model", model)
    if runtime == "codex":
        if any(item in CODEX_FORBIDDEN_OPTIONS for item in argv) or "danger-full-access" in argv:
            raise SupervisorError("Codex task command weakens the approved sandbox")
        if interaction_policy == "unattended":
            if git_common_dir is None or cmux_socket is None:
                raise SupervisorError("Codex unattended task is missing an approved runtime root")
            require_option(argv, "--add-dir", str(git_common_dir))
            require_option(argv, "-a", "never")
            require_option(argv, "-s", "workspace-write")
            if option_values(argv, "-c") != task_codex_config_values(cmux_socket, effort):
                raise SupervisorError("Codex task command has an unexpected network policy")
        elif any(option_value(argv, flag) is not None for flag in ("-a", "-s", "--add-dir")):
            raise SupervisorError("interactive Codex task command has unexpected approval overrides")
        elif option_values(argv, "-c") != [codex_effort_config(effort)]:
            raise SupervisorError("interactive Codex task command has unexpected config overrides")
        return
    require_option(argv, "--permission-mode", "auto")
    require_option(argv, "--effort", effort)
    if "--dangerously-skip-permissions" in argv or "--allowedTools" in argv:
        raise SupervisorError("Claude task command has unexpected permission overrides")


def expected_codex_home(meta: dict[str, Any]) -> str | None:
    raw = str(meta.get("codex_home") or "").strip()
    return str(Path(raw).expanduser().resolve()) if raw else None


def resolved_task_model_route(worktree: Path, meta: dict[str, Any], runtime: str) -> dict[str, Any]:
    """Resolve new routing envelopes while preserving concrete legacy metadata."""
    config_root = worktree if (worktree / "config/model-routing.toml").is_file() else _ROUTING_CONFIG.root
    try:
        config = load_routing_config(config_root)
        routing = meta.get("routing")
        effective = routing.get("effective") if isinstance(routing, dict) else None
        if isinstance(effective, dict):
            route = {
                "runtime": str(effective.get("runtime") or ""),
                "model": str(effective.get("model") or ""),
                "effort": str(effective.get("effort") or ""),
                "source": effective.get("source") or ["metadata-envelope"],
                "config_sha256": str(effective.get("config_sha256") or config.fingerprint),
            }
            if route["runtime"] != runtime or not route["model"]:
                raise RoutingError("task routing envelope disagrees with executor runtime")
            allowed_efforts = CODEX_EFFORTS if runtime == "codex" else CLAUDE_EFFORTS
            if route["effort"] not in allowed_efforts:
                raise RoutingError("task routing envelope has invalid effort")
            registered = config.data["model_registry"].get(route["model"])
            sources = route["source"] if isinstance(route["source"], list) else []
            if registered not in {None, runtime}:
                raise RoutingError("task routing envelope model/provider mismatch")
            if registered is None and not {"explicit-model", "explicit-runtime"} <= set(sources):
                raise RoutingError("unregistered task model requires explicit model and runtime sources")
            return route
        session = session_from_meta(meta)
        explicit_model = str(meta.get("model") or "").strip()
        explicit_effort = str(meta.get("effort") or "").strip()
        if session is not None:
            return resolve_model_route(
                config,
                "dispatch",
                session=session,
                explicit_runtime=runtime if runtime != session["runtime"] else "",
                explicit_model=explicit_model,
                explicit_effort=explicit_effort,
            )
        # v1/v2 metadata created before the routing envelope treats concrete
        # fields as explicit overrides and otherwise uses the central default.
        default = config.runtime_default(runtime)
        if explicit_model:
            registered = config.data["model_registry"].get(explicit_model)
            if registered not in {None, runtime}:
                raise RoutingError("legacy task metadata model/provider mismatch")
        default["model"] = explicit_model or default["model"]
        default["effort"] = explicit_effort or default["effort"]
        allowed_efforts = CODEX_EFFORTS if runtime == "codex" else CLAUDE_EFFORTS
        if default["effort"] not in allowed_efforts:
            raise RoutingError("legacy task metadata has invalid effort")
        default.update({"source": ["legacy-metadata" if explicit_model or explicit_effort else "tracked-default"], "config_sha256": config.fingerprint})
        return default
    except RoutingError as exc:
        raise SupervisorError(str(exc)) from exc


def resolved_git_common_dir(worktree: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw or "\n" in raw or "\0" in raw:
        raise SupervisorError("cannot resolve the task Git metadata root")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = worktree / candidate
    common = candidate.resolve()
    if not common.is_dir() or common.stat().st_uid != os.getuid():
        raise SupervisorError("task Git metadata root is missing or not owned by the current user")
    return common


def validated_task_git_common_dir(worktree: Path, meta: dict[str, Any]) -> Path:
    common = resolved_git_common_dir(worktree)
    target_raw = str(meta.get("target_repo") or "").strip()
    if not target_raw:
        return common
    target = Path(target_raw).expanduser().resolve()
    if not target.is_dir() or resolved_git_common_dir(target) != common:
        raise SupervisorError("task worktree does not belong to target_repo")
    return common


def validated_review_runtime(worktree: Path, meta: dict[str, Any]) -> Path:
    raw = str(meta.get("review_runtime_dir") or "").strip()
    if not raw:
        raise SupervisorError("Codex review metadata is missing review_runtime_dir")
    runtime = Path(raw).expanduser().resolve()
    if not runtime.is_dir():
        raise SupervisorError("Codex review runtime directory does not exist")
    dry_run = str(meta.get("review_surface") or "") == "00000000-0000-0000-0000-000000000000"
    if dry_run:
        expected_location = runtime.parent == worktree.parent and runtime.name.startswith(".review-runtime-")
    else:
        root = (SCRIPT_DIR.parent / ".vault-meta" / "review-runtimes").resolve()
        expected_location = (
            runtime.parent == root
            and runtime.name.startswith("llm-review-")
        )
    if not expected_location:
        raise SupervisorError("Codex review runtime is not a generated scratch directory")
    try:
        runtime.relative_to(worktree)
    except ValueError:
        pass
    else:
        # A coordinator review targets the canonical vault itself, so its
        # sanctioned scratch root is inside the reviewed checkout by design.
        # Permit only that exact canonical-vault case; linked task worktrees and
        # every other in-worktree runtime remain rejected.
        coordinator_vault = (
            not dry_run and worktree.resolve() == SCRIPT_DIR.parent.resolve()
        )
        if not coordinator_vault:
            raise SupervisorError("Codex review runtime must be outside the product worktree")
    try:
        worktree.relative_to(runtime)
    except ValueError:
        pass
    else:
        raise SupervisorError("Codex review runtime must not contain the product worktree")
    stat = runtime.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise SupervisorError("Codex review runtime must be owner-only")
    if any(runtime.iterdir()):
        raise SupervisorError("Codex review runtime must be empty before launch")
    return runtime


def validate_routing(worktree: Path, kind: str, surface: str, spec: dict[str, Any]) -> None:
    task_meta = read_task_json(worktree / ".task-meta.json")
    try:
        task_policy = normalize(task_meta)
    except ContractError as exc:
        raise SupervisorError(str(exc)) from exc
    if kind == "task":
        source_meta = task_meta
        expected_surface = str(task_meta.get("task_surface") or "")
        expected_runtime = str(task_meta.get("executor_runtime") or task_meta.get("runtime") or "")
    else:
        source_meta = read_json(worktree / ".review-meta.json")
        expected_surface = str(source_meta.get("review_surface") or "")
        expected_runtime = str(source_meta.get("reviewer_runtime") or "")
    if surface != expected_surface or not surface:
        raise SupervisorError(f"{kind} supervisor surface does not match metadata")
    if spec["runtime"] != expected_runtime:
        raise SupervisorError(f"{kind} supervisor runtime does not match metadata")
    expected_env: dict[str, str] = {}
    if kind == "task" and task_policy["interaction_policy"] == "unattended":
        expected_env["DCG_CONFIG"] = str(task_dcg_config())
    if spec["runtime"] == "codex":
        expected_cwd = (
            validated_review_runtime(worktree, source_meta)
            if kind == "reviewer"
            else worktree
        )
        require_option(spec["argv"], "--cd", str(expected_cwd))
        expected_home = expected_codex_home(source_meta)
        if expected_home is not None:
            expected_env["CODEX_HOME"] = expected_home
        expected_tmp = str(expected_cwd) if kind == "reviewer" else None
        if expected_tmp is not None:
            expected_env["TMPDIR"] = expected_tmp
        expected_socket = (
            validated_cmux_socket_path()
            if kind == "task" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        if expected_socket is not None:
            expected_env["CMUX_SOCKET_PATH"] = str(expected_socket)
        actual_env = dict(spec["env"])
        runtime_path = actual_env.pop("PATH", "")
        validate_trusted_runtime_path(runtime_path, spec["runtime"])
        if actual_env != expected_env:
            raise SupervisorError("Codex supervisor environment does not match the approved runtime")
    else:
        actual_env = dict(spec["env"])
        runtime_path = actual_env.pop("PATH", "")
        validate_trusted_runtime_path(runtime_path, spec["runtime"])
        if actual_env != expected_env:
            raise SupervisorError("Claude supervisor environment does not match the approved runtime")
    if kind == "reviewer":
        validate_reviewer_safety(
            spec["argv"],
            spec["runtime"],
            str(source_meta.get("reviewer_model") or ""),
            str(source_meta.get("reviewer_effort") or ""),
        )
    else:
        task_route = resolved_task_model_route(worktree, source_meta, spec["runtime"])
        git_common_dir = (
            validated_task_git_common_dir(worktree, source_meta)
            if spec["runtime"] == "codex" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        cmux_socket = (
            validated_cmux_socket_path()
            if spec["runtime"] == "codex" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        validate_task_safety(
            spec["argv"],
            spec["runtime"],
            task_policy["interaction_policy"],
            git_common_dir,
            cmux_socket,
            str(task_route["model"]),
            str(task_route["effort"]),
        )


def load_validated_spec(worktree: Path, kind: str, surface: str, raw_path: str = "") -> dict[str, Any]:
    spec = read_json(exact_spec_path(worktree, kind, raw_path))
    validate_spec_shape(spec, kind)
    validate_routing(worktree, kind, surface, spec)
    prompt = (worktree / PROMPT_FILES[kind]).resolve()
    try:
        prompt.relative_to(worktree)
    except ValueError as exc:
        raise SupervisorError("agent prompt resolves outside the worktree") from exc
    if not prompt.is_file():
        raise SupervisorError(f"agent prompt is missing: {prompt}")
    return spec


def prepare_task(worktree: Path, surface: str) -> Path:
    meta = read_task_json(worktree / ".task-meta.json")
    try:
        policy = normalize(meta)
    except ContractError as exc:
        raise SupervisorError(str(exc)) from exc
    runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "")
    if surface != str(meta.get("task_surface") or ""):
        raise SupervisorError("task preparation surface does not match metadata")
    route = resolved_task_model_route(worktree, meta, runtime)
    model = str(route["model"])
    effort = str(route["effort"])
    env: dict[str, str] = {}
    if runtime == "codex":
        argv = ["codex", "--cd", str(worktree)]
        profile = str(meta.get("codex_profile") or "").strip()
        if profile:
            argv.extend(["--profile", profile])
        if effort not in CODEX_EFFORTS:
            raise SupervisorError(f"Codex task effort must be one of {sorted(CODEX_EFFORTS)}")
        argv.extend(["--model", model])
        if policy["interaction_policy"] == "unattended":
            cmux_socket = validated_cmux_socket_path()
            argv.extend(["--add-dir", str(validated_task_git_common_dir(worktree, meta))])
            argv.extend(["-a", "never", "-s", "workspace-write"])
            append_task_codex_network_policy(argv, cmux_socket, effort)
            env["CMUX_SOCKET_PATH"] = str(cmux_socket)
        else:
            argv.extend(["-c", codex_effort_config(effort)])
        codex_home = str(meta.get("codex_home") or "").strip()
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
    elif runtime == "claude":
        if effort not in CLAUDE_EFFORTS:
            raise SupervisorError(f"Claude task effort must be one of {sorted(CLAUDE_EFFORTS)}")
        argv = [
            "claude", "--permission-mode", "auto",
            "--model", model,
            "--effort", effort,
        ]
    else:
        raise SupervisorError("task executor runtime must be claude or codex")
    if policy["interaction_policy"] == "unattended":
        env["DCG_CONFIG"] = str(task_dcg_config())
    return write_agent_spec(worktree, "task", runtime, argv, PROMPT_FILES["task"], env)


def relay_state(worktree: Path) -> dict[str, Any]:
    path = worktree / REVIEW_RELAY_FILE
    if path.exists():
        try:
            value = read_json(path)
            if value.get("version") == 1:
                return value
        except SupervisorError:
            pass
    return {
        "version": 1,
        "status": "waiting",
        "attempts": 0,
        "sent_count": 0,
        "failure_count": 0,
        "last_payload_sha256": None,
    }


def relay_review_outbox_once(
    worktree: Path,
    runtime: Path,
    runner: Any = subprocess.run,
) -> bool:
    """Validate and forward one stable outbox payload outside the reviewer sandbox."""
    outbox = runtime / REVIEW_OUTBOX_FILE
    try:
        raw = outbox.read_bytes()
    except FileNotFoundError:
        return False
    if not raw:
        return False
    digest = hashlib.sha256(raw).hexdigest()
    state = relay_state(worktree)
    if state.get("status") == "failed" and state.get("last_payload_sha256") == digest:
        return False

    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_payload_sha256"] = digest
    command = [
        sys.executable,
        str(SCRIPT_DIR.parent / "skills" / "review-send" / "scripts" / "send_review.py"),
        "submit",
        "--worktree",
        str(worktree),
    ]
    try:
        result = runner(
            command,
            input=raw.decode("utf-8"),
            text=True,
            capture_output=True,
            cwd=worktree,
            timeout=REVIEW_RELAY_TIMEOUT_SECONDS,
            check=False,
        )
        succeeded = result.returncode == 0
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired):
        succeeded = False

    if succeeded:
        outbox.unlink(missing_ok=True)
        state["status"] = "sent"
        state["sent_count"] = int(state.get("sent_count") or 0) + 1
    else:
        state["status"] = "failed"
        state["failure_count"] = int(state.get("failure_count") or 0) + 1
    write_json(worktree / REVIEW_RELAY_FILE, state)
    return succeeded


def run_review_relay(worktree: Path, runtime: Path, stop: threading.Event) -> None:
    state = relay_state(worktree)
    state["status"] = "waiting"
    write_json(worktree / REVIEW_RELAY_FILE, state)
    while not stop.wait(REVIEW_RELAY_POLL_SECONDS):
        relay_review_outbox_once(worktree, runtime)
    relay_review_outbox_once(worktree, runtime)
    state = relay_state(worktree)
    if state.get("status") != "failed":
        state["status"] = "stopped"
        write_json(worktree / REVIEW_RELAY_FILE, state)


def stop_watchdog(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def run_agent(worktree: Path, kind: str, surface: str, raw_spec: str = "") -> int:
    spec = load_validated_spec(worktree, kind, surface, raw_spec)
    started = time.monotonic()
    prompt = (worktree / spec["prompt_file"]).read_text(encoding="utf-8")
    argv = [*spec["argv"], prompt]
    env = os.environ.copy()
    env.update(spec["env"])
    watchdog: subprocess.Popen[bytes] | None = None
    relay_stop: threading.Event | None = None
    relay_thread: threading.Thread | None = None
    agent_rc = 127
    try:
        if kind == "reviewer" and spec["runtime"] == "codex":
            review_meta = read_json(worktree / ".review-meta.json")
            runtime = validated_review_runtime(worktree, review_meta)
            relay_stop = threading.Event()
            relay_thread = threading.Thread(
                target=run_review_relay,
                args=(worktree, runtime, relay_stop),
                name="review-outbox-relay",
                daemon=True,
            )
            relay_thread.start()
        watchdog = subprocess.Popen(
            [
                sys.executable, str(SCRIPT_DIR / "cmux_task_watchdog.py"), "run",
                "--worktree", str(worktree), "--kind", kind, "--surface", surface,
            ],
            cwd=worktree,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        agent_rc = subprocess.run(argv, cwd=worktree, env=env, check=False).returncode
    except KeyboardInterrupt:
        agent_rc = 130
    except OSError as exc:
        print(f"ERROR: cannot start {spec['runtime']} agent: {exc}", file=sys.stderr)
    finally:
        stop_watchdog(watchdog)
        if relay_stop is not None:
            relay_stop.set()
        if relay_thread is not None:
            relay_thread.join(timeout=REVIEW_RELAY_TIMEOUT_SECONDS + 1)

    lifecycle = subprocess.run(
        [
            sys.executable, str(SCRIPT_DIR / "cmux_surface_lifecycle.py"), "after-exit",
            "--worktree", str(worktree), "--kind", kind, "--surface", surface,
        ],
        cwd=worktree,
        check=False,
    )
    watchdog_state = read_object(
        worktree / (".review-watchdog.json" if kind == "reviewer" else ".task-watchdog.json")
    )
    relay = read_object(worktree / REVIEW_RELAY_FILE) if kind == "reviewer" else {}
    normalized_agent_rc = nonnegative_int(agent_rc)
    normalized_lifecycle_rc = nonnegative_int(lifecycle.returncode)
    counts = {
        "duration_ms": round((time.monotonic() - started) * 1000),
        "agent_exit_code": normalized_agent_rc,
        "agent_signal": abs(agent_rc) if agent_rc < 0 else 0,
        "lifecycle_exit_code": normalized_lifecycle_rc,
        "lifecycle_signal": abs(lifecycle.returncode) if lifecycle.returncode < 0 else 0,
        "watchdog_warnings": nonnegative_int(watchdog_state.get("warning_count")),
        "watchdog_alerts": nonnegative_int(watchdog_state.get("alert_count")),
        "watchdog_degraded": nonnegative_int(watchdog_state.get("degraded_count")),
        "watchdog_recoveries": nonnegative_int(watchdog_state.get("recovery_count")),
        "watchdog_sampling_recoveries": nonnegative_int(
            watchdog_state.get("sampling_recovery_count")
        ),
        "watchdog_read_failures": nonnegative_int(watchdog_state.get("read_failure_count")),
        "watchdog_notification_failures": nonnegative_int(
            watchdog_state.get("notification_failures")
        ),
        "relay_sent": nonnegative_int(relay.get("sent_count")),
        "relay_failures": nonnegative_int(relay.get("failure_count")),
    }
    emit_lifecycle_event(
        worktree,
        "agent-run",
        actor=f"{kind}:{spec['runtime']}",
        counts=counts,
        status="ok" if agent_rc == 0 and lifecycle.returncode == 0 else "error",
    )
    return agent_rc if agent_rc != 0 else lifecycle.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-task")
    prepare.add_argument("--worktree", default=".")
    prepare.add_argument("--surface", required=True)
    for name in ("validate", "run"):
        command = sub.add_parser(name)
        command.add_argument("--worktree", default=".")
        command.add_argument("--kind", choices=sorted(SPEC_FILES), required=True)
        command.add_argument("--surface", required=True)
        command.add_argument("--spec", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    try:
        if args.command == "prepare-task":
            print(prepare_task(worktree, args.surface))
            return 0
        if args.command == "validate":
            spec = load_validated_spec(worktree, args.kind, args.surface, args.spec)
            print(shlex.join([*spec["argv"], f"<{spec['prompt_file']}>"]))
            return 0
        return run_agent(worktree, args.kind, args.surface, args.spec)
    except (ContractError, SupervisorError, OSError, ValueError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
