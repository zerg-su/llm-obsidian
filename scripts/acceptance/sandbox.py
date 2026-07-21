"""Disposable checkout, route override, and local cleanup primitives."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .contracts import AcceptanceRunnerError, ROOT, atomic_json, read_json

SEED_ROOT = ROOT / "evals" / "acceptance" / "seed"
SEED_COMMIT_DATE = "2000-01-01T00:00:00Z"


def acceptance_seed_sha256(seed_root: Path = SEED_ROOT) -> str:
    """Hash one canonical seed tree by relative path and file bytes."""

    digest = hashlib.sha256()
    files = sorted(path for path in seed_root.rglob("*") if path.is_file())
    if not files or any(path.is_symlink() for path in files):
        raise AcceptanceRunnerError("acceptance seed must contain regular files")
    for path in files:
        rel = path.relative_to(seed_root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def materialize_seed_commit(sandbox: Path, seed_root: Path = SEED_ROOT) -> str:
    """Replace tracked vault data with the canonical seed in one clean commit."""

    acceptance_seed_sha256(seed_root)
    for name in ("wiki", ".vault-meta"):
        target = sandbox / name
        if target.is_symlink():
            raise AcceptanceRunnerError("acceptance seed target must not be a symlink")
        if target.exists():
            shutil.rmtree(target)
        source = seed_root / name
        if not source.is_dir():
            raise AcceptanceRunnerError(f"acceptance seed is missing {name}")
        shutil.copytree(source, target)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": SEED_COMMIT_DATE,
        "GIT_COMMITTER_DATE": SEED_COMMIT_DATE,
    }
    for argv in (
        ["git", "add", "-A", "-f", "--", "wiki", ".vault-meta"],
        [
            "git", "-c", "user.name=Acceptance Seed",
            "-c", "user.email=acceptance@example.invalid",
            "commit", "-qm", "acceptance: materialize seed vault",
        ],
    ):
        result = subprocess.run(
            argv, cwd=sandbox, env=env, text=True, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise AcceptanceRunnerError(
                (result.stderr or result.stdout).strip()[:600]
                or "acceptance seed commit failed"
            )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if (
        head.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}\n?", head.stdout) is None
        or status.returncode != 0
        or status.stdout.strip()
    ):
        raise AcceptanceRunnerError("acceptance seed did not produce a clean commit")
    return head.stdout.strip()

def git_head() -> str:
    pinned = os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT", "").strip()
    if pinned:
        if not re.fullmatch(r"[0-9a-f]{40}", pinned):
            raise AcceptanceRunnerError("invalid pinned acceptance source commit")
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{pinned}^{{commit}}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if exists.returncode != 0:
            raise AcceptanceRunnerError("pinned acceptance source commit is unavailable")
        return pinned
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise AcceptanceRunnerError(
            "source checkout must be clean so live cells test the committed release candidate"
        )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}\n?", result.stdout):
        raise AcceptanceRunnerError("cannot resolve the committed source HEAD")
    return result.stdout.strip()

def create_sandbox(run_dir: Path) -> tuple[Path, str]:
    sandbox = run_dir / "sandbox"
    commit = git_head()
    cloned = subprocess.run(
        ["git", "clone", "--shared", "--no-hardlinks", "--quiet", str(ROOT), str(sandbox)],
        text=True,
        capture_output=True,
        check=False,
    )
    if cloned.returncode != 0:
        raise AcceptanceRunnerError(cloned.stderr.strip() or "local acceptance clone failed")
    checked = subprocess.run(
        ["git", "checkout", "--detach", "--quiet", commit], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if checked.returncode != 0:
        raise AcceptanceRunnerError(checked.stderr.strip() or "acceptance checkout failed")
    seeded_commit = materialize_seed_commit(sandbox)
    disable_acceptance_autocommit(sandbox)
    atomic_json(
        sandbox / ".acceptance-sandbox.json",
        {
            "schema_version": 1,
            "run_dir": str(run_dir),
            "source_commit": commit,
            "commit": seeded_commit,
            "seed_sha256": acceptance_seed_sha256(),
        },
    )
    return sandbox, seeded_commit

def disable_acceptance_autocommit(sandbox: Path) -> None:
    """Keep host Stop hooks from committing inside a disposable live clone."""

    atomic_json(
        sandbox / ".vault-meta" / "auto-commit.disabled",
        {"schema_version": 1, "reason": "live-acceptance"},
    )

def install_acceptance_model_overrides(
    sandbox: Path,
    overrides: dict[str, str] | None = None,
    effort: str | None = None,
) -> None:
    """Install ignored, sandbox-local routes for cost-aware live tests."""
    if overrides is None:
        overrides = {
            runtime: str(
                os.environ.get(f"LLM_OBSIDIAN_ACCEPTANCE_{runtime.upper()}_MODEL") or ""
            ).strip()
            for runtime in ("claude", "codex")
        }
    if effort is None:
        effort = str(os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_EFFORT") or "").strip()
    selected = {runtime: model for runtime, model in overrides.items() if model}
    if effort and effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        raise AcceptanceRunnerError("invalid acceptance effort override")
    if not selected and not effort:
        return
    if set(selected) - {"claude", "codex"}:
        raise AcceptanceRunnerError("acceptance model override names an unknown runtime")
    for runtime, model in selected.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
            raise AcceptanceRunnerError(f"invalid {runtime} acceptance model override")
    lines = ["schema_version = 1", ""]
    for runtime in ("claude", "codex"):
        model = selected.get(runtime)
        if model is None and not effort:
            continue
        lines.append(f"[runtimes.{runtime}]")
        if model is not None:
            lines.append(f'model = "{model}"')
        if effort:
            lines.append(f'effort = "{effort}"')
        lines.extend(("", f"[roles.review.{runtime}]"))
        if model is not None:
            lines.append(f'model = "{model}"')
        if effort:
            lines.append(f'effort = "{effort}"')
        lines.append("")
    if selected:
        lines.append("[model_registry]")
        lines.extend(f'"{model}" = "{runtime}"' for runtime, model in sorted(selected.items()))
    path = sandbox / "config" / "model-routing.local.toml"
    if path.exists():
        raise AcceptanceRunnerError("acceptance sandbox unexpectedly contains a local routing override")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def install_acceptance_runtime_fixture(sandbox: Path) -> None:
    """Provision only the ignored local gateway fixture required by lifecycle tests."""

    runtime_env = sandbox / "scripts" / "mcp-gateway" / "runtime.env"
    if not runtime_env.exists():
        shutil.copy2(runtime_env.with_name("runtime.env.example"), runtime_env)

def run_checked(argv: list[str], *, cwd: Path, input_text: str | None = None) -> str:
    result = subprocess.run(
        argv, cwd=cwd, input=input_text, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AcceptanceRunnerError(detail[:600] or f"command exited {result.returncode}: {argv[0]}")
    return result.stdout

def commit_file(sandbox: Path, commit: str, rel: str) -> tuple[bool, str | None]:
    """Return one exact tracked file, distinguishing absence from Git failure."""

    listing = subprocess.run(
        ["git", "ls-tree", "-z", "--name-only", commit, "--", rel], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if listing.returncode != 0:
        return False, None
    names = [name for name in listing.stdout.split("\0") if name]
    if not names:
        return True, None
    if names != [rel]:
        return False, None
    content = subprocess.run(
        ["git", "show", f"{commit}:{rel}"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if content.returncode != 0:
        return False, None
    return True, content.stdout

def git_output(repo: Path, *args: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False,
    )
    return result.returncode == 0, result.stdout

def scratch_root_for(run_dir: Path) -> Path:
    return Path(tempfile.gettempdir()).resolve() / f"llm-obsidian-acceptance-{run_dir.name}"

def safe_cleanup(run_dir: Path) -> None:
    sandbox = run_dir / "sandbox"
    marker = sandbox / ".acceptance-sandbox.json"
    if sandbox.is_dir() and marker.is_file() and sandbox.parent == run_dir:
        shutil.rmtree(sandbox)
    scratch = scratch_root_for(run_dir)
    scratch_marker = scratch / ".acceptance-scratch.json"
    if scratch.is_dir() and not scratch.is_symlink() and scratch_marker.is_file() and not scratch_marker.is_symlink():
        try:
            marker_value = read_json(scratch_marker)
        except AcceptanceRunnerError:
            marker_value = {}
        if marker_value == {"schema_version": 1, "run_dir": str(run_dir)}:
            shutil.rmtree(scratch)
