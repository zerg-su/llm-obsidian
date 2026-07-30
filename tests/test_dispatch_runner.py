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
from harness.contracts import OwnedResources
from harness.runtime_sessions import RuntimeSessionResult
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.workflows.dispatch import operation_spec

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
    shutil.copytree(ROOT / "scripts" / "harness", vault / "scripts" / "harness")
    shutil.copyfile(
        ROOT / "skills" / "dispatch" / "references" / "task-prompt-template.md",
        vault / "skills" / "dispatch" / "references" / "task-prompt-template.md",
    )
    shutil.copyfile(ROOT / "config" / "model-routing.toml", vault / "config" / "model-routing.toml")
    shutil.copyfile(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
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
    review = runner.review_policy(request, config)
    lifecycle_contract = runner.lifecycle_contract(review)
    check(
        "route preview consumes the compiled lifecycle catalog",
        lifecycle_contract["pipeline"] == "lifecycle/default@1.0.0"
        and len(lifecycle_contract["definition_sha256"]) == 64
        and "state-free reconciliation"
        in lifecycle_contract["summary"],
    )
    check(
        "route preview renders the exact review and execution envelope",
        "Review: mode=simple, verification-iterations=1"
        in lifecycle_contract["summary"]
        and "deadline=1800s" in lifecycle_contract["summary"]
        and "cmux target scope=policy-only"
        in lifecycle_contract["summary"],
    )
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
        "workspace dispatch remains an explicit placement",
        workspace_request["placement"] == "workspace"
        and "scripts/harness-cli.py" in workspace_prompt,
    )
    invalid_placement = json.loads(json.dumps(raw_request))
    invalid_placement["placement"] = "focused"
    expect_error(
        "dispatch placement never guesses from focus",
        lambda: runner.validate_request(invalid_placement),
        "split or workspace",
    )
    check(
        "unattended finalization uses the internal callback broker",
        "internal callback broker" in prompt,
    )
    check(
        "prompt delegates lifecycle mechanics to harness",
        "scripts/harness-cli.py" in prompt
        and "do not orchestrate cmux/model commands manually" in prompt,
    )
    check(
        "default dispatch requires automatic simple review",
        "automatic review gate" in prompt
        and runner.harness_request(request, config, effective).review.mode
        == "simple",
        prompt,
    )
    check(
        "task prompt leaves automatic review launch to the harness",
        "write `.task-summary.json` to trigger the automatic review gate"
        in prompt
        and "task-review-runner.py run" not in prompt,
        prompt,
    )

    expert_raw = json.loads(json.dumps(raw_request))
    expert_raw["review"] = {
        "mode": "deep",
        "cross_model": True,
        "runtime": "claude",
        "model": "fable",
        "effort": "xhigh",
    }
    expert = runner.validate_request(expert_raw)
    expert_policy = runner.review_policy(expert, config)
    check(
        "dispatch accepts the complete expert review override",
        expert_policy
        == runner.ReviewPolicy(
            depth="deep",
            cross_model=True,
            enabled=True,
            runtime="claude",
            model="fable",
            effort="xhigh",
            verification_profile="scoped",
            verification_profile_sha256=runner.load_profiles(
                vault / "config" / "verification-profiles.toml"
            )["scoped"].sha256,
        ),
    )
    base_spec = operation_spec(runner.harness_request(request, config, effective))
    expert_spec = operation_spec(runner.harness_request(expert, config, effective))
    check(
        "expert review override participates in operation identity",
        base_spec.idempotency_key != expert_spec.idempotency_key,
    )
    check(
        "dispatch operation binds the exact compiled lifecycle hash",
        base_spec.contract_sha256
        == lifecycle_contract["definition_sha256"],
    )
    no_review_conflict = json.loads(json.dumps(raw_request))
    no_review_conflict["review"] = {
        "mode": "skip",
        "cross_model": True,
    }
    expect_error(
        "no-review conflicts fail closed",
        lambda: runner.review_policy(
            runner.validate_request(no_review_conflict), config
        ),
        "skip review cannot carry",
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
    check(
        "runner writes v3 metadata",
        meta["version"] == 3
        and meta["task_id"] == request_id
        and meta["worktree"] == str(request["worktree"]),
    )
    check(
        "runner binds automatic review to an exact verification profile",
        meta["review_policy"]["verification_profile"] == "scoped"
        and len(meta["review_policy"]["verification_profile_sha256"]) == 64
        and meta["review_policy"]["max_verify_iterations"] == 1,
    )
    check(
        "runner writes the deterministic simple review preset",
        meta["review_policy"]["mode"] == "simple"
        and meta["review_policy"]["cross_model"] is False
        and meta["review_policy"]["runtime"] == ""
        and meta["review_policy"]["model"] == ""
        and meta["review_policy"]["effort"] == "",
    )
    v3_schema = json.loads(
        (ROOT / "schemas" / "task-meta-v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "writer output has exact strict v3 schema parity",
        set(meta) == set(v3_schema["required"])
        and set(meta) <= set(v3_schema["properties"])
        and v3_schema["additionalProperties"] is False,
    )
    expert_meta = runner.write_task_files(
        expert,
        config,
        session,
        effective,
        identity,
        {"surface_id": raw_request["origin_surface"], "surface_ref": "surface:1"},
        {"surface": "22222222-2222-4222-8222-222222222222", "surface_ref": "surface:2"},
    )
    check(
        "runner persists exact expert review preset and deep budget",
        expert_meta["review_policy"]
        == {
            "mode": "deep",
            "cross_model": True,
            "runtime": "claude",
            "model": "fable",
            "effort": "xhigh",
            "max_verify_iterations": 2,
            "verification_profile": "scoped",
            "verification_profile_sha256": (
                meta["review_policy"]["verification_profile_sha256"]
            ),
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
    )
    invalid_budget = json.loads(json.dumps(expert_meta))
    invalid_budget["review_policy"]["max_verify_iterations"] = 1
    try:
        runner.normalize_task_contract(invalid_budget)
    except runner.ContractError as exc:
        check(
            "v3 deep review budget fails closed",
            "exactly 2" in str(exc),
            str(exc),
        )
    else:
        check("v3 deep review budget fails closed", False)
    invalid_skip = json.loads(json.dumps(meta))
    invalid_skip["review_policy"].update(
        {"mode": "skip", "cross_model": True, "max_verify_iterations": 0}
    )
    try:
        runner.normalize_task_contract(invalid_skip)
    except runner.ContractError as exc:
        check(
            "v3 skip metadata rejects review overrides",
            "cannot carry" in str(exc),
            str(exc),
        )
    else:
        check("v3 skip metadata rejects review overrides", False)
    invalid_extra = json.loads(json.dumps(meta))
    invalid_extra["unowned_extension"] = True
    try:
        runner.normalize_task_contract(invalid_extra)
    except runner.ContractError as exc:
        check(
            "v3 executable contract rejects schema extensions",
            "unknown fields" in str(exc),
            str(exc),
        )
    else:
        check("v3 executable contract rejects schema extensions", False)
    check("runner writes exact task handoff", (worktree / ".task-cmux-surface").read_text().strip() == meta["task_surface"])
    check(
        "runner binds task commands to the exact origin session",
        (worktree / ".task-origin-session").read_text().strip() == meta["origin_session"],
    )
    detected = subprocess.run(
        [str(ROOT / "scripts" / "current-session-id.sh")],
        cwd=worktree,
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "CLAUDE_CODE_SESSION_ID": "native-task-session",
            "CODEX_THREAD_ID": "native-task-thread",
        },
        check=False,
    )
    summary = worktree / ".task-summary.json"
    summary.write_text(
        json.dumps({"type": "session", "title": "Fast dispatch result"}) + "\n",
        encoding="utf-8",
    )
    handoff = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "task_contract.py"),
            "check-handoff",
            "--meta",
            str(worktree / ".task-meta.json"),
            "--summary",
            str(summary),
            "--current-session",
            detected.stdout.strip(),
        ],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "native task thread completes the exact v3 handoff",
        detected.returncode == 0
        and detected.stdout.strip() == "unit-session"
        and handoff.returncode == 0,
        detected.stderr + handoff.stderr,
    )
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
    harness_raw = json.loads(json.dumps(raw_request))
    harness_raw["request_id"] = str(uuid.uuid4())
    harness_raw["task_name"] = "harness-start"
    harness_raw["branch"] = "task/harness-start"
    harness_raw["worktree"] = str(tmp / "worktrees" / "harness-start")
    harness_request = runner.validate_request(harness_raw)
    class FakeRuntime:
        def __init__(self, root: Path) -> None:
            self.store = OperationStore(root)
            self.requests: list[object] = []

        def start(self, runtime_request, *, on_surface_opened=None):
            self.requests.append(runtime_request)
            record = self.store.create(
                runtime_request.spec,
                lane_id=runtime_request.lane_id,
                run_id=runtime_request.run_id,
            )
            self.store.transition(
                record.spec.owner_id, record.spec.operation_id, "preflight"
            )
            self.store.transition(
                record.spec.owner_id, record.spec.operation_id, "starting"
            )
            supervisor = OperationSupervisor(
                self.store, record.spec.owner_id, record.spec.operation_id
            )
            supervisor.bind_resources(
                OwnedResources(
                    "33333333-3333-4333-8333-333333333333"
                )
            )
            opened = RuntimeSessionResult(
                supervisor.read(),
                "surface-opened",
                surface_ref="surface:3",
                workspace_id="44444444-4444-4444-8444-444444444444",
                workspace_ref="workspace:4",
                window_id="55555555-5555-4555-8555-555555555555",
                window_ref="window:5",
            )
            if on_surface_opened is not None:
                on_surface_opened(opened)
            supervisor.transition("running")
            final = supervisor.transition("awaiting-callback")
            return RuntimeSessionResult(
                final,
                "started",
                surface_ref=opened.surface_ref,
                workspace_id=opened.workspace_id,
                workspace_ref=opened.workspace_ref,
                window_id=opened.window_id,
                window_ref=opened.window_ref,
            )

    fake_runtime = FakeRuntime(vault / ".vault-meta" / "harness")
    original_sync = runner.sync_codex_profile
    original_log = runner.dispatch_log
    runner.sync_codex_profile = lambda *_args, **_kwargs: None
    runner.dispatch_log = lambda *_args, **_kwargs: None
    try:
        harness_result = runner.start(
            harness_request, "c" * 64, runtime_manager=fake_runtime
        )
        harness_replay = runner.start(
            harness_request, "c" * 64, runtime_manager=fake_runtime
        )
    finally:
        runner.sync_codex_profile = original_sync
        runner.dispatch_log = original_log
    harness_record = OperationStore(vault / ".vault-meta/harness").read(
        harness_request["request_id"], harness_request["request_id"]
    )
    check(
        "public start executes one durable harness launch",
        len(fake_runtime.requests) == 1
        and harness_result["harness"]["run_id"] == harness_record.run_id
        and harness_replay == harness_result
        and harness_record.state == "awaiting-callback",
    )
    check(
        "dispatch binds the product root for provider permission compilation",
        fake_runtime.requests[0].product_root
        == Path(harness_request["worktree"]).resolve(),
    )
    spec_hash = "a" * 64
    state_path, prior = runner.begin_run(second, spec_hash)
    check("new run claims exact request once", prior is None and json.loads(state_path.read_text())["status"] == "preparing")
    expect_error(
        "preparing request cannot duplicate a surface",
        lambda: runner.begin_run(second, spec_hash),
        "already preparing",
    )
    result = {
        "schema_version": 1,
        "status": "launched",
        "task_surface": "exact",
        "harness": {
            "owner_id": second["request_id"],
            "operation_id": second["request_id"],
            "lane_id": "state-only-lane",
            "run_id": "state-only-run",
        },
    }
    state_lifecycle = runner.harness_request(second, config, effective)
    OperationStore(vault / ".vault-meta/harness").create(
        operation_spec(state_lifecycle),
        lane_id=result["harness"]["lane_id"],
        run_id=result["harness"]["run_id"],
    )
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
