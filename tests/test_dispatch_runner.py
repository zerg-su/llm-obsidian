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
    shutil.copyfile(
        ROOT / "config" / "harness.toml",
        vault / "config" / "harness.toml",
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
        "reap": {"type": "repo-touch", "title": "Fast dispatch result"},
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
    check("classic dispatch defaults to split placement", request["placement"] == "split")
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
        "write `.task-summary.json` with exactly this canonical JSON shape"
        in prompt
        and (
            '{"schema_version":1,"type":"repo-touch",'
            '"title":"Fast dispatch result","session":"unit-session",'
            '"body":"<bounded Markdown summary>"}'
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
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    custom_raw = json.loads(json.dumps(raw_request))
    custom_raw["pipeline"] = "custom"
    custom_raw["custom_pipeline_spec"] = str(custom_spec)
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
        [sys.executable, str(SCRIPT), "start", "--spec", str(cli_spec)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "custom CLI start fails before effects without exact approval",
        cli_without_approval.returncode == 3
        and "--approval-token" in cli_without_approval.stderr
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
    check(
        "custom start installs the approved snapshot despite mutable spec drift",
        frozen_spec.spec_id == custom_payload["spec_id"]
        and frozen_spec.spec_id != changed_cli_custom["spec_id"]
        and frozen_compiled.definition_sha256
        == cli_exact_challenge["definition_sha256"]
        and runner.approved_plan_sha256(frozen_cli_request)
        == cli_exact_challenge["plan_sha256"]
        and runner.render_task_prompt(frozen_cli_request, config) == cli_prompt
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
    v4_schema = json.loads(
        (ROOT / "schemas" / "task-meta-v4.schema.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "writer output has exact strict v4 schema parity",
        set(meta) == set(v4_schema["required"])
        and set(meta) <= set(v4_schema["properties"])
        and v4_schema["additionalProperties"] is False,
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
        json.dumps({"type": "repo-touch", "title": "Fast dispatch result"}) + "\n",
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
        and workspace_meta["task_window"] == "55555555-5555-4555-8555-555555555555"
        and workspace_meta["reap_policy"]["mode"] == "shared",
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
    original_sync = runner.sync_codex_profile
    original_log = runner.dispatch_log
    sync_failure_raw = json.loads(json.dumps(raw_request))
    sync_failure_raw["request_id"] = str(uuid.uuid4())
    sync_failure_raw["task_name"] = "runtime-sync-failure"
    sync_failure_raw["branch"] = "task/runtime-sync-failure"
    sync_failure_raw["worktree"] = str(
        tmp / "worktrees" / "runtime-sync-failure"
    )
    sync_failure_request = runner.validate_request(sync_failure_raw)
    runner.sync_codex_profile = lambda *_args, **_kwargs: (_ for _ in ()).throw(
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
    runner.sync_codex_profile = lambda *_args, **_kwargs: None
    runner.dispatch_log = lambda *_args, **_kwargs: None
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
        runner.sync_codex_profile = original_sync
        runner.dispatch_log = original_log
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
