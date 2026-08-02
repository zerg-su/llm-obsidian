#!/usr/bin/env python3
"""Contract tests for the bounded engineering-skill live eval runner."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "engineering-eval-runner.py"


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
