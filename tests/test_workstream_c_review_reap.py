#!/usr/bin/env python3
"""Focused Workstream C contracts for outcome-aware review and reap."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.workflows.review import ReviewContext
from harness.workflows.review_gate import (
    authorize_task_finalization,
    review_context_sha256,
)
from wiki_summary_contract import validate_summary


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


task_review = load_script(
    "workstream_c_task_review", ROOT / "scripts/task-review-runner.py"
)
reap = load_script("workstream_c_reap", ROOT / "scripts/reap-runner.py")

failures: list[str] = []


def check(label: str, value: bool) -> None:
    print(("ok" if value else "not ok") + " - " + label)
    if not value:
        failures.append(label)


SUMMARY = {
    "schema_version": 2,
    "type": "repo-touch",
    "title": "Workstream C fixture",
    "session": "executor-session",
    "body": "The implementer declares the work complete.",
    "outcome_disposition": "partially-achieved",
    "outcome_evidence_ids": ["review-contract"],
    "residual_gap_pointers": ["docs/follow-up.md"],
}
SUMMARY_BYTES = (json.dumps(SUMMARY, sort_keys=True) + "\n").encode()
SUMMARY_SHA = hashlib.sha256(SUMMARY_BYTES).hexdigest()
HEAD = "a" * 40
PROFILE_SHA = "b" * 64


with tempfile.TemporaryDirectory(prefix="workstream-c-review.") as raw:
    root = Path(raw)
    vault = root / "vault"
    worktree = root / "worktree"
    runtime = root / "runtime"
    (vault / "skills/review").mkdir(parents=True)
    worktree.mkdir()
    runtime.mkdir()
    plan = vault / "approved-plan.md"
    plan.write_text(
        "# Approved plan\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Keep the outcome exact.",'
        '"success_evidence":[{"evidence_id":"review-contract",'
        '"observable":"Review independently establishes the contract."}],'
        '"non_goals":["Do not add a review lane."]}\n```\n',
        encoding="utf-8",
    )
    (vault / "skills/review/SKILL.md").write_text(
        "# Review\n\nReview the exact evidence.\n", encoding="utf-8"
    )
    (worktree / ".task-summary.json").write_bytes(SUMMARY_BYTES)
    meta = {
        "version": 4,
        "task_name": "workstream-c",
        "plan_file": str(plan),
        "outcome_contract_sha256": hashlib.sha256(
            b'{"desired_outcome":"Keep the outcome exact.","non_goals":["Do not add a review lane."],"schema_version":1,"success_evidence":[{"evidence_id":"review-contract","observable":"Review independently establishes the contract."}]}'
        ).hexdigest(),
        "review_policy": {
            "verification_profile": "scoped",
            "verification_profile_sha256": PROFILE_SHA,
        },
    }
    import task_review_context

    original_git = task_review_context._git
    task_review_context._git = lambda _root, *args: (
        HEAD if args == ("rev-parse", "HEAD") else "bounded HEAD diff"
    )
    try:
        context, manifest_path = task_review._context(
            meta, vault, worktree, runtime, "workstream-c"
        )
    finally:
        task_review_context._git = original_git

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    packet_files = {
        path.name
        for path in manifest_path.parent.iterdir()
        if path.is_file()
    }
    check(
        "review packet materializes readable plan, instructions, HEAD, and diff",
        any(name.endswith("-plan-approved-plan.md") for name in packet_files)
        and any(
            name.endswith("-instructions-review-skill.md")
            for name in packet_files
        )
        and any(name.endswith("-head-exact-head.txt") for name in packet_files)
        and any(name.endswith("-diff-head-diff.patch") for name in packet_files),
    )
    summary_inputs = [
        item for item in manifest["inputs"]
        if item["name"] == "implementer-summary.json"
    ]
    check(
        "v4 ContextPacket carries the implementer summary as an unverified input",
        len(summary_inputs) == 1
        and summary_inputs[0]["sha256"] == SUMMARY_SHA
        and getattr(context, "implementer_summary_sha256", "") == SUMMARY_SHA,
    )

    prompt_pointer = task_review._prompt(
        vault=vault,
        worktree=worktree,
        runtime_root=runtime,
        context=context,
        axis="openai-holistic",
        verification=False,
    )
    prompt = (runtime / prompt_pointer).read_text(encoding="utf-8")
    outcome_position = prompt.find("Outcome Contract")
    mechanics_position = prompt.find("implementation mechanics")
    check(
        "simple review judges the Outcome Contract before mechanics",
        outcome_position >= 0
        and mechanics_position >= 0
        and outcome_position < mechanics_position,
    )
    check(
        "review treats implementer reports as unverified claims",
        "unverified claims" in prompt,
    )
    check(
        "review classifies every declared success-evidence item",
        all(word in prompt for word in ("established", "missing", "contradicted"))
        and "every declared success-evidence item" in prompt,
    )
    check(
        "review checks non-goals for scope creep",
        "non-goal" in prompt and "scope creep" in prompt,
    )

    standards_pointer = task_review._prompt(
        vault=vault,
        worktree=worktree,
        runtime_root=runtime,
        context=context,
        axis="openai-engineering",
        verification=False,
    )
    standards_prompt = (runtime / standards_pointer).read_text(encoding="utf-8")
    check(
        "deep standards lane stays independent from Fable outcome assessment",
        "every declared success-evidence item" not in standards_prompt,
    )


context_fields = inspect.signature(ReviewContext).parameters
check(
    "review identity can bind exact implementer-summary bytes",
    "implementer_summary_sha256" in context_fields,
)

legacy = ReviewContext("packets/review.json", HEAD, "scoped", PROFILE_SHA)
legacy_expected = hashlib.sha256(
    json.dumps(
        {
            "manifest": legacy.manifest,
            "head_sha": legacy.head_sha,
            "verification_profile": legacy.verification_profile,
            "verification_profile_sha256": legacy.verification_profile_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
check(
    "legacy review identity remains byte-compatible",
    review_context_sha256(legacy) == legacy_expected,
)

authorization_fields = inspect.signature(authorize_task_finalization).parameters
check(
    "finalization accepts an exact implementer-summary binding",
    "expected_summary_sha256" in authorization_fields,
)
if "expected_summary_sha256" in authorization_fields:
    with tempfile.TemporaryDirectory(prefix="workstream-c-finalization.") as raw:
        gate = Path(raw)
        (gate / "review-gate.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dispatch_operation_id": "task-c",
                    "status": "approved",
                    "policy": {"enabled": True},
                    "context": {
                        "head_sha": HEAD,
                        "verification_profile": "scoped",
                        "verification_profile_sha256": PROFILE_SHA,
                        "implementer_summary_sha256": SUMMARY_SHA,
                    },
                    "evidence": {},
                }
            ),
            encoding="utf-8",
        )
        try:
            authorize_task_finalization(
                gate,
                dispatch_operation_id="task-c",
                expected_head_sha=HEAD,
                expected_profile="scoped",
                expected_profile_sha256=PROFILE_SHA,
                expected_summary_sha256="c" * 64,
            )
        except ValueError as exc:
            check(
                "changed v4 summary bytes make approved review evidence stale",
                "stale" in str(exc),
            )
        else:
            check(
                "changed v4 summary bytes make approved review evidence stale",
                False,
            )


legacy_summary = validate_summary(
    {
        "schema_version": 1,
        "type": "repo-touch",
        "title": "Legacy result",
        "session": "legacy-session",
        "body": "Readable legacy body.",
    }
)
check(
    "legacy Wiki Summary v1 remains readable",
    legacy_summary["schema_version"] == 1
    and legacy_summary["body"] == "Readable legacy body.",
)
check(
    "Wiki Summary v2 outcome fields remain durable in reap rendering",
    "Outcome disposition: `partially-achieved`" in reap.outcome_markdown(SUMMARY)
    and "`review-contract`" in reap.outcome_markdown(SUMMARY)
    and "docs/follow-up.md" in reap.outcome_markdown(SUMMARY),
)
check(
    "shared-plan reap does not close the master plan",
    reap.reap_closes_plan({"reap_policy": {"mode": "shared"}}) is False,
)

review_skill = (ROOT / "skills/review/SKILL.md").read_text(encoding="utf-8")
reap_skill = (ROOT / "skills/reap/SKILL.md").read_text(encoding="utf-8")
check(
    "review skill names the v4 outcome-first contract",
    "v4" in review_skill
    and "established" in review_skill
    and "scope creep" in review_skill,
)
check(
    "active unattended v3 review retains the code-owned runner path",
    "dispatched v3/v4 task" in review_skill,
)
check(
    "review skill exposes purpose-bound outcome checkpoints",
    all(
        marker in review_skill
        for marker in (
            "intent",
            "implementation",
            "release",
            "review-program.py",
            "--boundary-input",
            "approval-or-stop",
        )
    ),
)
check(
    "reap names normal v4 and frozen unattended v3 runner paths",
    "Normal v4 unattended path" in reap_skill
    and "Active unattended" in reap_skill
    and "v3 tasks use the same runner" in reap_skill
    and "legacy v1/v2" in reap_skill
    and "Normal v3 unattended path" not in reap_skill,
)

if failures:
    raise SystemExit("workstream C failures: " + ", ".join(failures))
print("Workstream C review/reap checks passed.")
