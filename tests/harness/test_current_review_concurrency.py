#!/usr/bin/env python3
"""Multiprocess admission regressions for one current-review target."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_review_current  # noqa: E402
from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
from harness.finalization_ledger import FinalizationLedger  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from task_review_context import _context, _gate_root  # noqa: E402
from task_review_shared import _atomic_json  # noqa: E402


spec = importlib.util.spec_from_file_location(
    "concurrent_task_review_runner", ROOT / "scripts/task-review-runner.py"
)
assert spec is not None and spec.loader is not None
task_review_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_review_runner)


PROVIDER_EFFECTS = None
PROVIDER_LOCK = None
NONE_READERS = None
NONE_READERS_LOCK = None
COLLISION_ENABLED = None
LATE_ENABLED = None
LATE_RUNS = None
LATE_LOCK = None
LATE_WINNER_READY = None
LATE_JOINER_DONE = None
ORIGINAL_ACTIVE = task_review_current._active_current_review


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def coordinator(path: Path) -> Path:
    (path / "wiki").mkdir(parents=True)
    (path / "scripts/harness").mkdir(parents=True)
    (path / "skills/review").mkdir(parents=True)
    (path / "config").mkdir()
    (path / "docs/skill-references").mkdir(parents=True)
    (path / "scripts/task-review-runner.py").write_text(
        "# fixture\n", encoding="utf-8"
    )
    shutil.copy2(
        ROOT / "scripts/review-inspect.py", path / "scripts/review-inspect.py"
    )
    shutil.copy2(
        ROOT / "scripts/harness/review_submit.py",
        path / "scripts/harness/review_submit.py",
    )
    shutil.copy2(
        ROOT / "docs/skill-references/engineering-quality-contract.md",
        path / "docs/skill-references/engineering-quality-contract.md",
    )
    shutil.copy2(ROOT / "skills/review/SKILL.md", path / "skills/review/SKILL.md")
    shutil.copy2(
        ROOT / "config/model-routing.toml", path / "config/model-routing.toml"
    )
    shutil.copy2(
        ROOT / "config/verification-profiles.toml",
        path / "config/verification-profiles.toml",
    )
    return path.resolve()


def product(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Concurrent Review Test"],
        cwd=path,
        check=True,
    )
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "AGENTS.md").write_text(
        "# Product instructions\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path.resolve()


def review_plan(path: Path) -> Path:
    path.write_text(
        "\n".join(
            (
                "# Approved concurrent review scope",
                "",
                "## Outcome Contract",
                "",
                "```json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "desired_outcome": (
                            "Concurrent same-target current-review starts share "
                            "one durable task owner."
                        ),
                        "success_evidence": [
                            {
                                "evidence_id": "same-target-single-owner",
                                "evidence_kind": "behavior",
                                "observable": (
                                    "Concurrent starts return one task ID and "
                                    "materialize one provider launch."
                                ),
                                "subject": "current-review:same-target-admission",
                            }
                        ],
                        "non_goals": [
                            "Do not serialize reviews of different targets."
                        ],
                    },
                    sort_keys=True,
                ),
                "```",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path.resolve()


def exhaust_active_review(
    product_root: Path,
    vault: Path,
    plan: Path,
    scratch: Path,
) -> str:
    COLLISION_ENABLED.value = 0
    old_result = task_review_runner.run_current_review(
        product_root,
        vault_root=vault,
        plan_file=plan,
        origin_surface="11111111-1111-4111-8111-111111111111",
        scratch_root=scratch,
    )
    old_task_id = str(old_result["task_id"])
    old_active = next(
        candidate
        for candidate in (
            vault / ".vault-meta/harness/current-review"
        ).glob("*/active.json")
        if json.loads(candidate.read_text(encoding="utf-8")).get("task_id")
        == old_task_id
    )
    old_meta = json.loads(old_active.read_text(encoding="utf-8"))
    old_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=product_root,
        text=True,
    ).strip()
    _atomic_json(
        _gate_root(vault, old_task_id) / "review-gate.json",
        {"status": "changes-requested", "context": {"head_sha": old_head}},
    )
    ledger = FinalizationLedger(
        vault / ".vault-meta/harness/finalization-ledger",
        lineage_id=old_task_id,
        origin_task_id=old_task_id,
        plan_sha256=str(old_meta["approved_plan_sha256"]),
        outcome_contract_sha256=str(old_meta["outcome_contract_sha256"]),
    )
    for number in range(1, 6):
        attempt_id = str(uuid.UUID(int=number))
        ledger.reserve(
            attempt_id=attempt_id,
            exact_head=f"{number:040x}",
            task_id=old_task_id,
            worktree=str(product_root),
            provider_policy={
                "routes": ["finalization-primary"],
                "reason": "explicit-single-model",
            },
        )
        ledger.record_terminal(
            attempt_id=attempt_id,
            terminal_result="changes-requested",
        )
    store = OperationStore(vault / ".vault-meta/harness")
    operation_id = str(uuid.uuid5(uuid.UUID(old_task_id), "quiescent-operation"))
    store.create(
        OperationSpec(
            operation_id,
            "a" * 64,
            "simple-review-holistic",
            old_task_id,
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                "reviewer-callback",
                "b" * 64,
            ),
            "packets/review/manifest.json",
            "scoped",
        ),
        lane_id="c" * 32,
        run_id="d" * 32,
    )
    for state in (
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
    ):
        store.transition(old_task_id, operation_id, state)
    (product_root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=product_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve exhausted review"],
        cwd=product_root,
        check=True,
        capture_output=True,
    )
    return old_task_id


def collision_active(*args, **kwargs):
    result = ORIGINAL_ACTIVE(*args, **kwargs)
    if result is None and COLLISION_ENABLED.value:
        with NONE_READERS_LOCK:
            NONE_READERS.value += 1
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            if NONE_READERS.value >= 2:
                break
            time.sleep(0.01)
    return result


def fake_run_review(
    meta,
    vault,
    worktree,
    task_id,
    runtime_root,
    **_kwargs,
):
    if LATE_ENABLED.value and meta.get("supersedes_task_id"):
        with LATE_LOCK:
            LATE_RUNS.value += 1
            late_run = LATE_RUNS.value
        if late_run == 1:
            LATE_WINNER_READY.set()
            LATE_JOINER_DONE.wait(timeout=2)
        else:
            LATE_JOINER_DONE.set()
    gate_path = _gate_root(vault, task_id) / "review-gate.json"
    with PROVIDER_LOCK:
        if not gate_path.exists():
            PROVIDER_EFFECTS.value += 1
            time.sleep(0.15)
            _atomic_json(
                gate_path,
                {
                    "status": "reviewing",
                    "context": {
                        "head_sha": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"],
                            cwd=worktree,
                            text=True,
                        ).strip()
                    },
                },
            )
    return {
        "status": "reviewing",
        "task_id": task_id,
        "runtime_root": str(runtime_root),
    }


def mutate_plan_then_build_context(
    meta,
    vault,
    worktree,
    task_id,
    runtime_root,
    **_kwargs,
):
    source = Path(str(meta["plan_file"]))
    original = source.read_bytes()
    source.write_bytes(original.replace(b"Concurrent", b"Mutated   ", 1))
    context, manifest = _context(meta, vault, worktree, runtime_root, task_id)
    packet = json.loads(manifest.read_text(encoding="utf-8"))
    approved = next(
        item for item in packet["inputs"] if item["name"] == "approved-plan.md"
    )
    return {
        "status": "reviewing",
        "task_id": task_id,
        "runtime_root": str(runtime_root),
        "context_head": context.head_sha,
        "approved_plan_sha256": approved["sha256"],
        "expected_plan_sha256": hashlib.sha256(original).hexdigest(),
        "plan_snapshot_file": str(meta.get("plan_snapshot_file") or ""),
    }


def run_one(start, output, worktree, vault, plan, scratch, new_lineage):
    start.wait()
    try:
        result = task_review_runner.run_current_review(
            worktree,
            vault_root=vault,
            plan_file=plan,
            new_lineage=new_lineage,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except Exception as exc:  # pragma: no cover - surfaced in parent
        output.put({"error": f"{type(exc).__name__}: {exc}"})
    else:
        output.put(result)


def concurrent_start(ctx, worktree, vault, plan, scratch, *, new_lineage):
    PROVIDER_EFFECTS.value = 0
    NONE_READERS.value = 0
    COLLISION_ENABLED.value = 1
    start = ctx.Event()
    output = ctx.Queue()
    processes = [
        ctx.Process(
            target=run_one,
            args=(
                start,
                output,
                worktree,
                vault,
                plan,
                scratch,
                new_lineage,
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    COLLISION_ENABLED.value = 0
    check(
        "concurrent child processes exit cleanly",
        all(process.exitcode == 0 for process in processes),
        [process.exitcode for process in processes],
    )
    check(
        "concurrent public calls do not raise",
        all("error" not in result for result in results),
        results,
    )
    return results


def late_replacement_start(ctx, worktree, vault, plan, scratch):
    PROVIDER_EFFECTS.value = 0
    COLLISION_ENABLED.value = 0
    LATE_RUNS.value = 0
    LATE_ENABLED.value = 1
    LATE_WINNER_READY.clear()
    LATE_JOINER_DONE.clear()
    output = ctx.Queue()
    first_start = ctx.Event()
    first = ctx.Process(
        target=run_one,
        args=(
            first_start,
            output,
            worktree,
            vault,
            plan,
            scratch,
            True,
        ),
    )
    first.start()
    first_start.set()
    check(
        "replacement publishes its winning owner before the late caller",
        LATE_WINNER_READY.wait(timeout=10),
    )
    second_start = ctx.Event()
    second = ctx.Process(
        target=run_one,
        args=(
            second_start,
            output,
            worktree,
            vault,
            plan,
            scratch,
            True,
        ),
    )
    second.start()
    second_start.set()
    results = [output.get(timeout=15) for _ in range(2)]
    LATE_JOINER_DONE.set()
    for process in (first, second):
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    LATE_ENABLED.value = 0
    check(
        "staggered replacement child processes exit cleanly",
        all(process.exitcode == 0 for process in (first, second)),
        [first.exitcode, second.exitcode],
    )
    check(
        "a late fresh-lineage caller joins the published winner",
        all("error" not in result for result in results),
        results,
    )
    return results


if "fork" not in multiprocessing.get_all_start_methods():
    raise AssertionError("current review concurrency requires POSIX fork")

ctx = multiprocessing.get_context("fork")
PROVIDER_EFFECTS = ctx.Value("i", 0)
PROVIDER_LOCK = ctx.Lock()
NONE_READERS = ctx.Value("i", 0)
NONE_READERS_LOCK = ctx.Lock()
COLLISION_ENABLED = ctx.Value("i", 0)
LATE_ENABLED = ctx.Value("i", 0)
LATE_RUNS = ctx.Value("i", 0)
LATE_LOCK = ctx.Lock()
LATE_WINNER_READY = ctx.Event()
LATE_JOINER_DONE = ctx.Event()
task_review_current._active_current_review = collision_active
task_review_current._run_review = fake_run_review

previous = {
    name: os.environ.get(name)
    for name in (
        "LLM_OBSIDIAN_SESSION_RUNTIME",
        "LLM_OBSIDIAN_SESSION_MODEL",
        "LLM_OBSIDIAN_SESSION_EFFORT",
    )
}
os.environ["LLM_OBSIDIAN_SESSION_RUNTIME"] = "codex"
os.environ["LLM_OBSIDIAN_SESSION_MODEL"] = "gpt-5.6-sol"
os.environ["LLM_OBSIDIAN_SESSION_EFFORT"] = "high"

try:
    with tempfile.TemporaryDirectory(prefix="current-review-concurrency.") as raw:
        root = Path(raw)
        vault = coordinator(root / "coordinator")
        plan = review_plan(root / "review-plan.md")

        snapshot_product = product(root / "snapshot-product")
        snapshot_plan = review_plan(root / "snapshot-plan.md")
        snapshot_scratch = (root / "snapshot-scratch").resolve()
        task_review_current._run_review = mutate_plan_then_build_context
        try:
            snapshot_result = task_review_runner.run_current_review(
                snapshot_product,
                vault_root=vault,
                plan_file=snapshot_plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=snapshot_scratch,
            )
        finally:
            task_review_current._run_review = fake_run_review
        snapshot_ids = {path.name for path in snapshot_scratch.iterdir()}
        check(
            "post-publication plan mutation uses the pre-owner frozen bytes",
            snapshot_result["approved_plan_sha256"]
            == snapshot_result["expected_plan_sha256"]
            and bool(snapshot_result["plan_snapshot_file"]),
            snapshot_result,
        )
        check(
            "post-publication plan mutation leaves one linked scratch owner",
            snapshot_ids == {snapshot_result["task_id"]},
            snapshot_ids,
        )

        initial_product = product(root / "initial-product")
        initial_scratch = (root / "initial-scratch").resolve()
        initial_results = concurrent_start(
            ctx,
            initial_product,
            vault,
            plan,
            initial_scratch,
            new_lineage=False,
        )
        initial_ids = {str(result.get("task_id") or "") for result in initial_results}
        check(
            "concurrent initial starts share one task ID",
            len(initial_ids) == 1 and "" not in initial_ids,
            initial_results,
        )
        check(
            "concurrent initial starts create one provider effect",
            PROVIDER_EFFECTS.value == 1,
            PROVIDER_EFFECTS.value,
        )
        check(
            "concurrent initial starts leave no orphan scratch owner",
            {path.name for path in initial_scratch.iterdir()} == initial_ids,
            list(initial_scratch.iterdir()),
        )

        replacement_product = product(root / "replacement-product")
        replacement_scratch = (root / "replacement-scratch").resolve()
        old_task_id = exhaust_active_review(
            replacement_product,
            vault,
            plan,
            replacement_scratch,
        )

        replacement_results = concurrent_start(
            ctx,
            replacement_product,
            vault,
            plan,
            replacement_scratch,
            new_lineage=True,
        )
        replacement_ids = {
            str(result.get("task_id") or "") for result in replacement_results
        }
        check(
            "concurrent exhausted-lineage replacements share one new task ID",
            len(replacement_ids) == 1
            and "" not in replacement_ids
            and old_task_id not in replacement_ids,
            replacement_results,
        )
        check(
            "concurrent exhausted-lineage replacements create one provider effect",
            PROVIDER_EFFECTS.value == 1,
            PROVIDER_EFFECTS.value,
        )
        scratch_owners = {path.name for path in replacement_scratch.iterdir()}
        check(
            "concurrent replacement preserves only old and winning scratch owners",
            scratch_owners == {old_task_id, *replacement_ids},
            scratch_owners,
        )

        late_product = product(root / "late-replacement-product")
        late_scratch = (root / "late-replacement-scratch").resolve()
        late_old_task_id = exhaust_active_review(
            late_product,
            vault,
            plan,
            late_scratch,
        )
        late_results = late_replacement_start(
            ctx,
            late_product,
            vault,
            plan,
            late_scratch,
        )
        late_ids = {
            str(result.get("task_id") or "") for result in late_results
        }
        check(
            "staggered replacements return the same new task ID",
            len(late_ids) == 1
            and "" not in late_ids
            and late_old_task_id not in late_ids,
            late_results,
        )
        check(
            "staggered replacements create one provider effect",
            PROVIDER_EFFECTS.value == 1,
            PROVIDER_EFFECTS.value,
        )
finally:
    task_review_current._active_current_review = ORIGINAL_ACTIVE
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

print("\nAll current-review concurrency tests passed.")
