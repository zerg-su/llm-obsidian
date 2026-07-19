#!/usr/bin/env python3
"""Hermetic contract tests for the repo-shipped live acceptance runner."""

from __future__ import annotations

import json
import importlib.util
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
    (repo / ".vault-meta" / "address-counter.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", ".vault-meta/address-counter.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "bookkeeping fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()
    (repo / ".vault-meta" / "address-counter.txt").write_text("2\n", encoding="utf-8")
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
    scratch = tmp / "scratch"
    scratch.mkdir()
    _argv, scratch_env = module.agent_argv(
        "claude", repo, "fixture-model", "high", "prompt", scratch_root=scratch
    )
    check("acceptance temp files are operation-scoped", all(scratch_env[name] == str(scratch) for name in ("TMPDIR", "TMP", "TEMP")))

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

    prompt = module.prompt_text(
        row(), module.load_scenarios()["conversation-readonly"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit, "Ask the exact fixture question and finish.",
    )
    check("prompt embeds exact per-skill fixture", "Ask the exact fixture question and finish." in prompt)
    check("conversation fixture forbids human wait", "instead of waiting for another human message" in prompt)
    check("prompt delegates runner-owned cleanup", "Do not run `git restore`" in prompt and "run-scoped temporary directory" in prompt)

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

    child_root = tmp / "child-sandbox"
    child_state = child_root / ".vault-meta/task-sessions/projects/p/tasks/t/lanes/l/operations/o/state.json"
    child_state.parent.mkdir(parents=True)
    child_state.write_text(json.dumps({
        "coordinator_surface": "00000000-0000-0000-0000-000000000003",
        "fetch_surface": "00000000-0000-0000-0000-000000000004",
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
    check("interrupted operation closes exact registered child", child_closed == 1 and not child_failures)
    check("registered child close never targets coordinator", all(call[-1] == "00000000-0000-0000-0000-000000000004" for call in child_calls))

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

if failures:
    print(f"\n{len(failures)} live acceptance runner test(s) failed")
    raise SystemExit(1)
print("\nAll live acceptance runner tests passed.")
