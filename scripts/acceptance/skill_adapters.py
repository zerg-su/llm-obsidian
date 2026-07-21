"""Skill-owned fixtures, prompts, proofs, and cleanup."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from .contracts import AcceptanceRunnerError, atomic_json, read_json
from .sandbox import commit_file, git_output, run_checked
from .scenario_adapters import is_disposable_bookkeeping

ROOT = Path(__file__).resolve().parents[2]
AUTORESEARCH_OUTPUT_LIMIT = 15

sys.path.insert(0, str(ROOT / "scripts"))
from vault_schema import FrontmatterError, parse_frontmatter, split_frontmatter  # noqa: E402

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

def review_acceptance_fixture(
    sandbox: Path,
    run_id: str,
    runtime: str,
    source_commit: str,
    session_id: str,
) -> dict[str, str]:
    """Provision one committed, approved v3 task before testing review skills."""

    token = run_id.split("-", 1)[0]
    task_name = f"acceptance-review-{token}"
    branch = f"task/{task_name}"
    fixture_rel = f"acceptance-review-{token}.py"
    fixture_text = (
        "def render(ready: bool) -> str:\n"
        "    status = \"ready\" if ready else \"not ready\"\n"
        "    return f\"{status}\"\n"
    )
    plan_rel = f"wiki/plans/{date.today().isoformat()}-{task_name}.md"
    plan_path = sandbox / plan_rel
    plan_title = f"Acceptance review {token} plan"
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

Create only `{fixture_rel}` as the disposable review target and commit it once.
Resolve its known non-blocking redundant-f-string warning if the required
review finds it. The review lifecycle is verification of this scope, not another
product action. Do not merge, push, publish, deploy, or expand scope.
"""
    run_checked(
        [sys.executable, str(sandbox / "scripts" / "vault-write.py"), "--output", "json"],
        cwd=sandbox,
        input_text=json.dumps(
            {
                "schema_version": 1,
                "request_id": f"acceptance-review-{token}",
                "actor": "acceptance",
                "session": session_id,
                "pages": [{"op": "create", "path": plan_rel, "content": plan_text}],
            },
            ensure_ascii=False,
        ),
    )
    nested_worktree = (
        sandbox / ".vault-meta" / "acceptance-worktrees" / f"sandbox-{task_name}"
    )
    nested_worktree.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            "git", "-C", str(sandbox), "worktree", "add", "-b", branch,
            str(nested_worktree), source_commit,
        ],
        cwd=sandbox,
    )
    (nested_worktree / fixture_rel).write_text(fixture_text, encoding="utf-8")
    run_checked(["git", "add", "--", fixture_rel], cwd=nested_worktree)
    run_checked(
        [
            "git", "-c", "user.name=Acceptance Runner",
            "-c", "user.email=acceptance@example.invalid", "commit", "-m",
            "test: add review acceptance fixture",
        ],
        cwd=nested_worktree,
    )
    identity = json.loads(
        run_checked(
            [
                sys.executable,
                str(sandbox / "scripts" / "task_sessions.py"),
                "--vault-root", str(sandbox),
                "init-task", "--worktree", str(nested_worktree),
                "--task-id", run_id, "--runtime", runtime,
                "--session-id", session_id,
            ],
            cwd=sandbox,
        )
    )
    fixture = {
        "fixture_kind": "review",
        "task_name": task_name,
        "branch": branch,
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "fixture_rel": fixture_rel,
        "fixture_text": fixture_text,
        "fixture_commit": run_checked(
            ["git", "rev-parse", "HEAD"], cwd=nested_worktree
        ).strip(),
        "source_commit": source_commit,
        "nested_worktree": str(nested_worktree.resolve()),
        "project_id": str(identity["project_id"]),
        "task_id": str(identity["task_id"]),
        "session_id": session_id,
        "runtime": runtime,
        "result_title": f"Acceptance review {token} result",
        "review_script": str(
            (sandbox / "skills" / "review-dispatch" / "scripts" / "spawn_review.py").resolve()
        ),
    }
    atomic_json(
        sandbox / ".vault-meta" / "acceptance" / "review-fixture.json", fixture
    )
    return fixture

def bind_review_acceptance_fixture(
    sandbox: Path,
    fixture: dict[str, str],
    surface: str,
    route: dict[str, Any],
    config: Any,
) -> None:
    """Bind the prepared task to the exact live coordinator surface."""

    worktree = Path(fixture["nested_worktree"])
    handoffs = {
        ".task-cmux-surface": surface,
        ".wiki-cmux-surface": surface,
        ".wiki-agent-runtime": fixture["runtime"],
        ".wiki-reap-command": "$llm-obsidian:reap",
        ".task-reap-send-skill": "$llm-obsidian:reap-send",
        ".task-review-skill": "$llm-obsidian:review-dispatch",
        ".task-review-send-skill": "$llm-obsidian:review-send",
    }
    for name, value in handoffs.items():
        (worktree / name).write_text(value + "\n", encoding="utf-8")
    (worktree / ".task-prompt.md").write_text(
        f"# Task: {fixture['task_name']}\n\n"
        f"Create only `{fixture['fixture_rel']}` as specified by the approved plan. "
        "The required review gate is lifecycle verification, not product-scope drift.\n",
        encoding="utf-8",
    )
    routing_session = {
        "runtime": route["runtime"],
        "model": route["model"],
        "effort": route["effort"],
        "source": "acceptance-fixture",
    }
    meta = {
        "version": 3,
        "project_id": fixture["project_id"],
        "task_id": fixture["task_id"],
        "task_name": fixture["task_name"],
        "wiki_runtime": fixture["runtime"],
        "executor_runtime": fixture["runtime"],
        "runtime": fixture["runtime"],
        "origin_session": fixture["session_id"],
        "spawned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wiki_surface": surface,
        "task_surface": surface,
        "target_repo": str(sandbox),
        "vault_root": str(sandbox),
        "branch": fixture["branch"],
        "base_branch": fixture["source_commit"],
        "wiki_reap_command": "$llm-obsidian:reap",
        "reap_send_skill": "$llm-obsidian:reap-send",
        "review_skill": "$llm-obsidian:review-dispatch",
        "review_send_skill": "$llm-obsidian:review-send",
        "routing": {
            "schema_version": 1,
            "session": routing_session,
            "effective": {
                "schema_version": 1,
                **routing_session,
                "role": "dispatch",
                "source": ["session:acceptance-fixture"],
                "config_sha256": config.fingerprint,
                "local_override": config.local_override,
            },
        },
        "plan_file": fixture["plan_path"],
        "approved_plan_sha256": fixture["plan_sha256"],
        "interaction_policy": "unattended",
        "review_policy": {
            "mode": "light",
            "max_verify_iterations": 2,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": fixture["result_title"],
        },
        "surface_policy": {"auto_close": True},
        "watchdog_policy": {
            "enabled": True,
            "poll_seconds": 30,
            "warn_after_seconds": 900,
            "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push", "deploy", "publish", "delete-worktree", "delete-branch",
            "expand-scope",
        ],
        "suggested_agents": [],
    }
    atomic_json(worktree / ".task-meta.json", meta)
    run_checked(
        [sys.executable, str(sandbox / "scripts" / "task_contract.py"), "validate"],
        cwd=worktree,
    )

def review_fixture_prompt(fixture: dict[str, str], skill: str) -> str:
    emphasis = (
        "The opposite-runtime reviewer must exercise review-send exactly once and publish its "
        "typed callback before you resolve it. "
        if skill == "review-send"
        else "Exercise review-dispatch start/receive and its exact typed finish path. "
    )
    return (
        f"Use the runner-prepared approved task at `{fixture['nested_worktree']}` and its existing "
        f"commit `{fixture['fixture_commit']}`. Do not create a plan, task, branch, worktree, metadata, "
        "or another fixture commit, and do not enter Plan Mode or ask for approval. "
        f"{emphasis}Start one light opposite-runtime review with "
        f"`python3 {fixture['review_script']} start --light --worktree {fixture['nested_worktree']}`; "
        "do not pass same-model, reviewer-runtime, model, or effort "
        "overrides. Return idle after each launch and let typed callbacks start later turns; never poll. "
        "A real reviewer may either approve the fixture or report its known redundant-f-string warning; "
        "never fabricate a finding to force a verify round. On approve, make no product change and drive "
        "finish directly. On a warning or nit, return the prepared `status` value directly, commit only "
        "that behavior-preserving fix, verify in the same reviewer lane, then drive approval and finish. "
        "Either validated path is a pass. Once the code-owned drive command reports exit 0 with `applied=true`, "
        "treat that durable operation result as authoritative and publish the acceptance outbox "
        "immediately; do not wait for, inspect, or poll the already-finished reviewer surface. Leave the "
        "task worktree, branch, plan, registry, and review artifacts for "
        "runner proof and cleanup."
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
    coordinator_model: str, coordinator_effort: str, placement: str = "split",
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
        "placement": placement,
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
