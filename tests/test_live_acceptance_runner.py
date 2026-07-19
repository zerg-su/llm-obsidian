#!/usr/bin/env python3
"""Hermetic contract tests for the repo-shipped live acceptance runner."""

from __future__ import annotations

import json
import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "live-acceptance-runner.py"
RELEASE = ROOT / "scripts" / "release-acceptance.py"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}: {detail}")


def row(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "phase": "final",
        "skill": "clarify",
        "runtime": "codex",
        "scenario": "conversation-readonly",
        "expected": "Ask one material question at a time and make no repository mutation.",
    }
    value.update(updates)
    return value


def run(*args: str, payload: dict[str, object], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        input=json.dumps(payload) + "\n",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


with tempfile.TemporaryDirectory(prefix="live-acceptance-runner-test.") as raw:
    tmp = Path(raw)
    backend = tmp / "backend.py"
    backend.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin)\n"
        "print(json.dumps({**r,'verdict':'pass','model':'fixture-model','effort':'high',"
        "'actual':'asked one bounded question','cleanup':'no files changed',"
        "'evidence':'clean git status'}))\n",
        encoding="utf-8",
    )
    result = run("--backend-command", sys.executable, str(backend), payload=row())
    payload = json.loads(result.stdout)
    check("backend pass exits zero", result.returncode == 0, result.stderr)
    check("backend row identity preserved", payload["skill"] == "clarify" and payload["runtime"] == "codex")
    check("backend evidence preserved", payload["verdict"] == "pass" and payload["evidence"] == "clean git status")

    mismatch = tmp / "mismatch.py"
    mismatch.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin); r['skill']='daily'; r.update(verdict='pass',model='x',effort='high',actual='x',cleanup='x',evidence='x')\n"
        "print(json.dumps(r))\n",
        encoding="utf-8",
    )
    result = run("--backend-command", sys.executable, str(mismatch), payload=row())
    blocked = json.loads(result.stdout)
    check("mismatched backend is bounded data", result.returncode == 0 and blocked["verdict"] == "blocked", result.stderr)
    check("mismatched backend names identity defect", "does not match" in blocked["defect"])

    secret = tmp / "secret.py"
    secret.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin); r.update(verdict='pass',model='x',effort='high',actual='token=abcdefghijk',cleanup='x',evidence='x')\n"
        "print(json.dumps(r))\n",
        encoding="utf-8",
    )
    result = run("--backend-command", sys.executable, str(secret), payload=row())
    blocked = json.loads(result.stdout)
    check("credential-like backend result is sanitized", blocked["verdict"] == "pass")
    check("credential value omitted", "abcdefghijk" not in result.stdout)

    env = os.environ.copy()
    env.pop("CMUX_SURFACE_ID", None)
    result = run(payload=row(), env=env)
    blocked = json.loads(result.stdout)
    check("missing cmux is a valid blocked cell", result.returncode == 0 and blocked["verdict"] == "blocked")
    check("missing cmux is actionable", "CMUX_SURFACE_ID" in blocked["defect"])

    result = run(payload=row(scenario="unregistered"))
    check("unknown scenario fails before operation", result.returncode == 3 and not result.stdout, result.stderr)

    result = run(payload=row(expected="substituted expectation"))
    check("substituted fixture contract is rejected", result.returncode == 3 and not result.stdout, result.stderr)

    fixture_registry = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
    missing_fixture = tmp / "missing-fixture.json"
    missing_fixture_data = json.loads(json.dumps(fixture_registry))
    missing_fixture_data["skills"]["clarify"].pop("fixture")
    missing_fixture.write_text(json.dumps(missing_fixture_data), encoding="utf-8")
    result = run("--skills", str(missing_fixture), payload=row())
    check("missing skill fixture is rejected", result.returncode == 3 and not result.stdout, result.stderr)

    release = subprocess.run(
        [sys.executable, str(RELEASE), "check"], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )
    check("release check validates scenario parity", release.returncode == 0 and "skills x 2 runtimes" in release.stdout, release.stderr)

    spec = importlib.util.spec_from_file_location("live_acceptance_runner_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    override_repo = tmp / "override-repo"
    (override_repo / "config").mkdir(parents=True)
    module.install_acceptance_model_overrides(override_repo, {"claude": "sonnet"})
    override_text = (override_repo / "config/model-routing.local.toml").read_text(encoding="utf-8")
    from model_routing import validate_local_config
    from acceptance_fingerprints import canonical_generation, read_manifest

    manifest = read_manifest(ROOT)
    check(
        "Codex Sol and Terra share one major generation",
        canonical_generation("gpt-5.6-sol", manifest)
        == canonical_generation("gpt-5.6-terra", manifest)
        == "codex:5.6",
    )

    validated_override = validate_local_config(ROOT, override_text)
    check(
        "live acceptance supports sandbox-only cheaper model aliases",
        '[runtimes.claude]' in override_text
        and '[roles.review.claude]' in override_text
        and 'model = "sonnet"' in override_text
        and '"sonnet" = "claude"' in override_text
        and validated_override.runtime_default("claude")["model"] == "sonnet",
    )
    repo = tmp / "cleanup-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()
    (repo / ".acceptance-sandbox.json").write_text("{}\n", encoding="utf-8")
    clean, _ = module.sandbox_cleanup_proof(repo, commit)
    check("runner-owned marker is cleanup-neutral", clean)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("independent cleanup proof catches product drift", not clean and "changes" in reason)
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (repo / ".vault-meta").mkdir()
    (repo / "wiki" / "sources").mkdir(parents=True)
    (repo / ".vault-meta" / "address-counter.txt").write_text("1\n", encoding="utf-8")
    (repo / "wiki" / "sources" / "_index.md").write_text("# Sources\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".vault-meta/address-counter.txt", "wiki/sources/_index.md"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "bookkeeping fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()
    (repo / ".vault-meta" / "address-counter.txt").write_text("2\n", encoding="utf-8")
    (repo / "wiki" / "sources" / "_index.md").write_text(
        "# Sources\n- disposable\n", encoding="utf-8"
    )
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("runner accepts disposable vault bookkeeping", clean and "bookkeeping" in reason, reason)
    (repo / "unexpected.md").write_text("product residue\n", encoding="utf-8")
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("runner rejects untracked product output", not clean and "changes" in reason, reason)
    (repo / "unexpected.md").unlink()

    claude_argv, _ = module.agent_argv("claude", repo, "fixture-model", "high", "prompt")
    check("Claude acceptance runs outside project hooks", "--add-dir" in claude_argv and str(repo) in claude_argv)
    module.validated_cmux_socket_path = lambda: Path("/tmp/fixture-cmux.sock")
    codex_argv, _ = module.agent_argv("codex", repo, "fixture-model", "high", "prompt")
    check("Codex acceptance disables hooks", "--disable" in codex_argv and "hooks" in codex_argv)
    check("Codex acceptance keeps Fast user-only", 'service_tier="default"' in codex_argv)
    check(
        "Codex acceptance grants only disposable Git metadata",
        codex_argv[codex_argv.index("--add-dir") + 1] == str(module.resolved_git_common_dir(repo)),
    )
    scratch = tmp / "scratch"
    scratch.mkdir()
    _argv, scratch_env = module.agent_argv(
        "claude", repo, "fixture-model", "high", "prompt", scratch_root=scratch,
        surface="00000000-0000-0000-0000-000000000003",
    )
    check("acceptance temp files are operation-scoped", all(scratch_env[name] == str(scratch) for name in ("TMPDIR", "TMP", "TEMP")))
    check(
        "acceptance agent is anchored to its exact surface",
        scratch_env["CMUX_SURFACE_ID"] == "00000000-0000-0000-0000-000000000003",
    )

    settling = tmp / "settling-outbox.json"
    state: dict[str, object] = {}
    settling.write_text('{"schema_version":', encoding="utf-8")
    check("partial outbox waits during grace", module.settled_outbox(settling, state, 10.0) is None)
    settling.write_text('{"schema_version": 1}', encoding="utf-8")
    check("first valid outbox sample waits for stability", module.settled_outbox(settling, state, 10.2) is None)
    check("stable valid outbox is accepted", module.settled_outbox(settling, state, 11.3) == {"schema_version": 1})

    invalid = tmp / "invalid-outbox.json"
    invalid.write_text("{", encoding="utf-8")
    invalid_state: dict[str, object] = {}
    module.settled_outbox(invalid, invalid_state, 20.0)
    try:
        module.settled_outbox(invalid, invalid_state, 25.1)
    except module.AcceptanceRunnerError as exc:
        check("persistently invalid outbox fails after bounded grace", "grace period" in str(exc))
    else:
        check("persistently invalid outbox fails after bounded grace", False, "no error")

    symlink = tmp / "symlink-outbox.json"
    symlink.symlink_to(settling)
    try:
        module.settled_outbox(symlink, {}, 30.0)
    except module.AcceptanceRunnerError as exc:
        check("outbox symlink is rejected", "non-symlink" in str(exc))
    else:
        check("outbox symlink is rejected", False, "no error")

    oversized = tmp / "oversized-outbox.json"
    oversized.write_bytes(b"x" * (module.OUTBOX_MAX_BYTES + 1))
    try:
        module.settled_outbox(oversized, {}, 40.0)
    except module.AcceptanceRunnerError as exc:
        check("oversized outbox is rejected", "size limit" in str(exc))
    else:
        check("oversized outbox is rejected", False, "no error")

    exited = tmp / "agent-exit.json"
    exited.write_text('{"schema_version": 1}\n', encoding="utf-8")
    close_calls: list[list[str]] = []
    original_run = module.subprocess.run
    original_send_surface = module.send_surface
    module.send_surface = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("already-exited agent must not receive /exit")
    )
    module.subprocess.run = lambda argv, **_kwargs: (
        close_calls.append(list(argv))
        or subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
    )
    try:
        close_result = module.close_surface("00000000-0000-0000-0000-000000000001", "codex", exited)
    finally:
        module.subprocess.run = original_run
        module.send_surface = original_send_surface
    check("interrupted exited agent closes without a second command", close_result == "exact surface closed")
    check("interrupted cleanup targets exact surface once", close_calls == [[
        "cmux", "close-surface", "--surface", "00000000-0000-0000-0000-000000000001"
    ]])

    confirming_exit = tmp / "confirming-agent-exit.json"
    confirm_calls: list[list[str]] = []
    module.send_surface = lambda *_args, **_kwargs: None
    def confirm_run(argv, **_kwargs):
        confirm_calls.append(list(argv))
        if argv[1] == "read-screen":
            return subprocess.CompletedProcess(argv, 0, stdout="1. Exit anyway\n2. Move to background and exit\n3. Stay\n", stderr="")
        if argv[1] == "send-key":
            confirming_exit.write_text('{"schema_version": 1}\n', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
    module.subprocess.run = confirm_run
    try:
        close_result = module.close_surface("00000000-0000-0000-0000-000000000002", "claude", confirming_exit)
    finally:
        module.subprocess.run = original_run
        module.send_surface = original_send_surface
    check("Claude background-task exit confirmation is handled", close_result == "exact surface closed")
    check("Claude exact exit confirmation is submitted", any(call[1:3] == ["send-key", "--surface"] for call in confirm_calls))
    check("agent exit grace covers slow interactive shutdown", module.AGENT_EXIT_GRACE_SECONDS >= 300)

    prompt = module.prompt_text(
        row(), module.load_scenarios()["conversation-readonly"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit, "Ask the exact fixture question and finish.",
    )
    check("prompt embeds exact per-skill fixture", "Ask the exact fixture question and finish." in prompt)
    check("conversation fixture forbids human wait", "instead of waiting for another human message" in prompt)
    check("prompt delegates runner-owned cleanup", "Do not run `git restore`" in prompt and "run-scoped temporary directory" in prompt)
    check("prompt forbids temp-root drift", "pass `--tmp-root`/`--state-root`" in prompt and "`TMPDIR`/`TMP`/`TEMP`" in prompt)
    check("prompt forbids duplicate dry-run", "Do not precede it with a `--no-spawn`" in prompt)
    check("prompt pins runner-owned nested worktrees", "Use `LLM_OBSIDIAN_WORKTREES`" in prompt)
    check("prompt validates before disposable cleanup", "Validate product output before removing" in prompt)
    check(
        "prompt permits native subscription without exposing credentials",
        "already authenticated" in prompt and "Never read, copy, print, export" in prompt,
    )

    dispatch_fixture = module.load_skill_fixtures()["dispatch"]["fixture"]
    check(
        "dispatch fixture delegates deterministic proof and cleanup to the runner",
        "runner-prepared approved plan" in dispatch_fixture
        and "typed approve review" in dispatch_fixture
        and "independent runner proof" in dispatch_fixture
        and "substitute narrative evidence" in dispatch_fixture,
    )
    check(
        "Codex dispatch fixture provisions ignored runtime config only",
        "runtime.env.example" in inspect.getsource(module.dispatch_acceptance_fixture)
        and "codex-sync" not in inspect.getsource(module.dispatch_acceptance_fixture),
    )

    prepared = {
        "task_name": "acceptance-dispatch-deadbeef",
        "branch": "task/acceptance-dispatch-deadbeef",
        "plan_path": str(repo / "wiki/plans/acceptance.md"),
        "fixture_rel": "acceptance-dispatch-deadbeef.txt",
        "fixture_text": "dispatch acceptance deadbeef\n",
        "result_title": "Acceptance dispatch deadbeef result",
        "nested_worktree": str(repo / ".vault-meta/acceptance-worktrees/sandbox-acceptance-dispatch-deadbeef"),
        "dispatch_spec": str(repo / ".vault-meta/acceptance/dispatch-request.json"),
        "request_id": "11111111-1111-4111-8111-111111111111",
        "coordinator_runtime": "codex",
    }
    exact_dispatch = module.dispatch_fixture_prompt(prepared)
    dispatch_prompt = module.prompt_text(
        row(skill="dispatch", scenario="dispatch-review-reap",
            expected="Start one isolated task worktree and deliver its approved plan exactly once."),
        module.load_scenarios()["dispatch-review-reap"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit, exact_dispatch, prepared,
    )
    check(
        "dispatch prompt pins one exact runner-prepared operation",
        all(value in dispatch_prompt for value in (
            prepared["task_name"], prepared["branch"], prepared["plan_path"],
            prepared["nested_worktree"], prepared["result_title"], prepared["dispatch_spec"],
        )),
    )
    check(
        "dispatch prompt uses mechanical runner once",
        "dispatch-runner.py start --spec" in dispatch_prompt
        and "do not reproduce its setup commands manually" in dispatch_prompt,
    )
    check(
        "dispatch acceptance forbids invented summary links",
        "typed summary body free of invented" in dispatch_prompt
        and "attaches the validated review archive link itself" in dispatch_prompt,
    )
    check(
        "dispatch coordinator returns idle for typed callbacks",
        "finish the coordinator turn and return" in dispatch_prompt
        and "Do not shell-poll task files" in dispatch_prompt
        and "agent wait tools" in dispatch_prompt,
    )
    check(
        "dispatch launch turn cannot publish final acceptance",
        "Do not publish the acceptance agent outbox in that launch turn" in dispatch_prompt
        and "returning idle without an outbox keeps this cell running" in dispatch_prompt,
    )
    check(
        "dispatch start failure has a bounded immediate outbox path",
        "start` invocation exits non-zero" in dispatch_prompt
        and "not retry it, perform open-ended diagnosis" in dispatch_prompt
        and "Publish the typed fail/blocked outbox immediately" in dispatch_prompt,
    )
    check(
        "dispatch successful reap avoids duplicate model-side proof",
        "final reap runner returns `status: complete`" in dispatch_prompt
        and "publish the\n  typed pass outbox immediately" in dispatch_prompt
        and "Do not enumerate proof files" in dispatch_prompt
        and "outer acceptance runner performs" in dispatch_prompt,
    )
    module.write_dispatch_acceptance_request(
        repo,
        prepared,
        source_commit=commit,
        coordinator_surface="22222222-2222-4222-8222-222222222222",
        coordinator_model="gpt-5.6-sol",
        coordinator_effort="high",
    )
    prepared_request = json.loads(Path(prepared["dispatch_spec"]).read_text(encoding="utf-8"))
    check(
        "acceptance request delegates current session route to runner",
        "origin_session" not in prepared_request
        and prepared_request["session_route"]["source"] == "acceptance-runner",
    )
    check(
        "acceptance request binds exact coordinator surface",
        prepared_request["origin_surface"] == "22222222-2222-4222-8222-222222222222",
    )
    Path(prepared["dispatch_spec"]).unlink()
    check(
        "dispatch prompt preserves durable proof artifacts",
        "Leave them in place" in dispatch_prompt and "independently proves the exact commit" in dispatch_prompt,
    )

    cleanup_run = tmp / "cleanup-run"
    cleanup_sandbox = cleanup_run / "sandbox"
    system_tmp = tmp / "system-tmp"
    system_tmp.mkdir()
    original_gettempdir = module.tempfile.gettempdir
    module.tempfile.gettempdir = lambda: str(system_tmp)
    cleanup_scratch = module.scratch_root_for(cleanup_run)
    cleanup_sandbox.mkdir(parents=True)
    cleanup_scratch.mkdir()
    (cleanup_sandbox / ".acceptance-sandbox.json").write_text("{}\n", encoding="utf-8")
    (cleanup_scratch / ".acceptance-scratch.json").write_text(
        json.dumps({"schema_version": 1, "run_dir": str(cleanup_run)}) + "\n",
        encoding="utf-8",
    )
    (cleanup_scratch / "nested").mkdir()
    (cleanup_scratch / "nested" / "artifact.json").write_text("{}\n", encoding="utf-8")
    module.safe_cleanup(cleanup_run)
    module.tempfile.gettempdir = original_gettempdir
    check("runner removes exact sandbox and scratch roots", not cleanup_sandbox.exists() and not cleanup_scratch.exists())

    proof_repo = tmp / "dispatch-proof"
    proof_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proof_repo, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=proof_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=proof_repo, check=True)
    (proof_repo / ".gitignore").write_text(
        ".vault-meta/task-sessions/\n", encoding="utf-8",
    )
    (proof_repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "base.txt"], cwd=proof_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=proof_repo, check=True)
    source = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proof_repo, text=True, capture_output=True, check=True,
    ).stdout.strip()
    nested = proof_repo / ".vault-meta/acceptance-worktrees/sandbox-acceptance-dispatch-proof"
    nested.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(proof_repo), str(nested)], check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=nested, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=nested, check=True)
    fixture_text = "dispatch acceptance proof\n"
    (nested / "acceptance-dispatch-proof.txt").write_text(fixture_text, encoding="utf-8")
    subprocess.run(["git", "add", "acceptance-dispatch-proof.txt"], cwd=nested, check=True)
    subprocess.run(["git", "commit", "-qm", "add exact acceptance fixture"], cwd=nested, check=True)
    plan = proof_repo / "wiki/plans/acceptance-dispatch-proof.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\ntype: plan\nstatus: executed\n---\n# Proof plan\n\nРезультат: [[Acceptance dispatch proof result]]\n",
        encoding="utf-8",
    )
    result_page = proof_repo / "wiki/meta/sessions/Acceptance dispatch proof result.md"
    result_page.parent.mkdir(parents=True)
    result_page.write_text("# Acceptance dispatch proof result\n", encoding="utf-8")
    project_id = "11111111-1111-4111-8111-111111111111"
    task_id = "22222222-2222-4222-8222-222222222222"
    meta = {
        "version": 3,
        "project_id": project_id,
        "task_id": task_id,
        "task_name": "acceptance-dispatch-proof",
        "branch": "task/acceptance-dispatch-proof",
        "plan_file": str(plan),
        "review_policy": {"mode": "light"},
        "reap_policy": {"mode": "final", "title": "Acceptance dispatch proof result"},
    }
    (nested / ".task-meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    task_root = proof_repo / ".vault-meta/task-sessions/projects" / project_id / "tasks" / task_id
    operation = task_root / "lanes/review/operations/one"
    operation.mkdir(parents=True)
    (task_root / "task.json").write_text(json.dumps({
        "project_id": project_id, "task_id": task_id,
        "status": "archived", "worktrees": [str(nested.resolve())],
    }) + "\n", encoding="utf-8")
    review = operation / ".task-review.json"
    review.write_text(json.dumps({
        "schema_version": 1, "mode": "light", "verdict": "approve",
    }) + "\n", encoding="utf-8")
    review_page = proof_repo / "wiki/meta/reviews/Acceptance dispatch proof review.md"
    review_page.parent.mkdir(parents=True)
    review_page.write_text("# Acceptance dispatch proof review\n", encoding="utf-8")
    (operation / ".review-archive.json").write_text(json.dumps({
        "schema_version": 1,
        "status": "already-current",
        "path": "wiki/meta/reviews/Acceptance dispatch proof review.md",
        "content_sha256": module.hashlib.sha256(review_page.read_bytes()).hexdigest(),
    }) + "\n", encoding="utf-8")
    (nested / ".task-reap-complete.json").write_text(json.dumps({
        "validated": True,
        "task_session_status": "archived",
        "plan_path": str(plan),
        "result_path": str(result_page),
        "result_sha256": module.hashlib.sha256(result_page.read_bytes()).hexdigest(),
    }) + "\n", encoding="utf-8")
    proof_fixture = {
        "task_name": "acceptance-dispatch-proof",
        "branch": "task/acceptance-dispatch-proof",
        "plan_rel": "wiki/plans/acceptance-dispatch-proof.md",
        "plan_path": str(plan.resolve()),
        "fixture_rel": "acceptance-dispatch-proof.txt",
        "fixture_text": fixture_text,
        "result_title": "Acceptance dispatch proof result",
        "nested_worktree": str(nested.resolve()),
    }
    proved, reason = module.dispatch_acceptance_proof(proof_repo, source, proof_fixture)
    check("dispatch proof accepts the complete durable lifecycle", proved, reason)
    review.unlink()
    proved, reason = module.dispatch_acceptance_proof(proof_repo, source, proof_fixture)
    check("dispatch proof rejects a narrative-only pass without typed review", not proved and "typed approve" in reason, reason)

    owned_nested = repo / ".vault-meta" / "acceptance-worktrees" / "task-one"
    owned_nested.mkdir(parents=True)
    (owned_nested / "fixture.txt").write_text("runner owned\n", encoding="utf-8")
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("runner contains its exact nested worktree root", clean, reason)

    child_root = tmp / "child-sandbox"
    child_state = child_root / ".vault-meta/task-sessions/projects/p/tasks/t/lanes/l/operations/o/state.json"
    child_state.parent.mkdir(parents=True)
    child_state.write_text(json.dumps({
        "coordinator_surface": "00000000-0000-0000-0000-000000000003",
        "fetch_surface": "00000000-0000-0000-0000-000000000004",
    }), encoding="utf-8")
    standalone_state = child_root / ".vault-meta/research-runs/r/state.json"
    standalone_state.parent.mkdir(parents=True)
    standalone_state.write_text(json.dumps({
        "coordinator_surface": "00000000-0000-0000-0000-000000000003",
        "synth_surface": "00000000-0000-0000-0000-000000000005",
    }), encoding="utf-8")
    dispatched_meta = child_root / ".vault-meta/acceptance-worktrees/task-one/.task-meta.json"
    dispatched_meta.parent.mkdir(parents=True)
    dispatched_meta.write_text(json.dumps({
        "wiki_surface": "00000000-0000-0000-0000-000000000003",
        "task_surface": "00000000-0000-0000-0000-000000000006",
    }), encoding="utf-8")
    child_calls: list[list[str]] = []
    module.subprocess.run = lambda argv, **_kwargs: (
        child_calls.append(list(argv))
        or subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
    )
    try:
        child_closed, child_failures = module.close_operation_children(
            child_root, "00000000-0000-0000-0000-000000000003"
        )
    finally:
        module.subprocess.run = original_run
    check("interrupted operation closes exact registered children", child_closed == 3 and not child_failures)
    check("registered child close never targets coordinator", {call[-1] for call in child_calls} == {
        "00000000-0000-0000-0000-000000000004",
        "00000000-0000-0000-0000-000000000005",
        "00000000-0000-0000-0000-000000000006",
    })
    surface_order: list[str] = []
    original_close_surface = module.close_surface
    original_close_children = module.close_operation_children
    original_wait_children = module.wait_for_operation_children
    module.close_surface = lambda *_args, **_kwargs: surface_order.append("coordinator") or "exact surface closed"
    module.wait_for_operation_children = lambda *_args, **_kwargs: surface_order.append("wait")
    module.close_operation_children = lambda *_args, **_kwargs: (surface_order.append("children") or (2, []))
    try:
        settled = module.settle_operation_surfaces(
            child_root,
            "00000000-0000-0000-0000-000000000003",
            "codex",
            child_root / "agent-exit.json",
        )
    finally:
        module.close_surface = original_close_surface
        module.wait_for_operation_children = original_wait_children
        module.close_operation_children = original_close_children
    check(
        "cleanup stops coordinator and gives children an auto-close grace",
        surface_order == ["coordinator", "wait", "children"]
        and settled == ("exact surface closed", 2, [])
        and module.CHILD_SURFACE_SETTLE_SECONDS >= 30,
    )

registry = json.loads((ROOT / "evals/acceptance/scenarios.json").read_text(encoding="utf-8"))
skills = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
check("acceptance runtime is gitignored", ".vault-meta/acceptance/" in (ROOT / ".gitignore").read_text(encoding="utf-8"))
check(
    "scenario registry exactly covers matrix",
    set(registry["scenarios"]) == {item["scenario"] for item in skills["skills"].values()},
)
check(
    "every skill has one bounded live fixture",
    all(isinstance(item.get("fixture"), str) and item["fixture"].strip() for item in skills["skills"].values()),
)
check(
    "autoresearch fixture pins its bounded URL and topic boundary",
    "https://docs.python.org/3/library/functions.html#len" in skills["skills"]["autoresearch"]["fixture"]
    and "only the research request" in skills["skills"]["autoresearch"]["fixture"],
)

if failures:
    print(f"\n{len(failures)} live acceptance runner test(s) failed")
    raise SystemExit(1)
print("\nAll live acceptance runner tests passed.")
