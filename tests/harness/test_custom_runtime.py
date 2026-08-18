#!/usr/bin/env python3
"""End-to-end custom sequence with model-free result submission."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.process import ProcessAdapter
from harness.artifact_repair import publish_pipeline_step_contract
from harness.contracts import OperationSpec, RuntimeRoute
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenPipelineStore,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry
from harness.runtime_worker import (
    _submit_failure_requires_attention,
    run as run_worker,
)
from harness.cmux_wake_source import WakeObservation
from harness.runtime_provider_events import RuntimeProviderEventStream
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.workflows.custom_sequence import custom_step_request, prepare_custom_step
from harness.workflows.review import ReviewContext
from harness.workflows.review_gate import ReviewGateController, ReviewPreset

ORIGIN = "11111111-1111-4111-8111-111111111111"
SURFACE = "22222222-2222-4222-8222-222222222222"
TASK = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
PROFILE = os.environ.get("CUSTOM_RUNTIME_PROFILE", "change")
if PROFILE not in {"change", "fix"}:
    raise SystemExit("CUSTOM_RUNTIME_PROFILE must be change or fix")
BASELINE = f"engineering/{PROFILE}"


def sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class FallbackWakeSource:
    """Hermetic pacing that never claims a cmux event was observed."""

    def start(self) -> bool:
        return True

    def wait(self, timeout: float) -> WakeObservation:
        time.sleep(min(max(0.0, timeout), 0.02))
        return WakeObservation("fallback-poll", observed_at=time.monotonic())

    def retry(self) -> bool:
        return True

    def refresh_generation(self, _generation: int) -> None:
        return None

    def close(self) -> None:
        return None


def custom_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "spec_id": f"e2e-custom-{PROFILE}",
        "version": "1.0.0",
        "intent": BASELINE.replace("/", "-"),
        "task_profile": PROFILE,
        "baseline_pipeline": BASELINE,
        "route_alias": "executor-default",
        "required_capabilities": ["route:resolved"],
        "input_schema": "approved-plan/v1",
        "output_schema": "reap-ready/v1",
        "steps": [
            {"step_id": "design", "primitive_id": "model_step", "primitive_version": "1.0.0", "input_schema": "approved-plan/v1", "output_schema": "approved-plan/v1", "session_mode": "worktree", "semantic_skills": ["dispatch"]},
            {"step_id": "implement", "primitive_id": "model_step", "primitive_version": "1.0.0", "input_schema": "approved-plan/v1", "output_schema": "implementation-result/v1", "session_mode": "parent-child", "semantic_skills": ["tdd"]},
            {"step_id": "verify", "primitive_id": "verify", "primitive_version": "1.0.0", "input_schema": "implementation-result/v1", "output_schema": "verified-result/v1", "session_mode": "verification", "semantic_skills": []},
            {"step_id": "review", "primitive_id": "review", "primitive_version": "1.0.0", "input_schema": "verified-result/v1", "output_schema": "reap-ready/v1", "session_mode": "review", "semantic_skills": ["review"]},
        ],
        "transitions": [
            {"from_step": "design", "outcome": "complete", "target": "implement", "max_traversals": 1},
            {"from_step": "implement", "outcome": "complete", "target": "verify", "max_traversals": 1},
            {"from_step": "verify", "outcome": "complete", "target": "review", "max_traversals": 1},
            {"from_step": "review", "outcome": "complete", "target": "terminal:completed", "max_traversals": 1},
        ],
        "controls": [],
        "budget": {"attempt_limit": 2, "model_restart_limit": 1, "time_budget_seconds": 900, "token_limit": 50000},
        "completion_policy": "attention",
        "requested_permissions": ["git-write", "product-worktree"],
        "requested_side_effects": ["git-write", "worktree"],
        "context_pointers": [],
        "verification_checks": ["diff-check"],
        "review_mode": "skip",
        "human_gates": ["initial-approval"],
        "terminal_outcomes": ["completed", "attention-required"],
    }


with tempfile.TemporaryDirectory(prefix="custom-runtime.") as raw:
    root = Path(raw)
    callback_race = root / "callback-race.json"
    callback_race.write_text("{}\n", encoding="utf-8")
    collided = subprocess.CompletedProcess([], 2, "", "already exists")
    if _submit_failure_requires_attention(collided, callback_race):
        raise AssertionError("published callback collision must remain recoverable")
    callback_race.unlink()
    if not _submit_failure_requires_attention(collided, callback_race):
        raise AssertionError("missing callback after submit failure must require attention")
    vault = root / "vault"
    worktree = root / "worktree"
    state_root = (
        vault
        / ".vault-meta"
        / "harness"
        / "owners"
        / TASK
        / "runtime"
        / TASK
    )
    (vault / "wiki" / "plans").mkdir(parents=True)
    (vault / "config").mkdir()
    (vault / "scripts").mkdir()
    shutil.copy2(ROOT / "config" / "verification-profiles.toml", vault / "config" / "verification-profiles.toml")
    for dependency in sorted((ROOT / "scripts").glob("*.py")):
        shutil.copyfile(dependency, vault / "scripts" / dependency.name)
    (vault / "scripts" / "reap-runner.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    shutil.copytree(ROOT / "scripts" / "harness", vault / "scripts" / "harness")
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Custom Runtime"], cwd=worktree, check=True)
    (worktree / "product.txt").write_text("ready\n", encoding="utf-8")
    # Runtime transport is repository-ignored exactly as in the product
    # checkout (`.git/info/exclude` there), so cleanliness observation sees
    # only real product dirt.
    (worktree / ".gitignore").write_text(
        ".task-*\n..task-*\n.provider-*\n.atomic-*\n"
        ".null-change-retry\n.review-*\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "product.txt", ".gitignore"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=worktree, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, text=True, capture_output=True, check=True).stdout.strip()
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text("# Approved\n", encoding="utf-8")
    plan_sha = sha(plan.read_bytes())

    parsed = parse_pipeline_spec(custom_spec())
    policy = CustomPipelinePolicy.default()
    compiled = compile_custom_spec(parsed, builtin_registry(), policy=policy, capabilities=("route:resolved",))
    card = render_custom_approval(parsed, compiled, policy=policy)
    approval = ExplicitPipelineApproval.for_card(
        definition_sha256=compiled.definition_sha256,
        approval_card=card,
        actor="user",
        decision="approve",
    )
    frozen = freeze_custom_pipeline(parsed, compiled, approval, card)
    route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
    parent_spec = OperationSpec(
        operation_id=TASK,
        idempotency_key=sha("custom-parent"),
        kind="dispatch",
        owner_id=TASK,
        route=route,
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
        contract_sha256=compiled.definition_sha256,
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    parent = store.create(parent_spec, lane_id="custom-lane", run_id="custom-run")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(TASK, TASK, state)
    parent = store.read(TASK, TASK)
    FrozenPipelineStore(store.root / "owners" / TASK / "runtime").save(
        operation_id=TASK,
        spec=parsed,
        frozen=frozen,
        approval=approval,
    )
    first = prepare_custom_step(
        store,
        parent,
        parsed,
        definition_sha256=compiled.definition_sha256,
        approved_plan_sha256=plan_sha,
        initial_head_sha=head,
        receipts=(),
    )
    publish_pipeline_step_contract(
        state_root=state_root,
        worktree=worktree,
        request=custom_step_request(first),
    )
    profile = load_profiles(vault / "config" / "verification-profiles.toml")["scoped"]
    write_json(
        vault / ".vault-meta" / "task-sessions" / "session-bindings" / "coordinator-session" / "binding.json",
        {"session_id": "coordinator-session", "project_id": PROJECT, "task_id": TASK},
    )
    write_json(
        worktree / ".task-meta.json",
        {
            "version": 3,
            "project_id": PROJECT,
            "task_id": TASK,
            "task_name": "Custom runtime",
            "origin_session": "coordinator-session",
            "executor_runtime": "codex",
            "interaction_policy": "unattended",
            "pipeline_policy": {"name": "custom", "source": "custom", "baseline": BASELINE, "definition_sha256": compiled.definition_sha256, "completion_policy": "attention", "total_pass_limit": 2},
            "plan_file": str(plan),
            "approved_plan_sha256": plan_sha,
            "vault_root": str(vault),
            "review_policy": {"mode": "skip", "cross_model": False, "runtime": "", "model": "", "effort": "", "max_verify_iterations": 0, "verification_profile": "scoped", "verification_profile_sha256": profile.sha256, "auto_resolve_severities": ["warning", "nit"], "escalate_severities": ["blocking"]},
            "reap_policy": {"mode": "final", "auto_file": True, "allowed_types": ["session"], "title": "Custom Runtime Result"},
            "surface_policy": {"auto_close": True, "placement": "split"},
            "watchdog_policy": {"enabled": True, "poll_seconds": 30, "warn_after_seconds": 900, "alert_after_seconds": 1200},
            "forbidden_actions": ["push", "deploy", "publish", "delete-worktree", "delete-branch", "expand-scope"],
            "task_surface": SURFACE,
            "worktree": str(worktree),
        },
    )
    ReviewGateController.skip(
        store.root / "review-data" / TASK / TASK,
        dispatch_operation_id=TASK,
        owner_id=TASK,
        preset=ReviewPreset.from_flags(no_review=True),
        context=ReviewContext("packets/task/manifest.json", head, "scoped", profile.sha256),
        product_root=worktree,
    )
    (worktree / ".task-prompt.md").write_text("Execute typed custom steps.\n", encoding="utf-8")

    summary = {"schema_version": 1, "type": "session", "title": "Custom Runtime Result", "session": "executor-session", "body": "Completed custom pipeline."}
    provider = root / "provider.py"
    provider.write_text(
        "import hashlib,json,pathlib,subprocess,sys,time\n"
        "root=pathlib.Path.cwd(); state=pathlib.Path(sys.argv[1]); summary=json.loads(sys.argv[2])\n"
        "request=root/'.task-pipeline-step-request.json'; seen=set()\n"
        "for expected in ('design','implement'):\n"
        "  for _ in range(800):\n"
        "    if request.is_file():\n"
        "      row=json.loads(request.read_text(encoding='utf-8'))\n"
        "      if row.get('step_id')==expected and row.get('operation_id') not in seen: break\n"
        "    time.sleep(0.01)\n"
        "  else: raise SystemExit(3)\n"
        "  pointer=pathlib.Path(row.get('contract_template_pointer',''))\n"
        "  if not pointer.is_absolute() or not pointer.is_file(): raise SystemExit(6)\n"
        "  seen.add(row['operation_id']); output=root/row['output_pointer']; result=root/row['result_pointer']\n"
        "  if not result.is_file(): raise SystemExit(7)\n"
        "  output.parent.mkdir(parents=True,exist_ok=True); output.write_text(expected+' evidence\\n',encoding='utf-8')\n"
        "  head=subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True,check=True).stdout.strip()\n"
        "  result.write_text(json.dumps({'schema_version':1,'status':'complete','outcome':'complete','output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'head_sha':head},sort_keys=True)+'\\n',encoding='utf-8')\n"
        "  for _ in range(800):\n"
        "    receipt=state/'pipeline-custom'/'receipts'/f\"{row['visit']:03d}.json\"\n"
        "    if receipt.is_file(): break\n"
        "    time.sleep(0.01)\n"
        "  else: raise SystemExit(4)\n"
        "for _ in range(800):\n"
        "  if (state/'pipeline-custom'/'finalization-notify.json').is_file(): break\n"
        "  time.sleep(0.01)\n"
        "else: raise SystemExit(5)\n"
        "(root/'.task-summary.json').write_text(json.dumps(summary,sort_keys=True)+'\\n',encoding='utf-8')\n"
        "for _ in range(800):\n"
        "  if (state/'callback-receipt.json').is_file(): break\n"
        "  time.sleep(0.01)\n"
        "else: raise SystemExit(8)\n"
        "time.sleep(0.4)\n",
        encoding="utf-8",
    )
    summary_path = worktree / ".task-summary.json"
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(str(Path(sys.executable).resolve()), str(provider), str(state_root), json.dumps(summary, sort_keys=True)),
        cwd=worktree,
        state_root=state_root,
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=summary_path,
        store_root=store.root,
        owner_id=TASK,
        operation_id=TASK,
        run_id="custom-run",
        surface_id=SURFACE,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=summary_path,
        origin_surface=ORIGIN,
    )
    write_json(
        launch.spec_path.parent / "callback-target.json",
        {"schema_version": 1, "generation": 1, "operation_id": first.spec.operation_id, "run_id": first.run_id, "callback_pointer": ".task-pipeline-step-callback.json"},
    )
    root_provider = RuntimeProviderEventStream.create(
        launch.spec_path.parent / "provider-events",
        owner_id=TASK,
        operation_id=TASK,
        run_id="custom-run",
        generation=1,
        process_identity="a" * 64,
        workspace_id="custom-workspace",
        surface_id=SURFACE,
        input_sha256=sha("initial provider input"),
    )
    root_provider.start()
    if root_provider.reserve_input().action != "send":
        raise AssertionError("root provider input was not reserved")
    root_provider.accept_input()

    verification_calls: list[tuple[str, ...]] = []

    def verify(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(argv, cwd=kwargs["cwd"], text=True, capture_output=True, check=False)
        verification_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_worker(
                launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint",
                cmux_adapter=type(
                    "Cmux",
                    (),
                    {"send": lambda *_args: None, "send_key": lambda *_args: None},
                )(),
                verification_runner=verify,
                wake_source=FallbackWakeSource(),
            )
        )
    )
    thread.start()

    # The worker can be slower than a fixed join under suite load.  Wait for
    # the durable terminal publication instead, then use a bounded join only
    # to prove that the worker relinquishes its local resources.
    publication_deadline = time.monotonic() + 30
    terminal_published = False
    while time.monotonic() < publication_deadline:
        published_root = store.read(TASK, TASK)
        terminal_published = (
            published_root.accepted_callback_kind == "wiki-summary"
            and root_provider.controller.current_state().cursor.result_published
        )
        if terminal_published or not thread.is_alive():
            break
        time.sleep(0.02)
    thread.join(timeout=3)
    final = store.read(TASK, TASK)
    receipts = sorted((state_root / "pipeline-custom" / "receipts").glob("*.json"))
    submit_failure = state_root / "pipeline-custom" / "submit-failed.json"
    callback_target = json.loads(
        (launch.spec_path.parent / "callback-target.json").read_text(
            encoding="utf-8"
        )
    )
    root_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (
                launch.spec_path.parent
                / "provider-events"
                / "generation-1"
                / "events"
            ).glob("*.json")
        )
    ]
    root_result_sha256s = [
        str(event["result_sha256"])
        for event in root_events
        if event.get("kind") == "result-published"
    ]
    if callback_target.get("generation") != 2:
        raise AssertionError(
            ("custom child callback did not retarget generation", callback_target)
        )
    if root_result_sha256s != [final.accepted_callback_sha256]:
        raise AssertionError(
            (
                "accepted terminal summary was not published exactly once on "
                "the immutable root provider generation",
                final.accepted_callback_sha256,
                root_result_sha256s,
            )
        )
    if (
        not terminal_published
        or thread.is_alive()
        or not result
        or result[0] != 0
        or final.state != "finalizing"
        or final.accepted_callback_kind != "wiki-summary"
        or len(receipts) != 2
        or (worktree / ".task-pipeline-step-callback.json").exists()
        or ("git", "diff", "--check") not in verification_calls
    ):
        raise AssertionError(
            (
                thread.is_alive(),
                result,
                final,
                receipts,
                verification_calls,
                submit_failure.read_text(encoding="utf-8")
                if submit_failure.is_file()
                else "",
            )
        )
    print("OK   custom runtime reconciles result-only steps through verify/review")
