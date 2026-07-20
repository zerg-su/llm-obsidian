#!/usr/bin/env python3
"""Repo-shipped interactive runner for one release-acceptance matrix row."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = ROOT / ".vault-meta" / "acceptance" / "runs"
SCENARIOS = ROOT / "evals" / "acceptance" / "scenarios.json"
SKILLS = ROOT / "evals" / "acceptance" / "skills.json"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
SURFACE_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
OUTBOX_MAX_BYTES = 64 * 1024
OUTBOX_INVALID_GRACE_SECONDS = 5.0
OUTBOX_STABLE_SECONDS = 1.0
AGENT_EXIT_GRACE_SECONDS = 300.0
CHILD_SURFACE_SETTLE_SECONDS = 45.0
AUTORESEARCH_OUTPUT_LIMIT = 15
DISPOSABLE_VAULT_BOOKKEEPING = {
    ".raw/.manifest.json",
    ".vault-meta/address-counter.txt",
    ".vault-meta/address-map.tsv",
    ".vault-meta/index.jsonl",
    ".vault-meta/recent.jsonl",
    ".vault-meta/session-to-pages.jsonl",
    ".vault-meta/tag-index.json",
    "wiki/hot.md",
    "wiki/log.md",
}

sys.path.insert(0, str(ROOT / "scripts"))
from lib_sanitize import residual_credential_kinds, sanitize  # noqa: E402
from model_routing import load_config  # noqa: E402
from pipeline_events import emit_event  # noqa: E402
from task_sessions import TaskSessionError, close_surface_exact, spawn_right  # noqa: E402
from vault_schema import FrontmatterError, parse_frontmatter, split_frontmatter  # noqa: E402
from cmux_agent_support import (  # noqa: E402
    SupervisorError,
    resolved_git_common_dir,
    task_codex_config_values,
    validated_cmux_socket_path,
)
from cmux_trust_prompt import (  # noqa: E402
    claude_background_exit_prompt_visible,
    workspace_trust_prompt_visible,
)


class AcceptanceRunnerError(ValueError):
    pass


def die(message: str, code: int = 3) -> NoReturn:
    print(f"live-acceptance-runner: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceRunnerError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceRunnerError(f"{path} must contain an object")
    return value


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_scenarios(path: Path = SCENARIOS) -> dict[str, dict[str, Any]]:
    raw = read_json(path)
    values = raw.get("scenarios")
    if raw.get("schema_version") != 1 or not isinstance(values, dict):
        raise AcceptanceRunnerError("scenario registry must use schema_version 1")
    result: dict[str, dict[str, Any]] = {}
    for name, item in values.items():
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(item, dict):
            raise AcceptanceRunnerError(f"invalid scenario {name!r}")
        timeout = item.get("timeout_seconds")
        instructions = item.get("instructions")
        network = item.get("network")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 60 <= timeout <= 3600:
            raise AcceptanceRunnerError(f"{name}: timeout_seconds must be 60..3600")
        if network not in {"none", "protected", "direct-readonly"}:
            raise AcceptanceRunnerError(f"{name}: invalid network class")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 1000:
            raise AcceptanceRunnerError(f"{name}: invalid instructions")
        result[name] = dict(item)
    return result


def load_skill_fixtures(path: Path = SKILLS) -> dict[str, dict[str, str]]:
    raw = read_json(path)
    values = raw.get("skills")
    if raw.get("schema_version") != 1 or not isinstance(values, dict):
        raise AcceptanceRunnerError("skill fixture registry must use schema_version 1")
    result: dict[str, dict[str, str]] = {}
    for name, item in values.items():
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(item, dict):
            raise AcceptanceRunnerError(f"invalid skill fixture {name!r}")
        scenario = item.get("scenario")
        expected = item.get("expected")
        fixture = item.get("fixture")
        if not isinstance(scenario, str) or not SAFE_ID.fullmatch(scenario):
            raise AcceptanceRunnerError(f"{name}: invalid fixture scenario")
        if not isinstance(expected, str) or not expected.strip() or len(expected) > 300:
            raise AcceptanceRunnerError(f"{name}: invalid fixture expectation")
        if not isinstance(fixture, str) or not fixture.strip() or len(fixture) > 1000:
            raise AcceptanceRunnerError(f"{name}: invalid live fixture")
        result[name] = {
            "scenario": scenario,
            "expected": expected.strip(),
            "fixture": fixture.strip(),
        }
    discovered = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
    if set(result) != discovered:
        raise AcceptanceRunnerError("skill fixture registry does not exactly cover installed skills")
    return result


def validate_row(
    value: Any,
    scenarios: dict[str, dict[str, Any]],
    fixtures: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise AcceptanceRunnerError("input row must be a schema_version 1 object")
    for key in ("phase", "skill", "runtime", "scenario", "expected"):
        if not isinstance(value.get(key), str) or not str(value[key]).strip():
            raise AcceptanceRunnerError(f"row {key} is required")
    if value["phase"] not in {"baseline", "final"}:
        raise AcceptanceRunnerError("row phase is invalid")
    if value["runtime"] not in {"claude", "codex"}:
        raise AcceptanceRunnerError("row runtime is invalid")
    if value["scenario"] not in scenarios:
        raise AcceptanceRunnerError("row scenario is not registered")
    if not SAFE_ID.fullmatch(value["skill"]):
        raise AcceptanceRunnerError("row skill is invalid")
    if not (ROOT / "skills" / value["skill"] / "SKILL.md").is_file():
        raise AcceptanceRunnerError("row skill is not installed in the source checkout")
    fixture = fixtures.get(value["skill"])
    if fixture is None:
        raise AcceptanceRunnerError("row skill has no registered live fixture")
    if fixture["scenario"] != value["scenario"]:
        raise AcceptanceRunnerError("row scenario does not match the registered live fixture")
    if fixture["expected"] != value["expected"]:
        raise AcceptanceRunnerError("row expected result does not match the registered live fixture")
    return value


def result_payload(
    row: dict[str, Any], *, verdict: str, model: str, effort: str,
    actual: str, cleanup: str, evidence: str, defect: str = "", decision: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        **{key: row[key] for key in ("schema_version", "phase", "skill", "runtime", "scenario", "expected")},
        "verdict": verdict,
        "model": model,
        "effort": effort,
        "actual": actual,
        "cleanup": cleanup,
        "evidence": evidence,
    }
    if defect:
        value["defect"] = defect
    if decision:
        value["decision"] = decision
    return value


def validate_agent_result(row: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise AcceptanceRunnerError("agent outbox must contain a schema_version 1 object")
    for key in ("phase", "skill", "runtime", "scenario"):
        if raw.get(key) != row[key]:
            raise AcceptanceRunnerError(f"agent outbox {key} does not match the operation")
    verdict = raw.get("verdict")
    if verdict not in {"pass", "fail", "blocked", "n-a"}:
        raise AcceptanceRunnerError("agent outbox verdict is invalid")
    result = result_payload(
        row,
        verdict=verdict,
        model=str(raw.get("model") or ""),
        effort=str(raw.get("effort") or ""),
        actual=str(raw.get("actual") or ""),
        cleanup=str(raw.get("cleanup") or ""),
        evidence=str(raw.get("evidence") or ""),
        defect=str(raw.get("defect") or ""),
        decision=str(raw.get("decision") or ""),
    )
    for key, value in result.items():
        if isinstance(value, str):
            cleaned, _ = sanitize(value)
            if residual_credential_kinds(cleaned):
                raise AcceptanceRunnerError(f"agent outbox {key} contains credential-like text")
            result[key] = cleaned[:600]
    if verdict in {"pass", "fail"} and any(not result[key].strip() for key in ("model", "effort", "actual", "cleanup", "evidence")):
        raise AcceptanceRunnerError(f"{verdict} result lacks bounded evidence fields")
    if verdict in {"fail", "blocked"} and not result.get("defect"):
        raise AcceptanceRunnerError(f"{verdict} result lacks defect")
    if verdict == "n-a" and not result.get("decision"):
        raise AcceptanceRunnerError("n-a result lacks decision")
    return result


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
    disable_acceptance_autocommit(sandbox)
    atomic_json(sandbox / ".acceptance-sandbox.json", {"schema_version": 1, "run_dir": str(run_dir), "commit": commit})
    return sandbox, commit


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


def dispatch_acceptance_fixture(
    sandbox: Path, run_id: str, runtime: str,
) -> dict[str, str]:
    """Create deterministic dispatch inputs before the interactive skill run."""
    token = run_id.split("-", 1)[0]
    task_name = f"acceptance-dispatch-{token}"
    plan_rel = f"wiki/plans/{date.today().isoformat()}-{task_name}.md"
    fixture_rel = f"{task_name}.txt"
    fixture_text = f"dispatch acceptance {token}\n"
    result_title = f"Acceptance dispatch {token} result"
    nested_worktree = sandbox / ".vault-meta" / "acceptance-worktrees" / f"sandbox-{task_name}"
    plan_title = f"Acceptance dispatch {token} plan"
    plan_text = f"""---
type: plan
title: "{plan_title}"
status: pending
created: {date.today().isoformat()}
updated: {date.today().isoformat()}
tags:
  - plan
  - acceptance
sessions: []
---

# {plan_title}

## Approved scope

1. Create only `{fixture_rel}` with the exact single line `dispatch acceptance {token}`.
2. Commit that file in exactly one product commit on `task/{task_name}`.
3. Run one light opposite-model review and require an approved typed callback.
4. Finalize through reap as a session titled “{result_title}”.

Do not merge, push, publish, deploy, delete the task worktree, or expand scope.
"""
    payload = {
        "schema_version": 1,
        "request_id": f"acceptance-dispatch-{token}",
        "actor": "acceptance",
        "session": f"acceptance-{token}",
        "pages": [{"op": "create", "path": plan_rel, "content": plan_text}],
    }
    run_checked(
        [sys.executable, str(sandbox / "scripts" / "vault-write.py"), "--output", "json"],
        cwd=sandbox,
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    if runtime == "codex":
        runtime_env = sandbox / "scripts" / "mcp-gateway" / "runtime.env"
        if not runtime_env.exists():
            shutil.copy2(runtime_env.with_name("runtime.env.example"), runtime_env)
    fixture = {
        "task_name": task_name,
        "branch": f"task/{task_name}",
        "plan_rel": plan_rel,
        "plan_path": str((sandbox / plan_rel).resolve()),
        "fixture_rel": fixture_rel,
        "fixture_text": fixture_text,
        "fixture_sha256": hashlib.sha256(fixture_text.encode("utf-8")).hexdigest(),
        "result_title": result_title,
        "nested_worktree": str(nested_worktree.resolve()),
        "dispatch_spec": str((sandbox / ".vault-meta" / "acceptance" / "dispatch-request.json").resolve()),
        "request_id": run_id,
        "coordinator_runtime": runtime,
    }
    atomic_json(sandbox / ".vault-meta" / "acceptance" / "dispatch-fixture.json", fixture)
    return fixture


def dispatch_fixture_prompt(fixture: dict[str, str]) -> str:
    return (
        f"Execute the already-approved plan `{fixture['plan_path']}` exactly once. "
        f"The deterministic dispatch request is `{fixture['dispatch_spec']}`. Start it exactly once with "
        f"`python3 {Path(fixture['plan_path']).parents[2]}/scripts/dispatch-runner.py start --spec "
        f"{fixture['dispatch_spec']}`; do not reproduce its setup commands manually. "
        f"Use task name `{fixture['task_name']}`, branch `{fixture['branch']}`, and exact worktree "
        f"`{fixture['nested_worktree']}`. Create only `{fixture['fixture_rel']}` with exact bytes "
        f"`{fixture['fixture_text'].rstrip()}` plus one newline and commit it in exactly one commit. "
        "Run one light opposite-model review, require its typed approve callback, then perform final reap "
        f"as a session titled `{fixture['result_title']}`. Keep the typed summary body free of invented "
        "wikilinks; the reap runner attaches the validated review archive link itself. The runner already prepared local runtime "
        "configuration and owns setup, artifact proof, and disposable-clone cleanup. Do not make a second "
        "plan, repeat configuration setup, remove result/review/plan artifacts, or ask for approval again."
    )


def close_acceptance_fixture(run_id: str) -> dict[str, str]:
    """Return one exact save target for the runner-owned close surface."""

    token = run_id.split("-", 1)[0]
    title = f"Acceptance Close Fixture {token}"
    return {"title": title, "page_rel": f"wiki/meta/sessions/{title}.md"}


def close_fixture_prompt(fixture: dict[str, str]) -> str:
    return (
        "Use this current runner-created acceptance surface as the disposable close fixture; "
        "do not create another cmux surface or launch another agent. "
        f"Save one short reusable session note titled `{fixture['title']}` at exactly "
        f"`{fixture['page_rel']}` through the documented save workflow and one vault-write transaction. "
        "The save contract still requires a DragonScale `address: c-NNNNNN` and session provenance; "
        "do not use the schema's session-type address exemption. "
        "State only that it is a disposable local acceptance record for exact-surface graceful exit. "
        "Validate the saved page but do not delete it: the outer runner owns proof and deletion after exit."
    )


def close_acceptance_proof(sandbox: Path, fixture: dict[str, str]) -> tuple[bool, str]:
    """Validate and transactionally remove the exact close fixture after agent exit."""

    page = sandbox / fixture["page_rel"]
    if page.is_symlink() or not page.is_file():
        return False, "close fixture page is missing"
    content = page.read_text(encoding="utf-8")
    block = split_frontmatter(content)
    try:
        frontmatter = parse_frontmatter(block) if block is not None else {}
    except FrontmatterError:
        frontmatter = {}
    sessions = frontmatter.get("sessions")
    if (
        frontmatter.get("type") != "session"
        or frontmatter.get("title") != fixture["title"]
        or re.fullmatch(r"c-\d{6}", str(frontmatter.get("address") or "")) is None
        or not isinstance(sessions, list)
        or not sessions
    ):
        return False, "close fixture page does not match the required session note"
    validated = subprocess.run(
        [sys.executable, str(sandbox / "scripts" / "validate-vault.py"), "--summary"],
        cwd=sandbox,
        text=True,
        capture_output=True,
        check=False,
    )
    if validated.returncode != 0:
        return False, "close fixture page failed vault validation"
    digest = hashlib.sha256(page.read_bytes()).hexdigest()
    payload = {
        "schema_version": 1,
        "request_id": f"acceptance-close-cleanup-{digest[:16]}",
        "actor": "acceptance",
        "session": "acceptance-close-cleanup",
        "pages": [{
            "op": "delete",
            "path": fixture["page_rel"],
            "expected_sha256": digest,
        }],
    }
    try:
        run_checked(
            [sys.executable, str(sandbox / "scripts" / "vault-write.py"), "--output", "json"],
            cwd=sandbox,
            input_text=json.dumps(payload, ensure_ascii=False),
        )
    except AcceptanceRunnerError:
        return False, "close fixture page cleanup failed"
    if page.exists():
        return False, "close fixture page remained after cleanup"
    return True, "saved page validated and transactionally removed after exact agent exit"


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


def autoresearch_acceptance_cleanup(
    sandbox: Path, commit: str, coordinator_surface: str
) -> tuple[bool, str]:
    """Validate and transactionally remove outputs from one bound research run."""

    locator_root = sandbox / ".vault-meta" / "research-runs"
    if locator_root.is_symlink() or not locator_root.is_dir():
        return False, "autoresearch run locator root is invalid"
    try:
        locators = sorted(locator_root.glob("*/locator.json"))
    except OSError:
        return False, "autoresearch run locator is unreadable"
    if len(locators) != 1:
        return False, "autoresearch must leave exactly one run locator"
    locator_path = locators[0]
    if locator_path.parent.is_symlink() or locator_path.is_symlink() or not locator_path.is_file():
        return False, "autoresearch run locator is not a regular file"
    try:
        locator = read_json(locator_path)
    except AcceptanceRunnerError:
        return False, "autoresearch run locator is invalid"
    run_id = str(locator.get("run_id") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", run_id) or locator_path.parent.name != run_id:
        return False, "autoresearch run locator identity is invalid"
    locator_vault = str(locator.get("vault") or "").strip()
    if not locator_vault or not Path(locator_vault).is_absolute():
        return False, "autoresearch run locator has the wrong vault"
    if Path(locator_vault).resolve() != sandbox.resolve():
        return False, "autoresearch run locator has the wrong vault"
    operation_dir = Path(str(locator.get("operation_dir") or "")).resolve()
    task_root = (sandbox / ".vault-meta" / "task-sessions").resolve()
    try:
        operation_dir.relative_to(task_root)
    except ValueError:
        return False, "autoresearch run locator escapes task sessions"
    if (
        operation_dir.is_symlink()
        or operation_dir.name != run_id
        or operation_dir.parent.name != "operations"
    ):
        return False, "autoresearch operation binding is invalid"
    state_path = operation_dir / "state.json"
    if state_path.is_symlink() or not state_path.is_file():
        return False, "autoresearch state is missing"
    try:
        state = read_json(state_path)
    except AcceptanceRunnerError:
        return False, "autoresearch state is invalid"
    outputs = state.get("outputs")
    if (
        state.get("run_id") != run_id
        or Path(str(state.get("operation_dir") or "")).resolve() != operation_dir
        or Path(str(state.get("vault") or "")).resolve() != sandbox.resolve()
        or state.get("status") != "complete"
        or state.get("fetch_artifact_status") != "accepted"
        or state.get("coordinator_surface") != coordinator_surface
        or not isinstance(outputs, list)
        or not 1 <= len(outputs) <= AUTORESEARCH_OUTPUT_LIMIT
        or any(not isinstance(item, str) for item in outputs)
        or len(set(outputs)) != len(outputs)
    ):
        return False, "autoresearch state is not one complete bound run"

    pages: list[dict[str, str]] = []
    output_paths: set[str] = set()
    restored_outputs: dict[str, str] = {}
    wiki_root = sandbox / "wiki"
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return False, "autoresearch wiki root is invalid"
    for raw in outputs:
        rel_path = Path(raw)
        if (
            rel_path.is_absolute()
            or not rel_path.parts
            or rel_path.parts[0] != "wiki"
            or rel_path.suffix != ".md"
            or any(part in {"", ".", ".."} for part in rel_path.parts)
        ):
            return False, "autoresearch output path is outside the wiki"
        page = (sandbox / rel_path).resolve()
        try:
            page.relative_to(wiki_root.resolve())
        except ValueError:
            return False, "autoresearch output path escapes the wiki"
        if page.is_symlink() or not page.is_file():
            return False, "autoresearch output page is missing"
        output_paths.add(raw)
        current_sha256 = hashlib.sha256(page.read_bytes()).hexdigest()
        baseline_ok, baseline = commit_file(sandbox, commit, raw)
        if not baseline_ok:
            return False, "autoresearch source commit is unreadable"
        if baseline is not None:
            restored_outputs[raw] = baseline
            pages.append({
                "op": "update",
                "path": raw,
                "content": baseline,
                "expected_sha256": current_sha256,
            })
        else:
            pages.append({
                "op": "delete",
                "path": raw,
                "expected_sha256": current_sha256,
            })

    try:
        run_checked(
            [sys.executable, str(sandbox / "scripts" / "validate-vault.py"), "--summary"],
            cwd=sandbox,
        )
    except AcceptanceRunnerError:
        return False, "autoresearch output failed independent vault validation"

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if status.returncode != 0:
        return False, "autoresearch index cleanup status is unreadable"
    for line in status.stdout.splitlines():
        rel = line[3:]
        if rel in output_paths or not re.fullmatch(r"wiki(?:/[^/]+)*/_index\.md", rel):
            continue
        if line[:2] == "??":
            return False, "autoresearch left an unbound product index"
        current = sandbox / rel
        baseline_ok, baseline = commit_file(sandbox, commit, rel)
        if (
            not baseline_ok
            or baseline is None
            or current.is_symlink()
            or not current.is_file()
        ):
            return False, "autoresearch product index cannot be restored safely"
        pages.append({
            "op": "update",
            "path": rel,
            "content": baseline,
            "expected_sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
        })

    payload = {
        "schema_version": 1,
        "request_id": f"acceptance-autoresearch-cleanup-{run_id}",
        "actor": "acceptance",
        "session": run_id,
        "pages": pages,
    }
    try:
        run_checked(
            [sys.executable, str(sandbox / "scripts" / "vault-write.py"), "--output", "json"],
            cwd=sandbox,
            input_text=json.dumps(payload, ensure_ascii=False),
        )
    except AcceptanceRunnerError:
        return False, "autoresearch transactional cleanup failed"
    for path in output_paths:
        output = sandbox / path
        if path in restored_outputs:
            if output.is_symlink() or not output.is_file():
                return False, "autoresearch tracked output was not restored"
            if output.read_text(encoding="utf-8") != restored_outputs[path]:
                return False, "autoresearch tracked output differs from source commit"
        elif output.exists():
            return False, "autoresearch new output remained after cleanup"
    return True, "autoresearch output independently validated and transactionally restored"


def write_dispatch_acceptance_request(
    sandbox: Path, fixture: dict[str, str], *, source_commit: str, coordinator_surface: str,
    coordinator_model: str, coordinator_effort: str,
) -> None:
    atomic_json(Path(fixture["dispatch_spec"]), {
        "schema_version": 1,
        "request_id": fixture["request_id"],
        "task_name": fixture["task_name"],
        "description": (
            f"Execute {fixture['plan_path']} exactly once, create only {fixture['fixture_rel']} "
            "with its specified bytes, run one light opposite-model review, and finalize through reap."
        ),
        "vault_root": str(sandbox),
        "target_repo": str(sandbox),
        "worktree": fixture["nested_worktree"],
        "branch": fixture["branch"],
        "base_branch": source_commit,
        "plan_file": fixture["plan_path"],
        "origin_surface": coordinator_surface,
        "session_route": {
            "runtime": fixture["coordinator_runtime"],
            "model": coordinator_model,
            "effort": coordinator_effort,
            "source": "acceptance-runner",
        },
        "executor": {},
        "wiki_context": [],
        "suggested_agents": [],
        "reap": {"type": "session", "title": fixture["result_title"]},
        "review_mode": "light",
    })


def git_output(repo: Path, *args: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False,
    )
    return result.returncode == 0, result.stdout


def dispatch_acceptance_proof(
    sandbox: Path, source_commit: str, fixture: dict[str, str],
) -> tuple[bool, str]:
    """Validate the complete dispatch/review/reap lifecycle from durable artifacts."""
    expected_worktree = Path(fixture["nested_worktree"]).resolve()
    root = sandbox / ".vault-meta" / "acceptance-worktrees"
    worktrees = sorted(path.resolve() for path in root.iterdir()) if root.is_dir() else []
    if worktrees != [expected_worktree] or not expected_worktree.is_dir():
        return False, "dispatch did not retain exactly the runner-bound task worktree"
    ok, head = git_output(expected_worktree, "rev-parse", "HEAD")
    if not ok:
        return False, "dispatch task HEAD is unreadable"
    head = head.strip()
    ok, parent = git_output(expected_worktree, "rev-parse", "HEAD^")
    if not ok or parent.strip() != source_commit:
        return False, "dispatch task did not create exactly one commit from the source commit"
    ok, changed = git_output(expected_worktree, "diff", "--name-only", source_commit, head)
    if not ok or changed.splitlines() != [fixture["fixture_rel"]]:
        return False, "dispatch task commit changed files outside the exact fixture"
    ok, content = git_output(expected_worktree, "show", f"{head}:{fixture['fixture_rel']}")
    if not ok or content != fixture["fixture_text"]:
        return False, "dispatch task commit does not contain the exact fixture bytes"
    try:
        meta = read_json(expected_worktree / ".task-meta.json")
    except AcceptanceRunnerError as exc:
        return False, str(exc)
    if (
        meta.get("version") != 3
        or meta.get("task_name") != fixture["task_name"]
        or meta.get("branch") != fixture["branch"]
        or str(Path(str(meta.get("plan_file") or "")).resolve()) != fixture["plan_path"]
        or not isinstance(meta.get("review_policy"), dict)
        or meta["review_policy"].get("mode") != "light"
        or not isinstance(meta.get("reap_policy"), dict)
        or meta["reap_policy"].get("mode") != "final"
        or meta["reap_policy"].get("title") != fixture["result_title"]
    ):
        return False, "dispatch task metadata drifted from the runner-bound contract"
    project_id = str(meta.get("project_id") or "")
    task_id = str(meta.get("task_id") or "")
    task_root = sandbox / ".vault-meta" / "task-sessions" / "projects" / project_id / "tasks" / task_id
    try:
        task = read_json(task_root / "task.json")
    except AcceptanceRunnerError as exc:
        return False, str(exc)
    if (
        task.get("project_id") != project_id
        or task.get("task_id") != task_id
        or task.get("status") != "archived"
        or task.get("worktrees") != [str(expected_worktree)]
    ):
        return False, "dispatch task session was not archived with its exact worktree"
    review_files = sorted(task_root.glob("lanes/*/operations/*/.task-review*.json"))
    try:
        reviews = [read_json(path) for path in review_files]
    except AcceptanceRunnerError as exc:
        return False, str(exc)
    if (
        len(reviews) != 1
        or reviews[0].get("schema_version") != 1
        or reviews[0].get("mode") != "light"
        or reviews[0].get("verdict") != "approve"
    ):
        return False, "dispatch did not produce exactly one typed approve review"
    try:
        reap = read_json(expected_worktree / ".task-reap-complete.json")
    except AcceptanceRunnerError as exc:
        return False, str(exc)
    result_path = Path(str(reap.get("result_path") or "")).resolve()
    plan_path = Path(fixture["plan_path"])
    try:
        result_rel = result_path.relative_to(sandbox.resolve()).as_posix()
    except ValueError:
        return False, "dispatch reap result escaped the disposable coordinator clone"
    if (
        reap.get("validated") is not True
        or reap.get("task_session_status") != "archived"
        or Path(str(reap.get("plan_path") or "")).resolve() != plan_path.resolve()
        or not result_path.is_file()
        or reap.get("result_sha256") != hashlib.sha256(result_path.read_bytes()).hexdigest()
    ):
        return False, "dispatch final reap marker is missing or inconsistent"
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return False, "dispatch approved plan is missing after reap"
    if not re.search(r"(?m)^status: executed$", plan_text) or "Результат:" not in plan_text:
        return False, "dispatch approved plan was not closed by final reap"
    archive_paths: set[str] = set()
    for marker_path in task_root.glob("lanes/*/operations/*/.review-archive.json"):
        try:
            marker = read_json(marker_path)
        except AcceptanceRunnerError as exc:
            return False, str(exc)
        archive_rel = str(marker.get("path") or "")
        if marker.get("status") not in {"archived", "already-current"} or not archive_rel.startswith("wiki/"):
            return False, "dispatch review archive marker is inconsistent"
        archive_path = sandbox / archive_rel
        if not archive_path.is_file() or marker.get("content_sha256") != hashlib.sha256(archive_path.read_bytes()).hexdigest():
            return False, "dispatch durable review archive is missing or changed"
        archive_paths.add(archive_rel)
    if not archive_paths:
        return False, "dispatch durable review archive is missing"
    ok, coordinator_head = git_output(sandbox, "rev-parse", "HEAD")
    if not ok or coordinator_head.strip() != source_commit:
        return False, "dispatch changed the disposable coordinator HEAD"
    ok, status = git_output(sandbox, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not ok:
        return False, "dispatch coordinator status is unreadable"
    allowed_pages = {fixture["plan_rel"], result_rel, *archive_paths}
    unexpected: list[str] = []
    for line in status.split("\0"):
        if not line:
            continue
        path = line[3:]
        if (
            path == ".acceptance-sandbox.json"
            or path.startswith(".vault-meta/acceptance-worktrees/")
            or path in allowed_pages
        ):
            continue
        if is_disposable_bookkeeping(path, line[:2]):
            continue
        unexpected.append(path)
    if unexpected:
        return False, "dispatch retained unexpected coordinator changes: " + ", ".join(unexpected[:5])
    return True, "exact one-commit dispatch, typed approve review, archived task, and validated final reap"


def prompt_text(
    row: dict[str, Any], scenario: dict[str, Any], sandbox: Path, outbox: Path,
    model: str, effort: str, commit: str, fixture: str,
    runner_fixture: dict[str, str] | None = None,
) -> str:
    if row["skill"] == "close":
        cleanup_contract = f"""- This current acceptance surface is the only session fixture. Do not create or close another surface.
- Leave the exact saved page in place for runner proof. The runner owns its transactional deletion,
  disposable bookkeeping, operation outbox, clone, scratch directory, and exact surface cleanup.
- Do not remove `.acceptance-sandbox.json`, pass `--tmp-root`/`--state-root`, or override `TMPDIR`/`TMP`/`TEMP`."""
    elif row["skill"] == "autoresearch":
        cleanup_contract = f"""- Complete one protected research run and independently validate its filed pages and provenance.
- Leave the exact filed output pages and any generated index links in place for runner proof. The runner
  resolves the one operation-bound run state, validates the vault, removes those exact outputs through
  one optimistic vault-write transaction, restores tracked indexes, and cleans the disposable clone.
- Close external processes through the documented autoresearch lifecycle, but do not manually delete
  product pages, indexes, task-session state, `.acceptance-sandbox.json`, the operation outbox, or scratch.
- After `research-isolation.py start` or `receive` returns, finish the coordinator turn immediately and
  wait for the exact typed callback. Do not poll marker/state files, inspect child screens, sleep, or loop;
  the callback starts the next turn and preserves the observable unattended lifecycle.
- Do not run `git restore`, `git checkout`, `git stash`, pass `--tmp-root`/`--state-root`, or override
  `TMPDIR`/`TMP`/`TEMP`."""
    elif row["scenario"] == "dispatch-review-reap" and runner_fixture is None:
        cleanup_contract = f"""- Treat every nested branch, worktree, task-session entry, and review operation under
  `{sandbox / '.vault-meta' / 'acceptance-worktrees'}` as runner-owned lifecycle proof.
- Exercise the named skill and close its task/reviewer processes through the documented lifecycle, but do not
  manually remove, prune, merge, restore, stash, or rewrite runner-owned branches, worktrees, registry state,
  review artifacts, `.acceptance-sandbox.json`, the operation outbox, or scratch.
- Clean only fixture-created product output and non-runner scratch. The outer runner independently validates
  operation identity, callback state, duplicate safety, and disposable-lane cleanup, then deletes the clone.
- Use `LLM_OBSIDIAN_WORKTREES` for every nested dispatch; do not invent another worktree root or pass
  `--tmp-root`/`--state-root`, and do not override `TMPDIR`/`TMP`/`TEMP`."""
    elif runner_fixture is None:
        cleanup_contract = f"""- Clean every disposable page, branch, worktree, surface, process, and scratch file you create before reporting pass.
- Do not remove `.acceptance-sandbox.json`; it is the runner-owned cleanup marker.
- The runner owns the disposable clone, its ignored task-session registry, the operation outbox,
  `{sandbox / '.vault-meta' / 'acceptance-worktrees'}`, and its run-scoped temporary directory.
  Use `LLM_OBSIDIAN_WORKTREES` for every nested dispatch; do not invent another worktree root.
  Do not run `git restore`, `git checkout`, `git stash`,
  manually delete those runner-owned paths, pass `--tmp-root`/`--state-root`, or override
  `TMPDIR`/`TMP`/`TEMP`. Remove the fixture's product output and close external processes/surfaces;
  the runner validates allowed vault bookkeeping and deletes the clone.
- Validate product output before removing disposable pages. After removal, append-only log/hot/index
  bookkeeping may still name those discarded pages; report it as runner-owned disposable bookkeeping
  instead of requiring a second whole-vault validation or treating it as a product failure."""
    else:
        cleanup_contract = f"""- This cell's plan, result page, review archive, task branch, exact nested worktree,
  task-session registry, and lifecycle markers are runner-owned proof artifacts. Leave them in place.
- Close the task and reviewer agent processes through their documented lifecycle. Do not manually close,
  delete, merge, restore, stash, or rewrite their branches, worktrees, registry, or proof artifacts.
- Do not remove `.acceptance-sandbox.json`; it is the runner-owned cleanup marker.
- Use only the exact runner-bound nested worktree `{runner_fixture['nested_worktree']}`.
  Do not pass `--tmp-root`/`--state-root` or override `TMPDIR`/`TMP`/`TEMP`.
- After `dispatch-runner.py start` returns, finish the coordinator turn and return
  to the idle prompt. Do not shell-poll task files, inspect cmux in a loop, or call
  agent wait tools. Typed review/reap callbacks begin later turns automatically.
- Do not publish the acceptance agent outbox in that launch turn. Publish it only
  in the later final-reap callback turn, after the durable lifecycle proof below
  has been validated; returning idle without an outbox keeps this cell running.
- If the single exact `dispatch-runner.py start` invocation exits non-zero, do
  not retry it, perform open-ended diagnosis, or clean runner-owned state.
  Publish the typed fail/blocked outbox immediately with that bounded error;
  the acceptance runner owns containment and exact cleanup for this path.
- After the exact final reap runner returns `status: complete`, publish the
  typed pass outbox immediately. Do not enumerate proof files or invoke
  `dispatch_acceptance_proof` yourself: the outer acceptance runner performs
  that independent durable-proof check before accepting the cell.
- Validate the task result before the typed outbox. The runner independently proves the exact commit,
  typed review, archived task, final reap, and plan closure, then deletes the disposable clone."""
    if row["skill"] == "close":
        final_contract = f"""For this close fixture only, the typed outbox is the penultimate action. Write it before exit,
then make your final tool call exactly `python3 scripts/queue-session-exit.py`. Perform no tool calls
after that command and end the turn immediately. The outer runner independently proves process exit,
surface retention, exact-surface cleanup, and removal of the saved fixture page."""
    elif row["skill"] == "autoresearch":
        final_contract = (
            "Do not merely describe a hypothetical test. After validating the protected run and its "
            "filed pages, write the outbox as the final action; runner-owned cleanup begins afterward."
        )
    else:
        final_contract = "Do not merely describe a hypothetical test. The outbox is the final action after cleanup."
    return f"""# Live release acceptance operation

You are running one real, bounded acceptance cell in a disposable local clone.

- Phase: `{row['phase']}`
- Runtime: `{row['runtime']}`
- Effective model: `{model}`
- Effective effort: `{effort}`
- Skill: `{row['skill']}`
- Scenario: `{row['scenario']}`
- Expected: {row['expected']}
- Network class: `{scenario['network']}`
- Source commit: `{commit}`

Read `{sandbox / 'skills' / row['skill'] / 'SKILL.md'}` completely and exercise that skill faithfully.
Scenario instructions: {scenario['instructions']}

Exact skill fixture (treat this as the complete end-user request for the cell):

> {fixture}

Complete this fixture in one bounded agent turn. If the named skill would normally
ask the user a question or present a quiz/draft for a later reply, that observable
response is the end of this acceptance interaction: record it and continue to
cleanup and the typed outbox instead of waiting for another human message.

Hard boundaries:

- Work only inside `{sandbox}` and disposable nested paths it creates.
- Never push, publish, deploy, send communication, access credential material, or mutate the source checkout.
- Native Claude/Codex processes and their opposite-model reviewers may use an already authenticated
  subscription session. Never read, copy, print, export, or request its credential material.
- A public web read is allowed only when the declared network class permits it.
- If authentication is required, return `blocked` and name only the credential class; never print a value.
- Install nothing unless it is already covered by an explicit local noninteractive fixture. Missing optional dependencies must produce a visible blocked/degraded result.
{cleanup_contract}
- Exercise the exact live fixture once. Do not precede it with a `--no-spawn`/dry-run copy of the flow.
- Preserve real first-failure evidence; do not turn a retry into a clean pass without mentioning it.
- An acceptance cell must not repair or edit product scripts, skills, tests, hooks, or configuration. If a
  repo-owned mechanism fails, preserve the evidence and report it; the outer coordinator owns any fix and rerun.

Finally write exactly one JSON object to `{outbox}` using this shape:

```json
{{
  "schema_version": 1,
  "phase": "{row['phase']}",
  "skill": "{row['skill']}",
  "runtime": "{row['runtime']}",
  "scenario": "{row['scenario']}",
  "verdict": "pass | fail | blocked | n-a",
  "model": "{model}",
  "effort": "{effort}",
  "actual": "bounded observed behavior",
  "cleanup": "bounded cleanup proof",
  "evidence": "bounded commands/artifacts/status proof without content or secrets",
  "defect": "required for fail/blocked",
  "decision": "required for n-a"
}}
```

{final_contract}
"""


def agent_argv(
    runtime: str,
    sandbox: Path,
    model: str,
    effort: str,
    prompt: str,
    *,
    scratch_root: Path | None = None,
    surface: str = "",
) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env["LLM_OBSIDIAN_ACCEPTANCE"] = "1"
    env["LLM_OBSIDIAN_WORKTREES"] = str(sandbox / ".vault-meta" / "acceptance-worktrees")
    env["DCG_CONFIG"] = str(sandbox / "config" / "dcg" / "task.toml")
    if surface:
        if SURFACE_RE.fullmatch(surface) is None:
            raise AcceptanceRunnerError("acceptance agent surface is invalid")
        env["CMUX_SURFACE_ID"] = surface
    if scratch_root is not None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(scratch_root)
    if runtime == "claude":
        return [
            "claude", "--permission-mode", "auto", "--add-dir", str(sandbox),
            "--plugin-dir", str(sandbox),
            "--disallowedTools", "AskUserQuestion",
            "--model", model, "--effort", effort, prompt,
        ], env
    socket = validated_cmux_socket_path()
    argv = [
        "codex", "--cd", str(sandbox), "-a", "never", "-s", "workspace-write",
        "--disable", "hooks",
        "--add-dir", str(resolved_git_common_dir(sandbox)),
        "--model", model,
    ]
    for value in task_codex_config_values(socket, effort):
        argv.extend(["-c", value])
    dispatch_env = sandbox / ".codex" / "dispatch-env.toml"
    if dispatch_env.is_file() and sys.version_info >= (3, 11):
        import tomllib

        raw = tomllib.loads(dispatch_env.read_text(encoding="utf-8")).get("codex_dispatch", {})
        profile = str(raw.get("profile") or "").strip() if isinstance(raw, dict) else ""
        codex_home = str(raw.get("codex_home") or "").strip() if isinstance(raw, dict) else ""
        if profile:
            argv.extend(["--profile", profile])
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
    env["CMUX_SOCKET_PATH"] = str(socket)
    argv.append(prompt)
    return argv, env


def run_agent_process(spec_path: Path) -> int:
    spec = read_json(spec_path)
    run_dir = spec_path.parent.resolve()
    sandbox = Path(str(spec.get("sandbox") or "")).resolve()
    prompt_path = Path(str(spec.get("prompt_file") or "")).resolve()
    scratch_root = Path(str(spec.get("scratch_root") or "")).resolve()
    if sandbox.parent != run_dir or not (sandbox / ".acceptance-sandbox.json").is_file():
        raise AcceptanceRunnerError("acceptance sandbox is not bound to its run directory")
    if prompt_path != run_dir / "prompt.md" or not prompt_path.is_file():
        raise AcceptanceRunnerError("acceptance prompt is not operation-scoped")
    if scratch_root != scratch_root_for(run_dir) or not (scratch_root / ".acceptance-scratch.json").is_file():
        raise AcceptanceRunnerError("acceptance scratch directory is not operation-scoped")
    runtime = str(spec.get("runtime") or "")
    config = load_config(sandbox)
    route = config.runtime_default(runtime)
    if route["model"] != spec.get("model") or route["effort"] != spec.get("effort"):
        raise AcceptanceRunnerError("acceptance route drifted after preparation")
    argv, env = agent_argv(
        runtime,
        sandbox,
        route["model"],
        route["effort"],
        prompt_path.read_text(encoding="utf-8"),
        scratch_root=scratch_root,
        surface=str(spec.get("surface") or ""),
    )
    try:
        launch_cwd = run_dir if runtime == "claude" else sandbox
        return subprocess.run(argv, cwd=launch_cwd, env=env, check=False).returncode
    finally:
        atomic_json(run_dir / "agent-exit.json", {"schema_version": 1, "finished": True})


def send_surface(surface: str, text: str, *, submit_key: str = "Enter") -> None:
    for argv in (
        ["cmux", "send", "--surface", surface, text],
        ["cmux", "send-key", "--surface", surface, submit_key],
    ):
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise AcceptanceRunnerError((result.stdout + result.stderr).strip() or "cmux send failed")


def settled_outbox(
    outbox: Path,
    state: dict[str, Any],
    now: float,
) -> dict[str, Any] | None:
    """Return one stable JSON outbox, tolerating a bounded non-atomic write."""
    try:
        metadata = outbox.lstat()
    except FileNotFoundError:
        state.clear()
        return None
    except OSError as exc:
        raise AcceptanceRunnerError("acceptance outbox metadata is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AcceptanceRunnerError("acceptance outbox must be a regular non-symlink file")
    if metadata.st_size > OUTBOX_MAX_BYTES:
        raise AcceptanceRunnerError("acceptance outbox exceeds the bounded size limit")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(outbox, flags)
    except OSError as exc:
        raise AcceptanceRunnerError("acceptance outbox is unreadable") from exc
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise AcceptanceRunnerError("acceptance outbox changed identity while opening")
        payload = stream.read(OUTBOX_MAX_BYTES + 1)
        if len(payload) > OUTBOX_MAX_BYTES:
            raise AcceptanceRunnerError("acceptance outbox exceeds the bounded size limit")
    first_seen = float(state.setdefault("first_seen", now))
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        state.pop("digest", None)
        state.pop("stable_since", None)
        if now - first_seen >= OUTBOX_INVALID_GRACE_SECONDS:
            raise AcceptanceRunnerError(
                "acceptance outbox remained invalid after the bounded write grace period"
            ) from exc
        return None
    digest = hashlib.sha256(payload).hexdigest()
    if state.get("digest") != digest:
        state["digest"] = digest
        state["stable_since"] = now
        return None
    if now - float(state.get("stable_since", now)) < OUTBOX_STABLE_SECONDS:
        return None
    if not isinstance(parsed, dict):
        raise AcceptanceRunnerError("acceptance outbox must contain a JSON object")
    return parsed


def wait_for_outbox(
    outbox: Path, exit_marker: Path, timeout: int, *, surface: str, runtime: str
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    trust_accepted = False
    outbox_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        candidate = settled_outbox(outbox, outbox_state, time.monotonic())
        if candidate is not None:
            return candidate
        if exit_marker.is_file():
            raise AcceptanceRunnerError("acceptance agent exited before writing its outbox")
        if not trust_accepted:
            screen = subprocess.run(
                ["cmux", "read-screen", "--surface", surface, "--lines", "80"],
                text=True,
                capture_output=True,
                check=False,
            )
            if screen.returncode == 0 and workspace_trust_prompt_visible(runtime, screen.stdout):
                accepted = subprocess.run(
                    ["cmux", "send-key", "--surface", surface, "Enter"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if accepted.returncode != 0:
                    raise AcceptanceRunnerError("exact workspace trust prompt could not be accepted")
                trust_accepted = True
        time.sleep(1)
    raise AcceptanceRunnerError("acceptance agent timed out")


def close_surface(
    surface: str, runtime: str, exit_marker: Path, *, force: bool = False
) -> str:
    if force:
        try:
            close_surface_exact(surface, subprocess.run)
        except (TaskSessionError, OSError):
            return "exact surface close failed; surface left visible"
        return "exact surface closed"
    if not exit_marker.is_file():
        try:
            if runtime == "codex":
                for _ in range(40):
                    subprocess.run(["cmux", "send-key", "--surface", surface, "backspace"], capture_output=True, check=False)
                send_surface(surface, "/exit", submit_key="tab")
                subprocess.run(["cmux", "send-key", "--surface", surface, "Enter"], capture_output=True, check=False)
            else:
                send_surface(surface, "/exit")
        except AcceptanceRunnerError:
            return "exit-request-failed; surface left visible"
    deadline = time.monotonic() + AGENT_EXIT_GRACE_SECONDS
    exit_confirmation_sent = False
    while time.monotonic() < deadline and not exit_marker.is_file():
        if runtime == "claude" and not exit_confirmation_sent:
            screen = subprocess.run(
                ["cmux", "read-screen", "--surface", surface, "--lines", "40"],
                text=True,
                capture_output=True,
                check=False,
            )
            if (
                screen.returncode == 0
                and claude_background_exit_prompt_visible(screen.stdout)
            ):
                confirmed = subprocess.run(
                    ["cmux", "send-key", "--surface", surface, "Enter"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if confirmed.returncode != 0:
                    return "exit-confirmation-failed; surface left visible"
                exit_confirmation_sent = True
        time.sleep(0.5)
    if not exit_marker.is_file():
        return "agent did not exit; surface left visible"
    try:
        close_surface_exact(surface, subprocess.run)
    except (TaskSessionError, OSError):
        return "exact surface close failed; surface left visible"
    return "exact surface closed"


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


def operation_child_surfaces(sandbox: Path, coordinator_surface: str) -> set[str]:
    """Return exact child surfaces durably bound to this coordinator."""
    surfaces: set[str] = set()
    task_root = sandbox / ".vault-meta" / "task-sessions"
    candidates = list(task_root.glob("projects/*/tasks/*/lanes/*/operations/*/state.json"))
    candidates.extend((sandbox / ".vault-meta" / "research-runs").glob("*/state.json"))
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            state = read_json(path)
        except AcceptanceRunnerError:
            continue
        if state.get("coordinator_surface") != coordinator_surface:
            continue
        for key in ("surface", "fetch_surface", "synth_surface"):
            value = str(state.get(key) or "")
            if value != coordinator_surface and SURFACE_RE.fullmatch(value):
                surfaces.add(value)
    for path in (sandbox / ".vault-meta" / "acceptance-worktrees").glob("*/.task-meta.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            task_meta = read_json(path)
        except AcceptanceRunnerError:
            continue
        if task_meta.get("wiki_surface") != coordinator_surface:
            continue
        task_surface = str(task_meta.get("task_surface") or "")
        if task_surface != coordinator_surface and SURFACE_RE.fullmatch(task_surface):
            surfaces.add(task_surface)
    return surfaces


def surface_is_open(surface: str) -> bool:
    result = subprocess.run(
        ["cmux", "read-screen", "--surface", surface, "--lines", "1"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    output = (result.stdout + result.stderr).lower()
    return not any(token in output for token in ("not found", "not_found", "unknown surface"))


def wait_for_operation_children(
    sandbox: Path, coordinator_surface: str, grace_seconds: float = CHILD_SURFACE_SETTLE_SECONDS
) -> None:
    """Give armed task/reviewer wrappers a bounded chance to close themselves."""
    surfaces = operation_child_surfaces(sandbox, coordinator_surface)
    if not surfaces or grace_seconds <= 0:
        return
    deadline = time.monotonic() + grace_seconds
    while any(surface_is_open(surface) for surface in surfaces):
        if time.monotonic() >= deadline:
            return
        time.sleep(0.25)


def close_operation_children(sandbox: Path, coordinator_surface: str) -> tuple[int, list[str]]:
    """Close only exact child surfaces durably bound to this coordinator."""
    closed = 0
    failures: list[str] = []
    surfaces = operation_child_surfaces(sandbox, coordinator_surface)
    for surface in sorted(surfaces):
        try:
            status = close_surface_exact(surface, subprocess.run)
        except (TaskSessionError, OSError):
            failures.append(surface)
        else:
            if status == "closed":
                closed += 1
            elif status != "already-gone":
                failures.append(surface)
    return closed, failures


def settle_operation_surfaces(
    sandbox: Path,
    coordinator_surface: str,
    runtime: str,
    exit_marker: Path,
    *,
    force: bool = False,
) -> tuple[str, int, list[str]]:
    """Stop child creation before enumerating exact operation descendants."""
    coordinator_close = close_surface(
        coordinator_surface, runtime, exit_marker, force=force
    )
    if not force:
        wait_for_operation_children(sandbox, coordinator_surface)
    children_closed, child_failures = close_operation_children(sandbox, coordinator_surface)
    return coordinator_close, children_closed, child_failures


def is_disposable_bookkeeping(path: str, status: str) -> bool:
    if status.startswith("??"):
        # A fresh ingestion manifest is derived provenance inside the disposable
        # clone. Product pages and raw sources are still rejected independently.
        return path == ".raw/.manifest.json"
    return path in DISPOSABLE_VAULT_BOOKKEEPING or re.fullmatch(
        r"wiki(?:/[^/]+)*/_index\.md", path
    ) is not None


def sandbox_cleanup_proof(sandbox: Path, commit: str) -> tuple[bool, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != commit:
        return False, "disposable clone HEAD changed"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if status.returncode != 0:
        return False, "disposable clone status is unreadable"
    dirty = []
    bookkeeping = []
    for line in status.stdout.splitlines():
        path = line[3:]
        if path == ".acceptance-sandbox.json":
            continue
        if path.startswith(".vault-meta/acceptance-worktrees/"):
            continue
        if is_disposable_bookkeeping(path, line[:2]):
            bookkeeping.append(path)
            continue
        dirty.append(line)
    if dirty:
        return False, "disposable clone retained product or vault changes"
    if bookkeeping:
        return True, "product outputs removed; only disposable vault bookkeeping remains"
    return True, "committed HEAD and worktree restored"


def daily_acceptance_cleanup(sandbox: Path, commit: str) -> tuple[bool, str]:
    """Accept one exact disposable session-evidence commit deleted after proof."""

    count = subprocess.run(
        ["git", "rev-list", "--count", f"{commit}..HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if count.returncode != 0 or count.stdout.strip() != "1":
        return False, "daily fixture must create exactly one local evidence commit"
    changed = subprocess.run(
        ["git", "diff", "--name-status", "-z", commit, "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    parts = changed.stdout.rstrip("\0").split("\0") if changed.returncode == 0 else []
    if len(parts) != 2 or parts[0] != "A":
        return False, "daily evidence commit changed unexpected paths"
    path = parts[1]
    if re.fullmatch(r"wiki/meta/sessions/Acceptance[^/]*\.md", path) is None:
        return False, "daily evidence commit used an unexpected fixture path"
    if (sandbox / path).exists():
        return False, "daily fixture session was not removed after verification"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if head.returncode != 0:
        return False, "daily fixture HEAD is unreadable"
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, head.stdout.strip()],
        cwd=sandbox, text=True, capture_output=True, check=False,
    )
    if ancestry.returncode != 0:
        return False, "daily fixture commit is not based on the release candidate"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", path],
        cwd=sandbox, text=True, capture_output=True, check=False,
    )
    deletion = status.stdout.strip("\n")
    if status.returncode != 0 or not deletion.startswith(" D "):
        return False, "daily fixture deletion is not independently proven"
    restored = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if restored.returncode != 0:
        return False, "disposable clone status is unreadable"
    dirty = []
    for line in restored.stdout.splitlines():
        candidate = line[3:]
        if line == deletion or candidate == ".acceptance-sandbox.json":
            continue
        if candidate.startswith(".vault-meta/acceptance-worktrees/"):
            continue
        if is_disposable_bookkeeping(candidate, line[:2]):
            continue
        dirty.append(line)
    if dirty:
        return False, "disposable clone retained product or vault changes"
    return True, "one bounded daily evidence commit verified and product outputs removed"


def run_with_backend(row: dict[str, Any], command: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        command, input=json.dumps(row, ensure_ascii=False) + "\n", text=True,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AcceptanceRunnerError(f"test backend exited {proc.returncode}")
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceRunnerError(f"test backend returned invalid JSON: {exc}") from exc
    return validate_agent_result(row, raw)


def run_live(row: dict[str, Any], scenario: dict[str, Any], fixture: str) -> dict[str, Any]:
    origin = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    if not origin or shutil.which("cmux") is None:
        raise AcceptanceRunnerError("cmux and CMUX_SURFACE_ID are required for live acceptance")
    run_id = str(uuid.uuid4())
    run_dir = STATE_ROOT / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    surface = ""
    cleanup = "sandbox retained for diagnosis"
    prepared_dispatch: dict[str, str] | None = None
    prepared_close: dict[str, str] | None = None
    stage = "setup"
    stage_started = time.monotonic()
    try:
        sandbox, commit = create_sandbox(run_dir)
        install_acceptance_model_overrides(sandbox)
        if row["scenario"] == "dispatch-review-reap":
            install_acceptance_runtime_fixture(sandbox)
        route = load_config(sandbox).runtime_default(row["runtime"])
        if row["skill"] == "dispatch":
            prepared_dispatch = dispatch_acceptance_fixture(sandbox, run_id, row["runtime"])
            fixture = dispatch_fixture_prompt(prepared_dispatch)
        elif row["skill"] == "close":
            prepared_close = close_acceptance_fixture(run_id)
            fixture = close_fixture_prompt(prepared_close)
        scratch_root = scratch_root_for(run_dir)
        scratch_root.mkdir(mode=0o700)
        atomic_json(
            scratch_root / ".acceptance-scratch.json",
            {"schema_version": 1, "run_dir": str(run_dir)},
        )
        outbox = sandbox / ".vault-meta" / "acceptance" / "agent-outbox.json"
        prompt = prompt_text(
            row, scenario, sandbox, outbox, route["model"], route["effort"], commit, fixture,
            prepared_dispatch,
        )
        prompt_path = run_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        spec = {
            "schema_version": 1,
            "row": {key: row[key] for key in ("phase", "skill", "runtime", "scenario", "expected")},
            "runtime": row["runtime"],
            "model": route["model"],
            "effort": route["effort"],
            "sandbox": str(sandbox),
            "scratch_root": str(scratch_root),
            "prompt_file": str(prompt_path),
        }
        if prepared_dispatch is not None:
            spec["dispatch_fixture"] = prepared_dispatch
        spec_path = run_dir / "operation.json"
        atomic_json(spec_path, spec)
        try:
            created = spawn_right(origin)
        except TaskSessionError as exc:
            raise AcceptanceRunnerError(str(exc)) from exc
        surface = created["surface"]
        spec.update(
            {
                "surface": surface,
                "surface_ref": created.get("surface_ref") or "",
                "status": "running",
            }
        )
        atomic_json(spec_path, spec)
        if prepared_dispatch is not None:
            write_dispatch_acceptance_request(
                sandbox,
                prepared_dispatch,
                source_commit=commit,
                coordinator_surface=surface,
                coordinator_model=route["model"],
                coordinator_effort=route["effort"],
            )
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "agent", "--spec", str(spec_path)])
        send_surface(surface, command)
        emit_event(
            "acceptance-cell-stage", actor="setup", session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)}, root=ROOT,
        )
        stage_started = time.monotonic()
        stage = "model-wait"
        raw = wait_for_outbox(
            outbox,
            run_dir / "agent-exit.json",
            int(scenario["timeout_seconds"]),
            surface=surface,
            runtime=row["runtime"],
        )
        if prepared_close is not None:
            exit_deadline = time.monotonic() + 10.0
            while time.monotonic() < exit_deadline and not (run_dir / "agent-exit.json").is_file():
                time.sleep(0.25)
        emit_event(
            "acceptance-cell-stage", actor="model-wait", session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)}, root=ROOT,
        )
        stage_started = time.monotonic()
        stage = "proof"
        result = validate_agent_result(row, raw)
        close, children_closed, child_failures = settle_operation_surfaces(
            sandbox, surface, row["runtime"], run_dir / "agent-exit.json"
        )
        if child_failures:
            result["verdict"] = "blocked"
            result["defect"] = "exact operation child surface close failed"
        elif children_closed and result["verdict"] in {"pass", "n-a"}:
            result["verdict"] = "blocked"
            result["defect"] = f"runner had to close {children_closed} leftover operation child surface(s)"
        elif close != "exact surface closed":
            result["verdict"] = "blocked"
            result["defect"] = close
        elif result["verdict"] in {"pass", "n-a"}:
            if prepared_dispatch is not None:
                clean, proof = dispatch_acceptance_proof(sandbox, commit, prepared_dispatch)
            elif prepared_close is not None:
                clean, proof = close_acceptance_proof(sandbox, prepared_close)
                if clean:
                    clean, cleanup_proof = sandbox_cleanup_proof(sandbox, commit)
                    proof = f"{proof}; {cleanup_proof}"
            elif row["skill"] == "autoresearch":
                clean, proof = autoresearch_acceptance_cleanup(sandbox, commit, surface)
                if clean:
                    clean, cleanup_proof = sandbox_cleanup_proof(sandbox, commit)
                    proof = f"{proof}; {cleanup_proof}"
            elif row["skill"] == "daily":
                clean, proof = daily_acceptance_cleanup(sandbox, commit)
            else:
                clean, proof = sandbox_cleanup_proof(sandbox, commit)
            if not clean:
                result["verdict"] = "blocked"
                result["defect"] = proof
                result["cleanup"] = f"{result['cleanup']}; diagnostic clone retained"[:600]
            else:
                cleanup_started = time.monotonic()
                safe_cleanup(run_dir)
                emit_event(
                    "acceptance-cell-stage", actor="cleanup", session=run_id,
                    counts={"duration_ms": round((time.monotonic() - cleanup_started) * 1000)}, root=ROOT,
                )
                cleanup = "disposable clone removed; exact surface closed"
                result["cleanup"] = f"{result['cleanup']}; {proof}; {cleanup}"[:600]
                result["evidence"] = f"{result['evidence']}; runner proof: {proof}"[:600]
        else:
            result["cleanup"] = f"{result['cleanup']}; diagnostic clone retained; exact surface closed"[:600]
        spec["status"] = "complete" if result["verdict"] in {"pass", "n-a"} else "blocked"
        spec["verdict"] = result["verdict"]
        atomic_json(run_dir / "result.json", result)
        atomic_json(spec_path, spec)
        emit_event(
            "acceptance-cell-stage", actor="proof", session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
            status="ok" if result["verdict"] in {"pass", "n-a"} else "degraded", root=ROOT,
        )
        return result
    except BaseException as exc:
        emit_event(
            "acceptance-cell-stage", actor=stage, session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
            status="error", root=ROOT,
        )
        close = "surface was not created"
        if surface:
            close, _children_closed, _child_failures = settle_operation_surfaces(
                locals().get("sandbox", run_dir / "sandbox"),
                surface,
                row["runtime"],
                run_dir / "agent-exit.json",
                force=isinstance(exc, KeyboardInterrupt),
            )
        spec_path = run_dir / "operation.json"
        if spec_path.is_file():
            try:
                interrupted = read_json(spec_path)
                interrupted["status"] = "interrupted"
                interrupted["cleanup"] = close
                atomic_json(spec_path, interrupted)
            except (AcceptanceRunnerError, OSError):
                pass
        raise


def blocked(row: dict[str, Any], message: str) -> dict[str, Any]:
    try:
        route = load_config(ROOT).runtime_default(row["runtime"])
    except Exception:
        route = {"model": "unknown", "effort": "unknown"}
    override = str(
        os.environ.get(f"LLM_OBSIDIAN_ACCEPTANCE_{str(row['runtime']).upper()}_MODEL") or ""
    ).strip()
    if override:
        route = {**route, "model": override}
    effort_override = str(os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_EFFORT") or "").strip()
    if effort_override:
        route = {**route, "effort": effort_override}
    clean, _ = sanitize(message[:300])
    return result_payload(
        row, verdict="blocked", model=route["model"], effort=route["effort"],
        actual="Live acceptance cell did not complete.", cleanup="diagnostic state retained",
        evidence="runner boundary", defect=clean,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--skills", type=Path, default=SKILLS)
    parser.add_argument("--backend-command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    agent = sub.add_parser("agent", help=argparse.SUPPRESS)
    agent.add_argument("--spec", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "agent":
            return run_agent_process(args.spec.resolve())
        scenarios = load_scenarios(args.scenarios.resolve())
        fixtures = load_skill_fixtures(args.skills.resolve())
        row = validate_row(json.load(sys.stdin), scenarios, fixtures)
        if args.backend_command:
            result = run_with_backend(row, args.backend_command)
        else:
            result = run_live(row, scenarios[row["scenario"]], fixtures[row["skill"]]["fixture"])
    except (AcceptanceRunnerError, SupervisorError, json.JSONDecodeError, OSError, ValueError) as exc:
        if "row" not in locals():
            die(str(exc))
        result = blocked(row, str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A valid fail/blocked result is data for release-acceptance.py, not a
    # runner mechanism failure. The matrix aggregator owns the final exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
