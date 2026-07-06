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
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, NoReturn

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT = Path(__file__).resolve().parents[3]
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CODEX_MODEL = "gpt-5.5"
REVIEW_MODES = {"full", "light"}
HANDOFF_EXCLUDES = [
    ".task-prompt.md",
    ".task-summary.md",
    ".task-meta.json",
    ".task-cmux-surface",
    ".task-reap-send-skill",
    ".wiki-cmux-surface",
    ".wiki-agent-runtime",
    ".wiki-reap-command",
    ".task-review.md",
    ".task-review-verify.md",
    ".task-review-resolution.md",
    ".task-review-skill",
    ".task-review-send-skill",
    ".review-prompt.md",
    ".review-prompt-verify.md",
    ".review-meta.json",
    ".review-cmux-surface",
    ".review-baseline-status.txt",
    ".review-baseline-state.json",
    ".review-send-blocked.md",
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
]


def die(message: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    return f"${plugin}:review-dispatch" if runtime == "codex" else "/review-dispatch"


def default_review_send_skill(runtime: str, plugin: str) -> str:
    return f"${plugin}:review-send" if runtime == "codex" else "/review-send"


def normalize_skill_command(command: str, runtime: str, skill_name: str, plugin: str) -> str:
    """Keep handoff commands compatible with the receiving agent runtime."""
    command = command.strip()
    if not command:
        return command
    if runtime == "claude" and command.startswith("$"):
        return f"/{skill_name}"
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


def resolve_review_env(worktree: Path, vault: Path, meta: dict[str, Any], reviewer_runtime: str) -> dict[str, str]:
    plugin = plugin_name(vault)
    repo_env = parse_dispatch_env(worktree / ".codex" / "dispatch-env.toml")
    vault_env = parse_dispatch_env(vault / ".codex" / "dispatch-env.toml")
    merged: dict[str, Any] = {}
    merged.update(vault_env)
    merged.update(repo_env)

    codex_home = expand_user(merged.get("codex_home") or meta.get("codex_home") or os.environ.get("CODEX_HOME"))
    profile = str(merged.get("profile") or meta.get("codex_profile") or "").strip()
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

    codex_model = str(merged.get("codex_review_model") or DEFAULT_CODEX_MODEL).strip()
    claude_model = str(merged.get("claude_review_model") or DEFAULT_CLAUDE_MODEL).strip()
    reviewer_model = claude_model if reviewer_runtime == "claude" else codex_model

    if reviewer_runtime == "codex" and codex_home and not Path(codex_home).exists():
        die(f"CODEX_HOME for reviewer does not exist: {codex_home}")

    return {
        "codex_home": codex_home,
        "profile": profile,
        "review_skill": review_skill,
        "review_send_skill": review_send_skill,
        "reviewer_model": reviewer_model,
    }


def ensure_excludes(worktree: Path) -> None:
    info = worktree / ".git" / "info"
    if not info.is_dir():
        git_file = worktree / ".git"
        if git_file.is_file():
            gitdir_line = git_file.read_text(encoding="utf-8", errors="replace").strip()
            if gitdir_line.startswith("gitdir:"):
                gitdir = Path(gitdir_line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (worktree / gitdir).resolve()
                info = gitdir / "info"
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


def callback_command(review_skill: str, task_name: str) -> str:
    return f"{review_skill} receive {shlex.quote(task_name)}"


def launch_command(
    worktree: Path,
    vault: Path,
    reviewer_runtime: str,
    reviewer_model: str,
    codex_home: str,
    profile: str,
    prompt_file: str,
) -> str:
    worktree_q = shlex.quote(str(worktree))
    prompt_ref = shlex.quote(prompt_file)
    if reviewer_runtime == "claude":
        return (
            f"cd {worktree_q}; clear; claude --permission-mode auto "
            f"--model {shlex.quote(reviewer_model)} \"$(cat {prompt_ref})\""
        )

    parts: list[str] = []
    if codex_home:
        parts.append(f"CODEX_HOME={shlex.quote(codex_home)}")
    parts.extend(
        [
            "codex",
            "--cd",
            shlex.quote(str(worktree)),
            "--add-dir",
            shlex.quote(str(vault)),
            "-s",
            "workspace-write",
            "-a",
            "never",
        ]
    )
    if profile:
        parts.extend(["--profile", shlex.quote(profile)])
    parts.extend(["--model", shlex.quote(reviewer_model), f"\"$(cat {prompt_ref})\""])
    return f"cd {worktree_q}; clear; " + " ".join(parts)


def send_to_surface(surface: str, text: str) -> None:
    send = run(["cmux", "send", "--surface", surface, text])
    if send.returncode != 0:
        die((send.stdout + "\n" + send.stderr).strip() or "cmux send failed")
    enter = run(["cmux", "send-key", "--surface", surface, "Enter"])
    if enter.returncode != 0:
        die((enter.stdout + "\n" + enter.stderr).strip() or "cmux send-key failed")


def verify_handoff_message(
    worktree: Path,
    prompt_file: str,
    output_file: str,
    review_send_command: str,
) -> str:
    prompt_path = worktree / prompt_file
    output_path = worktree / output_file
    return (
        "# Cross-model review follow-up\n\n"
        f"Read `{prompt_path}` and follow it exactly. "
        "Do not review this short handoff message; the full instructions are in that file.\n\n"
        f"Write the review result to `{output_path}`.\n"
        f"Then invoke `{review_send_command}` to callback the executor.\n"
        "Stay open after sending; the executor may send another round."
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
    review_send_command: str,
    executor_callback_command: str,
    review_mode: str,
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
            "review_send_command": review_send_command,
            "executor_callback_command": executor_callback_command,
            "review_mode": review_mode,
            "review_mode_instructions": review_mode_instructions(review_mode),
            "previous_review": previous_review,
            "resolution": resolution,
        }
    )
    return render_template(template, values)


def cmd_start(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    vault = Path(ns.vault_root).expanduser().resolve()
    meta = read_json(worktree / ".task-meta.json")
    task_name = ns.task_name or read_task_name(worktree, meta)
    base_branch = ns.base_branch or str(meta.get("base_branch") or "").strip()
    if not base_branch:
        die("base branch missing in .task-meta.json; pass --base-branch")
    meta["base_branch"] = base_branch

    executor_runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "").strip()
    reviewer_runtime = ns.reviewer_runtime or opposite_runtime(executor_runtime)
    if reviewer_runtime not in {"claude", "codex"}:
        die("--reviewer-runtime must be claude or codex")

    executor_surface = read_text_file(worktree / ".task-cmux-surface") or str(meta.get("task_surface") or "")
    if not executor_surface:
        die(".task-cmux-surface missing; cannot callback executor")

    env = resolve_review_env(worktree, vault, meta, reviewer_runtime)
    review_mode = normalize_review_mode("light" if ns.light else ns.mode)
    reviewer_model = ns.model or env["reviewer_model"]
    review_skill = ns.review_skill or env["review_skill"]
    review_send_skill = ns.review_send_skill or env["review_send_skill"]
    output_file = ".task-review.md"
    prompt_file = ".review-prompt.md"
    executor_callback = callback_command(review_skill, task_name)

    ensure_excludes(worktree)
    (worktree / ".task-review-skill").write_text(review_skill + "\n", encoding="utf-8")
    (worktree / ".task-review-send-skill").write_text(review_send_skill + "\n", encoding="utf-8")
    write_baseline(worktree)
    prompt = render_review_prompt(
        worktree,
        vault,
        meta,
        task_name,
        "initial-review",
        output_file,
        review_send_skill,
        executor_callback,
        review_mode,
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
    )

    review_meta = {
        "version": 2,
        "task_name": task_name,
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "worktree": str(worktree),
        "base_branch": base_branch,
        "branch": str(meta.get("branch") or "(unknown)"),
        "executor_runtime": executor_runtime,
        "executor_surface": executor_surface,
        "review_surface": review_surface,
        "review_surface_ref": review_ref,
        "reviewer_runtime": reviewer_runtime,
        "reviewer_model": reviewer_model,
        "codex_home": env["codex_home"] or None,
        "codex_profile": env["profile"] or None,
        "review_skill": review_skill,
        "review_send_command": review_send_skill,
        "review_mode": review_mode,
        "executor_callback_command": executor_callback,
        "phase": "initial-review",
        "iteration": 1,
        "prompt_file": prompt_file,
        "output_file": output_file,
        "cmux_output": cmux_output,
        "command": command,
        "status": "prepared",
    }
    write_json(worktree / ".review-meta.json", review_meta)

    if ns.no_spawn:
        print(command)
        return 0

    send_to_surface(review_surface, command)
    review_meta["status"] = "spawned"
    review_meta["updated_at"] = utc_now()
    write_json(worktree / ".review-meta.json", review_meta)

    print(f"review surface: {review_ref or review_surface}")
    print(f"review output: {worktree / output_file}")
    print("reviewer stays open; close it later with review-dispatch finish")
    return 0


def cmd_verify(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    vault = Path(ns.vault_root).expanduser().resolve()
    meta = read_json(worktree / ".task-meta.json")
    review_meta = read_json(worktree / ".review-meta.json")
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
    executor_callback = str(review_meta.get("executor_callback_command") or callback_command(str(review_meta.get("review_skill")), task_name))
    output_file = ".task-review-verify.md"
    prompt_file = ".review-prompt-verify.md"

    write_baseline(worktree)
    prompt = render_review_prompt(
        worktree,
        vault,
        meta,
        task_name,
        "verify-fixes",
        output_file,
        review_send_skill,
        executor_callback,
        review_mode,
    )
    (worktree / prompt_file).write_text(prompt, encoding="utf-8")

    review_meta["phase"] = "verify-fixes"
    review_meta["iteration"] = int(review_meta.get("iteration") or 1) + 1
    review_meta["prompt_file"] = prompt_file
    review_meta["output_file"] = output_file
    review_meta["review_send_command"] = review_send_skill
    review_meta["review_mode"] = review_mode
    review_meta["executor_callback_command"] = executor_callback
    review_meta["send_mode"] = "file-reference"
    review_meta["status"] = "verify_sent" if not ns.no_send else "verify_prepared"
    review_meta["updated_at"] = utc_now()
    write_json(worktree / ".review-meta.json", review_meta)
    handoff = verify_handoff_message(worktree, prompt_file, output_file, review_send_skill)

    if ns.no_send:
        print(handoff)
        return 0

    send_to_surface(review_surface, handoff)
    print(f"sent verify prompt to reviewer: {review_meta.get('review_surface_ref') or review_surface}")
    print(f"review output: {worktree / output_file}")
    return 0


def cmd_status(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    review_meta = read_json(worktree / ".review-meta.json")
    print(json.dumps(review_meta, indent=2, ensure_ascii=False))
    return 0


def cmd_finish(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    review_meta = read_json(worktree / ".review-meta.json")
    surface = str(review_meta.get("review_surface") or read_text_file(worktree / ".review-cmux-surface"))
    runtime = str(review_meta.get("reviewer_runtime") or "")
    if not surface:
        die("review surface missing; cannot finish")

    if ns.no_send:
        print(f"would send /exit to {runtime} reviewer surface {surface}")
        return 0

    if runtime == "codex":
        for _ in range(40):
            run(["cmux", "send-key", "--surface", surface, "backspace"])
        run(["cmux", "send", "--surface", surface, "/exit"])
        run(["cmux", "send-key", "--surface", surface, "tab"])
        fallback = "Codex reviewer may require manual fallback: focus the reviewer split and run /exit."
    else:
        run(["cmux", "send", "--surface", surface, "/exit"])
        run(["cmux", "send-key", "--surface", surface, "Enter"])
        fallback = ""

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
    start.add_argument("--vault-root", default=str(DEFAULT_VAULT), help="llm-obsidian vault root")
    start.add_argument("--task-name", default="", help="override task name")
    start.add_argument("--base-branch", default="", help="override base branch")
    start.add_argument("--reviewer-runtime", choices=["claude", "codex"], default="", help="override opposite runtime")
    start.add_argument("--review-skill", default="", help="executor callback skill command")
    start.add_argument("--review-send-skill", default="", help="reviewer handoff skill command")
    start.add_argument("--model", default="", help="reviewer model; defaults opus for Claude, gpt-5.5 for Codex")
    start.add_argument("--mode", choices=sorted(REVIEW_MODES), default="full", help="review depth: full gate or light pass")
    start.add_argument("--light", action="store_true", help="shortcut for --mode light")
    start.add_argument("--no-spawn", action="store_true", help="write files and print launch command without cmux")
    start.set_defaults(func=cmd_start)

    verify = sub.add_parser("verify", help="send fixes back to the same reviewer split")
    verify.add_argument("--worktree", default=".", help="task worktree path")
    verify.add_argument("--vault-root", default=str(DEFAULT_VAULT), help="llm-obsidian vault root")
    verify.add_argument("--task-name", default="", help="override task name")
    verify.add_argument("--review-send-skill", default="", help="reviewer handoff skill command")
    verify.add_argument("--no-send", action="store_true", help="write prompt and print it without cmux send")
    verify.set_defaults(func=cmd_verify)

    status = sub.add_parser("status", help="print .review-meta.json")
    status.add_argument("--worktree", default=".", help="task worktree path")
    status.set_defaults(func=cmd_status)

    finish = sub.add_parser("finish", help="exit the reviewer agent process after user approval")
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
