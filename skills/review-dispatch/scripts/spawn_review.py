#!/usr/bin/env python3
"""Stateful cross-model review orchestration for dispatch task worktrees."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, NoReturn


VAULT_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(VAULT_SCRIPTS))
from review_contract import (
    ReviewContractError,
    decode_review,
    parse_review_json,
    render_markdown,
)
from task_contract import ContractError as TaskContractError, normalize as normalize_task_contract, review_action
from cmux_agent_supervisor import (
    CLAUDE_REVIEW_ALLOWED_TOOLS,
    CLAUDE_REVIEW_TOOL_SURFACE,
    reviewer_codex_config_values,
    write_agent_spec,
)
from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event, nonnegative_int
from model_routing import RoutingError, load_config as load_routing_config, resolve as resolve_model_route, session_from_meta

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT = Path(__file__).resolve().parents[3]
_ROUTING_CONFIG = load_routing_config(DEFAULT_VAULT)
DEFAULT_CLAUDE_MODEL = _ROUTING_CONFIG.runtime_default("claude")["model"]
DEFAULT_CODEX_MODEL = _ROUTING_CONFIG.runtime_default("codex")["model"]
DEFAULT_CLAUDE_EFFORT = _ROUTING_CONFIG.runtime_default("claude")["effort"]
DEFAULT_CODEX_EFFORT = _ROUTING_CONFIG.runtime_default("codex")["effort"]
REVIEW_MODES = {"full", "light"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Codex `--model` takes precedence over profile/config reasoning settings.
# Preserve an explicitly requested review effort with a later argv override.
CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
CMUX_PASTE_SETTLE_SECONDS = 0.2
HANDOFF_EXCLUDES = [
    ".task-prompt.md",
    ".task-summary.md",
    ".task-summary.json",
    ".task-meta.json",
    ".task-cmux-surface",
    ".task-reap-send-skill",
    ".wiki-cmux-surface",
    ".wiki-agent-runtime",
    ".wiki-reap-command",
    ".task-review.md",
    ".task-review.json",
    ".task-review-verify.md",
    ".task-review-verify.json",
    ".task-review-resolution.md",
    ".task-review-skill",
    ".task-review-send-skill",
    ".review-history.json",
    ".review-archive.json",
    ".review-archive-request.json",
    ".review-prompt.md",
    ".review-prompt-verify.md",
    ".review-meta.json",
    ".review-cmux-surface",
    ".review-baseline-status.txt",
    ".review-baseline-state.json",
    ".review-send-blocked.md",
    ".review-outbox.json",
    ".review-callback.json",
    ".review-relay.json",
    ".review-close-armed.json",
    ".task-close-armed.json",
    ".task-reap-prepared.json",
    ".task-reap-complete.json",
    ".task-needs-attention.json",
    ".task-watchdog.json",
    ".task-watchdog.lock",
    ".review-watchdog.json",
    ".review-watchdog.lock",
    ".task-agent-command.json",
    ".review-agent-command.json",
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
]
REVIEW_CALLBACK_FILE = ".review-callback.json"
REVIEW_ROUND_ARTIFACTS = (
    ".task-review.md",
    ".task-review.json",
    ".task-review-verify.md",
    ".task-review-verify.json",
    ".task-review-resolution.md",
    REVIEW_CALLBACK_FILE,
)


def die(message: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def review_actor(review_meta: dict[str, Any]) -> str:
    runtime = str(review_meta.get("reviewer_runtime") or "unknown")
    model = str(review_meta.get("reviewer_model") or "default")
    mode = str(review_meta.get("review_mode") or "full")
    return f"review:{runtime}:{model}:{mode}"


def review_vault(review_meta: dict[str, Any]) -> Path | None:
    raw = str(review_meta.get("vault_root") or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def valid_vault_root(path: Path) -> bool:
    candidate = path.expanduser().resolve()
    return (
        (candidate / "wiki").is_dir()
        and (candidate / "scripts" / "vault-write.py").is_file()
        and (candidate / "skills" / "review-dispatch" / "scripts" / "archive_review.py").is_file()
    )


def vault_from_plan(task_meta: dict[str, Any]) -> Path | None:
    raw = str(task_meta.get("plan_file") or "").strip()
    if not raw:
        return None
    plan = Path(raw).expanduser().resolve()
    if plan.parent.name != "plans" or plan.parent.parent.name != "wiki":
        return None
    candidate = plan.parents[2]
    return candidate if valid_vault_root(candidate) else None


def resolve_vault_root(
    worktree: Path,
    *,
    explicit: str = "",
    task_meta: dict[str, Any] | None = None,
    review_meta: dict[str, Any] | None = None,
) -> Path:
    """Resolve the coordinator vault without trusting the executing script copy.

    A self-dogfood task can execute this file from its linked worktree, so
    ``DEFAULT_VAULT`` is only a legacy fallback.  Dispatch metadata and its
    approved plan bind the authoritative coordinator vault first.
    """

    meta = task_meta if task_meta is not None else read_json(worktree / ".task-meta.json")
    sources: list[tuple[str, str]] = []
    if explicit.strip():
        sources.append(("--vault-root", explicit))
    declared = str(meta.get("vault_root") or "").strip()
    if declared:
        sources.append((".task-meta.json vault_root", declared))
    plan_vault = vault_from_plan(meta)
    if plan_vault is not None:
        sources.append((".task-meta.json plan_file", str(plan_vault)))
    if review_meta is not None:
        prior = str(review_meta.get("vault_root") or "").strip()
        if prior:
            sources.append((".review-meta.json vault_root", prior))
    sources.append(("script-location fallback", str(DEFAULT_VAULT)))

    for source, raw in sources:
        candidate = Path(raw).expanduser().resolve()
        if valid_vault_root(candidate):
            return candidate
        if source == "--vault-root":
            die(f"{source} is not an llm-obsidian vault: {candidate}")
    die("cannot resolve the coordinator llm-obsidian vault")


def emit_review_event(
    worktree: Path,
    review_meta: dict[str, Any],
    op: str,
    counts: dict[str, int | float],
    *,
    status: str = "ok",
) -> None:
    try:
        emit_lifecycle_event(
            worktree,
            op,
            actor=review_actor(review_meta),
            counts=counts,
            status=status,
            vault_root=review_vault(review_meta),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"{path} not found; run from a dispatch task worktree")
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc}")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_text_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return default


def review_request_description(worktree: Path, limit: int = 6_000) -> str:
    """Extract the task request without retaining the orchestration prompt."""
    text = read_text_file(worktree / ".task-prompt.md").replace("\x00", "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    description_heading = next(
        (index for index, line in enumerate(lines) if line.strip() == "## Task description"),
        None,
    )
    if description_heading is not None:
        start = description_heading + 1
        end = next(
            (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
            len(lines),
        )
        description = "\n".join(lines[start:end]).strip()
    else:
        start = 1 if lines and lines[0].startswith("# Task:") else 0
        # Legacy/custom task prompts predate the standard Task description
        # heading; their remaining sections are still human-authored scope.
        end = len(lines)
        description = "\n".join(lines[start:end]).strip()
    if len(description) > limit:
        description = description[: limit - 1].rstrip() + "…"
    return description


def initialize_review_history(
    worktree: Path,
    review_id: str,
    task_name: str,
    meta: dict[str, Any],
    review_mode: str,
) -> None:
    (worktree / ".review-archive.json").unlink(missing_ok=True)
    (worktree / ".review-archive-request.json").unlink(missing_ok=True)
    write_json(
        worktree / ".review-history.json",
        {
            "schema_version": 1,
            "review_id": review_id,
            "task_name": task_name,
            "request": {
                "description": review_request_description(worktree),
                "base_branch": str(meta.get("base_branch") or ""),
                "branch": str(meta.get("branch") or ""),
                "review_mode": review_mode,
            },
            "rounds": [],
        },
    )


def reset_review_round_artifacts(worktree: Path) -> None:
    """Prevent an archived cycle from leaking into a newly started review."""
    for name in REVIEW_ROUND_ARTIFACTS:
        (worktree / name).unlink(missing_ok=True)


def ensure_review_cycle_can_start(worktree: Path) -> None:
    history_path = worktree / ".review-history.json"
    if not history_path.is_file():
        if (worktree / ".task-review.json").is_file() and not (worktree / ".review-archive.json").is_file():
            die("previous legacy review is not archived; run the archive/reap step before starting another cycle")
        return
    history = read_json(history_path)
    if not isinstance(history, dict):
        die(".review-history.json must contain an object")
    rounds = history.get("rounds")
    if not isinstance(rounds, list):
        die(".review-history.json rounds must be a list")
    if not rounds:
        return
    marker_path = worktree / ".review-archive.json"
    if not marker_path.is_file():
        die("previous review history is not archived; run the archive/reap step before starting another cycle")
    marker = read_json(marker_path)
    archive_path = str(marker.get("path") or "")
    archive_title = str(marker.get("title") or "")
    if (
        marker.get("review_id") != history.get("review_id")
        or marker.get("status") not in {"archived", "already-current"}
        or not archive_path.startswith("wiki/meta/reviews/")
        or not archive_path.endswith(".md")
        or Path(archive_path).stem != archive_title
        or marker.get("wikilink") != f"[[{archive_title}]]"
    ):
        die("previous review archive marker does not match the completed review cycle")


def record_review_round(
    worktree: Path,
    review_meta: dict[str, Any],
    review: dict[str, Any],
) -> None:
    path = worktree / ".review-history.json"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        history = {
            "schema_version": 1,
            "review_id": str(review_meta.get("review_id") or review["run_id"]),
            "task_name": str(review_meta.get("task_name") or "review"),
            "request": {
                "description": review_request_description(worktree),
                "base_branch": str(review_meta.get("base_branch") or ""),
                "branch": str(review_meta.get("branch") or ""),
                "review_mode": str(review_meta.get("review_mode") or review["mode"]),
            },
            "rounds": [],
        }
    except json.JSONDecodeError as exc:
        die(f".review-history.json is not valid JSON: {exc}")
    if not isinstance(history, dict):
        die(".review-history.json must contain an object")
    rounds = history.get("rounds")
    if not isinstance(rounds, list):
        die(".review-history.json rounds must be a list")
    entry = {
        "iteration": max(1, int(review_meta.get("iteration") or 1)),
        "phase": str(review_meta.get("phase") or "initial-review"),
        "received_at": utc_now(),
        "review": review,
        "resolution": None,
    }
    for index, existing in enumerate(rounds):
        existing_review = existing.get("review") if isinstance(existing, dict) else None
        if isinstance(existing_review, dict) and existing_review.get("run_id") == review["run_id"]:
            entry["resolution"] = existing.get("resolution")
            rounds[index] = entry
            break
    else:
        rounds.append(entry)
    if len(rounds) > 10:
        die("review history exceeds 10 rounds")
    history["schema_version"] = 1
    history["review_id"] = str(history.get("review_id") or review_meta.get("review_id") or review["run_id"])
    history["task_name"] = str(history.get("task_name") or review_meta.get("task_name") or "review")
    if not isinstance(history.get("request"), dict):
        history["request"] = {
            "description": review_request_description(worktree),
            "base_branch": str(review_meta.get("base_branch") or ""),
            "branch": str(review_meta.get("branch") or ""),
            "review_mode": str(review_meta.get("review_mode") or review["mode"]),
        }
    history["rounds"] = rounds
    write_json(path, history)


def snapshot_latest_resolution(worktree: Path) -> None:
    history_path = worktree / ".review-history.json"
    if not history_path.is_file():
        die(".review-history.json is missing; cannot bind an executor resolution")
    history = read_json(history_path)
    rounds = history.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        die(".review-history.json has no received round to verify")
    latest = rounds[-1]
    if not isinstance(latest, dict):
        die(".review-history.json latest round must be an object")
    review = latest.get("review")
    if not isinstance(review, dict):
        die(".review-history.json latest round has no review object")
    findings = review.get("findings")
    if not isinstance(findings, list):
        die(".review-history.json latest review has invalid findings")
    resolution = read_text_file(worktree / ".task-review-resolution.md")
    if findings and not resolution:
        die("latest review has findings; write .task-review-resolution.md before verify")
    if not resolution:
        return
    if len(resolution) > 20_000:
        die(".task-review-resolution.md exceeds 20000 characters")
    latest["resolution"] = resolution
    write_json(history_path, history)


def coordinator_repo_root(cwd: Path | None = None) -> Path | None:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or Path.cwd())
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).expanduser().resolve()


def is_primary_coordinator_review(
    worktree: Path,
    vault: Path,
    review_meta: dict[str, Any],
) -> bool:
    resolved_worktree = worktree.resolve()
    resolved_vault = vault.resolve()
    return (
        review_meta.get("archive_mode") == "coordinator"
        and resolved_worktree == resolved_vault
        and (resolved_vault / ".git").is_dir()
        and coordinator_repo_root(resolved_vault) == resolved_vault
    )


def run_review_archive(
    worktree: Path,
    vault: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(vault / "skills" / "review-dispatch" / "scripts" / "archive_review.py"),
        "--worktree",
        str(worktree),
        "--vault-root",
        str(vault),
        "--json",
    ]
    if dry_run:
        command.append("--dry-run")
    result = run(command, cwd=vault)
    if result.returncode != 0:
        die((result.stderr or result.stdout).strip() or "review archive failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        die(f"review archive returned invalid JSON: {exc}")
    if not isinstance(value, dict):
        die("review archive result must be an object")
    return value


def archive_or_defer(
    worktree: Path,
    review_meta: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    vault = review_vault(review_meta) or DEFAULT_VAULT
    review_id = str(review_meta.get("review_id") or "").strip()
    if is_primary_coordinator_review(worktree, vault, review_meta):
        return run_review_archive(worktree, vault, dry_run=dry_run)
    # A task worktree must never become its own coordinator merely because a
    # script copy was executed there.  Only the distinct canonical vault may
    # perform the contentful archive transaction.
    if worktree.resolve() == vault.resolve() or coordinator_repo_root() != vault.resolve():
        result = {
            "schema_version": 1,
            "status": "deferred",
            "review_id": review_id,
            "reason": "coordinator-reap-required",
        }
        if not dry_run:
            write_json(worktree / ".review-archive-request.json", result)
        return result
    return run_review_archive(worktree, vault, dry_run=dry_run)


def read_task_name(worktree: Path, meta: dict[str, Any]) -> str:
    name = str(meta.get("task_name") or "").strip()
    if name:
        return name
    prompt = worktree / ".task-prompt.md"
    if prompt.exists():
        first = prompt.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        if first:
            match = re.match(r"^# Task:\s*(.+?)\s*$", first[0])
            if match:
                return match.group(1)
    die("cannot determine task name from .task-meta.json or .task-prompt.md")


def parse_dispatch_env(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
            section = data.get("codex_dispatch", {})
            return section if isinstance(section, dict) else {}
        except Exception as exc:
            print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
            return {}

    current = ""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current != "codex_dispatch" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().split("#", 1)[0].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def expand_user(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("~", str(Path.home()), 1) if text.startswith("~") else text


def plugin_name(vault: Path) -> str:
    for rel in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        path = vault / rel
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        name = str(data.get("name") or "").strip()
        if name:
            return name
    return "llm-obsidian"


def default_review_skill(runtime: str, plugin: str) -> str:
    return f"${plugin}:review-dispatch" if runtime == "codex" else f"/{plugin}:review-dispatch"


def default_review_send_skill(runtime: str, plugin: str) -> str:
    return f"${plugin}:review-send" if runtime == "codex" else f"/{plugin}:review-send"


def normalize_skill_command(command: str, runtime: str, skill_name: str, plugin: str) -> str:
    """Keep handoff commands compatible with the receiving agent runtime."""
    command = command.strip()
    if not command:
        return command
    if runtime == "claude" and command.startswith("$"):
        return f"/{plugin}:{skill_name}"
    if runtime == "claude" and command == f"/{skill_name}":
        return f"/{plugin}:{skill_name}"
    if runtime == "codex" and command.startswith("/"):
        return f"${plugin}:{skill_name}"
    return command


def opposite_runtime(runtime: str) -> str:
    if runtime == "codex":
        return "claude"
    if runtime == "claude":
        return "codex"
    die(f"cannot choose opposite reviewer runtime from executor_runtime={runtime!r}")


def normalize_review_mode(mode: str) -> str:
    normalized = (mode or "full").strip().lower()
    if normalized not in REVIEW_MODES:
        die(f"review mode must be full or light, got {mode!r}")
    return normalized


def resolve_review_env(worktree: Path, vault: Path, meta: dict[str, Any], reviewer_runtime: str, *, same_model: bool = False) -> dict[str, str]:
    plugin = plugin_name(vault)
    repo_env = parse_dispatch_env(worktree / ".codex" / "dispatch-env.toml")
    vault_env = parse_dispatch_env(vault / ".codex" / "dispatch-env.toml")
    merged: dict[str, Any] = {}
    merged.update(vault_env)
    merged.update(repo_env)

    codex_home = expand_user(merged.get("codex_home") or meta.get("codex_home") or os.environ.get("CODEX_HOME"))
    # The dispatch `profile` is the executor's full-MCP profile. A read-only
    # reviewer must not inherit it: doing so can exceed tool-schema limits and
    # prevent startup. Select only an explicit reviewer profile or the generated
    # dedicated readonly profile; no profile is safer than an executor fallback.
    profile = str(merged.get("reviewer_profile") or meta.get("reviewer_profile") or "").strip()
    executor_runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "codex")
    raw_review_skill = str(
        meta.get("review_skill")
        or read_text_file(worktree / ".task-review-skill")
        or (merged.get("review_skill") if executor_runtime == "codex" else "")
        or default_review_skill(executor_runtime, plugin)
    )
    raw_review_send_skill = str(
        meta.get("review_send_skill")
        or read_text_file(worktree / ".task-review-send-skill")
        or (merged.get("review_send_skill") if reviewer_runtime == "codex" else "")
        or default_review_send_skill(reviewer_runtime, plugin)
    )
    review_skill = normalize_skill_command(raw_review_skill, executor_runtime, "review-dispatch", plugin)
    review_send_skill = normalize_skill_command(raw_review_send_skill, reviewer_runtime, "review-send", plugin)

    config_root = worktree if (worktree / "config/model-routing.toml").is_file() else vault
    if not (config_root / "config/model-routing.toml").is_file():
        config_root = DEFAULT_VAULT
    try:
        config = load_routing_config(config_root)
        session = session_from_meta(meta)
        if session is None:
            executor_default = config.runtime_default(executor_runtime)
            session = {
                "runtime": executor_runtime,
                "model": str(meta.get("model") or executor_default["model"]),
                "effort": str(meta.get("effort") or executor_default["effort"]),
            }
        legacy_model_key = f"{reviewer_runtime}_review_model"
        legacy_effort_key = f"{reviewer_runtime}_review_effort"
        route = resolve_model_route(
            config,
            "review",
            session=session,
            explicit_runtime=reviewer_runtime,
            explicit_model="" if same_model else str(meta.get(legacy_model_key) or merged.get(legacy_model_key) or "").strip(),
            explicit_effort="" if same_model else str(meta.get(legacy_effort_key) or merged.get(legacy_effort_key) or "").strip(),
            same_model=same_model,
        )
    except RoutingError as exc:
        message = str(exc)
        if " effort must be one of " in message:
            family = "Claude" if reviewer_runtime == "claude" else "Codex"
            die(f"{family} reviewer effort must be one of {sorted(CLAUDE_EFFORTS if reviewer_runtime == 'claude' else CODEX_EFFORTS)}")
        die(message)
    reviewer_model = str(route["model"])

    if reviewer_runtime == "codex" and codex_home and not Path(codex_home).exists():
        die(f"CODEX_HOME for reviewer does not exist: {codex_home}")
    if reviewer_runtime == "codex" and not profile and codex_home:
        candidate = f"{plugin}-reviewer-readonly"
        if (Path(codex_home) / f"{candidate}.config.toml").is_file():
            profile = candidate

    return {
        "codex_home": codex_home,
        "profile": profile,
        "review_skill": review_skill,
        "review_send_skill": review_send_skill,
        "reviewer_model": reviewer_model,
        "reviewer_effort": str(route["effort"]),
        "routing_config_sha256": str(route["config_sha256"]),
        "routing_source": json.dumps(route["source"], separators=(",", ":")),
    }


def ensure_excludes(worktree: Path) -> None:
    result = run(["git", "rev-parse", "--git-common-dir"], cwd=worktree)
    if result.returncode != 0 or not result.stdout.strip():
        die(result.stderr.strip() or "cannot resolve git common directory")
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (worktree / common).resolve()
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = set()
    if exclude.exists():
        existing = {line.strip() for line in exclude.read_text(encoding="utf-8").splitlines()}
    with exclude.open("a", encoding="utf-8") as fh:
        for item in HANDOFF_EXCLUDES:
            if item not in existing:
                fh.write(item + "\n")


def is_handoff(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in HANDOFF_EXCLUDES)


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def git_paths(worktree: Path, *args: str) -> list[str]:
    result = run(["git", *args, "-z"], cwd=worktree)
    if result.returncode != 0:
        die((result.stdout + "\n" + result.stderr).strip() or f"git {' '.join(args)} failed")
    return [path for path in result.stdout.split("\0") if path]


def file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_baseline(worktree: Path) -> None:
    tracked = git_paths(worktree, "ls-files")
    untracked = git_paths(worktree, "ls-files", "--others", "--exclude-standard")
    files: dict[str, str | None] = {}
    for rel in sorted(set(tracked + untracked)):
        if is_handoff(rel):
            continue
        files[rel] = file_hash(worktree / rel)

    state = {"version": 1, "captured_at": utc_now(), "files": files}
    write_json(worktree / ".review-baseline-state.json", state)
    status = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree)
    (worktree / ".review-baseline-status.txt").write_text(status.stdout, encoding="utf-8")


def render_template(template: str, values: dict[str, str]) -> str:
    required = {field for _, field, _, _ in Formatter().parse(template) if field}
    missing = sorted(required - values.keys())
    if missing:
        die(f"template is missing values for: {', '.join(missing)}")
    return template.format(**values)


def parse_surface_uuid(output: str) -> tuple[str, str]:
    match_uuid = re.search(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
        output,
    )
    match_ref = re.search(r"\bsurface:\d+\b", output)
    if not match_uuid:
        die(f"could not parse cmux surface UUID from: {output.strip()}")
    return match_uuid.group(0), match_ref.group(0) if match_ref else ""


def spawn_cmux_split(no_spawn: bool) -> tuple[str, str, str]:
    if no_spawn:
        return "00000000-0000-0000-0000-000000000000", "surface:dry-run", "dry-run"
    cmd = ["cmux", "--id-format", "both", "new-split", "right", "--focus", "false"]
    result = run(cmd)
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        die(f"cmux new-split failed: {output}")
    surface, ref = parse_surface_uuid(output)
    return surface, ref, output


def prepare_review_runtime(
    worktree: Path,
    vault: Path,
    task_name: str,
    run_id: str,
    no_spawn: bool,
) -> Path:
    """Create the reviewer's sole writable root outside the product worktree."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", task_name).strip("-._") or "task"
    if no_spawn:
        runtime = worktree.parent / f".review-runtime-{run_id}"
        runtime.mkdir(mode=0o700)
    else:
        root = vault / ".vault-meta" / "review-runtimes"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        runtime = root / f"llm-review-{safe_name}-{run_id}"
        runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    return runtime.resolve()


def callback_command(vault: Path, worktree: Path) -> str:
    """Build a runtime-neutral callback prompt instead of a fragile slash command."""
    script = vault / "skills" / "review-dispatch" / "scripts" / "spawn_review.py"
    receive = shlex.join(
        ["python3", str(script), "receive", "--worktree", str(worktree)]
    )
    return (
        "Cross-model review callback for the active dispatched task. Process it now without waiting for user input. "
        "After the command succeeds, continue the Review Gate in .task-prompt.md. "
        f"Run this exact command: {receive}"
    )


def launch_command(
    worktree: Path,
    vault: Path,
    reviewer_runtime: str,
    reviewer_model: str,
    codex_home: str,
    profile: str,
    prompt_file: str,
    review_surface: str,
    reviewer_effort: str,
    review_runtime_dir: Path | None,
) -> str:
    env: dict[str, str] = {}
    if reviewer_runtime == "claude":
        # Claude Code documents Edit(...) as the canonical scoped permission
        # for every built-in file editor, including the Write tool.  Anchor the
        # sole writable handoff at the reviewer's original cwd; scoped Write
        # rules are not matched consistently by current Claude Code releases.
        argv = [
            "claude", "--permission-mode", "dontAsk",
            "--tools", CLAUDE_REVIEW_TOOL_SURFACE,
            "--allowedTools", *CLAUDE_REVIEW_ALLOWED_TOOLS,
            "--model", reviewer_model,
        ]
        if reviewer_effort:
            argv.extend(["--effort", reviewer_effort])
    else:
        if review_runtime_dir is None:
            die("Codex reviewer runtime directory is missing")
        argv = [
            "codex",
            "--cd",
            str(review_runtime_dir),
            "-s",
            "workspace-write",
            "-a",
            "never",
            "--disable",
            "hooks",
        ]
        for value in reviewer_codex_config_values():
            argv.extend(["-c", value])
        if profile:
            argv.extend(["--profile", profile])
        argv.extend(["--model", reviewer_model])
        if reviewer_effort:
            argv.extend(["-c", f'model_reasoning_effort="{reviewer_effort}"'])
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
        env["TMPDIR"] = str(review_runtime_dir)

    write_agent_spec(worktree, "reviewer", reviewer_runtime, argv, prompt_file, env)
    supervisor = vault / "scripts" / "cmux_agent_supervisor.py"
    return shlex.join(
        [
            "python3", str(supervisor), "run", "--worktree", str(worktree),
            "--kind", "reviewer", "--surface", review_surface,
        ]
    )


def send_to_surface(surface: str, text: str) -> None:
    send = run(["cmux", "send", "--surface", surface, text])
    if send.returncode != 0:
        die((send.stdout + "\n" + send.stderr).strip() or "cmux send failed")
    time.sleep(CMUX_PASTE_SETTLE_SECONDS)
    enter = run(["cmux", "send-key", "--surface", surface, "Enter"])
    if enter.returncode != 0:
        die((enter.stdout + "\n" + enter.stderr).strip() or "cmux send-key failed")


def verify_handoff_message(
    worktree: Path,
    prompt_file: str,
) -> str:
    prompt_path = worktree / prompt_file
    return (
        "# Cross-model review follow-up\n\n"
        f"Read `{prompt_path}` and follow it exactly. "
        "Do not review this short handoff message; the full instructions are in that file.\n\n"
        "Submit the typed JSON review using the transport in that prompt.\n"
        "Stay open after sending; the executor may send another round."
    )


def submit_command(
    vault: Path,
    worktree: Path,
    reviewer_runtime: str,
    review_runtime_dir: Path | None,
) -> str:
    script = vault / "skills" / "review-send" / "scripts" / "send_review.py"
    if reviewer_runtime == "codex":
        if review_runtime_dir is None:
            die("Codex reviewer runtime directory is missing")
        return f"supervisor relay watches {review_runtime_dir / '.review-outbox.json'}"
    command = f"python3 {shlex.quote(str(script))} submit --worktree {shlex.quote(str(worktree))}"
    if reviewer_runtime == "claude":
        command += f" --input-file {shlex.quote(str(worktree / '.review-outbox.json'))}"
    return command


def submission_instructions(
    reviewer_runtime: str,
    command: str,
    worktree: Path,
    review_runtime_dir: Path | None,
) -> str:
    if reviewer_runtime == "claude":
        return (
            "Write the JSON object only to `.review-outbox.json` with the Write tool, "
            f"then run `{command}` with no pipe or heredoc. This isolated outbox is the only file you may write; "
            "the callback validates and removes it."
        )
    if review_runtime_dir is None:
        die("Codex reviewer runtime directory is missing")
    outbox = review_runtime_dir / ".review-outbox.json"
    staging = review_runtime_dir / ".review-outbox.json.tmp"
    return (
        f"Write exactly the JSON object to `{staging}`, then atomically rename it to `{outbox}` only after "
        "the JSON is complete. This isolated outbox is inside your only writable "
        "scratch directory. Do not run `review-send`, do not call `cmux`, and do not try to access its socket. "
        "The supervisor watches this exact file, validates and forwards the payload, then removes it. "
        "After the outbox disappears, stay open for a possible verification round."
    )


def repository_diagnostics(reviewer_runtime: str, worktree: Path) -> str:
    if reviewer_runtime == "claude":
        return (
            "any existing cwd-relative `python3 tests/test_<name>.py` or "
            "`bash tests/test_<name>.sh` entrypoint, plus "
            "`python3 scripts/lint-instructions.py` and the exact DCG policy "
            "smoke command `bash scripts/dcg-test-suite.sh`"
        )
    commands = (
        ["python3", str(worktree / "tests" / "test_task_lifecycle.py")],
        ["bash", str(worktree / "tests" / "test_review_dispatch.sh")],
        ["python3", str(worktree / "tests" / "test_contract_schemas.py")],
        ["python3", str(worktree / "scripts" / "lint-instructions.py")],
    )
    return ", ".join(f"`{shlex.join(command)}`" for command in commands)


def repository_inspection_instructions(reviewer_runtime: str, worktree: Path) -> str:
    if reviewer_runtime == "claude":
        return (
            "Your process starts in the product worktree. Resolve repository-relative paths from the current "
            "directory and use cwd-relative `git status ...`, `git diff ...`, `git log ...`, or `git show ...` "
            "commands. Do not prefix them with `git -C`: the locked-down Claude allowlist intentionally "
            "recognizes only these cwd-relative, read-only forms."
        )
    return (
        f"Your process starts in an isolated scratch directory. Resolve every repository-relative path against "
        f"`{worktree}` and use `git -C {worktree} ...` for git inspection."
    )


def base_context(worktree: Path, vault: Path, meta: dict[str, Any], task_name: str) -> dict[str, str]:
    return {
        "task_name": task_name,
        "worktree": str(worktree),
        "base_branch": str(meta.get("base_branch") or ""),
        "branch": str(meta.get("branch") or "(unknown)"),
        "executor_runtime": str(meta.get("executor_runtime") or meta.get("runtime") or "unknown"),
        "model": str(meta.get("model") or "default"),
        "plan_file": str(meta.get("plan_file") or "none"),
        "vault": str(vault),
    }


def review_mode_instructions(review_mode: str) -> str:
    if review_mode == "light":
        return (
            "- Mode: `light`.\n"
            "- Spend the pass on correctness, regressions, missing tests, security-sensitive mistakes, and broken contracts.\n"
            "- Return at most the top 5 actionable findings. Skip broad style, naming, and preference-only comments.\n"
            "- Do not run an exhaustive discipline checklist unless the changed files are clearly high-risk.\n"
            "- If nothing material is wrong, approve with `Findings: none` and mention only real verification gaps."
        )
    return (
        "- Mode: `full`.\n"
        "- Run the normal review gate: inspect intent, diff, tests, operational constraints, and relevant discipline rules.\n"
        "- Prioritize correctness, regressions, security, missing tests, and contract mismatches before nits.\n"
        "- Include every material finding needed before `reap-send`; keep preference-only comments out."
    )


def render_review_prompt(
    worktree: Path,
    vault: Path,
    meta: dict[str, Any],
    task_name: str,
    phase: str,
    output_file: str,
    run_id: str,
    submission_command: str,
    review_send_command: str,
    executor_callback_command: str,
    review_mode: str,
    reviewer_runtime: str,
    review_runtime_dir: Path | None,
) -> str:
    template = (
        SKILL_ROOT
        / "references"
        / "review-prompt-template.md"
    ).read_text(encoding="utf-8")
    previous_review = read_text_file(worktree / ".task-review.md", "none")
    if phase == "verify-fixes":
        try:
            prior_meta = json.loads((worktree / ".review-meta.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            prior_meta = {}
        prior_output = str(prior_meta.get("sent_output_file") or prior_meta.get("output_file") or "").strip()
        if prior_output:
            previous_review = read_text_file(worktree / prior_output, previous_review)
    resolution = read_text_file(worktree / ".task-review-resolution.md", "none")
    values = base_context(worktree, vault, meta, task_name)
    values.update(
        {
            "phase": phase,
            "output_file": output_file,
            "run_id": run_id,
            "submission_command": submission_command,
            "review_send_command": review_send_command,
            "executor_callback_command": executor_callback_command,
            "review_mode": review_mode,
            "review_mode_instructions": review_mode_instructions(review_mode),
            "submission_instructions": submission_instructions(
                reviewer_runtime, submission_command, worktree, review_runtime_dir
            ),
            "repository_inspection_instructions": repository_inspection_instructions(
                reviewer_runtime, worktree
            ),
            "repository_diagnostics": repository_diagnostics(reviewer_runtime, worktree),
            "previous_review": previous_review,
            "resolution": resolution,
        }
    )
    return render_template(template, values)


def cmd_start(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    ensure_review_cycle_can_start(worktree)
    meta = read_json(worktree / ".task-meta.json")
    vault = resolve_vault_root(worktree, explicit=ns.vault_root, task_meta=meta)
    if ns.coordinator_review:
        if not ns.vault_root.strip():
            die("--coordinator-review requires explicit --vault-root")
        if worktree != vault:
            die("--coordinator-review requires worktree and vault root to be identical")
        if not (vault / ".git").is_dir() or coordinator_repo_root(vault) != vault:
            die("--coordinator-review requires the primary coordinator checkout")
    try:
        task_policy = normalize_task_contract(meta)
    except TaskContractError as exc:
        die(str(exc))
    task_name = ns.task_name or read_task_name(worktree, meta)
    base_branch = ns.base_branch or str(meta.get("base_branch") or "").strip()
    if not base_branch:
        die("base branch missing in .task-meta.json; pass --base-branch")
    meta["base_branch"] = base_branch

    executor_runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "").strip()
    if ns.same_model and (ns.reviewer_runtime or ns.model):
        die("--same-model cannot be combined with --reviewer-runtime or --model")
    reviewer_runtime = executor_runtime if ns.same_model else (ns.reviewer_runtime or opposite_runtime(executor_runtime))
    if reviewer_runtime not in {"claude", "codex"}:
        die("--reviewer-runtime must be claude or codex")
    if reviewer_runtime == "claude" and not ns.no_spawn:
        subscription = run([sys.executable, str(vault / "scripts" / "claude-subscription-check.py")])
        if subscription.returncode != 0:
            die(subscription.stderr.strip() or "Claude subscription preflight failed")

    executor_surface = read_text_file(worktree / ".task-cmux-surface") or str(meta.get("task_surface") or "")
    if not executor_surface:
        die(".task-cmux-surface missing; cannot callback executor")

    env = resolve_review_env(worktree, vault, meta, reviewer_runtime, same_model=ns.same_model)
    configured_mode = str(task_policy["review_policy"].get("mode") or "")
    selected_mode = "light" if ns.light else (ns.mode or configured_mode or "full")
    if selected_mode == "skip":
        die("review_policy.mode=skip; do not start a reviewer")
    review_mode = normalize_review_mode(selected_mode)
    reviewer_model = ns.model or env["reviewer_model"]
    reviewer_effort = ns.effort or env["reviewer_effort"]
    routing_source = json.loads(env.get("routing_source") or "[]")
    if ns.model:
        routing_source.append("cli-model")
    if ns.effort:
        routing_source.append("cli-effort")
    if reviewer_runtime == "claude" and reviewer_effort not in CLAUDE_EFFORTS | {""}:
        die(f"Claude reviewer effort must be one of {sorted(CLAUDE_EFFORTS)}")
    if reviewer_runtime != "claude" and reviewer_effort not in CODEX_EFFORTS | {""}:
        die(f"Codex reviewer effort must be one of {sorted(CODEX_EFFORTS)}")
    review_skill = ns.review_skill or env["review_skill"]
    review_send_skill = ns.review_send_skill or env["review_send_skill"]
    output_file = ".task-review.md"
    output_json_file = ".task-review.json"
    prompt_file = ".review-prompt.md"
    run_id = str(uuid.uuid4())
    review_runtime_dir = (
        prepare_review_runtime(worktree, vault, task_name, run_id, ns.no_spawn)
        if reviewer_runtime == "codex"
        else None
    )
    executor_callback = callback_command(vault, worktree)
    submission_command = submit_command(vault, worktree, reviewer_runtime, review_runtime_dir)

    reset_review_round_artifacts(worktree)
    ensure_excludes(worktree)
    (worktree / ".review-relay.json").unlink(missing_ok=True)
    (worktree / ".task-review-skill").write_text(review_skill + "\n", encoding="utf-8")
    (worktree / ".task-review-send-skill").write_text(review_send_skill + "\n", encoding="utf-8")
    write_baseline(worktree)
    prompt = render_review_prompt(
        worktree,
        vault,
        meta,
        task_name,
        "initial-review",
        output_json_file,
        run_id,
        submission_command,
        review_send_skill,
        executor_callback,
        review_mode,
        reviewer_runtime,
        review_runtime_dir,
    )
    (worktree / prompt_file).write_text(prompt, encoding="utf-8")

    review_surface, review_ref, cmux_output = spawn_cmux_split(ns.no_spawn)
    (worktree / ".review-cmux-surface").write_text(review_surface + "\n", encoding="utf-8")
    command = launch_command(
        worktree,
        vault,
        reviewer_runtime,
        reviewer_model,
        env["codex_home"],
        env["profile"],
        prompt_file,
        review_surface,
        reviewer_effort,
        review_runtime_dir,
    )

    started_at = utc_now()
    review_meta = {
        "version": 5,
        "review_id": run_id,
        "run_id": run_id,
        "task_name": task_name,
        "started_at": started_at,
        "phase_started_at": started_at,
        "updated_at": started_at,
        "vault_root": str(vault),
        "worktree": str(worktree),
        "base_branch": base_branch,
        "branch": str(meta.get("branch") or "(unknown)"),
        "executor_runtime": executor_runtime,
        "model": str(meta.get("model") or "default"),
        "plan_file": str(meta.get("plan_file") or "none"),
        "executor_surface": executor_surface,
        "review_surface": review_surface,
        "review_surface_ref": review_ref,
        "reviewer_runtime": reviewer_runtime,
        "reviewer_model": reviewer_model,
        "reviewer_effort": reviewer_effort or None,
        "routing": {
            "schema_version": 1,
            "same_model": bool(ns.same_model),
            "config_sha256": env.get("routing_config_sha256"),
            "source": routing_source,
        },
        "codex_home": env["codex_home"] or None,
        "codex_profile": env["profile"] or None,
        "review_skill": review_skill,
        "review_send_command": review_send_skill,
        "review_mode": review_mode,
        "archive_mode": "coordinator" if ns.coordinator_review else "reap",
        "executor_callback_command": executor_callback,
        "phase": "initial-review",
        "iteration": 1,
        "prompt_file": prompt_file,
        "output_file": output_file,
        "output_json_file": output_json_file,
        "submission_command": submission_command,
        "review_runtime_dir": str(review_runtime_dir) if review_runtime_dir else None,
        "cmux_output": cmux_output,
        "command": command,
        "status": "prepared",
    }
    write_json(worktree / ".review-meta.json", review_meta)
    initialize_review_history(worktree, run_id, task_name, meta, review_mode)

    if ns.no_spawn:
        print(command)
        return 0

    send_to_surface(review_surface, command)
    review_meta["status"] = "spawned"
    review_meta["updated_at"] = utc_now()
    write_json(worktree / ".review-meta.json", review_meta)
    emit_review_event(
        worktree,
        review_meta,
        "review-round-start",
        {"rounds_started": 1, "iteration": 1},
    )

    print(f"review surface: {review_ref or review_surface}")
    print(f"review output: {worktree / output_file}")
    print("reviewer stays open; close it later with review-dispatch finish")
    return 0


def cmd_verify(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    meta = read_json(worktree / ".task-meta.json")
    review_meta = read_json(worktree / ".review-meta.json")
    vault = resolve_vault_root(
        worktree,
        explicit=ns.vault_root,
        task_meta=meta,
        review_meta=review_meta,
    )
    review_meta["vault_root"] = str(vault)
    try:
        policy = normalize_task_contract(meta)
    except TaskContractError as exc:
        die(str(exc))
    if policy["interaction_policy"] == "unattended":
        completed_verifies = max(0, int(review_meta.get("iteration") or 1) - 1)
        if completed_verifies >= int(policy["review_policy"]["max_verify_iterations"]):
            die("unattended verify iteration limit reached; escalate to the coordinator")
    task_name = ns.task_name or str(review_meta.get("task_name") or read_task_name(worktree, meta))
    review_surface = str(review_meta.get("review_surface") or read_text_file(worktree / ".review-cmux-surface"))
    if not review_surface:
        die("review surface missing; run review-dispatch start first")

    reviewer_runtime = str(review_meta.get("reviewer_runtime") or "")
    review_mode = normalize_review_mode(str(review_meta.get("review_mode") or "full"))
    raw_review_send_skill = ns.review_send_skill or str(
        review_meta.get("review_send_command") or default_review_send_skill(reviewer_runtime, plugin_name(vault))
    )
    review_send_skill = normalize_skill_command(raw_review_send_skill, reviewer_runtime, "review-send", plugin_name(vault))
    executor_callback = callback_command(vault, worktree)
    output_file = ".task-review-verify.md"
    output_json_file = ".task-review-verify.json"
    prompt_file = ".review-prompt-verify.md"
    run_id = str(uuid.uuid4())
    raw_runtime_dir = str(review_meta.get("review_runtime_dir") or "").strip()
    review_runtime_dir = Path(raw_runtime_dir).expanduser().resolve() if raw_runtime_dir else None
    if reviewer_runtime == "codex" and (
        review_runtime_dir is None or not review_runtime_dir.is_dir()
    ):
        die("Codex reviewer runtime directory is missing; start a fresh reviewer")
    snapshot_latest_resolution(worktree)
    submission_command = submit_command(vault, worktree, reviewer_runtime, review_runtime_dir)
    (worktree / REVIEW_CALLBACK_FILE).unlink(missing_ok=True)

    prompt_meta = dict(meta)
    for key in ("base_branch", "branch", "executor_runtime", "model", "plan_file"):
        stable_value = review_meta.get(key)
        if stable_value not in (None, ""):
            prompt_meta[key] = stable_value

    write_baseline(worktree)
    prompt = render_review_prompt(
        worktree,
        vault,
        prompt_meta,
        task_name,
        "verify-fixes",
        output_json_file,
        run_id,
        submission_command,
        review_send_skill,
        executor_callback,
        review_mode,
        reviewer_runtime,
        review_runtime_dir,
    )
    (worktree / prompt_file).write_text(prompt, encoding="utf-8")

    review_meta["phase"] = "verify-fixes"
    review_meta["iteration"] = int(review_meta.get("iteration") or 1) + 1
    review_meta["phase_started_at"] = utc_now()
    review_meta["prompt_file"] = prompt_file
    review_meta["output_file"] = output_file
    review_meta["output_json_file"] = output_json_file
    review_meta["run_id"] = run_id
    review_meta["submission_command"] = submission_command
    review_meta["review_send_command"] = review_send_skill
    review_meta["review_mode"] = review_mode
    review_meta["executor_callback_command"] = executor_callback
    review_meta["send_mode"] = "file-reference"
    review_meta["status"] = "verify_sent" if not ns.no_send else "verify_prepared"
    review_meta["archive_status"] = "pending"
    review_meta["updated_at"] = utc_now()
    write_json(worktree / ".review-meta.json", review_meta)
    handoff = verify_handoff_message(worktree, prompt_file)

    if ns.no_send:
        print(handoff)
        return 0

    send_to_surface(review_surface, handoff)
    emit_review_event(
        worktree,
        review_meta,
        "review-round-start",
        {
            "rounds_started": 1,
            "iteration": nonnegative_int(review_meta.get("iteration")),
        },
    )
    print(f"sent verify prompt to reviewer: {review_meta.get('review_surface_ref') or review_surface}")
    print(f"review output: {worktree / output_file}")
    return 0


def cmd_receive(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    task_meta = read_json(worktree / ".task-meta.json")
    review_meta = read_json(worktree / ".review-meta.json")
    vault = resolve_vault_root(
        worktree,
        task_meta=task_meta,
        review_meta=review_meta,
    )
    review_meta["vault_root"] = str(vault)
    run_id = str(review_meta.get("run_id") or "").strip()
    review_mode = normalize_review_mode(str(review_meta.get("review_mode") or "full"))
    if not run_id:
        die("review metadata is missing run_id")
    relay_path: Path | None = None
    try:
        if ns.relay_file:
            expected = worktree / REVIEW_CALLBACK_FILE
            candidate = Path(ns.relay_file).expanduser()
            if not candidate.is_absolute():
                candidate = worktree / candidate
            candidate = candidate.parent.resolve() / candidate.name
            if candidate != expected or candidate.is_symlink():
                raise ReviewContractError(
                    f"relay file must be the regular file {expected}"
                )
            try:
                raw_payload = candidate.read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeError, OSError) as exc:
                raise ReviewContractError(f"cannot read relay file: {exc}") from exc
            relay_path = candidate
            review = parse_review_json(
                raw_payload, expected_run_id=run_id, expected_mode=review_mode
            )
        else:
            review = decode_review(
                ns.payload_b64, expected_run_id=run_id, expected_mode=review_mode
            )
    except ReviewContractError as exc:
        duration = elapsed_ms(review_meta.get("phase_started_at") or review_meta.get("started_at"))
        emit_review_event(
            worktree,
            review_meta,
            "review-round",
            {
                "invalid_callbacks": 1,
                "iteration": nonnegative_int(review_meta.get("iteration")),
                **({"duration_ms": duration} if duration is not None else {}),
            },
            status="error",
        )
        die(f"invalid review payload: {exc}", 3)

    output_file = str(review_meta.get("output_file") or ".task-review.md")
    output_json_file = str(review_meta.get("output_json_file") or ".task-review.json")
    task_name = str(review_meta.get("task_name") or "task")
    write_json(worktree / output_json_file, review)
    (worktree / output_file).write_text(render_markdown(review, task_name), encoding="utf-8")
    record_review_round(worktree, review_meta, review)
    review_meta["status"] = "review_received"
    review_meta["archive_status"] = "pending"
    review_meta["updated_at"] = utc_now()
    review_meta["sent_output_file"] = output_file
    review_meta["sent_output_json_file"] = output_json_file
    try:
        completed_verifies = max(0, int(review_meta.get("iteration") or 1) - 1)
        review_meta["recommended_action"] = review_action(task_meta, review, completed_verifies)
    except TaskContractError as exc:
        die(str(exc))
    write_json(worktree / ".review-meta.json", review_meta)
    if review_meta.get("recommended_action") == "escalate":
        archive_result = archive_or_defer(worktree, review_meta)
        review_meta["archive_status"] = str(archive_result.get("status") or "unknown")
        if archive_result.get("wikilink"):
            review_meta["archive_wikilink"] = archive_result["wikilink"]
        write_json(worktree / ".review-meta.json", review_meta)
    findings = review["findings"]
    severities = {
        severity: sum(1 for finding in findings if finding.get("severity") == severity)
        for severity in ("blocking", "warning", "nit")
    }
    verdict = str(review.get("verdict") or "unknown").replace("-", "_")
    action = str(review_meta.get("recommended_action") or "unknown").replace("-", "_")
    duration = elapsed_ms(review_meta.get("phase_started_at") or review_meta.get("started_at"))
    emit_review_event(
        worktree,
        review_meta,
        "review-round",
        {
            "valid_callbacks": 1,
            "iteration": nonnegative_int(review_meta.get("iteration")),
            "findings": len(findings),
            "blocking_findings": severities["blocking"],
            "warning_findings": severities["warning"],
            "nit_findings": severities["nit"],
            "verification_gaps": len(review["verification_gaps"]),
            "residual_risks": len(review["residual_risks"]),
            f"verdict_{verdict}": 1,
            f"action_{action}": 1,
            **({"duration_ms": duration} if duration is not None else {}),
        },
    )
    (worktree / ".task-review-resolution.md").unlink(missing_ok=True)
    if relay_path is not None:
        relay_path.unlink(missing_ok=True)
    print(f"received typed review: {worktree / output_json_file}")
    print(f"rendered review: {worktree / output_file}")
    print(f"recommended action: {review_meta['recommended_action']}")
    return 0


def cmd_status(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    review_meta = read_json(worktree / ".review-meta.json")
    print(json.dumps(review_meta, indent=2, ensure_ascii=False))
    return 0


def cmd_archive(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    if not ns.dry_run:
        ensure_excludes(worktree)
    task_meta = read_json(worktree / ".task-meta.json")
    review_meta = read_json(worktree / ".review-meta.json")
    vault = resolve_vault_root(
        worktree,
        explicit=ns.vault_root,
        task_meta=task_meta,
        review_meta=review_meta,
    )
    review_meta["vault_root"] = str(vault)
    result = archive_or_defer(worktree, review_meta, dry_run=ns.dry_run)
    if not ns.dry_run:
        review_meta["archive_status"] = str(result.get("status") or "unknown")
        if result.get("wikilink"):
            review_meta["archive_wikilink"] = result["wikilink"]
        review_meta["updated_at"] = utc_now()
        write_json(worktree / ".review-meta.json", review_meta)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_finish(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    task_meta = read_json(worktree / ".task-meta.json")
    try:
        task_policy = normalize_task_contract(task_meta)
    except TaskContractError as exc:
        die(str(exc))
    review_meta = read_json(worktree / ".review-meta.json")
    vault = resolve_vault_root(worktree, task_meta=task_meta, review_meta=review_meta)
    review_meta["vault_root"] = str(vault)
    surface = str(review_meta.get("review_surface") or read_text_file(worktree / ".review-cmux-surface"))
    runtime = str(review_meta.get("reviewer_runtime") or "")
    if not surface:
        die("review surface missing; cannot finish")

    if ns.no_send:
        auto_close = task_policy["interaction_policy"] == "unattended" and task_policy[
            "surface_policy"
        ].get("auto_close") is True
        archive_result = archive_or_defer(worktree, review_meta, dry_run=True)
        print(
            f"would archive={archive_result.get('status')} arm close={str(auto_close).lower()} "
            f"and send /exit to {runtime} reviewer surface {surface}"
        )
        return 0

    auto_close = task_policy["interaction_policy"] == "unattended" and task_policy[
        "surface_policy"
    ].get("auto_close") is True
    if auto_close:
        if review_meta.get("status") != "review_received" or review_meta.get("recommended_action") != "approve":
            die("unattended reviewer finish requires a received approve callback")
    archive_result = archive_or_defer(worktree, review_meta)
    review_meta["archive_status"] = str(archive_result.get("status") or "unknown")
    if archive_result.get("wikilink"):
        review_meta["archive_wikilink"] = archive_result["wikilink"]
    review_meta["updated_at"] = utc_now()
    write_json(worktree / ".review-meta.json", review_meta)
    if auto_close:
        lifecycle = vault / "scripts" / "cmux_surface_lifecycle.py"
        result = run(
            [sys.executable, str(lifecycle), "request-exit", "--worktree", str(worktree), "--kind", "reviewer"]
        )
        if result.returncode != 0:
            die((result.stdout + result.stderr).strip() or "cannot arm reviewer close")
        review_meta["status"] = "finish_sent_close_armed"
        review_meta["updated_at"] = utc_now()
        write_json(worktree / ".review-meta.json", review_meta)
        print(result.stdout.strip())
        return 0

    if runtime == "codex":
        for _ in range(40):
            run(["cmux", "send-key", "--surface", surface, "backspace"])
        sent = run(["cmux", "send", "--surface", surface, "/exit"])
        time.sleep(CMUX_PASTE_SETTLE_SECONDS)
        accepted = run(["cmux", "send-key", "--surface", surface, "tab"])
        time.sleep(0.1)
        entered = run(["cmux", "send-key", "--surface", surface, "Enter"])
        fallback = "Codex reviewer may require manual fallback: focus the reviewer split and run /exit."
    else:
        sent = run(["cmux", "send", "--surface", surface, "/exit"])
        time.sleep(CMUX_PASTE_SETTLE_SECONDS)
        accepted = subprocess.CompletedProcess([], 0, "", "")
        entered = run(["cmux", "send-key", "--surface", surface, "Enter"])
        fallback = ""
    for result in (sent, accepted, entered):
        if result.returncode != 0:
            die((result.stdout + result.stderr).strip() or "cannot submit reviewer /exit")

    review_meta["status"] = "finish_sent"
    review_meta["updated_at"] = utc_now()
    write_json(worktree / ".review-meta.json", review_meta)
    print(f"sent /exit to reviewer surface: {review_meta.get('review_surface_ref') or surface}")
    if fallback:
        print(fallback)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", aliases=["spawn"], help="open the opposite-model reviewer split")
    start.add_argument("--worktree", default=".", help="task worktree path")
    start.add_argument("--vault-root", default="", help="explicit coordinator llm-obsidian vault root")
    start.add_argument(
        "--coordinator-review",
        action="store_true",
        help="review the explicit primary vault checkout and archive directly on finish",
    )
    start.add_argument("--task-name", default="", help="override task name")
    start.add_argument("--base-branch", default="", help="override base branch")
    start.add_argument("--reviewer-runtime", choices=["claude", "codex"], default="", help="override opposite runtime")
    start.add_argument("--same-model", action="store_true", help="review with the current executor runtime and exact model; --effort may override effort")
    start.add_argument("--review-skill", default="", help="executor callback skill command")
    start.add_argument("--review-send-skill", default="", help="reviewer handoff skill command")
    start.add_argument(
        "--model",
        default="",
        help="reviewer model; defaults are resolved from config/model-routing.toml",
    )
    start.add_argument(
        "--effort",
        choices=sorted(CLAUDE_EFFORTS | CODEX_EFFORTS),
        default="",
        help="reviewer reasoning effort (defaults high; validated per runtime)",
    )
    start.add_argument("--mode", choices=sorted(REVIEW_MODES), default="", help="override review depth; otherwise use task policy or full legacy default")
    start.add_argument("--light", action="store_true", help="shortcut for --mode light")
    start.add_argument("--no-spawn", action="store_true", help="write files and print launch command without cmux")
    start.set_defaults(func=cmd_start)

    verify = sub.add_parser("verify", help="send fixes back to the same reviewer split")
    verify.add_argument("--worktree", default=".", help="task worktree path")
    verify.add_argument("--vault-root", default="", help="explicit coordinator llm-obsidian vault root")
    verify.add_argument("--task-name", default="", help="override task name")
    verify.add_argument("--review-send-skill", default="", help="reviewer handoff skill command")
    verify.add_argument("--no-send", action="store_true", help="write prompt and print it without cmux send")
    verify.set_defaults(func=cmd_verify)

    receive = sub.add_parser("receive", help="validate a typed reviewer callback and render handoff files")
    receive.add_argument("--worktree", default=".", help="task worktree path")
    receive_source = receive.add_mutually_exclusive_group(required=True)
    receive_source.add_argument("--relay-file", default="", help="validated callback file from review-send")
    receive_source.add_argument("--payload-b64", default="", help="legacy compressed review payload token")
    receive.set_defaults(func=cmd_receive)

    status = sub.add_parser("status", help="print .review-meta.json")
    status.add_argument("--worktree", default=".", help="task worktree path")
    status.set_defaults(func=cmd_status)

    archive = sub.add_parser("archive", help="archive validated review history into the coordinator wiki")
    archive.add_argument("--worktree", default=".", help="reviewed task worktree path")
    archive.add_argument("--vault-root", default="", help="override coordinator vault root")
    archive.add_argument("--dry-run", action="store_true", help="validate without writing or deferring")
    archive.set_defaults(func=cmd_archive)

    finish = sub.add_parser("finish", help="exit reviewer; unattended tasks arm exact-surface close")
    finish.add_argument("--worktree", default=".", help="task worktree path")
    finish.add_argument("--no-send", action="store_true", help="print exit action without sending it")
    finish.set_defaults(func=cmd_finish)
    return parser


def main() -> int:
    parser = build_parser()
    ns = parser.parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
