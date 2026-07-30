#!/usr/bin/env python3
"""Autonomous sequential controller over existing kernel operation ports."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import RuntimeRoute
from harness.pipeline_builtins import builtin_definitions, builtin_registry
from harness.pipeline_controller import (
    PipelineRunRequest,
    PipelineStepResult,
    run_pipeline,
)
from harness.pipeline_state import PipelineLedger
from harness.pipelines import compile_pipeline
from harness.store import OperationStore


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


compiled = compile_pipeline(
    builtin_definitions()["engineering/change"],
    builtin_registry(),
    capabilities=("provider:authenticated",),
)
executor = RuntimeRoute(
    "codex",
    "gpt-5.6-sol",
    "high",
    "executor",
    "a" * 64,
)
reviewer = RuntimeRoute(
    "claude",
    "claude-opus-5",
    "high",
    "reviewer-readonly",
    "b" * 64,
)
request = PipelineRunRequest(
    owner_id="owner-1",
    pipeline_run_id="change-run-1",
    approved_input_sha256="c" * 64,
    context_manifest="packets/change/manifest.json",
    verification_profile="full",
    routes={
        "worktree": executor,
        "verification": executor,
        "review": reviewer,
    },
)

with tempfile.TemporaryDirectory(prefix="pipeline-controller.") as raw:
    ledger = PipelineLedger(OperationStore(Path(raw) / "harness"))
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def execute(binding, step):
        calls.append(
            (step.step_id, binding.session_mode, step.semantic_skills)
        )
        output = hashlib.sha256(
            f"{binding.input_sha256}:{step.step_id}".encode()
        ).hexdigest()
        return PipelineStepResult(output)

    result = run_pipeline(compiled, ledger, request, execute=execute)
    check(
        "controller autonomously reaches the coordinator-owned terminal boundary",
        result.state.status == "reap-ready"
        and result.state.completed_steps == ("tdd-slices", "verify", "review")
        and tuple(binding.step_id for binding in result.bindings)
        == ("tdd-slices", "verify", "review"),
    )
    check(
        "controller preserves session modes and skill semantics",
        calls
        == [
            ("tdd-slices", "worktree", ("tdd",)),
            ("verify", "verification", ()),
            ("review", "review", ("review",)),
        ],
    )
    check(
        "every semantic step owns a distinct kernel operation",
        len({binding.spec.operation_id for binding in result.bindings}) == 3
        and len({binding.replay_key for binding in result.bindings}) == 3,
    )

    calls.clear()
    replayed = run_pipeline(compiled, ledger, request, execute=execute)
    check(
        "controller restart reuses exact receipts without model or effect replay",
        replayed == result and calls == [],
    )

with tempfile.TemporaryDirectory(prefix="pipeline-controller-resume.") as raw:
    ledger = PipelineLedger(OperationStore(Path(raw) / "harness"))
    failed_calls: list[str] = []

    def fail_once(binding, step):
        failed_calls.append(step.step_id)
        if step.step_id == "verify" and failed_calls.count("verify") == 1:
            raise RuntimeError("verification adapter stopped")
        return PipelineStepResult(
            hashlib.sha256(
                f"{binding.input_sha256}:{step.step_id}".encode()
            ).hexdigest()
        )

    try:
        run_pipeline(compiled, ledger, request, execute=fail_once)
    except RuntimeError:
        pass
    else:
        check("controller interruption fixture fails", False)
    resumed = run_pipeline(compiled, ledger, request, execute=fail_once)
    check(
        "resume continues after the last accepted receipt",
        resumed.state.status == "reap-ready"
        and failed_calls == ["tdd-slices", "verify", "verify", "review"],
    )

fix_compiled = compile_pipeline(
    builtin_definitions()["engineering/fix"],
    builtin_registry(),
    capabilities=("provider:authenticated",),
)
fix_request = PipelineRunRequest(
    owner_id="owner-1",
    pipeline_run_id="fix-run-1",
    approved_input_sha256="d" * 64,
    context_manifest="packets/fix/manifest.json",
    verification_profile="full",
    routes={
        "worktree": executor,
        "verification": executor,
        "review": reviewer,
    },
)
with tempfile.TemporaryDirectory(prefix="pipeline-controller-fix.") as raw:
    ledger = PipelineLedger(OperationStore(Path(raw) / "harness"))
    fix_calls: list[tuple[str, str, str]] = []

    def execute_fix(binding, step):
        fix_calls.append(
            (
                step.step_id,
                binding.session_mode,
                binding.spec.route.profile,
            )
        )
        return PipelineStepResult(
            hashlib.sha256(
                f"{binding.input_sha256}:{step.step_id}".encode()
            ).hexdigest()
        )

    fixed = run_pipeline(
        fix_compiled,
        ledger,
        fix_request,
        execute=execute_fix,
    )
    check(
        "fix child rounds inherit the original worktree route",
        fixed.state.status == "reap-ready"
        and fix_calls
        == [
            ("reproduce", "worktree", "executor"),
            ("root-cause", "parent-child", "executor"),
            ("regression-test", "parent-child", "executor"),
            ("minimal-fix", "parent-child", "executor"),
            ("verify", "verification", "executor"),
            ("review", "review", "reviewer-readonly"),
        ],
    )
