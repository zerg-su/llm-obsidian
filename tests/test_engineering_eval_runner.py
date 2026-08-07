#!/usr/bin/env python3
"""Contract tests for the bounded engineering-skill live eval runner."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "engineering-eval-runner.py"
spec = importlib.util.spec_from_file_location("engineering_eval_runner_test", RUNNER)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def check(label: str, value: bool, detail: str = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="engineering-eval-runner.") as raw:
    tmp = Path(raw)
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    capture = tmp / "capture.json"
    fake = fake_bin / "codex"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
prompt = args[-1]
pathlib.Path(os.environ["ENGINEERING_EVAL_CAPTURE"]).write_text(
    json.dumps({"args": args, "prompt": prompt}), encoding="utf-8"
)
output.write_text(
    json.dumps(
        {
            "output": "Keep the cohesive deep module.",
            "artifacts": {
                "decision": "keep-cohesive",
                "mechanical_split": False,
                "size_is_proof": False,
            },
        }
    ),
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    case = {
        "schema_version": 1,
        "id": "engineering.cohesive-small-negative",
        "capability": "codebase-design",
        "input": {
            "scenario": "A cohesive 180-line parser is proposed for a size-only split."
        },
        "assertions": [
            {"kind": "equals", "path": "artifacts.decision", "value": "keep-cohesive"},
            {"kind": "equals", "path": "artifacts.mechanical_split", "value": False},
            {"kind": "equals", "path": "artifacts.size_is_proof", "value": False},
        ],
    }
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["ENGINEERING_EVAL_CAPTURE"] = str(capture)
    proc = subprocess.run(
        ["python3", str(RUNNER)],
        cwd=ROOT,
        env=env,
        input=json.dumps(case),
        text=True,
        capture_output=True,
    )
    check("runner exits green", proc.returncode == 0, proc.stderr)
    check("runner emits typed result", json.loads(proc.stdout)["artifacts"]["decision"] == "keep-cohesive")
    seen = json.loads(capture.read_text(encoding="utf-8"))
    args = seen["args"]
    prompt = seen["prompt"]
    check("runner uses ephemeral read-only Codex", "--ephemeral" in args and args[args.index("--sandbox") + 1] == "read-only")
    check("approval policy is a global Codex option", args.index("--ask-for-approval") < args.index("exec"))
    check("runner defaults to Terra", args[args.index("--model") + 1] == "gpt-5.6-terra")
    check("runner defaults to medium effort", "model_reasoning_effort=\"medium\"" in args)
    check("runner requests structured output", "--output-schema" in args)
    check("scenario reaches model", case["input"]["scenario"] in prompt)
    check("real skill contract reaches model", "deep module" in prompt.lower())
    check("artifact orientation is explicit", "action you recommend" in prompt and "initial state" in prompt)
    check("grading assertions stay hidden", '"assertions"' not in prompt and "artifacts.decision" not in prompt)

    invalid = subprocess.run(
        ["python3", str(RUNNER)],
        cwd=ROOT,
        env=env,
        input=json.dumps({**case, "capability": "unknown"}),
        text=True,
        capture_output=True,
    )
    check("unknown capability fails closed", invalid.returncode != 0 and "unsupported capability" in invalid.stderr)

print("\nAll engineering eval runner tests passed.")


EXPECTED_RC4_CASES = {
    "rc4.ambiguity",
    "rc4.overengineering",
    "rc4.unrelated-edits",
    "rc4.missing-regression-proof",
}
cases = module.rc4_cases()
check(
    "RC4 aggregate has four exact unique identities",
    {case["id"] for case in cases} == EXPECTED_RC4_CASES
    and len(cases) == len(EXPECTED_RC4_CASES),
)
module.validate_aggregate_cases(cases)
for case in cases:
    fixture = case["fixture_result"]
    check(f"{case['id']} fixture passes", module.grade_case(case, fixture) == [])
    field = case["assertions"][0]["path"].split(".", 1)[1]
    mutated = json.loads(json.dumps(fixture))
    original = mutated["artifacts"][field]
    mutated["artifacts"][field] = not original if type(original) is bool else "mutated"
    check(f"{case['id']} is mutation-sensitive", bool(module.grade_case(case, mutated)))

try:
    module.validate_aggregate_cases([cases[0], cases[0], *cases[2:]])
except module.RunnerError:
    pass
else:
    raise AssertionError("duplicate RC4 scenario identity must fail closed")
print("OK   duplicate RC4 scenario identity rejected")

with tempfile.TemporaryDirectory(prefix="engineering-eval-aggregate.") as raw:
    tmp = Path(raw)
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    capture = tmp / "capture.jsonl"
    fake = fake_bin / "codex"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
prompt = args[-1]
case_id = prompt.split("Case id: ", 1)[1].splitlines()[0]
answers = {
    "rc4.ambiguity": {
        "output": "Surface the material ambiguity before implementation.",
        "artifacts": {"assumptions_surfaced": True, "coding_started": False, "decision": "clarify"},
    },
    "rc4.overengineering": {
        "output": "Make the direct bounded change.",
        "artifacts": {"decision": "direct-change", "fallback_added": False, "new_abstraction": False},
    },
    "rc4.unrelated-edits": {
        "output": "Keep the patch surgical and preserve unrelated work.",
        "artifacts": {"decision": "requested-only", "dirty_work_preserved": True, "unrelated_edit": False},
    },
    "rc4.missing-regression-proof": {
        "output": "Add regression proof and withhold the outcome claim.",
        "artifacts": {"local_green_is_completion": False, "outcome_claim": "withhold", "regression_test_required": True},
    },
}
with pathlib.Path(os.environ["ENGINEERING_EVAL_CAPTURE"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"args": args, "prompt": prompt}) + "\\n")
if os.environ.get("ENGINEERING_EVAL_FAIL_CASE") == case_id:
    raise SystemExit(9)
result = answers[case_id]
if os.environ.get("ENGINEERING_EVAL_EPERM_CASE") == case_id:
    if os.environ.get("ENGINEERING_EVAL_EPERM_WITH_OUTPUT"):
        output.write_text(json.dumps(result), encoding="utf-8")
    print(
        "Error: failed to initialize in-process app-server client: "
        "Operation not permitted (os error 1)",
        file=sys.stderr,
    )
    raise SystemExit(1)
if os.environ.get("ENGINEERING_EVAL_MUTATE_CASE") == case_id:
    first = sorted(result["artifacts"])[0]
    value = result["artifacts"][first]
    result["artifacts"][first] = not value if type(value) is bool else "mutated"
output.write_text(json.dumps(result), encoding="utf-8")
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["ENGINEERING_EVAL_CAPTURE"] = str(capture)
    aggregate = subprocess.run(
        ["python3", str(RUNNER), "--rc4-aggregate"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    check("RC4 aggregate exits green", aggregate.returncode == 0, aggregate.stderr)
    receipt = json.loads(aggregate.stdout)
    check(
        "RC4 aggregate emits one complete receipt",
        receipt["schema_version"] == 1
        and receipt["type"] == "rc4-engineering-discipline-aggregate"
        and receipt["summary"] == {"total": 4, "passed": 4, "failed": 0}
        and [row["id"] for row in receipt["cases"]] == [case["id"] for case in cases]
        and all(row["status"] == "pass" for row in receipt["cases"]),
    )
    captures = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    check("each RC4 scenario runs exactly once", len(captures) == 4)
    check(
        "RC4 grading assertions stay outside every prompt",
        all('"assertions"' not in row["prompt"] and "fixture_result" not in row["prompt"] for row in captures),
    )

    env["ENGINEERING_EVAL_FAIL_CASE"] = "rc4.unrelated-edits"
    failed_transport = subprocess.run(
        ["python3", str(RUNNER), "--rc4-aggregate"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    check(
        "RC4 aggregate fails closed on nonzero transport",
        failed_transport.returncode != 0 and "exited 9" in failed_transport.stderr,
    )

    env.pop("ENGINEERING_EVAL_FAIL_CASE")
    env["ENGINEERING_EVAL_MUTATE_CASE"] = "rc4.missing-regression-proof"
    failed_semantics = subprocess.run(
        ["python3", str(RUNNER), "--rc4-aggregate"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    check(
        "RC4 aggregate cannot claim success on a non-pass result",
        failed_semantics.returncode != 0
        and json.loads(failed_semantics.stdout)["summary"]["failed"] == 1,
    )

    env.pop("ENGINEERING_EVAL_MUTATE_CASE")
    env["ENGINEERING_EVAL_EPERM_CASE"] = "rc4.ambiguity"
    failed_init = subprocess.run(
        ["python3", str(RUNNER), "--rc4-aggregate"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    check(
        "nested Codex EPERM emits no success receipt",
        failed_init.returncode == 3 and failed_init.stdout == "",
        failed_init.stdout,
    )
    prefix = "engineering-eval-runner: "
    failure_record = json.loads(failed_init.stderr.split(prefix, 1)[1])
    check(
        "nested Codex EPERM is a typed pre-model initialization failure",
        failure_record
        == {
            "accepted_case_receipts": 0,
            "code": "nested-codex-app-server-eperm",
            "completed_model_results": 0,
            "failure_class": "pre-model-transport-initialization",
            "schema_version": 1,
            "type": "engineering-eval-transport-failure",
            "typed_case_output_present": False,
        },
    )
    check(
        "typed preflight authorizes only a zero-result replacement",
        module.replacement_preflight(failure_record)["replacement_safe"] is True,
    )
    for field, value in (
        ("accepted_case_receipts", 1),
        ("completed_model_results", 1),
        ("typed_case_output_present", True),
        ("code", "other"),
    ):
        drifted = {**failure_record, field: value}
        try:
            module.replacement_preflight(drifted)
        except module.RunnerError:
            pass
        else:
            raise AssertionError(f"ambiguous replacement preflight accepted {field}")
    print("OK   replacement preflight rejects ambiguous prior effects")

    env["ENGINEERING_EVAL_EPERM_WITH_OUTPUT"] = "1"
    ambiguous_init = subprocess.run(
        ["python3", str(RUNNER), "--rc4-aggregate"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    check(
        "EPERM with typed output is not classified as pre-model",
        ambiguous_init.returncode == 3
        and "pre-model-transport-initialization" not in ambiguous_init.stderr
        and ambiguous_init.stdout == "",
    )

print("\nAll RC4 engineering aggregate tests passed.")
