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
        "expected": "Ask one material question and make no repository mutation.",
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

registry = json.loads((ROOT / "evals/acceptance/scenarios.json").read_text(encoding="utf-8"))
skills = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
check(
    "scenario registry exactly covers matrix",
    set(registry["scenarios"]) == {item["scenario"] for item in skills["skills"].values()},
)

if failures:
    print(f"\n{len(failures)} live acceptance runner test(s) failed")
    raise SystemExit(1)
print("\nAll live acceptance runner tests passed.")
