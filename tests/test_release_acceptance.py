#!/usr/bin/env python3
"""Hermetic tests for the dynamic release acceptance contract."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release-acceptance.py"
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_fingerprints import cell_metadata, read_manifest


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


result = run("check")
check("dynamic coverage", result.returncode == 0 and "runtimes" in result.stdout, result.stderr)

with tempfile.TemporaryDirectory(prefix="release-acceptance-test.") as raw:
    tmp = Path(raw)
    matrix = tmp / "matrix.json"
    result = run("matrix", "--phase", "baseline", "--output", str(matrix))
    data = json.loads(matrix.read_text(encoding="utf-8"))
    skills = sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md"))
    check("matrix exit", result.returncode == 0, result.stderr)
    check("matrix complete", len(data["rows"]) == len(skills) * 2)
    check("matrix runtime parity", {row["runtime"] for row in data["rows"]} == {"claude", "codex"})

    fragment_root = tmp / "fragment-root"
    (fragment_root / "evals/acceptance").mkdir(parents=True)
    (fragment_root / "skills/close").mkdir(parents=True)
    (fragment_root / "skills/clarify").mkdir(parents=True)
    for rel in ("evals/acceptance/skills.json", "evals/acceptance/scenarios.json"):
        (fragment_root / rel).write_bytes((ROOT / rel).read_bytes())
    (fragment_root / "skills/close/SKILL.md").write_text("# Close\n", encoding="utf-8")
    (fragment_root / "skills/clarify/SKILL.md").write_text("# Clarify\n", encoding="utf-8")
    fragment_manifest = read_manifest(ROOT)
    fixed_environment = {"os": "test", "os_release": "1", "architecture": "test", "cmux": "1", "claude": "1", "codex": "1"}
    fixed_generations = {
        "claude": {"model": "opus", "generation": "claude:opus-4.8"},
        "codex": {"model": "gpt-5.6-sol", "generation": "codex:5.6"},
    }
    close_row = next(row for row in data["rows"] if row["skill"] == "close" and row["runtime"] == "claude")
    clarify_row = next(row for row in data["rows"] if row["skill"] == "clarify" and row["runtime"] == "claude")
    close_before = cell_metadata(fragment_root, fragment_manifest, close_row, environment=fixed_environment, generations=fixed_generations)
    clarify_before = cell_metadata(fragment_root, fragment_manifest, clarify_row, environment=fixed_environment, generations=fixed_generations)
    fragment_skills_path = fragment_root / "evals/acceptance/skills.json"
    fragment_skills = json.loads(fragment_skills_path.read_text(encoding="utf-8"))
    fragment_skills["skills"]["close"]["fixture"] += " changed"
    fragment_skills_path.write_text(json.dumps(fragment_skills), encoding="utf-8")
    close_after = cell_metadata(fragment_root, fragment_manifest, close_row, environment=fixed_environment, generations=fixed_generations)
    clarify_after = cell_metadata(fragment_root, fragment_manifest, clarify_row, environment=fixed_environment, generations=fixed_generations)
    check(
        "shared registry hashes only the exact cell fragment",
        close_before["cell_fingerprint"] != close_after["cell_fingerprint"]
        and clarify_before["cell_fingerprint"] == clarify_after["cell_fingerprint"],
    )

    spec = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
    missing = tmp / "missing.json"
    missing_data = json.loads(json.dumps(spec))
    missing_data["skills"].pop(skills[0])
    missing.write_text(json.dumps(missing_data), encoding="utf-8")
    result = run("--spec", str(missing), "check")
    check("missing skill rejected", result.returncode == 3 and "coverage mismatch" in result.stderr)

    stale = tmp / "stale.json"
    stale_data = json.loads(json.dumps(spec))
    stale_data["skills"]["stale-skill"] = {
        "scenario": "stale",
        "expected": "Never exists.",
        "fixture": "Run the impossible stale fixture.",
    }
    stale.write_text(json.dumps(stale_data), encoding="utf-8")
    result = run("--spec", str(stale), "check")
    check("stale skill rejected", result.returncode == 3 and "coverage mismatch" in result.stderr)

    no_fixture = tmp / "no-fixture.json"
    no_fixture_data = json.loads(json.dumps(spec))
    no_fixture_data["skills"][skills[0]].pop("fixture")
    no_fixture.write_text(json.dumps(no_fixture_data), encoding="utf-8")
    result = run("--spec", str(no_fixture), "check")
    check("missing live fixture rejected", result.returncode == 3 and "invalid live fixture" in result.stderr)

    scenarios = json.loads((ROOT / "evals/acceptance/scenarios.json").read_text(encoding="utf-8"))
    missing_scenario = tmp / "missing-scenario.json"
    missing_scenario_data = json.loads(json.dumps(scenarios))
    missing_scenario_data["scenarios"].pop(next(iter(missing_scenario_data["scenarios"])))
    missing_scenario.write_text(json.dumps(missing_scenario_data), encoding="utf-8")
    result = run("--scenarios", str(missing_scenario), "check")
    check("missing scenario rejected", result.returncode == 3 and "scenario coverage mismatch" in result.stderr)

    runner = tmp / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high',"
        "'actual':'bounded pass','cleanup':'none','evidence':'fixture'}))\n",
        encoding="utf-8",
    )
    report = tmp / "report.json"
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("green report", result.returncode == 0 and payload["summary"]["failed"] == 0, result.stderr)
    check("schema-2 evidence", payload["schema_version"] == 2)
    check(
        "rows carry content-addressed proof",
        all(
            row["cell_fingerprint"] and row["dependencies"] and row["generation"]
            and row["row_integrity_sha256"] and row["reason"] == "executed"
            for row in payload["rows"]
        ),
    )
    check("expected retained", all(row["expected"] for row in payload["rows"]))
    check("duration measured", all(row["duration_seconds"] is not None for row in payload["rows"]))
    check("completed report is checkpoint-compatible", payload["summary"]["complete"] is True and payload["summary"]["pending"] == 0)

    runner.write_text("raise SystemExit(99)\n", encoding="utf-8")
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    check("matching completed report resumes without rerunning cells", result.returncode == 0, result.stderr)
    payload = json.loads(report.read_text(encoding="utf-8"))
    check(
        "reuse reason and evidence age are visible",
        all(row["reason"] == "reused-identical" and row["evidence_age_seconds"] >= 0 for row in payload["rows"]),
    )

    payload["rows"][0]["row_integrity_sha256"] = "0" * 64
    report.write_text(json.dumps(payload), encoding="utf-8")
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check(
        "tampered row reruns while intact rows reuse",
        result.returncode == 1
        and payload["summary"]["verdicts"]["blocked"] == 1
        and sum(row["reason"] == "reused-identical" for row in payload["rows"]) == len(payload["rows"]) - 1,
        result.stderr,
    )

    stale_report = json.loads(report.read_text(encoding="utf-8"))
    stale_report["source_commit"] = "0" * 40
    report.write_text(json.dumps(stale_report), encoding="utf-8")
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check(
        "unknown prior commit reruns instead of guessing",
        result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] == len(payload["rows"]),
        result.stderr,
    )

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("evidence-free pass blocks", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] > 0)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'n-a','decision':''}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("invalid n-a blocks", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] > 0)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'n-a','decision':'approved architecture boundary'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("approved n-a is neutral", result.returncode == 0 and payload["summary"]["accepted"] == len(payload["rows"]))

    result = run(
        "run", "--restart", "--phase", "final", "--runner", str(tmp / "missing-runner"),
        "--timeout", "1", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("missing runner blocks every row", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] == len(payload["rows"]))

    selected_report = tmp / "selected-report.json"
    runner.write_text(
        "import json,sys\nrow=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high','actual':'ok','cleanup':'ok','evidence':'ok'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--phase", "final", "--skill", skills[0],
        "--runner", f"{sys.executable} {runner}", "--timeout", "5", "--report", str(selected_report),
    )
    selected_payload = json.loads(selected_report.read_text(encoding="utf-8"))
    check(
        "skill selection executes two cells and stays visibly partial",
        result.returncode == 0
        and selected_payload["summary"]["completed"] == 2
        and selected_payload["summary"]["pending"] == len(skills) * 2 - 2
        and {row["skill"] for row in selected_payload["rows"]} == {skills[0]},
        result.stderr,
    )
    result = run(
        "run", "--restart", "--phase", "final", "--skill", skills[0],
        "--runner", f"{sys.executable} {runner}", "--timeout", "5", "--report", str(selected_report),
    )
    check("restart cannot silently become partial", result.returncode == 3 and "full matrix" in result.stderr)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high',"
        "'actual':'token=abcdefghijk','cleanup':'none','evidence':'fixture'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--restart", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("credential text sanitized", result.returncode == 0 and all("abcdefghijk" not in row["actual"] for row in payload["rows"]))

    interrupt_marker = tmp / "runner-interrupted"
    interrupt_runner = tmp / "interrupt-runner.py"
    interrupt_runner.write_text(
        "import signal,sys,time\n"
        "from pathlib import Path\n"
        "marker=Path(sys.argv[1])\n"
        "def stop(_signum,_frame): marker.write_text('cleanup-complete\\n'); raise SystemExit(130)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "marker.with_suffix('.started').write_text('started\\n')\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    interrupted = subprocess.Popen(
        [
            sys.executable, str(SCRIPT), "run", "--restart", "--phase", "final",
            "--runner", f"{sys.executable} {interrupt_runner} {interrupt_marker}",
            "--timeout", "60", "--report", str(report),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not interrupt_marker.with_suffix('.started').is_file():
        time.sleep(0.05)
    check("interrupt fixture starts", interrupt_marker.with_suffix('.started').is_file())
    os.kill(interrupted.pid, signal.SIGINT)
    _stdout, interrupt_stderr = interrupted.communicate(timeout=15)
    check("matrix interrupt reaches active runner cleanup", interrupt_marker.is_file())
    check("matrix interrupt exits without traceback", interrupted.returncode == 130 and "Traceback" not in interrupt_stderr)

    incremental = tmp / "incremental"
    (incremental / "skills/demo-a").mkdir(parents=True)
    (incremental / "skills/demo-b").mkdir(parents=True)
    (incremental / "config").mkdir()
    (incremental / "evals").mkdir()
    (incremental / ".gitignore").write_text(".vault-meta/\n", encoding="utf-8")
    (incremental / "skills/demo-a/SKILL.md").write_text("# Demo A\n", encoding="utf-8")
    (incremental / "skills/demo-b/SKILL.md").write_text("# Demo B\n", encoding="utf-8")
    (incremental / "config/model-routing.toml").write_text(
        (ROOT / "config/model-routing.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    manifest = incremental / "config/acceptance-cells.toml"
    manifest.write_text(
        "schema_version = 1\nrunner_contract_version = 2\n"
        "global_dependencies = [\"config/acceptance-cells.toml\", \"config/model-routing.toml\"]\n"
        "[model_generations]\n\"gpt-5.6-sol\" = \"codex:5.6\"\n"
        "opus = \"claude:opus-4.8\"\nfable = \"claude:fable\"\n"
        "[generation_routes]\ninclude = [\"runtimes.codex\", \"runtimes.claude\"]\nexclude = []\n"
        "[scenarios.demo]\ndependencies = []\n",
        encoding="utf-8",
    )
    mini_spec = incremental / "evals/skills.json"
    mini_spec.write_text(json.dumps({
        "schema_version": 1,
        "skills": {
            name: {"scenario": "demo", "expected": "bounded", "fixture": "bounded fixture"}
            for name in ("demo-a", "demo-b")
        },
    }), encoding="utf-8")
    mini_scenarios = incremental / "evals/scenarios.json"
    mini_scenarios.write_text(json.dumps({"schema_version": 1, "scenarios": {"demo": {}}}), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=incremental, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=incremental, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=incremental, check=True)
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=incremental, check=True)
    invocation_log = tmp / "incremental-invocations.jsonl"
    incremental_runner = tmp / "incremental-runner.py"
    incremental_runner.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "row=json.load(sys.stdin)\n"
        "with Path(sys.argv[1]).open('a') as f: f.write(json.dumps([row['skill'],row['runtime']])+'\\n')\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high','actual':'ok','cleanup':'ok','evidence':'ok'}))\n",
        encoding="utf-8",
    )
    incremental_report = incremental / ".vault-meta/acceptance/latest-live.json"

    def incremental_run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(SCRIPT), "--root", str(incremental), "--spec", str(mini_spec),
            "--scenarios", str(mini_scenarios), "--manifest", str(manifest), "run",
            "--phase", "final", "--runner", f"{sys.executable} {incremental_runner} {invocation_log}",
            "--timeout", "5", "--report", str(incremental_report),
        ], text=True, capture_output=True, check=False)

    result = incremental_run()
    check("incremental fixture starts green", result.returncode == 0, result.stderr)
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "skills/demo-a/SKILL.md").write_text("# Demo A\n\nchanged\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "change one cell"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check(
        "cross-commit reuse reruns only affected skill cells",
        result.returncode == 0 and {item[0] for item in calls} == {"demo-a"} and len(calls) == 2,
        result.stderr,
    )
    invocation_log.write_text("", encoding="utf-8")
    (incremental / "unknown.txt").write_text("unknown dependency\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=incremental, check=True)
    subprocess.run(["git", "commit", "-qm", "unknown dependency"], cwd=incremental, check=True)
    result = incremental_run()
    calls = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    check("unknown changed path reruns every cell", result.returncode == 0 and len(calls) == 4, result.stderr)

print("\nAll release acceptance tests passed.")
