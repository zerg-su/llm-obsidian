#!/usr/bin/env python3
"""Hermetic regression checks for deterministic post-approval dispatch setup."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dispatch-runner.py"
spec = importlib.util.spec_from_file_location("dispatch_runner", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

failures: list[str] = []

ignored = subprocess.run(
    ["git", "check-ignore", ".vault-meta/dispatch-requests/example.json", ".vault-meta/dispatch-runs/example.json"],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok - {name}")
    else:
        failures.append(name)
        print(f"not ok - {name}: {detail}")


def expect_error(name: str, action, needle: str) -> None:
    try:
        action()
    except runner.DispatchError as exc:
        check(name, needle in str(exc), str(exc))
    else:
        check(name, False, "expected DispatchError")


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


with tempfile.TemporaryDirectory(prefix="dispatch-runner-test.") as raw:
    check("dispatch request/run state is gitignored", ignored.returncode == 0 and len(ignored.stdout.splitlines()) == 2)
    tmp = Path(raw)
    vault = tmp / "vault"
    target = tmp / "target"
    worktree = tmp / "worktrees" / "target-fast-dispatch"
    (vault / "wiki" / "plans").mkdir(parents=True)
    (vault / "wiki" / "context").mkdir(parents=True)
    (vault / "skills" / "dispatch" / "references").mkdir(parents=True)
    (vault / "config").mkdir(parents=True)
    (vault / "scripts").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "skills" / "dispatch" / "references" / "task-prompt-template.md",
        vault / "skills" / "dispatch" / "references" / "task-prompt-template.md",
    )
    shutil.copyfile(ROOT / "config" / "model-routing.toml", vault / "config" / "model-routing.toml")
    shutil.copyfile(ROOT / "scripts" / "task_sessions.py", vault / "scripts" / "task_sessions.py")
    (vault / "wiki" / "context" / "Dispatch Context.md").write_text("# Context\n", encoding="utf-8")
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text(
        "---\ntype: plan\nstatus: pending\nsession_id: unit-session\n---\n\n# Approved\n",
        encoding="utf-8",
    )
    target.mkdir()
    git("init", "-b", "main", cwd=target)
    git("config", "user.email", "dispatch@example.invalid", cwd=target)
    git("config", "user.name", "Dispatch Test", cwd=target)
    (target / "README.md").write_text("fixture\n", encoding="utf-8")
    git("add", "README.md", cwd=target)
    git("commit", "-m", "init", cwd=target)

    request_id = str(uuid.uuid4())
    raw_request = {
        "schema_version": 1,
        "request_id": request_id,
        "task_name": "fast-dispatch",
        "description": "Create one bounded fixture and verify it.",
        "vault_root": str(vault),
        "target_repo": str(target),
        "worktree": str(worktree),
        "branch": "task/fast-dispatch",
        "base_branch": "main",
        "plan_file": str(plan),
        "origin_surface": "11111111-1111-4111-8111-111111111111",
        "origin_session": "unit-session",
        "session_route": {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "source": "unit-test",
        },
        "executor": {},
        "wiki_context": [
            {"title": "Dispatch Context", "summary": "prior pipeline decision"},
        ],
        "suggested_agents": [],
        "reap": {"type": "session", "title": "Fast dispatch result"},
    }

    request = runner.validate_request(raw_request)
    config = runner.load_dispatch_config(vault, target)
    session, effective = runner.resolved_routes(request, persist=False)
    prompt = runner.render_task_prompt(request, config)
    check("route inherits captured runtime", effective["runtime"] == "codex")
    check("route inherits captured model", effective["model"] == "gpt-5.6-sol")
    check(
        "runner tells coordinator to return idle without polling",
        runner.COORDINATOR_ACTION == "return-to-idle-without-polling",
    )
    check("route preview does not persist session state", not (vault / ".vault-meta/session-routing/unit-session.json").exists())
    check("prompt keeps approved plan branch", "## Approved plan (already reviewed — execute)" in prompt)
    check("prompt removes classic approval branch", "## IMPORTANT: plan-first workflow" not in prompt)
    check("prompt renders exact context", "[[Dispatch Context]] — prior pipeline decision" in prompt)
    check("prompt omits empty agents", "## Suggested sub-agents" not in prompt)
    check("prompt has no branch control markers", "<!-- BRANCH" not in prompt)
    check("prompt binds reap skill", "$llm-obsidian:reap" in prompt)
    check("classic dispatch defaults to split placement", request["placement"] == "split")
    workspace_raw = json.loads(json.dumps(raw_request))
    workspace_raw["placement"] = "workspace"
    workspace_request = runner.validate_request(workspace_raw)
    workspace_prompt = runner.render_task_prompt(workspace_request, config)
    check(
        "workspace dispatch is explicit and rewrites coordinator navigation",
        workspace_request["placement"] == "workspace"
        and "the coordinator workspace" in workspace_prompt
        and "the left wiki split" not in workspace_prompt,
    )
    invalid_placement = json.loads(json.dumps(raw_request))
    invalid_placement["placement"] = "focused"
    expect_error(
        "dispatch placement never guesses from focus",
        lambda: runner.validate_request(invalid_placement),
        "split or workspace",
    )
    check(
        "unattended finalization uses the code-owned reap sender",
        "do not depend on runtime skill discovery" in prompt
        and "skills/reap-send/scripts/send_reap.py --worktree ." in prompt,
    )
    check(
        "prompt trusts one successful supervised review transition",
        "drive ... --apply-action" in prompt
        and "run that command exactly" in prompt
        and "do not\n   re-read operation/review artifacts" in prompt
        and "call a separate `finish`" in prompt,
    )

    tracked = json.loads(json.dumps(raw_request))
    tracked["session_route"]["source"] = "tracked-default"
    expect_error(
        "tracked-only session route fails closed",
        lambda: runner.validate_request(tracked),
        "host-confirmed",
    )
    wrong_branch = json.loads(json.dumps(raw_request))
    wrong_branch["branch"] = "task/something-else"
    expect_error(
        "branch drift fails closed",
        lambda: runner.validate_request(wrong_branch),
        "task/<task_name>",
    )
    missing_context = json.loads(json.dumps(raw_request))
    missing_context["wiki_context"][0]["title"] = "Missing Page"
    expect_error(
        "missing context link fails closed",
        lambda: runner.validate_request(missing_context),
        "must exist exactly once",
    )

    runner.create_worktree(request)
    identity = runner.initialize_task(request)
    meta = runner.write_task_files(
        request,
        config,
        session,
        effective,
        identity,
        {"surface_id": raw_request["origin_surface"], "surface_ref": "surface:1"},
        {"surface": "22222222-2222-4222-8222-222222222222", "surface_ref": "surface:2"},
    )
    check("runner writes v3 metadata", meta["version"] == 3 and meta["task_id"] == request_id)
    check("runner writes exact task handoff", (worktree / ".task-cmux-surface").read_text().strip() == meta["task_surface"])
    check("runner writes one plan branch", (worktree / ".task-prompt.md").read_text().count("## Approved plan") == 1)
    check("runner metadata validates", runner.normalize_task_contract(meta)["interaction_policy"] == "unattended")
    check("runner metadata records split placement", meta["surface_policy"]["placement"] == "split")
    workspace_meta = runner.write_task_files(
        workspace_request,
        config,
        session,
        effective,
        identity,
        {"surface_id": raw_request["origin_surface"], "surface_ref": "surface:1"},
        {
            "surface": "22222222-2222-4222-8222-222222222222",
            "surface_ref": "surface:2",
            "workspace": "44444444-4444-4444-8444-444444444444",
            "workspace_ref": "workspace:22",
            "window": "55555555-5555-4555-8555-555555555555",
            "window_ref": "window:7",
        },
    )
    check(
        "workspace dispatch persists exact container ownership",
        workspace_meta["surface_policy"]["placement"] == "workspace"
        and workspace_meta["task_workspace"] == "44444444-4444-4444-8444-444444444444"
        and workspace_meta["task_window"] == "55555555-5555-4555-8555-555555555555",
    )

    duplicate = json.loads(json.dumps(raw_request))
    expect_error(
        "existing worktree fails before another spawn",
        lambda: runner.validate_request(duplicate),
        "worktree already exists",
    )

    second_raw = json.loads(json.dumps(raw_request))
    second_raw["request_id"] = str(uuid.uuid4())
    second_raw["task_name"] = "state-only"
    second_raw["branch"] = "task/state-only"
    second_raw["worktree"] = str(tmp / "worktrees" / "state-only")
    second = runner.validate_request(second_raw)
    spec_hash = "a" * 64
    state_path, prior = runner.begin_run(second, spec_hash)
    check("new run claims exact request once", prior is None and json.loads(state_path.read_text())["status"] == "preparing")
    expect_error(
        "preparing request cannot duplicate a surface",
        lambda: runner.begin_run(second, spec_hash),
        "already preparing",
    )
    result = {"schema_version": 1, "status": "launched", "task_surface": "exact"}
    runner.atomic_json(state_path, {
        "schema_version": 1,
        "request_id": second["request_id"],
        "request_sha256": spec_hash,
        "task_name": second["task_name"],
        "status": "launched",
        "result": result,
    })
    _, replay = runner.begin_run(second, spec_hash)
    check("launched request replays typed result", replay == result)
    (Path(second_raw["worktree"])).mkdir(parents=True)
    plan.write_text("---\ntype: plan\nstatus: executed\nsession_id: unit-session\n---\n", encoding="utf-8")
    check(
        "completed replay bypasses mutable worktree and plan state",
        runner.completed_replay(second_raw, spec_hash) == {**result, "idempotent": True},
    )
    expect_error(
        "request UUID cannot be reused with changed bytes",
        lambda: runner.begin_run(second, "b" * 64),
        "different bytes",
    )

if failures:
    print(f"\n{len(failures)} dispatch runner test(s) failed")
    raise SystemExit(1)
print("\nAll dispatch runner tests passed.")
