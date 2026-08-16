#!/usr/bin/env python3
"""Hermetic regression checks for deterministic post-approval dispatch setup."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dispatch-runner.py"
spec = importlib.util.spec_from_file_location("dispatch_runner", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
review_spec = importlib.util.spec_from_file_location(
    "task_review_runner_public", ROOT / "scripts" / "task-review-runner.py"
)
assert review_spec and review_spec.loader
review_runner = importlib.util.module_from_spec(review_spec)
review_spec.loader.exec_module(review_runner)
from harness.contracts import OwnedResources
from harness.finalization_ledger import FinalizationLedger
from harness.split_activation import split_child_policy, split_child_policy_payload
from harness.split_contracts import (
    ChildBudget,
    FrozenSplitBudget,
    JoinSpec,
    ParentContract,
    SplitCandidate,
    build_split_preview,
)
from harness.review_finalization import task_finalization_policy
from harness.runtime_sessions import RuntimeSessionResult
from harness.store import OperationStore
from harness.dashboard_facade import DashboardLaunchReceipt
from harness.supervisor import OperationSupervisor
from harness.workflows.dispatch import operation_spec
from task_contract import normalize as normalize_task_meta
from task_review_flow import _exact_head_attempt_enabled
from approved_plan_snapshot import bind_approved_plan_snapshot

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
    (vault / "skills" / "review").mkdir(parents=True)
    (vault / "config").mkdir(parents=True)
    (vault / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / "scripts" / "harness", vault / "scripts" / "harness")
    shutil.copyfile(
        ROOT / "skills" / "dispatch" / "references" / "task-prompt-template.md",
        vault / "skills" / "dispatch" / "references" / "task-prompt-template.md",
    )
    shutil.copyfile(
        ROOT / "skills" / "review" / "SKILL.md",
        vault / "skills" / "review" / "SKILL.md",
    )
    shutil.copyfile(ROOT / "config" / "model-routing.toml", vault / "config" / "model-routing.toml")
    shutil.copyfile(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    shutil.copyfile(
        ROOT / "config" / "harness.toml",
        vault / "config" / "harness.toml",
    )
    shutil.copyfile(ROOT / "scripts" / "task_sessions.py", vault / "scripts" / "task_sessions.py")
    shutil.copyfile(
        ROOT / "scripts" / "task_session_contracts.py",
        vault / "scripts" / "task_session_contracts.py",
    )
    shutil.copyfile(
        ROOT / "scripts" / "task_session_cmux_layout.py",
        vault / "scripts" / "task_session_cmux_layout.py",
    )
    shutil.copyfile(
        ROOT / "scripts" / "task_session_store.py",
        vault / "scripts" / "task_session_store.py",
    )
    shutil.copyfile(
        ROOT / "scripts" / "task_session_store_io.py",
        vault / "scripts" / "task_session_store_io.py",
    )
    (vault / "wiki" / "context" / "Dispatch Context.md").write_text("# Context\n", encoding="utf-8")
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text(
        "---\ntype: plan\nstatus: pending\nsession_id: unit-session\n---\n\n"
        "# Approved\n\n## Outcome Contract\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Complete the dispatched fixture.",'
        '"success_evidence":[{"evidence_id":"fixture-green",'
        '"observable":"The deterministic dispatch fixture passes."}],'
        '"non_goals":["No external effects."]}\n```\n',
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
        "reap": {"type": "repo-touch", "title": "Fast dispatch result"},
    }

    current_context = dict(raw_request)
    current_context.pop("origin_session")
    with mock.patch(
        "dispatch_setup.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[str(vault / "scripts" / "current-session-id.sh")],
            returncode=0,
            stdout="materialized-session\n",
            stderr="",
        ),
    ):
        current_context = runner.materialize_current_context(current_context)
    check(
        "dispatch materializes the current coordinator session",
        current_context["origin_session"] == "materialized-session",
    )

    request = runner.validate_request(raw_request)

    observer_argv = runner.observer_command(request["vault_root"], request_id)
    resolved_vault = Path(request["vault_root"])
    builtin_spec = tmp / f"{request_id}.json"
    builtin_spec.write_text(json.dumps(raw_request, sort_keys=True), encoding="utf-8")
    builtin_validate = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--spec", str(builtin_spec)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    validate_payload = (
        json.loads(builtin_validate.stdout)
        if builtin_validate.returncode == 0
        else {}
    )
    observer_echo = validate_payload.get("observer") or {}
    check(
        "validate exposes the exact temporary observer command before root creation",
        builtin_validate.returncode == 0
        and observer_echo.get("temporary") == request_id
        and observer_echo.get("argv") == observer_argv
        and observer_argv[observer_argv.index("--temporary") + 1] == request_id
        and observer_argv[observer_argv.index("--vault") + 1]
        == str(resolved_vault)
        and observer_argv[observer_argv.index("--store") + 1]
        == str(resolved_vault / ".vault-meta" / "harness")
        and str(resolved_vault / "scripts" / "harness-dashboard.py")
        in observer_argv
        and "open" in observer_argv
        and "--surface" not in observer_argv
        and "--all" not in observer_argv,
        builtin_validate.stderr,
    )
    check(
        "exposing the observer command has no provider, store, or worktree effect",
        not worktree.exists()
        and not (vault / ".vault-meta" / "harness").exists(),
    )

    missing_outcome = json.loads(json.dumps(raw_request))
    missing_plan = vault / "wiki" / "plans" / "missing-outcome.md"
    missing_plan.write_text(
        "---\ntype: plan\nstatus: pending\n---\n\n# Missing\n",
        encoding="utf-8",
    )
    missing_outcome["plan_file"] = str(missing_plan)
    expect_error(
        "dispatch rejects a missing Outcome Contract before effects",
        lambda: runner.validate_request(missing_outcome),
        "exactly one Outcome Contract",
    )
    config = runner.load_dispatch_config(vault, target)
    session, effective = runner.resolved_routes(request, persist=False)
    target_dispatch = target / ".codex" / "dispatch-env.toml"
    target_dispatch.parent.mkdir(parents=True, exist_ok=True)
    target_dispatch.write_text(
        '[codex_dispatch]\nprofile = "target-mcp"\n',
        encoding="utf-8",
    )
    target_config = runner.load_dispatch_config(vault, target)
    with mock.patch("dispatch_workspace.run_command") as sync_command:
        runner.sync_codex_profile(
            request,
            target_config,
            {"runtime": "codex"},
        )
    target_gateway = target / "scripts" / "mcp-gateway" / "mcp-gateway.sh"
    check(
        "target-local dispatch profile sync uses the target gateway root",
        len(sync_command.call_args_list) == 2
        and all(
            Path(call.args[0][0]).resolve() == target_gateway.resolve()
            and Path(call.kwargs["cwd"]).resolve() == target.resolve()
            for call in sync_command.call_args_list
        ),
        str(sync_command.call_args_list),
    )
    target_dispatch.unlink()
    alias_raw = json.loads(json.dumps(raw_request))
    alias_raw["wiki_context"][0]["title"] = "Human display title"
    alias_raw["wiki_context"][0]["context_path"] = (
        "wiki/context/Dispatch Context.md"
    )
    alias_request = runner.validate_request(alias_raw)
    child_fixture = {
        "surface": "22222222-2222-4222-8222-222222222222",
    }
    with mock.patch(
        "dispatch_workspace.run_command",
        return_value=subprocess.CompletedProcess([], 0, "", ""),
    ) as log_write:
        runner.dispatch_log(alias_request, effective, child_fixture)
    logged = json.loads(log_write.call_args.kwargs["input_text"])["log_entry"]
    check(
        "dispatch log links the exact context stem with the display title as alias",
        "[[Dispatch Context|Human display title]]" in logged
        and "[[Human display title]]" not in logged,
        logged,
    )
    context_file = vault / "wiki" / "context" / "Dispatch Context.md"
    context_bytes = context_file.read_bytes()
    context_file.unlink()
    try:
        with mock.patch("dispatch_workspace.run_command") as missing_write:
            expect_error(
                "dispatch log fails closed if the exact context target disappears",
                lambda: runner.dispatch_log(
                    alias_request,
                    effective,
                    child_fixture,
                ),
                "must exist exactly",
            )
            check(
                "missing exact context target reaches no vault write",
                missing_write.call_count == 0,
                str(missing_write.call_args_list),
            )
    finally:
        context_file.write_bytes(context_bytes)
    prompt = runner.render_task_prompt(request, config)
    (vault / ".vault-meta").mkdir(exist_ok=True)
    frozen_prompt_request = bind_approved_plan_snapshot(request)
    original_plan = plan.read_bytes()
    plan.write_text(
        "# Drifted source\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Source drift only.",'
        '"success_evidence":[{"evidence_id":"source-drift",'
        '"observable":"This must not enter the frozen prompt."}],'
        '"non_goals":["No effects."]}\n```\n',
        encoding="utf-8",
    )
    try:
        frozen_prompt = runner.render_task_prompt(frozen_prompt_request, config)
    finally:
        plan.write_bytes(original_plan)
    check(
        "built-in prompt keeps snapshot Outcome after mutable source drift",
        "fixture-green" in frozen_prompt and "source-drift" not in frozen_prompt,
        frozen_prompt,
    )
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
    writable_line = (
        "Writable task worktree (the only product checkout you may edit): "
        f"{request['worktree']}"
    )
    source_line = (
        "Source repository (read-only reference; never cd, edit, stage, or commit here): "
        f"{request['target_repo']}"
    )
    git_guard = "Before every Git write, confirm that `pwd` is the writable task worktree"
    check(
        "prompt makes the isolated task worktree the only writable checkout",
        writable_line in prompt and source_line in prompt and git_guard in prompt,
        repr(
            {
                "writable_line": writable_line in prompt,
                "source_line": source_line in prompt,
                "git_guard": git_guard in prompt,
                "rendered": [
                    line
                    for line in prompt.splitlines()
                    if "worktree" in line.lower() or "repository" in line.lower()
                ],
            }
        ),
    )
    check(
        "continuable dispatch defaults to dedicated workspace placement",
        request["placement"] == "workspace",
    )
    explicit_split_raw = json.loads(json.dumps(raw_request))
    explicit_split_raw["placement"] = "split"
    explicit_split_request = runner.validate_request(explicit_split_raw)
    check(
        "split placement remains an explicit opt-in",
        explicit_split_request["placement"] == "split",
    )
    workspace_raw = json.loads(json.dumps(raw_request))
    workspace_raw["placement"] = "workspace"
    workspace_raw["reap"]["plan_mode"] = "shared"
    workspace_request = runner.validate_request(workspace_raw)
    workspace_prompt = runner.render_task_prompt(workspace_request, config)
    check(
        "workspace dispatch remains an explicit placement",
        workspace_request["placement"] == "workspace"
        and "scripts/harness-cli.py" in workspace_prompt,
    )
    check(
        "shared-plan task prompt cannot preclaim the final outcome",
        '"outcome_disposition":"partially-achieved"' in workspace_prompt
        and '"outcome_evidence_ids":[]' in workspace_prompt
        and '"residual_gap_pointers":[' in workspace_prompt
        and str(plan) in workspace_prompt
        and '"outcome_disposition":"achieved"' not in workspace_prompt,
        workspace_prompt,
    )
    split_parent = ParentContract(
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        outcome_contract_sha256=workspace_request["outcome_contract_sha256"],
        base_sha=workspace_request["base_sha"],
        evidence_ids=("fixture-green",),
        non_goals=("No external effects.",),
    )
    split_candidate = SplitCandidate(
        subplan_id="whole-plan",
        title="Whole plan",
        pipeline="lifecycle/default",
        route_alias="task-default",
        owned_paths=("README.md",),
        evidence_ids=("fixture-green",),
        dependencies=(),
        inherited_non_goals=split_parent.non_goals,
        budget=ChildBudget(200_000, 1_800),
        independence_proven=True,
    )
    split_manifest = build_split_preview(
        parent=split_parent,
        candidates=(split_candidate,),
        frozen_budget=FrozenSplitBudget(1, 1, 200_000, 1_800),
        requested_max_parallel=1,
        coordination_cost=0,
        parallel_benefit=1,
        fallback_pipeline="lifecycle/default",
        fallback_route_alias="task-default",
        join=JoinSpec(),
    ).manifest
    split_raw = json.loads(json.dumps(workspace_raw))
    split_raw["split"] = split_child_policy_payload(
        split_child_policy(split_manifest, split_manifest.subplans[0])
    )
    split_request = runner.validate_request(split_raw)
    split_prompt = runner.render_task_prompt(split_request, config)
    check(
        "Split child dispatch persists a frozen child-local policy",
        split_request["placement"] == "workspace"
        and split_request["split"]["manifest_sha256"]
        == split_manifest.manifest_sha256
        and "## Frozen Split child contract" in split_prompt
        and "`README.md`" in split_prompt,
    )
    wrong_split_placement = json.loads(json.dumps(split_raw))
    wrong_split_placement["placement"] = "split"
    expect_error(
        "Split child cannot fall back into the coordinator split",
        lambda: runner.validate_request(wrong_split_placement),
        "requires workspace placement",
    )
    unknown_reap = json.loads(json.dumps(raw_request))
    unknown_reap["reap"]["mode"] = "shared"
    expect_error(
        "dispatch rejects unknown reap keys",
        lambda: runner.validate_request(unknown_reap),
        "unknown reap keys: mode",
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
        and "Do not orchestrate cmux/model commands manually" in prompt,
    )
    harness_prefix = (
        f"python3 {request['vault_root']}/scripts/harness-cli.py "
        f"--store {request['vault_root']}/.vault-meta/harness "
        f"--owner {request['request_id']} --json"
    )
    check(
        "task prompt binds read-only harness diagnostics to canonical ownership",
        f"`{harness_prefix} status`" in prompt
        and f"`{harness_prefix} inspect <operation-id>`" in prompt
        and f"`{harness_prefix} doctor`" in prompt,
        prompt,
    )
    check(
        "task prompt keeps harness mutations coordinator-owned",
        "`resume`, `reconcile`, `cancel`, and `close` are coordinator-owned"
        in prompt
        and "scripts/harness-cli.py status|inspect|resume|reconcile|cancel|close|doctor"
        not in prompt,
        prompt,
    )
    idle_contract = (
        "End the current model turn while keeping this session open. "
        "The code-owned observer owns healthy waiting; act again in this same "
        "session only on a typed callback wake, typed escalation, or explicit "
        "coordinator request."
    )
    harness_completion = prompt.split("## Harness completion", 1)[1]
    check(
        "task prompt ends healthy callback turns without closing the session",
        idle_contract in harness_completion
        and "Remain available" not in harness_completion
        and "while the harness launches review" not in harness_completion,
        harness_completion,
    )
    check(
        "task prompt reserves diagnostics for typed attention boundaries",
        (
            "Use only these exact, read-only Harness diagnostics for a typed "
            "escalation, `attention-required`, or explicit coordinator request:"
        )
        in harness_completion,
        harness_completion,
    )
    claude_raw = json.loads(json.dumps(raw_request))
    claude_raw["executor"] = {
        "runtime": "claude",
        "model": "opus",
        "effort": "high",
    }
    claude_prompt = runner.render_task_prompt(
        runner.validate_request(claude_raw), config
    )
    check(
        "Codex and Claude rendered prompts share the exact idle contract",
        idle_contract in claude_prompt.split("## Harness completion", 1)[1]
        and idle_contract in harness_completion,
        claude_prompt,
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
        "write `.task-summary.json` with exactly this canonical JSON shape"
        in prompt
        and (
            '{"schema_version":2,"type":"repo-touch",'
            '"title":"Fast dispatch result","session":"unit-session",'
            '"body":"<bounded Markdown summary>",'
            '"outcome_disposition":"achieved",'
            '"outcome_evidence_ids":["fixture-green"],'
            '"residual_gap_pointers":[]}'
        )
        in prompt
        and "task-review-runner.py run" not in prompt,
        prompt,
    )
    change_raw = json.loads(json.dumps(raw_request))
    change_raw["pipeline"] = "engineering/change"
    change_request = runner.validate_request(change_raw)
    change_contract = runner.lifecycle_contract(
        review,
        change_request["pipeline"],
    )
    check(
        "approved dispatch can select the built-in engineering change profile",
        change_request["pipeline"] == "engineering/change"
        and change_contract["pipeline"] == "engineering/change@1.0.0"
        and "tdd-slices:model_step@1.0.0"
        in change_contract["summary"]
        and runner.harness_request(
            change_request,
            config,
            effective,
        ).pipeline_name
        == "engineering/change",
    )
    custom_dir = vault / ".vault-meta" / "dispatch-requests"
    custom_dir.mkdir(parents=True)
    custom_spec = custom_dir / "custom-pipeline.json"
    custom_spec.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_id": "change-with-diff-check",
                "version": "1.0.0",
                "intent": "engineering-change",
                "task_profile": "change",
                "baseline_pipeline": "engineering/change",
                "route_alias": "executor-default",
                "required_capabilities": ["route:resolved"],
                "input_schema": "approved-plan/v1",
                "output_schema": "reap-ready/v1",
                "steps": [
                    {
                        "step_id": "tdd-slices",
                        "primitive_id": "model_step",
                        "primitive_version": "1.0.0",
                        "input_schema": "approved-plan/v1",
                        "output_schema": "implementation-result/v1",
                        "session_mode": "worktree",
                        "semantic_skills": ["tdd"],
                    },
                    {
                        "step_id": "verify",
                        "primitive_id": "verify",
                        "primitive_version": "1.0.0",
                        "input_schema": "implementation-result/v1",
                        "output_schema": "verified-result/v1",
                        "session_mode": "verification",
                        "semantic_skills": [],
                    },
                    {
                        "step_id": "review",
                        "primitive_id": "review",
                        "primitive_version": "1.0.0",
                        "input_schema": "verified-result/v1",
                        "output_schema": "reap-ready/v1",
                        "session_mode": "review",
                        "semantic_skills": ["review"],
                    },
                ],
                "transitions": [
                    {"from_step": "tdd-slices", "outcome": "complete", "target": "verify", "max_traversals": 1},
                    {"from_step": "verify", "outcome": "complete", "target": "review", "max_traversals": 1},
                    {"from_step": "review", "outcome": "complete", "target": "terminal:completed", "max_traversals": 1},
                ],
                "controls": [],
                "budget": {
                    "attempt_limit": 2,
                    "model_restart_limit": 1,
                    "time_budget_seconds": 900,
                    "token_limit": 50000,
                },
                "completion_policy": "attention",
                "requested_permissions": [
                    "git-write",
                    "product-worktree",
                ],
                "requested_side_effects": ["git-write", "worktree"],
                "context_pointers": [],
                "verification_checks": ["diff-check"],
                "review_mode": "simple",
                "human_gates": ["initial-approval"],
                "terminal_outcomes": ["completed", "attention-required"],
                "finalization_policy": {
                    "max_cycles": 1,
                    "add_independent_model_after": 4,
                    "execution": "ephemeral",
                    "primary_route_alias": "finalization-primary",
                    "independent_route_alias": "finalization-independent",
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    custom_raw = json.loads(json.dumps(raw_request))
    custom_raw["pipeline"] = "custom"
    custom_raw["custom_pipeline_spec"] = str(custom_spec)
    custom_full_spec = custom_dir / "custom-full.json"
    custom_full_payload = json.loads(custom_spec.read_text(encoding="utf-8"))
    custom_full_payload["review_mode"] = "full"
    custom_full_spec.write_text(
        json.dumps(custom_full_payload, sort_keys=True),
        encoding="utf-8",
    )
    implicit_custom_full = json.loads(json.dumps(custom_raw))
    implicit_custom_full["custom_pipeline_spec"] = str(custom_full_spec)
    expect_error(
        "model-authored custom pipeline cannot select Full implicitly",
        lambda: runner.validate_request(implicit_custom_full),
        "explicit review.mode=full",
    )
    explicit_custom_full = json.loads(json.dumps(implicit_custom_full))
    explicit_custom_full["review"] = {"mode": "full"}
    check(
        "approved custom pipeline may bind an explicit Full request",
        runner.validate_request(explicit_custom_full)["review"]["mode"]
        == "full",
    )
    custom_request = runner.validate_request(custom_raw)
    custom_contract = runner.lifecycle_contract_for_request(
        custom_request,
        review,
    )
    custom_prompt_request = dict(custom_request)
    custom_prompt_request["_approved_plan_file"] = (
        runner.custom_approval_plan_path(custom_request)
    )
    custom_prompt = runner.render_task_prompt(custom_prompt_request, config)
    expect_error(
        "custom dispatch cannot start from validation alone",
        lambda: runner.harness_request(custom_request, config, effective),
        "approval evidence",
    )
    custom_challenge = runner.custom_approval_challenge(
        custom_request,
        request_sha256="a" * 64,
        effective=effective,
        review=review,
        prompt=custom_prompt,
    )
    host_calls = []
    original_run = runner.subprocess.run
    original_platform = runner.sys.platform
    original_program = runner.HOST_APPROVAL_PROGRAM
    try:
        runner.sys.platform = "darwin"
        runner.HOST_APPROVAL_PROGRAM = Path(sys.executable)

        def fake_host_run(argv, **kwargs):
            host_calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "Approve\n", "")

        runner.subprocess.run = fake_host_run
        host_choice = runner.host_custom_approval_decision(custom_challenge)
    finally:
        runner.subprocess.run = original_run
        runner.sys.platform = original_platform
        runner.HOST_APPROVAL_PROGRAM = original_program
    check(
        "host approval choice is not accepted through argv or stdin",
        host_choice == "approve"
        and host_calls[0][0][-1] == custom_challenge["challenge_sha256"]
        and host_calls[0][1].get("input") is None,
        host_calls,
    )
    expect_error(
        "custom approval must have a persisted validation challenge",
        lambda: runner.authorize_custom_request(
            custom_request,
            "a" * 64,
            "a" * 64,
        ),
        "validated before start",
    )
    runner.persist_custom_approval_challenge(
        custom_request,
        custom_challenge,
        runner.custom_approval_snapshot(
            custom_request,
            custom_challenge,
            session=session,
            effective=effective,
            review=review,
            prompt=custom_prompt,
        ),
    )
    expect_error(
        "custom decision rejects a different challenge digest",
        lambda: runner.record_custom_approval_decision(
            custom_request,
            custom_challenge,
            "f" * 64,
            host_decision=lambda _challenge: "approve",
        ),
        "does not match",
    )
    approval_decision = runner.record_custom_approval_decision(
        custom_request,
        custom_challenge,
        custom_challenge["challenge_sha256"],
        host_decision=lambda _challenge: "approve",
    )
    expect_error(
        "custom start rejects a different one-shot token",
        lambda: runner.authorize_custom_request(
            custom_request,
            "a" * 64,
            "f" * 64,
        ),
        "token does not match",
    )
    approved_custom_request = runner.authorize_custom_request(
        custom_request,
        "a" * 64,
        approval_decision["approval_token"],
    )
    custom_harness = runner.harness_request(
        approved_custom_request,
        config,
        effective,
    )
    check(
        "dispatch validates and binds one explicitly approved custom contract",
        custom_request["pipeline"] == "custom"
        and custom_contract["pipeline"] == "custom/change@1.0.0"
        and "Explicit user approval required" in custom_contract["summary"]
        and "Inherited harness permissions: cmux-target:policy-only"
        in custom_contract["summary"]
        and "Inherited harness side effects: cmux-surface:policy-only"
        in custom_contract["summary"]
        and "Finalization: cycles<=1; independent-after=4"
        in custom_contract["summary"]
        and request["origin_surface"] in custom_contract["summary"]
        and request["origin_session"] in custom_contract["summary"]
        and custom_harness.custom_pipeline is not None
        and operation_spec(custom_harness).contract_sha256
        == custom_contract["definition_sha256"],
    )
    check(
        "custom approval challenge binds exact coordinator target identity",
        custom_challenge["approval_card_sha256"]
        == hashlib.sha256(
            runner.custom_approval_card_for_request(custom_request).encode()
        ).hexdigest()
        and custom_challenge["request_id"] == custom_request["request_id"],
        custom_challenge,
    )
    check(
        "custom prompt keeps runtime transport out of product commits",
        "`.task-*`, `.wiki-*`, and `.task-pipeline/**` as runtime"
        in custom_prompt
        and "never stage or commit them" in custom_prompt,
        custom_prompt,
    )
    custom_policy = runner.task_pipeline_policy(custom_request)
    check(
        "task metadata classifies custom execution without embedding its raw spec",
        custom_policy["name"] == "custom"
        and custom_policy["source"] == "custom"
        and custom_policy["baseline"] == "engineering/change"
        and set(custom_policy)
        == {
            "name",
            "source",
            "baseline",
            "definition_sha256",
            "completion_policy",
            "total_pass_limit",
        },
        custom_policy,
    )
    custom_payload = json.loads(custom_spec.read_text(encoding="utf-8"))
    mutated_custom = json.loads(json.dumps(custom_payload))
    mutated_custom["spec_id"] = "changed-after-validation"
    custom_spec.write_text(json.dumps(mutated_custom), encoding="utf-8")
    changed_request = runner.validate_request(custom_raw)
    changed_prompt = runner.render_task_prompt(changed_request, config)
    changed_challenge = runner.custom_approval_challenge(
        changed_request,
        request_sha256="a" * 64,
        effective=effective,
        review=review,
        prompt=changed_prompt,
    )
    expect_error(
        "custom spec mutation invalidates the persisted approval challenge",
        lambda: runner.authorize_custom_request(
            changed_request,
            "b" * 64,
            approval_decision["approval_token"],
        ),
        "request bytes changed",
    )
    custom_spec.write_text(json.dumps(custom_payload), encoding="utf-8")

    cli_raw = json.loads(json.dumps(custom_raw))
    cli_raw["request_id"] = str(uuid.uuid4())
    cli_raw["task_name"] = "custom-cli-approval"
    cli_raw["branch"] = "task/custom-cli-approval"
    cli_raw["worktree"] = str(
        tmp / "worktrees" / "custom-cli-approval"
    )
    cli_spec = custom_dir / f"{cli_raw['request_id']}.json"
    cli_spec.write_text(
        json.dumps(cli_raw, sort_keys=True), encoding="utf-8"
    )
    cli_validate = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--spec", str(cli_spec)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    cli_challenge = json.loads(cli_validate.stdout)["challenge_sha256"]
    cli_without_approval = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "start",
            "--spec",
            str(cli_spec),
            "--approval-token",
            "f" * 64,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "custom CLI start rejects an invalid legacy approval token before effects",
        cli_without_approval.returncode == 3
        and "custom approval token does not match" in cli_without_approval.stderr
        and not Path(cli_raw["worktree"]).exists(),
        cli_without_approval.stderr,
    )
    model_cli_approve = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "approve",
            "--spec",
            str(cli_spec),
            "--challenge-sha256",
            cli_challenge,
            "--decision",
            "approve",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "model-accessible CLI cannot self-authorize a pending challenge",
        model_cli_approve.returncode != 0
        and "unrecognized arguments" in model_cli_approve.stderr
        and not Path(cli_raw["worktree"]).exists(),
        model_cli_approve.stderr,
    )
    cli_request = runner.validate_request(cli_raw)
    cli_prompt_request = dict(cli_request)
    cli_prompt_request["_approved_plan_file"] = (
        runner.custom_approval_plan_path(cli_request)
    )
    cli_prompt = runner.render_task_prompt(cli_prompt_request, config)
    cli_exact_challenge = runner.custom_approval_challenge(
        cli_request,
        request_sha256=runner.sha256_file(cli_spec),
        effective=effective,
        review=review,
        prompt=cli_prompt,
    )
    cli_approval = runner.record_custom_approval_decision(
        cli_request,
        cli_exact_challenge,
        cli_challenge,
        host_decision=lambda _challenge: "approve",
    )
    cli_token = cli_approval["approval_token"]
    check(
        "host custom decision is durable before start",
        not Path(cli_raw["worktree"]).exists(),
    )
    policy_valid_raw = json.loads(json.dumps(custom_raw))
    policy_valid_raw["request_id"] = str(uuid.uuid4())
    policy_valid_raw["task_name"] = "custom-policy-valid-start"
    policy_valid_raw["branch"] = "task/custom-policy-valid-start"
    policy_valid_raw["worktree"] = str(
        tmp / "worktrees" / "custom-policy-valid-start"
    )
    policy_valid_request = runner.validate_request(policy_valid_raw)
    policy_valid_prompt_request = dict(policy_valid_request)
    policy_valid_prompt_request["_approved_plan_file"] = (
        runner.custom_approval_plan_path(policy_valid_request)
    )
    policy_valid_prompt = runner.render_task_prompt(
        policy_valid_prompt_request, config
    )
    policy_valid_challenge = runner.custom_approval_challenge(
        policy_valid_request,
        request_sha256="c" * 64,
        effective=effective,
        review=review,
        prompt=policy_valid_prompt,
    )
    runner.persist_custom_approval_challenge(
        policy_valid_request,
        policy_valid_challenge,
        runner.custom_approval_snapshot(
            policy_valid_request,
            policy_valid_challenge,
            session=session,
            effective=effective,
            review=review,
            prompt=policy_valid_prompt,
        ),
    )
    with mock.patch.object(runner.subprocess, "run") as pre_start_process:
        policy_valid_start = runner.authorize_custom_request(
            policy_valid_request, "c" * 64, ""
        )
        policy_valid_frozen = runner.custom_pipeline_for_request(
            policy_valid_start
        )
    pre_start_process.assert_not_called()
    policy_valid_record = runner.read_object(
        runner.custom_approval_path(policy_valid_request)
    )
    check(
        "policy-valid custom start consumes and freezes its immutable snapshot without pre-start effects",
        policy_valid_start["_approved_custom_contract"][1].definition_sha256
        == policy_valid_challenge["definition_sha256"]
        and policy_valid_frozen is not None
        and policy_valid_frozen.definition_sha256
        == policy_valid_challenge["definition_sha256"]
        and policy_valid_frozen.approval.actor == "policy-valid-snapshot"
        and policy_valid_frozen.approval.decision == "approve"
        and policy_valid_frozen.approval_card
        == policy_valid_start["_approved_custom_contract"][3]
        and policy_valid_record["status"] == "consumed"
        and policy_valid_record.get("actor") == ""
        and not Path(policy_valid_raw["worktree"]).exists(),
        policy_valid_record,
    )

    def pending_policy_valid_case():
        suffix = uuid.uuid4().hex[:8]
        case_raw = json.loads(json.dumps(custom_raw))
        case_raw["request_id"] = str(uuid.uuid4())
        case_raw["task_name"] = f"custom-policy-drift-{suffix}"
        case_raw["branch"] = f"task/custom-policy-drift-{suffix}"
        case_raw["worktree"] = str(
            tmp / "worktrees" / f"custom-policy-drift-{suffix}"
        )
        case_request = runner.validate_request(case_raw)
        case_prompt_request = dict(case_request)
        case_prompt_request["_approved_plan_file"] = (
            runner.custom_approval_plan_path(case_request)
        )
        case_prompt = runner.render_task_prompt(case_prompt_request, config)
        case_request_sha256 = hashlib.sha256(suffix.encode()).hexdigest()
        case_challenge = runner.custom_approval_challenge(
            case_request,
            request_sha256=case_request_sha256,
            effective=effective,
            review=review,
            prompt=case_prompt,
        )
        runner.persist_custom_approval_challenge(
            case_request,
            case_challenge,
            runner.custom_approval_snapshot(
                case_request,
                case_challenge,
                session=session,
                effective=effective,
                review=review,
                prompt=case_prompt,
            ),
        )
        return case_raw, case_request, case_request_sha256

    drift_cases = (
        (
            "missing immutable snapshot",
            ("snapshot",),
            None,
            "snapshot is unavailable",
        ),
        (
            "request identity drift",
            ("challenge", "request_sha256"),
            "d" * 64,
            "request bytes changed",
        ),
        (
            "definition identity drift",
            ("challenge", "definition_sha256"),
            "d" * 64,
            "no longer matches",
        ),
        (
            "approval-card authority drift",
            ("snapshot", "approval_card"),
            "changed approval card",
            "no longer matches",
        ),
        (
            "route authority drift",
            ("snapshot", "effective"),
            {**effective, "model": "changed-model"},
            "no longer matches",
        ),
        (
            "review authority drift",
            ("snapshot", "review", "mode"),
            "deep",
            "no longer matches",
        ),
        (
            "session identity drift",
            ("snapshot", "session", "session_id"),
            "changed-session",
            "session snapshot changed",
        ),
        (
            "spec identity drift",
            ("snapshot", "pipeline_spec", "spec_id"),
            "changed-spec",
            "no longer matches",
        ),
        (
            "permission authority drift",
            ("snapshot", "pipeline_spec", "requested_permissions"),
            ["git-write"],
            "no longer matches",
        ),
        (
            "effect authority drift",
            ("snapshot", "pipeline_spec", "requested_side_effects"),
            ["git-write"],
            "no longer matches",
        ),
        (
            "budget authority drift",
            ("snapshot", "pipeline_spec", "budget", "token_limit"),
            50_001,
            "no longer matches",
        ),
        (
            "actor authority drift",
            ("actor",),
            "model",
            "no approved decision receipt",
        ),
    )
    for label, field_path, changed_value, error in drift_cases:
        case_raw, case_request, case_request_sha256 = (
            pending_policy_valid_case()
        )
        case_record = runner.read_object(
            runner.custom_approval_path(case_request)
        )
        target = case_record
        for field in field_path[:-1]:
            target = target[field]
        target[field_path[-1]] = changed_value
        runner.atomic_json(
            runner.custom_approval_path(case_request), case_record
        )
        with mock.patch.object(runner.subprocess, "run") as drift_process:
            expect_error(
                f"policy-valid custom start rejects {label}",
                lambda request=case_request, digest=case_request_sha256: (
                    runner.authorize_custom_request(request, digest, "")
                ),
                error,
            )
        drift_process.assert_not_called()
        check(
            f"policy-valid {label} reaches no worktree effect",
            not Path(case_raw["worktree"]).exists(),
        )

    plan_raw, plan_request, plan_request_sha256 = pending_policy_valid_case()
    runner.custom_approval_plan_path(plan_request).write_text(
        "changed plan snapshot\n", encoding="utf-8"
    )
    with mock.patch.object(runner.subprocess, "run") as plan_drift_process:
        expect_error(
            "policy-valid custom start rejects plan identity drift",
            lambda: runner.authorize_custom_request(
                plan_request, plan_request_sha256, ""
            ),
            "plan snapshot is unavailable",
        )
    plan_drift_process.assert_not_called()
    check(
        "policy-valid plan identity drift reaches no worktree effect",
        not Path(plan_raw["worktree"]).exists(),
    )
    expect_error(
        "policy-valid custom start rejects an empty-token replay after consumption",
        lambda: runner.authorize_custom_request(policy_valid_request, "c" * 64, ""),
        "custom pipeline has no approved decision receipt",
    )
    policy_valid_record.update({"status": "reject", "decision": "reject"})
    runner.atomic_json(
        runner.custom_approval_path(policy_valid_request), policy_valid_record
    )
    expect_error(
        "policy-valid custom start rejects an empty-token rejected record",
        lambda: runner.authorize_custom_request(policy_valid_request, "c" * 64, ""),
        "custom pipeline has no approved decision receipt",
    )
    changed_cli_custom = json.loads(json.dumps(custom_payload))
    changed_cli_custom["spec_id"] = "changed-before-cli-start"
    custom_spec.write_text(json.dumps(changed_cli_custom), encoding="utf-8")
    changed_cli_request = runner.validate_request(cli_raw)
    frozen_cli_request = runner.authorize_custom_request(
        changed_cli_request,
        runner.sha256_file(cli_spec),
        cli_token,
    )
    frozen_spec, frozen_compiled, _frozen_policy, _frozen_card = (
        runner.custom_contract_for_request(frozen_cli_request)
    )
    frozen_cli_request = bind_approved_plan_snapshot(frozen_cli_request)
    frozen_prompt = runner.render_task_prompt(frozen_cli_request, config)
    check(
        "custom start installs the approved snapshot despite mutable spec drift",
        frozen_spec.spec_id == custom_payload["spec_id"]
        and frozen_spec.spec_id != changed_cli_custom["spec_id"]
        and frozen_compiled.definition_sha256
        == cli_exact_challenge["definition_sha256"]
        and runner.approved_plan_sha256(frozen_cli_request)
        == cli_exact_challenge["plan_sha256"]
        and str(frozen_cli_request["_approved_plan_file"]) in frozen_prompt
        and str(runner.custom_approval_plan_path(cli_request)) not in frozen_prompt
        and not Path(cli_raw["worktree"]).exists(),
    )
    custom_spec.write_text(json.dumps(custom_payload), encoding="utf-8")

    unresolved_context = json.loads(json.dumps(custom_payload))
    unresolved_context["context_pointers"] = [
        {
            "pointer_id": "unapproved-context",
            "content_sha256": "a" * 64,
            "byte_limit": 1024,
        }
    ]
    custom_spec.write_text(json.dumps(unresolved_context), encoding="utf-8")
    expect_error(
        "custom context pointers must bind the approved context packet",
        lambda: runner.validate_request(custom_raw),
        "approved context packet",
    )
    custom_spec.write_text(json.dumps(custom_payload), encoding="utf-8")
    escaped_custom = json.loads(json.dumps(custom_raw))
    escaped_custom["custom_pipeline_spec"] = str(plan)
    expect_error(
        "custom specs stay in owner request scratch",
        lambda: runner.validate_request(escaped_custom),
        "request scratch",
    )
    harness_config = vault / "config" / "harness.toml"
    enabled_config = harness_config.read_text(encoding="utf-8")
    harness_config.write_text(
        enabled_config.replace(
            "custom_pipeline_authoring = true",
            "custom_pipeline_authoring = false",
        ),
        encoding="utf-8",
    )
    expect_error(
        "rollback switch disables new custom authoring without touching built-ins",
        lambda: runner.validate_request(custom_raw),
        "disabled",
    )
    check(
        "rollback switch keeps built-in dispatch available",
        runner.validate_request(change_raw)["pipeline"] == "engineering/change",
    )
    harness_config.write_text(enabled_config, encoding="utf-8")
    fix_raw = json.loads(json.dumps(raw_request))
    fix_raw["pipeline"] = "engineering/fix"
    fix_raw["completion_policy"] = "autonomous"
    fix_request = runner.validate_request(fix_raw)
    fix_contract = runner.lifecycle_contract(
        review,
        fix_request["pipeline"],
        fix_request["completion_policy"],
    )
    fix_prompt = runner.render_task_prompt(fix_request, config)
    check(
        "approved dispatch selects the executable engineering fix profile",
        fix_request["pipeline"] == "engineering/fix"
        and fix_request["completion_policy"] == "autonomous"
        and fix_contract["pipeline"] == "engineering/fix@1.0.0"
        and "root-cause:model_step@1.0.0" in fix_contract["summary"]
        and "bounded_loop@1.0.0" in fix_contract["summary"]
        and "human_gate@1.0.0" in fix_contract["summary"]
        and "Completion: policy=autonomous, total-passes=3"
        in fix_contract["summary"],
        fix_contract,
    )
    check(
        "engineering fix prompt binds the typed phase submit transport",
        "pipeline-step-submit.py" in fix_prompt
        and ".task-pipeline-step-request.json" in fix_prompt
        and "execute only the exact phase" in fix_prompt
        and '"output_sha256":"<sha256-of-output-file>"' in fix_prompt
        and "Only `reproduce` may use" in fix_prompt
        and "Stop after submission" in fix_prompt
        and "`.task-*`, `.wiki-*`, and `.task-pipeline/**` as runtime"
        in fix_prompt
        and "never stage or commit them" in fix_prompt
        and "completion_policy=autonomous" in fix_prompt
        and "total_pass_limit=3" in fix_prompt,
        fix_prompt,
    )
    invalid_completion = json.loads(json.dumps(fix_raw))
    invalid_completion["completion_policy"] = "forever"
    expect_error(
        "dispatch rejects an unbounded completion policy",
        lambda: runner.validate_request(invalid_completion),
        "completion_policy",
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
    expert_preview = runner.review_topology_preview(expert, expert_policy)
    check(
        "single-model deep preview shows two specialist sessions",
        expert_preview["session_count"] == 2
        and expert_preview["effective_mode"] == "deep"
        and len(expert_preview["topology_sha256"]) == 64
        and expert_preview["lanes"]
        == [
                {
                    "lane": "anthropic-intent",
                    "provider": "anthropic",
                    "runtime": "claude",
                    "model": "fable",
                    "responsibility": "intent",
                },
                {
                    "lane": "anthropic-engineering",
                    "provider": "anthropic",
                    "runtime": "claude",
                    "model": "fable",
                    "responsibility": "engineering",
                },
            ],
    )
    deep_default_raw = json.loads(json.dumps(raw_request))
    deep_default_raw["review"] = {"mode": "deep"}
    deep_default = runner.validate_request(deep_default_raw)
    deep_default_policy = runner.review_policy(deep_default, config)
    check(
        "default deep preview equals cycle-one single-model runtime",
        runner.review_topology_preview(deep_default, deep_default_policy)["lanes"]
        == [
            {
                "lane": "anthropic-intent",
                "provider": "anthropic",
                "runtime": "claude",
                "model": "fable",
                "responsibility": "intent",
            },
            {
                "lane": "anthropic-engineering",
                "provider": "anthropic",
                "runtime": "claude",
                "model": "fable",
                "responsibility": "engineering",
            },
        ],
    )
    full_raw = json.loads(json.dumps(raw_request))
    full_raw["review"] = {"mode": "full", "effort": "xhigh"}
    full = runner.validate_request(full_raw)
    full_policy = runner.review_policy(full, config)
    full_preview = runner.review_topology_preview(full, full_policy)
    full_lifecycle = runner.lifecycle_contract(full_policy)
    check(
        "explicit full preview shows the exact four-session grid",
        full_policy.depth == "full"
        and full_policy.max_verify_iterations == 2
        and full_preview["session_count"] == 4
        and full_preview["effective_mode"] == "full"
        and len(full_preview["topology_sha256"]) == 64
        and [lane["lane"] for lane in full_preview["lanes"]]
        == [
            "anthropic-intent",
            "anthropic-engineering",
            "openai-intent",
            "openai-engineering",
        ]
        and [
            (lane["provider"], lane["runtime"], lane["model"])
            for lane in full_preview["lanes"]
        ]
        == [
            ("anthropic", "claude", "fable"),
            ("anthropic", "claude", "fable"),
            ("openai", "codex", "gpt-5.6-sol"),
            ("openai", "codex", "gpt-5.6-sol"),
        ]
        and "Review: mode=full, verification-iterations=2"
        in full_lifecycle["summary"],
    )
    for field, value in (("runtime", "codex"), ("model", "sol")):
        invalid_full_raw = json.loads(json.dumps(full_raw))
        invalid_full_raw["review"][field] = value
        expect_error(
            f"full {field} override fails before provider effects",
            lambda raw=invalid_full_raw: runner.review_policy(
                runner.validate_request(raw), config
            ),
            "use Deep",
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
        "runner writes v4 metadata",
        meta["version"] == 4
        and meta["task_id"] == request_id
        and meta["worktree"] == str(request["worktree"]),
    )
    check(
        "runner binds v4 metadata to the approved Outcome Contract",
        meta["outcome_contract_sha256"] == request["outcome_contract_sha256"]
        and len(meta["outcome_contract_sha256"]) == 64,
    )
    snapshot_path = Path(str(meta.get("plan_snapshot_file") or ""))
    check(
        "runner binds v4 metadata to the canonical immutable plan snapshot",
        snapshot_path
        == request["vault_root"]
        / ".vault-meta/approved-plan-snapshots"
        / f"{meta['approved_plan_sha256']}.md"
        and snapshot_path.read_bytes() == request["plan_file"].read_bytes()
        and runner.sha256_file(snapshot_path) == meta["approved_plan_sha256"],
        meta,
    )
    check(
        "runner binds automatic review to an exact verification profile",
        meta["review_policy"]["verification_profile"] == "scoped"
        and len(meta["review_policy"]["verification_profile_sha256"]) == 64
        and meta["review_policy"]["max_verify_iterations"] == 1,
    )
    frozen_topology = meta.get("review_topology")
    expected_topology = runner.review_topology_preview(request, review)
    check(
        "runner freezes the validated review topology in task metadata",
        isinstance(frozen_topology, dict)
        and frozen_topology.get("sha256") == expected_topology["topology_sha256"]
        and frozen_topology.get("payload") == expected_topology["topology"],
    )
    check(
        "runner writes the deterministic simple review preset",
        meta["review_policy"]["mode"] == "simple"
        and meta["review_policy"]["cross_model"] is False
        and meta["review_policy"]["runtime"] == ""
        and meta["review_policy"]["model"] == ""
        and meta["review_policy"]["effort"] == "",
    )
    v4_schema = json.loads(
        (ROOT / "schemas" / "task-meta-v4.schema.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "writer output has exact strict v4 schema parity",
        set(meta)
        == set(v4_schema["required"])
        | {"finalization_policy", "review_topology"}
        and set(meta) <= set(v4_schema["properties"])
        and v4_schema["additionalProperties"] is False,
    )
    check(
        "writer persists the bounded additive finalization policy",
        meta["finalization_policy"]
        == {
            "max_cycles": 5,
            "add_independent_model_after": 3,
            "execution": "ephemeral",
            "primary_route_alias": "finalization-primary",
            "independent_route_alias": "finalization-independent",
        },
    )
    normalized_meta = normalize_task_meta(meta)
    check(
        "schema-valid generated v4 tasks select the exact-attempt protocol",
        normalized_meta["version"] == 4
        and normalized_meta["finalization_policy"]
        == meta["finalization_policy"]
        and normalized_meta["review_topology"] == meta["review_topology"]
        and _exact_head_attempt_enabled(meta)
        and set(meta["review_policy"])
        == {
            "mode",
            "cross_model",
            "runtime",
            "model",
            "effort",
            "max_verify_iterations",
            "verification_profile",
            "verification_profile_sha256",
        },
    )
    custom_meta = runner.write_task_files(
        approved_custom_request,
        config,
        session,
        effective,
        identity,
        {"surface_id": raw_request["origin_surface"], "surface_ref": "surface:1"},
        {"surface": "22222222-2222-4222-8222-222222222222", "surface_ref": "surface:2"},
    )
    normalized_custom = runner.normalize_task_contract(custom_meta)
    custom_finalization = task_finalization_policy(custom_meta)
    custom_spec_value, custom_compiled, custom_ceiling, _custom_card = (
        runner.custom_contract_for_request(approved_custom_request)
    )
    custom_ledger = FinalizationLedger(
        tmp / "custom-finalization-ledger",
        lineage_id=str(uuid.UUID(int=900)),
        origin_task_id=str(uuid.UUID(int=901)),
        plan_sha256=custom_meta["approved_plan_sha256"],
        outcome_contract_sha256=custom_meta["outcome_contract_sha256"],
        max_cycles=custom_meta["finalization_policy"]["max_cycles"],
    )
    check(
        "dispatch preserves an approved lower custom finalization ceiling",
        custom_spec_value.finalization_policy is not None
        and custom_meta["finalization_policy"]
        == runner.pipeline_spec_payload(custom_spec_value)["finalization_policy"]
        == normalized_custom["finalization_policy"]
        and custom_finalization == custom_spec_value.finalization_policy
        and custom_ledger.max_cycles == custom_spec_value.finalization_policy.max_cycles
        and custom_meta["pipeline_policy"]["definition_sha256"]
        == custom_compiled.definition_sha256,
        custom_meta,
    )
    legacy_payload = json.loads(json.dumps(custom_payload))
    legacy_payload.pop("finalization_policy")
    legacy_spec = runner.parse_pipeline_spec(legacy_payload)
    legacy_compiled = runner.compile_custom_spec(
        legacy_spec,
        runner.builtin_registry(),
        policy=custom_ceiling,
        capabilities=("route:resolved",),
    )
    legacy_request = dict(approved_custom_request)
    legacy_request["_approved_custom_contract"] = (
        legacy_spec,
        legacy_compiled,
        custom_ceiling,
        runner.render_custom_approval(
            legacy_spec, legacy_compiled, policy=custom_ceiling
        ),
    )
    legacy_meta = runner.write_task_files(
        legacy_request,
        config,
        session,
        effective,
        identity,
        {"surface_id": raw_request["origin_surface"], "surface_ref": "surface:1"},
        {"surface": "22222222-2222-4222-8222-222222222222", "surface_ref": "surface:2"},
    )
    check(
        "custom specs without the additive policy retain the code-owned default",
        legacy_spec.finalization_policy is None
        and legacy_meta["finalization_policy"] == meta["finalization_policy"],
        legacy_meta,
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
    origin_ignored = subprocess.run(
        ["git", "check-ignore", ".task-origin-session"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "runner keeps the task origin binding out of product status",
        origin_ignored.returncode == 0,
        origin_ignored.stderr,
    )
    exclude_result = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    exclude_path = Path(exclude_result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = worktree / exclude_path
    check(
        "runner installs the task origin exclusion once across replay",
        exclude_path.read_text(encoding="utf-8").splitlines().count(
            ".task-origin-session"
        )
        == 1,
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
        json.dumps(
            {
                "schema_version": 2,
                "type": "repo-touch",
                "title": "Fast dispatch result",
                "session": "unit-session",
                "body": "The fixture outcome is established.",
                "outcome_disposition": "achieved",
                "outcome_evidence_ids": ["fixture-green"],
                "residual_gap_pointers": [],
            }
        )
        + "\n",
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
        "native task thread completes the exact v4 handoff",
        detected.returncode == 0
        and detected.stdout.strip() == "unit-session"
        and handoff.returncode == 0,
        detected.stderr + handoff.stderr,
    )

    class PublicReviewRuntime:
        def __init__(self, root: Path) -> None:
            self.store = OperationStore(root)
            self.started = 0
            self.admissions = 0

        def start(
            self,
            runtime_request,
            *,
            on_surface_opened=None,
            admit_provider_start=None,
        ):
            self.started += 1
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
                OwnedResources("33333333-3333-4333-8333-333333333333")
            )
            opened = RuntimeSessionResult(
                supervisor.read(), "surface-opened", checkpoint="checkpoint-1"
            )
            if on_surface_opened is not None:
                on_surface_opened(opened)
            if admit_provider_start is not None:
                admit_provider_start()
                self.admissions += 1
            supervisor.transition("running")
            final = supervisor.transition("awaiting-callback")
            return RuntimeSessionResult(
                final, "started", checkpoint="checkpoint-1"
            )

        def register_callback_target(self, *_args: object) -> None:
            return None

    public_runtime = PublicReviewRuntime(vault / ".vault-meta" / "harness")
    public_review = review_runner.run_task_review(
        worktree, runtime_manager=public_runtime
    )
    public_gate = json.loads(
        (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / request_id
            / request_id
            / "review-gate.json"
        ).read_text(encoding="utf-8")
    )
    public_ledger = json.loads(
        (
            vault
            / ".vault-meta"
            / "harness"
            / "finalization-ledger"
            / f"{request_id}.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "generated normalized task reaches the public exact-attempt runner",
        public_review["status"] == "reviewing"
        and public_runtime.started == len(public_review["lanes"])
        and public_runtime.admissions == public_runtime.started
        and public_runtime.started > 0
        and public_gate["attempt"]["identity"]["cycle"] == 1
        and public_gate["attempt"]["identity"]["exact_head_sha"]
        == git("rev-parse", "HEAD", cwd=worktree)
        and public_ledger["max_cycles"] == 5
        and len(public_ledger["cycles"]) == 1,
        (public_review, public_gate, public_ledger),
    )
    public_preview = runner.review_topology_preview(
        expert, runner.review_policy(expert, config)
    )
    check(
        "dispatch preview and launched gate bind one topology digest",
        public_gate["topology_sha256"]
        == public_preview["topology_sha256"]
        and public_gate["topology"]["session_count"]
        == public_preview["session_count"]
        and [lane["axis"] for lane in public_gate["topology"]["lanes"]]
        == [lane["lane"] for lane in public_preview["lanes"]],
        json.dumps(
            {
                "gate_sha256": public_gate["topology_sha256"],
                "preview": public_preview,
                "gate_topology": public_gate["topology"],
            },
            sort_keys=True,
        ),
    )
    check("runner writes one plan branch", (worktree / ".task-prompt.md").read_text().count("## Approved plan") == 1)
    check("runner metadata validates", runner.normalize_task_contract(meta)["interaction_policy"] == "unattended")
    check(
        "runner metadata records default workspace placement",
        meta["surface_policy"]["placement"] == "workspace",
    )
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
        and workspace_meta["task_window"] == "55555555-5555-4555-8555-555555555555"
        and workspace_meta["reap_policy"]["mode"] == "shared",
    )
    split_meta = {**workspace_meta, "split_policy": split_request["split"]}
    check(
        "task-session metadata preserves the exact Split manifest slice",
        runner.normalize_task_contract(split_meta)["split_policy"]
        == split_request["split"],
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
    packet_request = runner.harness_request(harness_request, config, effective)
    packet_manifest = vault / packet_request.context_manifest
    packet_value = json.loads(packet_manifest.read_text(encoding="utf-8"))
    check(
        "built-in dispatch carries Outcome Contract through ContextPacket",
        packet_manifest.is_file()
        and any(
            row.get("pointer_id") == "outcome-contract"
            and row.get("role") == "outcome"
            and row.get("sha256") == harness_request["outcome_contract_sha256"]
            for row in packet_value["inputs"]
        ),
    )
    fix_harness_raw = json.loads(json.dumps(raw_request))
    fix_harness_raw["request_id"] = str(uuid.uuid4())
    fix_harness_raw["task_name"] = "harness-fix"
    fix_harness_raw["branch"] = "task/harness-fix"
    fix_harness_raw["worktree"] = str(tmp / "worktrees" / "harness-fix")
    fix_harness_raw["pipeline"] = "engineering/fix"
    fix_harness_raw["completion_policy"] = "autonomous"
    fix_harness_request = runner.validate_request(fix_harness_raw)
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
    import dispatch_execution

    original_sync = dispatch_execution.sync_codex_profile
    original_log = dispatch_execution.dispatch_log
    original_rebind = dispatch_execution.rebind_facade_dashboard
    rebound_dashboards: list[dict[str, object]] = []
    sync_failure_raw = json.loads(json.dumps(raw_request))
    sync_failure_raw["request_id"] = str(uuid.uuid4())
    sync_failure_raw["task_name"] = "runtime-sync-failure"
    sync_failure_raw["branch"] = "task/runtime-sync-failure"
    sync_failure_raw["worktree"] = str(
        tmp / "worktrees" / "runtime-sync-failure"
    )
    sync_failure_request = runner.validate_request(sync_failure_raw)
    dispatch_execution.sync_codex_profile = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        runner.DispatchError("fixture runtime sync failed")
    )
    expect_error(
        "runtime sync failure is contained before worktree creation",
        lambda: runner.start(sync_failure_request, "e" * 64),
        "runtime-sync failed",
    )
    check(
        "runtime sync failure leaves no orphan task worktree",
        not Path(sync_failure_request["worktree"]).exists(),
        sync_failure_request["worktree"],
    )
    dispatch_execution.sync_codex_profile = lambda *_args, **_kwargs: None
    dispatch_execution.dispatch_log = lambda *_args, **_kwargs: None
    dispatch_execution.rebind_facade_dashboard = lambda **kwargs: (
        rebound_dashboards.append(kwargs)
        or DashboardLaunchReceipt(
            "launched", "dispatch", "root", str(kwargs["root_operation_id"])
        )
    )
    try:
        harness_result = runner.start(
            harness_request, "c" * 64, runtime_manager=fake_runtime
        )
        harness_replay = runner.start(
            harness_request, "c" * 64, runtime_manager=fake_runtime
        )
        fix_harness_result = runner.start(
            fix_harness_request, "d" * 64, runtime_manager=fake_runtime
        )
    finally:
        dispatch_execution.sync_codex_profile = original_sync
        dispatch_execution.dispatch_log = original_log
        dispatch_execution.rebind_facade_dashboard = original_rebind
    harness_record = OperationStore(vault / ".vault-meta/harness").read(
        harness_request["request_id"], harness_request["request_id"]
    )
    check(
        "public start executes one durable harness launch",
        sum(
            item.spec.operation_id == harness_request["request_id"]
            for item in fake_runtime.requests
        )
        == 1
        and harness_result["harness"]["run_id"] == harness_record.run_id
        and harness_replay == harness_result
        and harness_record.state == "awaiting-callback",
    )
    check(
        "durable task creation rebinds the temporary observer exactly once",
        len(rebound_dashboards) == 2
        and rebound_dashboards[0]["temporary_request_id"]
        == harness_request["request_id"]
        and rebound_dashboards[0]["root_operation_id"]
        == harness_result["task_id"]
        and harness_result["observer"]["status"] == "launched",
    )
    check(
        "dispatch binds the product root for provider permission compilation",
        fake_runtime.requests[0].product_root
        == Path(harness_request["worktree"]).resolve(),
    )
    fix_runtime_request = fake_runtime.requests[1]
    fix_phase_request = json.loads(
        (
            Path(fix_harness_request["worktree"])
            / ".task-pipeline-step-request.json"
        ).read_text(encoding="utf-8")
    )
    fix_parent = OperationStore(vault / ".vault-meta/harness").read(
        fix_harness_request["request_id"],
        fix_harness_request["request_id"],
    )
    fix_child = OperationStore(vault / ".vault-meta/harness").read(
        fix_harness_request["request_id"],
        str(fix_phase_request["operation_id"]),
    )
    check(
        "engineering fix starts exactly one typed reproduce child",
        fix_harness_result["status"] == "launched"
        and fix_phase_request["step_id"] == "reproduce"
        and fix_phase_request["iteration"] == 0
        and fix_runtime_request.callback_pointer
        == ".task-pipeline-step-callback.json"
        and fix_runtime_request.task_summary_pointer
        == ".task-summary.json"
        and fix_runtime_request.initial_callback_operation_id
        == fix_phase_request["operation_id"]
        and fix_runtime_request.initial_callback_run_id
        == fix_phase_request["run_id"]
        and fix_runtime_request.attempt_limit == 9
        and fix_runtime_request.model_restart_limit == 1
        and fix_runtime_request.time_budget_seconds == 5400
        and fix_runtime_request.token_limit == 600_000
        and fix_child.spec.kind == "pipeline-model-step"
        and fix_child.lane_id == fix_parent.lane_id
        and fix_child.state == "awaiting-callback",
        (fix_phase_request, fix_parent, fix_child),
    )
    pipeline_events = [
        json.loads(line)
        for line in (
            vault / ".vault-meta" / "pipeline-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    check(
        "dispatch emits one content-free compiled pipeline identity",
        any(
            event.get("op") == "compiled-pipeline"
            and event.get("actor") == "dispatch"
            and event.get("identifiers", {}).get("pipeline_id")
            == "engineering"
            and event.get("identifiers", {}).get("profile") == "fix"
            and event.get("identifiers", {}).get("definition_sha")
            == fix_parent.spec.contract_sha256
            and event.get("counts", {}).get("bounded_loop_iteration")
            == 0
            for event in pipeline_events
        ),
        pipeline_events,
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
