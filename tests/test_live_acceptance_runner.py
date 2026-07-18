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

    claude_argv, _ = module.agent_argv("claude", repo, "fixture-model", "high", "prompt")
    check("Claude acceptance runs outside project hooks", "--add-dir" in claude_argv and str(repo) in claude_argv)
    module.validated_cmux_socket_path = lambda: Path("/tmp/fixture-cmux.sock")
    codex_argv, _ = module.agent_argv("codex", repo, "fixture-model", "high", "prompt")
    check("Codex acceptance disables hooks", "--disable" in codex_argv and "hooks" in codex_argv)

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

    prompt = module.prompt_text(
        row(), module.load_scenarios()["conversation-readonly"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit, "Ask the exact fixture question and finish.",
    )
    check("prompt embeds exact per-skill fixture", "Ask the exact fixture question and finish." in prompt)
    check("conversation fixture forbids human wait", "instead of waiting for another human message" in prompt)

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
