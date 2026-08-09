#!/usr/bin/env python3
"""Contract tests for the bounded engineering-skill live eval runner."""

from __future__ import annotations

import hashlib
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

canonical_sources = {
    relative: (ROOT / relative).read_bytes()
    for relative in module.RC4_SOURCE_SHA256
}
fork_sources = dict(canonical_sources)
fork_sources["AGENTS.md"] = fork_sources["AGENTS.md"].replace(
    b"codex plugin add llm-obsidian@llm-obsidian-codex",
    b"codex plugin add llm-obsidian-swarm@llm-obsidian-swarm-codex",
)
fork_sources["CLAUDE.md"] = (
    fork_sources["CLAUDE.md"]
    .replace(b"# llm-obsidian \xe2\x80\x94", b"# llm-obsidian-swarm \xe2\x80\x94")
    .replace(
        b"**Plugin name:** `llm-obsidian`",
        b"**Plugin name:** `llm-obsidian-swarm`",
    )
    .replace(
        b"codex plugin add llm-obsidian@llm-obsidian-codex",
        b"codex plugin add llm-obsidian-swarm@llm-obsidian-swarm-codex",
    )
)
module.validate_aggregate_sources(fork_sources)
check(
    "RC4 governing-source projection permits only the registered fork branding",
    all(
        module.prompt_for(case, source_snapshot=canonical_sources)
        == module.prompt_for(case, source_snapshot=fork_sources)
        for case in cases
    ),
)
drifted_sources = dict(fork_sources)
drifted_sources["AGENTS.md"] += b"\nUnreviewed authority expansion.\n"
try:
    module.validate_aggregate_sources(drifted_sources)
except module.RunnerError:
    pass
else:
    raise AssertionError("non-branding governing-source drift was accepted")
check("RC4 governing-source projection rejects non-branding drift", True)

live_evidence_path = (
    ROOT
    / "docs"
    / "acceptance"
    / "evidence"
    / "v2.6.6"
    / "rc4-engineering-discipline-live.json"
)
live_evidence = json.loads(live_evidence_path.read_text(encoding="utf-8"))
check(
    "RC4 live evidence binds the exact successful coordinator execution",
    live_evidence["schema_version"] == 2
    and live_evidence["type"] == "rc4-engineering-discipline-live-evidence"
    and live_evidence["evidence_id"]
    == "RC4-E8-agent-discipline-and-skill-quality"
    and live_evidence["execution"]["product_head_sha"]
    == "338ceec30c72c5afd34c78042a8b57a02fcdd99c"
    and live_evidence["execution"]["command_log_event_id"]
    == "b202717fa61ba4a358d39a0269ba8cdfd404bff08dfc179c6ec2a0cf5296793d"
    and live_evidence["receipt"]["summary"]
    == {"total": 4, "passed": 4, "failed": 0},
)
# The two working directories must stay distinguishable, but published evidence
# no longer carries operator-local absolute paths (rc4-live-evidence-embeds-
# operator-paths), so the distinction is asserted on the stable placeholders.
check(
    "RC4 live evidence distinguishes command-log and execution working directories",
    live_evidence["command_capture"]["command_event"]["cwd"]
    == "<coordinator-vault>"
    and live_evidence["execution"]["cwd"] == "<task-worktree>"
    and live_evidence["command_capture"]["exec_workdir"]
    == live_evidence["execution"]["cwd"]
    and live_evidence["command_capture"]["command_event"]["cwd"]
    != live_evidence["execution"]["cwd"],
)
check(
    "RC4 live evidence publishes no operator-local path",
    "/Users/"
    not in json.dumps(live_evidence, ensure_ascii=False),
)
expected_equivalence = {
    "rc4.ambiguity": (
        "d6f2b0d7069e729111f1f4b88ffd2a1ec5c296bbcd163b33e7a4e0fa607b85e3",
        "a749d6ddf7c9e6655e8ce99e4fb80d1a6e51fc4c7990539643462d8acd0b70a5",
    ),
    "rc4.overengineering": (
        "31bac420f3d2b182ef63aae161219167da6bcca1fc16b0b1f0e2637db7bb5d15",
        "08d07a2a3424849e9c9ca73eb8e6a252e0224ee8abeac26ea53eb9e423e975ce",
    ),
    "rc4.unrelated-edits": (
        "f44d4f075c334c50049a45705f9ad921c0659f4b6756c207c86358c4b5c4330f",
        "af1615eac1417d4d2846b452e1783a00fed5e3b1672b91d310875b968f5be2c9",
    ),
    "rc4.missing-regression-proof": (
        "25e14d7059175f260bdcbeeb7b68c582b57e4ed6f30a2b6d41ccbe4d0a576599",
        "3b22823348abf02f0b200c99a4e36084ffa5ec41137d94b4a57121ed2722f73a",
    ),
}
observed_equivalence = {}
for case in cases:
    prompt_digest = hashlib.sha256(module.prompt_for(case).encode()).hexdigest()
    schema_digest = hashlib.sha256(
        json.dumps(
            module.output_schema(case), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    observed_equivalence[case["id"]] = (prompt_digest, schema_digest)
check(
    "RC4 no-replay bundle pins equivalent prompt and schema bytes",
    observed_equivalence == expected_equivalence
    and {
        row["id"]: (row["prompt_sha256"], row["schema_sha256"])
        for row in live_evidence["no_replay_equivalence"]["rows"]
    }
    == expected_equivalence,
)
expected_records = {
    "resolution-8474d836d198524bd855d40329e3525a": "46b9a504bd198988efe0e1153d1319d13a8c6e1da2432d999c1ee2b524e7c629",
    "resolution-cfcdd2f98d3618d079596daf316e9458": "7437c03f933c24f45dc69a00fe78c5055517a86145fed7d6a6edc9be905ba269",
    "amendment-3fc9872d5e860b05f744ca6aecd56a03": "ad12b3afe84d789df2dd215ff3d5fc3e3e63698be25a9767a3d7fb4c2e4f945f",
    "amendment-7bd44e3f01f8d84c20ac6f957c52fb08": "1a4edea77dd85352401d5c8d1abf047d052068da8d19e7ebc514b34bbfd1da70",
    "amendment-8568fbd6114657339f7745d566b7f79d": "7ded610da466f6b1b5cd4ea018198fbca261992366da34d84f4f77555e17cd33",
}
check(
    "RC4 scope and resolution provenance is visible in the product packet",
    {
        row["record_id"]: row["record_sha256"]
        for row in live_evidence["provenance"]["tracked_authorization_materialization"]
    }
    == expected_records
    and all(
        row["decision"].strip()
        for row in live_evidence["provenance"]["tracked_authorization_materialization"]
    ),
)
audit = (ROOT / "docs/acceptance/v2.6.6-rc4-skill-audit.md").read_text(
    encoding="utf-8"
)
receipt_line = audit.split("```json\n", 1)[1].split("\n```", 1)[0] + "\n"
check(
    "RC4 live evidence preserves the exact typed receipt bytes",
    json.loads(receipt_line) == live_evidence["receipt"]
    and hashlib.sha256(receipt_line.encode()).hexdigest()
    == live_evidence["receipt_line_sha256"]
    and "6df616c7f71b86b4a074ad013cabd29311f1eed9213ef08ced04651fed47d986"
    in audit,
)
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

inverted = json.loads(json.dumps(cases))
inverted[1]["response_contract"]["decision"] = ["direct-change", "add-framework"]
inverted[1]["assertions"] = [
    {"kind": "equals", "path": "artifacts.decision", "value": "add-framework"},
    {"kind": "equals", "path": "artifacts.new_abstraction", "value": True},
    {"kind": "equals", "path": "artifacts.fallback_added", "value": True},
]
inverted[1]["fixture_result"]["artifacts"] = {
    "decision": "add-framework",
    "new_abstraction": True,
    "fallback_added": True,
}
try:
    module.validate_aggregate_cases(inverted)
except module.RunnerError:
    pass
else:
    raise AssertionError("semantic denominator inversion must fail closed")
print("OK   semantic denominator inversion rejected by frozen digest")

for profile in (
    ("gpt-5.6-sol", module.RC4_EFFORT, module.RC4_TIMEOUT),
    (module.RC4_MODEL, "high", module.RC4_TIMEOUT),
    (module.RC4_MODEL, module.RC4_EFFORT, 60.0),
):
    try:
        module.aggregate_receipt(cases, model=profile[0], effort=profile[1], timeout=profile[2])
    except module.RunnerError:
        pass
    else:
        raise AssertionError(f"aggregate profile drift accepted: {profile}")
print("OK   model, effort, and timeout drift reject before provider execution")

with tempfile.TemporaryDirectory(prefix="engineering-source-drift.") as raw:
    source_root = Path(raw)
    for relative in module.RC4_SOURCE_SHA256:
        source_root.joinpath(relative).write_bytes(ROOT.joinpath(relative).read_bytes())
    source_root.joinpath("AGENTS.md").write_text(
        ROOT.joinpath("AGENTS.md").read_text(encoding="utf-8").replace(
            "simplicity first", "simplicity optional", 1
        ),
        encoding="utf-8",
    )
    original_root = module.ROOT
    module.ROOT = source_root
    try:
        module.validate_aggregate_sources()
    except module.RunnerError:
        pass
    else:
        raise AssertionError("governing source weakening must fail closed")
    finally:
        module.ROOT = original_root
print("OK   governing source weakening rejected by frozen digest")

with tempfile.TemporaryDirectory(prefix="engineering-source-snapshot.") as raw:
    source_root = Path(raw)
    for relative in module.RC4_SOURCE_SHA256:
        source_root.joinpath(relative).write_bytes(ROOT.joinpath(relative).read_bytes())
    original_root = module.ROOT
    original_run = module.run
    prompts = []

    def snapshot_run(case, *, model, effort, timeout, source_snapshot=None):
        if not prompts:
            source_root.joinpath("AGENTS.md").write_text(
                source_root.joinpath("AGENTS.md").read_text(encoding="utf-8").replace(
                    "simplicity first", "simplicity optional", 1
                ),
                encoding="utf-8",
            )
        prompts.append(module.prompt_for(case, source_snapshot=source_snapshot))
        return case["fixture_result"]

    module.ROOT = source_root
    module.run = snapshot_run
    try:
        snapshot_receipt = module.aggregate_receipt(
            cases,
            model=module.RC4_MODEL,
            effort=module.RC4_EFFORT,
            timeout=module.RC4_TIMEOUT,
        )
    finally:
        module.run = original_run
        module.ROOT = original_root
    check(
        "aggregate prompts use one immutable governing-source snapshot",
        len(prompts) == 4
        and all("simplicity first" in prompt for prompt in prompts)
        and all("simplicity optional" not in prompt for prompt in prompts),
    )
    check(
        "aggregate receipt hashes the captured governing-source bytes",
        snapshot_receipt["source_sha256"] == module.RC4_SOURCE_SHA256,
    )

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
        "transport record carries no replay authorization",
        "replacement_safe" not in failure_record
        and not hasattr(module, "replacement_preflight"),
    )

    check(
        "mixed diagnostic is not classified as pre-model",
        module.transport_failure_record(
            returncode=9,
            stderr="model completed before wrapper failed\n" + module.APP_SERVER_EPERM,
            typed_case_output_present=False,
        )
        is None,
    )

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
