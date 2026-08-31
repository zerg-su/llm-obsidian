#!/usr/bin/env python3
"""Integration seam for coordinator-owned review of plain external Git roots."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_review_current  # noqa: E402
import current_review_admission  # noqa: E402
from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
from harness.finalization_ledger import FinalizationLedger  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from review_target import resolve_target  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
from task_review_context import _context  # noqa: E402
from task_review_request import _prompt, _request as compile_review_request  # noqa: E402
from task_review_transport import _callback_wake  # noqa: E402


ORIGINAL_ADMISSION_ATOMIC_JSON = current_review_admission._atomic_json


spec = importlib.util.spec_from_file_location(
    "external_task_review_runner", ROOT / "scripts/task-review-runner.py"
)
assert spec is not None and spec.loader is not None
task_review_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_review_runner)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def coordinator(path: Path) -> Path:
    (path / "wiki").mkdir(parents=True)
    (path / "scripts/harness").mkdir(parents=True)
    (path / "skills/review").mkdir(parents=True)
    (path / "config").mkdir()
    (path / "docs/skill-references").mkdir(parents=True)
    (path / "scripts/task-review-runner.py").write_text("# fixture\n", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/review-inspect.py", path / "scripts/review-inspect.py")
    shutil.copy2(
        ROOT / "scripts/harness/review_submit.py",
        path / "scripts/harness/review_submit.py",
    )
    shutil.copy2(
        ROOT / "docs/skill-references/engineering-quality-contract.md",
        path / "docs/skill-references/engineering-quality-contract.md",
    )
    shutil.copy2(ROOT / "skills/review/SKILL.md", path / "skills/review/SKILL.md")
    shutil.copy2(ROOT / "config/model-routing.toml", path / "config/model-routing.toml")
    shutil.copy2(ROOT / "config/verification-profiles.toml", path / "config/verification-profiles.toml")
    return path.resolve()


def product(path: Path, value: int) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "review@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "External Review Test"], cwd=path, check=True)
    (path / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    (path / "AGENTS.md").write_text("# Product instructions\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path.resolve()


def review_plan(
    path: Path,
    *,
    desired_outcome: str,
    evidence_id: str,
    observable: str,
    behavior_bound: bool = True,
) -> Path:
    evidence = {
        "evidence_id": evidence_id,
        "observable": observable,
    }
    if behavior_bound:
        evidence.update(
            {
                "evidence_kind": "behavior",
                "subject": f"current-review:{evidence_id}",
            }
        )
    path.write_text(
        "\n".join(
            (
                "# Approved implementation review scope",
                "",
                "## Outcome Contract",
                "",
                "```json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "purpose": "Verify the declared product behavior.",
                        "desired_outcome": desired_outcome,
                        "success_evidence": [evidence],
                        "non_goals": [
                            "Do not assess uncommitted or out-of-range bytes."
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


def fail_after_uuid_root_setup(
    worktree: Path,
    task_id: str,
    scratch_root: Path | None,
) -> Path:
    runtime_root = current_review_admission._current_runtime_path(
        worktree, task_id, scratch_root
    )
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    raise RuntimeError("uuid-root-setup fixture")


with tempfile.TemporaryDirectory(prefix="external-current-review.") as raw:
    base = Path(raw)
    vault = coordinator(base / "coordinator")
    first = product(base / "products/first", 1)
    review_base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=first, text=True
    ).strip()
    (first / "app.py").write_text("VALUE = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=first, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change app"],
        cwd=first,
        check=True,
        capture_output=True,
    )
    (first / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], cwd=first, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add feature"],
        cwd=first,
        check=True,
        capture_output=True,
    )
    second = product(base / "products/second", 2)
    first_plan = review_plan(
        base / "first-review-plan.md",
        desired_outcome=(
            "The reviewed checkout exports VALUE = 10 and enables FEATURE."
        ),
        evidence_id="external-feature-behavior",
        observable=(
            "The committed app.py contains VALUE = 10 and feature.py contains "
            "FEATURE = True."
        ),
    )
    second_plan = review_plan(
        base / "second-review-plan.md",
        desired_outcome="The reviewed checkout continues to export VALUE = 2.",
        evidence_id="external-value-behavior",
        observable="The committed app.py contains VALUE = 2.",
    )
    scratch = (base / "scratch").resolve()
    captures: list[tuple[dict, Path, Path, str, Path]] = []

    def stop_before_provider(meta, coordinator_root, worktree, task_id, runtime_root, **_kwargs):
        captures.append((dict(meta), coordinator_root, worktree, task_id, runtime_root))
        return {
            "status": "prepared",
            "task_id": task_id,
            "worktree": str(worktree),
            "vault_root": str(coordinator_root),
            "runtime_root": str(runtime_root),
        }

    original = task_review_current._run_review
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
    task_review_current._run_review = stop_before_provider
    try:
        first_result = task_review_runner.run_current_review(
            first,
            vault_root=vault,
            base=review_base,
            plan_file=first_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
        second_result = task_review_runner.run_current_review(
            second,
            vault_root=vault,
            plan_file=second_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    finally:
        task_review_current._run_review = original
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    check("plain external target reaches the provider boundary", first_result["status"] == "prepared")
    check("coordinator and product roots remain distinct", first_result["vault_root"] == str(vault) and first_result["worktree"] == str(first))
    first_meta, captured_vault, captured_target, _, first_runtime = captures[0]
    check("current metadata binds both roots", first_meta["vault_root"] == str(vault) and first_meta["worktree"] == str(first))
    check("current metadata carries a clean exact-HEAD lease", first_meta["review_lease"]["head"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=first, text=True).strip())
    check(
        "current metadata binds the exact review base",
        first_meta["review_policy"]["base_sha"] == review_base
        and first_meta["review_lease"]["base"] == review_base,
    )
    review_context, manifest_path = _context(
        first_meta,
        vault,
        first,
        first_runtime,
        str(first_meta["task_id"]),
    )
    packet = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    packet_inputs = {item["name"]: item for item in manifest["inputs"]}
    diff = next(packet.glob("*-diff-head-diff.patch")).read_text(
        encoding="utf-8"
    )
    check(
        "explicit base packages the complete committed range",
        "-VALUE = 1" in diff
        and "+VALUE = 10" in diff
        and "+FEATURE = True" in diff,
        diff,
    )
    check(
        "range identity is reviewer-observable",
        manifest["metadata"]["base_sha"] == review_base
        and packet_inputs["head-diff.patch"]["source"]
        == f"git:diff:{review_base}..HEAD",
    )
    outcome_input = packet_inputs["outcome-contract.json"]
    outcome_bytes = next(
        packet.glob("*-outcome-outcome-contract.json")
    ).read_bytes()
    check(
        "current review packages behavior-specific Outcome evidence",
        outcome_input["role"] == "outcome"
        and outcome_input["sha256"] == first_meta["outcome_contract_sha256"]
        and hashlib.sha256(outcome_bytes).hexdigest()
        == first_meta["outcome_contract_sha256"]
        and extract_from_bytes(first_plan.read_bytes()).sha256
        == first_meta["outcome_contract_sha256"]
        and json.loads(outcome_bytes)["success_evidence"][0]["evidence_id"]
        == "external-feature-behavior",
    )
    prompt_pointer = _prompt(
        vault=vault,
        worktree=first,
        runtime_root=first_runtime,
        context=review_context,
        axis="openai-holistic",
        verification=False,
    )
    prompt = (first_runtime / prompt_pointer).read_text(encoding="utf-8")
    coordinator_contract = (
        vault / "docs/skill-references/engineering-quality-contract.md"
    )
    coordinator_inspect = vault / "scripts/review-inspect.py"
    coordinator_submit = vault / "scripts/harness/review_submit.py"
    check(
        "external prompt uses existing coordinator-owned review authority",
        str(coordinator_contract) in prompt
        and str(coordinator_inspect) in prompt
        and str(coordinator_submit) in prompt
        and coordinator_contract.is_file()
        and coordinator_inspect.is_file()
        and coordinator_submit.is_file(),
        prompt,
    )
    check(
        "external prompt binds the coordinator facade to the product",
        f"{coordinator_inspect} --worktree {first}" in prompt
        and str(first / "scripts/review-inspect.py") not in prompt,
        prompt,
    )

    original_review_head = first_meta["review_lease"]["head"]
    gate_path = (
        vault
        / ".vault-meta/harness/review-data"
        / first_meta["task_id"]
        / first_meta["task_id"]
        / "review-gate.json"
    )
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(
        json.dumps(
            {
                "status": "changes-requested",
                "context": {"head_sha": original_review_head},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (first / "resolution.py").write_text("RESOLVED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "resolution.py"], cwd=first, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve review"],
        cwd=first,
        check=True,
        capture_output=True,
    )
    amended_plan = review_plan(
        base / "amended-first-review-plan.md",
        desired_outcome=(
            "The reviewed checkout exports VALUE = 10, enables FEATURE, and "
            "records the committed review resolution."
        ),
        evidence_id="external-resolution-behavior",
        observable=(
            "The committed range contains VALUE = 10, FEATURE = True, and "
            "RESOLVED = True."
        ),
    )
    task_review_current._run_review = stop_before_provider
    try:
        rebound_result = task_review_runner.run_current_review(
            first,
            vault_root=vault,
            base=review_base,
            plan_file=amended_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    finally:
        task_review_current._run_review = original
    rebound_meta, _, _, _, rebound_runtime = captures[-1]
    rebound_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=first, text=True
    ).strip()
    rebound_plan = Path(rebound_meta["plan_file"])
    rebound_contract = extract_from_bytes(rebound_plan.read_bytes())
    rebound_context, rebound_manifest_path = _context(
        rebound_meta,
        vault,
        first,
        rebound_runtime,
        str(rebound_meta["task_id"]),
    )
    rebound_manifest = json.loads(
        rebound_manifest_path.read_text(encoding="utf-8")
    )
    rebound_inputs = {
        item["name"]: item for item in rebound_manifest["inputs"]
    }
    check(
        "committed resolution may amend the explicit behavior contract",
        rebound_result["status"] == "prepared"
        and original_review_head != rebound_head
        and rebound_plan == amended_plan
        and rebound_meta["review_lease"]["head"] == rebound_head
        and hashlib.sha256(rebound_plan.read_bytes()).hexdigest()
        == rebound_meta["approved_plan_sha256"]
        and rebound_contract.sha256
        == rebound_meta["outcome_contract_sha256"]
        and rebound_context.head_sha == rebound_head
        and rebound_inputs["head-diff.patch"]["source"]
        == f"git:diff:{review_base}..HEAD"
        and rebound_inputs["outcome-contract.json"]["sha256"]
        == rebound_contract.sha256,
    )

    old_task_id = str(rebound_meta["task_id"])
    ledger = FinalizationLedger(
        vault / ".vault-meta/harness/finalization-ledger",
        lineage_id=old_task_id,
        origin_task_id=old_task_id,
        plan_sha256=str(first_meta["approved_plan_sha256"]),
        outcome_contract_sha256=str(first_meta["outcome_contract_sha256"]),
    )
    for number in range(1, 6):
        attempt_id = str(uuid.UUID(int=number))
        ledger.reserve(
            attempt_id=attempt_id,
            exact_head=f"{number:040x}",
            task_id=old_task_id,
            worktree=str(first),
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
    operation_id = str(uuid.UUID(int=10))
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
    old_gate_bytes = gate_path.read_bytes()
    old_ledger_bytes = ledger.path.read_bytes()
    exhausted_active_path = (
        vault
        / ".vault-meta/harness/current-review"
        / resolve_target(first).target_key
        / "active.json"
    )
    exhausted_active_bytes = exhausted_active_path.read_bytes()
    task_review_current._run_review = stop_before_provider
    try:
        amended_plan_bytes = amended_plan.read_bytes()
        amended_plan.unlink()
        try:
            same_lineage = task_review_runner.run_current_review(
                first,
                vault_root=vault,
                base=review_base,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            )
        finally:
            amended_plan.write_bytes(amended_plan_bytes)

        scratch_before_root_failure = set(scratch.iterdir())
        original_runtime_root = current_review_admission._current_runtime_root
        current_review_admission._current_runtime_root = fail_after_uuid_root_setup
        try:
            try:
                task_review_runner.run_current_review(
                    first,
                    vault_root=vault,
                    base=review_base,
                    plan_file=amended_plan,
                    new_lineage=True,
                    origin_surface="11111111-1111-4111-8111-111111111111",
                    scratch_root=scratch,
                )
            except RuntimeError as exc:
                check(
                    "replacement root-setup failure is observable",
                    "uuid-root-setup fixture" in str(exc),
                    str(exc),
                )
            else:
                check("replacement root-setup failure is observable", False)
        finally:
            current_review_admission._current_runtime_root = original_runtime_root
        check(
            "replacement root-setup failure preserves predecessor and scratch",
            exhausted_active_path.read_bytes() == exhausted_active_bytes
            and set(scratch.iterdir()) == scratch_before_root_failure,
        )

        scratch_before_pointer_failure = set(scratch.iterdir())

        def fail_replacement_directory_fsync(
            path: Path, value: object
        ) -> None:
            if path != exhausted_active_path:
                ORIGINAL_ADMISSION_ATOMIC_JSON(path, value)
                return
            original_fsync = os.fsync
            calls = 0

            def fail_second_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("replacement-directory-fsync fixture")
                original_fsync(descriptor)

            os.fsync = fail_second_fsync
            try:
                ORIGINAL_ADMISSION_ATOMIC_JSON(path, value)
            finally:
                os.fsync = original_fsync

        current_review_admission._atomic_json = fail_replacement_directory_fsync
        try:
            try:
                task_review_runner.run_current_review(
                    first,
                    vault_root=vault,
                    base=review_base,
                    plan_file=amended_plan,
                    new_lineage=True,
                    origin_surface="11111111-1111-4111-8111-111111111111",
                    scratch_root=scratch,
                )
            except task_review_runner.TaskReviewError as exc:
                check(
                    "replacement active publication failure is typed",
                    "active pointer publication failed" in str(exc),
                    str(exc),
                )
            else:
                check("replacement active publication failure is typed", False)
        finally:
            current_review_admission._atomic_json = (
                ORIGINAL_ADMISSION_ATOMIC_JSON
            )
        check(
            "replacement publication failure restores predecessor and scratch",
            exhausted_active_path.read_bytes() == exhausted_active_bytes
            and set(scratch.iterdir()) == scratch_before_pointer_failure,
        )

        fresh_lineage = task_review_runner.run_current_review(
            first,
            vault_root=vault,
            base=review_base,
            plan_file=amended_plan,
            new_lineage=True,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    finally:
        task_review_current._run_review = original
    check(
        "fresh current review requires an explicit exhausted-lineage opt-in",
        same_lineage["task_id"] == old_task_id
        and fresh_lineage["task_id"] != old_task_id,
    )
    check(
        "implementation callback resumes from frozen plan after source removal",
        same_lineage["task_id"] == old_task_id,
    )
    check(
        "fresh current review preserves exhausted lineage evidence",
        gate_path.read_bytes() == old_gate_bytes
        and ledger.path.read_bytes() == old_ledger_bytes,
    )
    check("Harness state is coordinator-owned", captured_vault == vault and captured_target == first)
    check("scratch remains outside coordinator and target", first_runtime.is_relative_to(scratch) and not first_runtime.is_relative_to(first) and not first_runtime.is_relative_to(vault))
    check("external target receives no review metadata", not (first / ".task-meta.json").exists() and not (first / ".vault-meta").exists())
    check("external target stays Git-clean", subprocess.run(["git", "status", "--porcelain"], cwd=first, text=True, capture_output=True, check=True).stdout == "")

    first_key = resolve_target(first).target_key
    second_key = resolve_target(second).target_key
    first_active = vault / ".vault-meta/harness/current-review" / first_key / "active.json"
    second_active = vault / ".vault-meta/harness/current-review" / second_key / "active.json"
    check("each target owns an independent active pointer", first_key != second_key and first_active.is_file() and second_active.is_file())
    check("two targets do not share scratch", first_result["runtime_root"] != second_result["runtime_root"])

    wake = _callback_wake(first_meta, vault, first)
    check("callback binds the exact coordinator", f"--vault-root {vault}" in wake, wake)
    check("callback binds the exact target", f"--worktree {first}" in wake, wake)
    check(
        "callback retains the exact review base without repeating fresh authorization",
        f"--base {review_base}" in wake and "--new-lineage" not in wake,
        wake,
    )

    public = task_review_runner.parser().parse_args(
        [
            "current",
            "--target",
            str(first),
            "--vault-root",
            str(vault),
            "--base",
            review_base,
            "--new-lineage",
        ]
    )
    legacy = task_review_runner.parser().parse_args(
        ["current", "--worktree", str(first), "--vault-root", str(vault)]
    )
    check(
        "public target and legacy worktree forms resolve identically",
        public.target == legacy.worktree == first
        and public.base == review_base
        and public.new_lineage,
    )
    defaulted = task_review_runner.parser().parse_args(
        ["current", "--vault-root", str(vault)]
    )
    check("current facade permits the current Git root default", defaulted.target is None and defaulted.worktree is None)

    full_target = product(base / "products/full-target", 28)
    full_plan = review_plan(
        base / "full-review-plan.md",
        desired_outcome="The exact external checkout supports Full review.",
        evidence_id="full-target-behavior",
        observable="Full review binds four independent XHigh lanes.",
    )
    task_review_current._run_review = stop_before_provider
    for name, value in (
        ("LLM_OBSIDIAN_SESSION_RUNTIME", "codex"),
        ("LLM_OBSIDIAN_SESSION_MODEL", "gpt-5.6-sol"),
        ("LLM_OBSIDIAN_SESSION_EFFORT", "high"),
    ):
        os.environ[name] = value
    try:
        full_result = task_review_runner.run_current_review(
            full_target,
            vault_root=vault,
            full=True,
            plan_file=full_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
        full_meta, _, _, full_task_id, full_runtime = captures[-1]
        full_context, _ = _context(
            full_meta,
            vault,
            full_target,
            full_runtime,
            full_task_id,
        )
        _, full_request = compile_review_request(
            full_meta,
            vault,
            full_task_id,
            full_context,
        )
    finally:
        task_review_current._run_review = original
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    check(
        "external Full review compiles four XHigh provider lanes",
        full_result["status"] == "prepared"
        and full_request is not None
        and full_request.policy.axes
        == (
            "anthropic-intent",
            "anthropic-engineering",
            "openai-intent",
            "openai-engineering",
        )
        and {
            full_request.route_for(axis).effort
            for axis in full_request.policy.axes
        }
        == {"xhigh"},
    )

    for label, override in (
        ("effort", {"effort": "low"}),
        ("cross-model", {"cross_model": True}),
    ):
        override_target = product(
            base / f"products/full-{label}-override",
            40,
        )
        override_plan = review_plan(
            base / f"full-{label}-override-plan.md",
            desired_outcome="Full review retains its fixed topology.",
            evidence_id=f"full-{label}-override",
            observable="Full review rejects every explicit override.",
        )
        override_key = resolve_target(override_target).target_key
        captures_before_override = len(captures)
        scratch_entries_before = set(scratch.iterdir())
        task_review_current._run_review = stop_before_provider
        for name, value in (
            ("LLM_OBSIDIAN_SESSION_RUNTIME", "codex"),
            ("LLM_OBSIDIAN_SESSION_MODEL", "gpt-5.6-sol"),
            ("LLM_OBSIDIAN_SESSION_EFFORT", "high"),
        ):
            os.environ[name] = value
        try:
            task_review_runner.run_current_review(
                override_target,
                vault_root=vault,
                full=True,
                plan_file=override_plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
                **override,
            )
        except (task_review_runner.TaskReviewError, ValueError) as exc:
            check(
                f"current Full review rejects {label} override",
                "Full review" in str(exc),
                str(exc),
            )
        else:
            check(f"current Full review rejects {label} override", False)
        finally:
            task_review_current._run_review = original
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        check(
            f"Full {label} rejection precedes owner, scratch, and provider effects",
            len(captures) == captures_before_override
            and set(scratch.iterdir()) == scratch_entries_before
            and not (
                vault
                / ".vault-meta/harness/current-review"
                / override_key
                / "active.json"
            ).exists(),
        )

    missing_parent = product(base / "products/missing-target-parent", 29)
    missing_target = missing_parent / "intended-external-product"
    missing_target_plan = review_plan(
        base / "missing-target-review-plan.md",
        desired_outcome="The exact requested external checkout is reviewed.",
        evidence_id="missing-target-behavior",
        observable="Only the exact existing target may enter current review.",
    )
    captures_before_missing_target = len(captures)
    scratch_entries_before = set(scratch.iterdir())
    active_entries_before = set(
        (vault / ".vault-meta/harness/current-review").glob("*/active.json")
    )
    task_review_current._run_review = stop_before_provider
    for name, value in (
        ("LLM_OBSIDIAN_SESSION_RUNTIME", "codex"),
        ("LLM_OBSIDIAN_SESSION_MODEL", "gpt-5.6-sol"),
        ("LLM_OBSIDIAN_SESSION_EFFORT", "high"),
    ):
        os.environ[name] = value
    try:
        task_review_runner.run_current_review(
            missing_target,
            vault_root=vault,
            plan_file=missing_target_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check(
            "current review rejects a missing exact target",
            "does not exist" in str(exc),
            str(exc),
        )
    else:
        check("current review rejects a missing exact target", False)
    finally:
        task_review_current._run_review = original
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    check(
        "missing target rejection precedes owner, scratch, and provider effects",
        len(captures) == captures_before_missing_target
        and set(scratch.iterdir()) == scratch_entries_before
        and set(
            (vault / ".vault-meta/harness/current-review").glob("*/active.json")
        )
        == active_entries_before,
    )

    dirty = product(base / "products/dirty", 3)
    dirty_plan = review_plan(
        base / "dirty-review-plan.md",
        desired_outcome="The reviewed checkout exports VALUE = 3.",
        evidence_id="dirty-value-behavior",
        observable="The committed app.py contains VALUE = 3.",
    )
    (dirty / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    dirty_key = resolve_target(dirty).target_key
    captures_before_dirty = len(captures)
    task_review_current._run_review = stop_before_provider
    try:
        task_review_runner.run_current_review(
            dirty,
            vault_root=vault,
            plan_file=dirty_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check("dirty target is rejected before provider effect", "clean" in str(exc))
    else:
        check("dirty target is rejected before provider effect", False)
    finally:
        task_review_current._run_review = original
    check("dirty rejection creates no active pointer", len(captures) == captures_before_dirty and not (vault / ".vault-meta/harness/current-review" / dirty_key / "active.json").exists())

    no_outcome = product(base / "products/no-outcome", 5)
    no_outcome_plan = base / "no-outcome-plan.md"
    no_outcome_plan.write_text(
        "# Review scope\n\nReview this checkout without a contract.\n",
        encoding="utf-8",
    )
    no_outcome_key = resolve_target(no_outcome).target_key
    captures_before_no_outcome = len(captures)
    scratch_entries_before = set(scratch.iterdir())
    task_review_current._run_review = stop_before_provider
    for name, value in (
        ("LLM_OBSIDIAN_SESSION_RUNTIME", "codex"),
        ("LLM_OBSIDIAN_SESSION_MODEL", "gpt-5.6-sol"),
        ("LLM_OBSIDIAN_SESSION_EFFORT", "high"),
    ):
        os.environ[name] = value
    try:
        task_review_runner.run_current_review(
            no_outcome,
            vault_root=vault,
            plan_file=no_outcome_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check(
            "approval-capable current review rejects an outcome-free plan",
            "Outcome Contract" in str(exc),
            str(exc),
        )
    else:
        check(
            "approval-capable current review rejects an outcome-free plan",
            False,
        )
    finally:
        task_review_current._run_review = original
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    check(
        "outcome-free rejection has no review ownership or provider effect",
        len(captures) == captures_before_no_outcome
        and not (
            vault
            / ".vault-meta/harness/current-review"
            / no_outcome_key
            / "active.json"
        ).exists()
        and set(scratch.iterdir()) == scratch_entries_before,
        repr(
            {
                "capture_delta": len(captures) - captures_before_no_outcome,
                "active": (
                    vault
                    / ".vault-meta/harness/current-review"
                    / no_outcome_key
                    / "active.json"
                ).exists(),
                "scratch_added": sorted(
                    str(path) for path in set(scratch.iterdir()) - scratch_entries_before
                ),
            }
        ),
    )

    missing_plan = product(base / "products/missing-plan", 6)
    missing_plan_key = resolve_target(missing_plan).target_key
    captures_before_missing_plan = len(captures)
    scratch_entries_before = set(scratch.iterdir())
    task_review_current._run_review = stop_before_provider
    try:
        task_review_runner.run_current_review(
            missing_plan,
            vault_root=vault,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check(
            "first approval-capable current review requires an explicit plan",
            "requires --plan" in str(exc),
            str(exc),
        )
    else:
        check(
            "first approval-capable current review requires an explicit plan",
            False,
        )
    finally:
        task_review_current._run_review = original
    check(
        "missing-plan rejection has no ownership, scratch, or provider effect",
        len(captures) == captures_before_missing_plan
        and not (
            vault
            / ".vault-meta/harness/current-review"
            / missing_plan_key
            / "active.json"
        ).exists()
        and set(scratch.iterdir()) == scratch_entries_before,
    )

    circular = product(base / "products/circular", 7)
    circular_plan = review_plan(
        base / "circular-review-plan.md",
        desired_outcome=(
            "The committed review denominator is correct, complete, "
            "maintainable, and ready for its intended use."
        ),
        evidence_id="verification",
        observable=(
            "Relevant deterministic verification passes or gaps are reported."
        ),
        behavior_bound=False,
    )
    circular_key = resolve_target(circular).target_key
    captures_before_circular = len(captures)
    scratch_entries_before = set(scratch.iterdir())
    task_review_current._run_review = stop_before_provider
    try:
        task_review_runner.run_current_review(
            circular,
            vault_root=vault,
            plan_file=circular_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check(
            "circular current-review contract is rejected before ownership",
            "behavior-bound success evidence" in str(exc),
            str(exc),
        )
    else:
        check(
            "circular current-review contract is rejected before ownership",
            False,
        )
    finally:
        task_review_current._run_review = original
    check(
        "circular rejection has no ownership, scratch, or provider effect",
        len(captures) == captures_before_circular
        and not (
            vault
            / ".vault-meta/harness/current-review"
            / circular_key
            / "active.json"
        ).exists()
        and set(scratch.iterdir()) == scratch_entries_before,
    )

    renamed_generic = product(base / "products/renamed-generic", 8)
    renamed_generic_plan = review_plan(
        base / "renamed-generic-review-plan.md",
        desired_outcome=(
            "The committed review denominator is correct, complete, "
            "maintainable, and ready for its intended use."
        ),
        evidence_id="checks-pass",
        observable=(
            "Relevant deterministic verification passes or gaps are reported."
        ),
        behavior_bound=False,
    )
    renamed_generic_key = resolve_target(renamed_generic).target_key
    captures_before_renamed = len(captures)
    scratch_entries_before = set(scratch.iterdir())
    task_review_current._run_review = stop_before_provider
    try:
        task_review_runner.run_current_review(
            renamed_generic,
            vault_root=vault,
            plan_file=renamed_generic_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check(
            "renamed generic evidence is rejected before ownership",
            "behavior-bound success evidence" in str(exc),
            str(exc),
        )
    else:
        check("renamed generic evidence is rejected before ownership", False)
    finally:
        task_review_current._run_review = original
    check(
        "renamed generic rejection has no ownership, scratch, or provider effect",
        len(captures) == captures_before_renamed
        and not (
            vault
            / ".vault-meta/harness/current-review"
            / renamed_generic_key
            / "active.json"
        ).exists()
        and set(scratch.iterdir()) == scratch_entries_before,
    )

    mixed_generic = product(base / "products/mixed-generic", 9)
    mixed_generic_plan = base / "mixed-generic-review-plan.md"
    mixed_generic_plan.write_text(
        "\n".join(
            (
                "# Mixed generic review plan",
                "",
                "## Outcome Contract",
                "",
                "```json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "desired_outcome": "The reviewed change is ready.",
                        "success_evidence": [
                            {
                                "evidence_id": "verification",
                                "observable": "Verification passes.",
                            },
                            {
                                "evidence_id": "checks-pass",
                                "observable": "Relevant checks pass.",
                            },
                        ],
                        "non_goals": ["Do not inspect unrelated changes."],
                    },
                    sort_keys=True,
                ),
                "```",
                "",
            )
        ),
        encoding="utf-8",
    )
    mixed_generic_key = resolve_target(mixed_generic).target_key
    captures_before_mixed = len(captures)
    scratch_entries_before = set(scratch.iterdir())
    task_review_current._run_review = stop_before_provider
    try:
        task_review_runner.run_current_review(
            mixed_generic,
            vault_root=vault,
            plan_file=mixed_generic_plan,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    except task_review_runner.TaskReviewError as exc:
        check(
            "mixed renamed generic evidence is rejected before ownership",
            "behavior-bound success evidence" in str(exc),
            str(exc),
        )
    else:
        check("mixed renamed generic evidence is rejected before ownership", False)
    finally:
        task_review_current._run_review = original
    check(
        "mixed generic rejection has no ownership, scratch, or provider effect",
        len(captures) == captures_before_mixed
        and not (
            vault
            / ".vault-meta/harness/current-review"
            / mixed_generic_key
            / "active.json"
        ).exists()
        and set(scratch.iterdir()) == scratch_entries_before,
    )

    preflight_products = {
        name: product(base / f"products/preflight-{name}", 30 + index)
        for index, name in enumerate(
            (
                "missing-surface",
                "tracked-default",
                "request",
                "publication",
                "root-setup",
                "active-publication",
            )
        )
    }
    preflight_plans = {
        name: review_plan(
            base / f"preflight-{name}-plan.md",
            desired_outcome=f"The checkout preserves preflight behavior {name}.",
            evidence_id=f"preflight-{name}",
            observable=f"The {name} preflight has no ownerless scratch.",
        )
        for name in preflight_products
    }
    session_environment = {
        name: os.environ.get(name)
        for name in (
            "LLM_OBSIDIAN_SESSION_RUNTIME",
            "LLM_OBSIDIAN_SESSION_MODEL",
            "LLM_OBSIDIAN_SESSION_EFFORT",
            "CMUX_SURFACE_ID",
        )
    }
    os.environ["LLM_OBSIDIAN_SESSION_RUNTIME"] = "codex"
    os.environ["LLM_OBSIDIAN_SESSION_MODEL"] = "gpt-5.6-sol"
    os.environ["LLM_OBSIDIAN_SESSION_EFFORT"] = "high"
    os.environ.pop("CMUX_SURFACE_ID", None)

    def preflight_rejection(
        name: str,
        invoke,
        needle: str,
    ) -> None:
        target = preflight_products[name]
        target_key = resolve_target(target).target_key
        scratch_before = set(scratch.iterdir())
        captures_before = len(captures)
        try:
            invoke(target, preflight_plans[name])
        except (task_review_runner.TaskReviewError, RuntimeError) as exc:
            check(f"{name} preflight is typed", needle in str(exc), str(exc))
        else:
            check(f"{name} preflight is typed", False)
        check(
            f"{name} preflight leaves no owner or scratch",
            len(captures) == captures_before
            and not (
                vault
                / ".vault-meta/harness/current-review"
                / target_key
                / "active.json"
            ).exists()
            and set(scratch.iterdir()) == scratch_before,
        )

    preflight_rejection(
        "missing-surface",
        lambda target, plan: task_review_runner.run_current_review(
            target,
            vault_root=vault,
            plan_file=plan,
            scratch_root=scratch,
        ),
        "exact cmux origin surface",
    )

    original_routing = current_review_admission.routing_from_environment
    current_review_admission.routing_from_environment = lambda _config: (
        {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
        },
        "tracked-default",
    )
    try:
        preflight_rejection(
            "tracked-default",
            lambda target, plan: task_review_runner.run_current_review(
                target,
                vault_root=vault,
                plan_file=plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            ),
            "host-confirmed current session route",
        )
    finally:
        current_review_admission.routing_from_environment = original_routing

    original_request = current_review_admission._request

    def fail_request_preparation(*_args, **_kwargs):
        raise RuntimeError("request-preparation fixture")

    current_review_admission._request = fail_request_preparation
    try:
        preflight_rejection(
            "request",
            lambda target, plan: task_review_runner.run_current_review(
                target,
                vault_root=vault,
                plan_file=plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            ),
            "request-preparation fixture",
        )
    finally:
        current_review_admission._request = original_request

    original_atomic_json = current_review_admission._atomic_json

    def fail_unpublished_meta(path: Path, value: object) -> None:
        if path.name == "current-review.json":
            raise RuntimeError("unpublished-metadata fixture")
        original_atomic_json(path, value)

    current_review_admission._atomic_json = fail_unpublished_meta
    try:
        preflight_rejection(
            "publication",
            lambda target, plan: task_review_runner.run_current_review(
                target,
                vault_root=vault,
                plan_file=plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            ),
            "unpublished-metadata fixture",
        )
    finally:
        current_review_admission._atomic_json = original_atomic_json

    original_runtime_root = current_review_admission._current_runtime_root

    current_review_admission._current_runtime_root = fail_after_uuid_root_setup
    try:
        preflight_rejection(
            "root-setup",
            lambda target, plan: task_review_runner.run_current_review(
                target,
                vault_root=vault,
                plan_file=plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            ),
            "uuid-root-setup fixture",
        )
    finally:
        current_review_admission._current_runtime_root = original_runtime_root

    active_publication_target = preflight_products["active-publication"]
    active_publication_path = (
        vault
        / ".vault-meta/harness/current-review"
        / resolve_target(active_publication_target).target_key
        / "active.json"
    )

    def fail_active_directory_fsync(path: Path, value: object) -> None:
        if path != active_publication_path:
            original_atomic_json(path, value)
            return
        original_fsync = os.fsync
        calls = 0

        def fail_second_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("active-directory-fsync fixture")
            original_fsync(descriptor)

        os.fsync = fail_second_fsync
        try:
            original_atomic_json(path, value)
        finally:
            os.fsync = original_fsync

    current_review_admission._atomic_json = fail_active_directory_fsync
    try:
        preflight_rejection(
            "active-publication",
            lambda target, plan: task_review_runner.run_current_review(
                target,
                vault_root=vault,
                plan_file=plan,
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            ),
            "active pointer publication failed",
        )
    finally:
        current_review_admission._atomic_json = original_atomic_json
        for name, value in session_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    mandatory_review_artifacts = (
        "scripts/review-inspect.py",
        "scripts/harness/review_submit.py",
        "docs/skill-references/engineering-quality-contract.md",
    )
    for index, relative in enumerate(mandatory_review_artifacts):
        incomplete = coordinator(base / f"incomplete-coordinator-{index}")
        (incomplete / relative).unlink()
        untouched = product(base / f"products/incomplete-{index}", index + 20)
        incomplete_key = resolve_target(untouched).target_key
        captures_before_incomplete = len(captures)
        scratch_entries_before = set(scratch.iterdir())
        task_review_current._run_review = stop_before_provider
        try:
            task_review_runner.run_current_review(
                untouched,
                vault_root=incomplete,
                plan_file=review_plan(
                    base / f"incomplete-review-plan-{index}.md",
                    desired_outcome=(
                        f"The reviewed checkout exports VALUE = {index + 20}."
                    ),
                    evidence_id=f"incomplete-value-{index}",
                    observable=(
                        f"The committed app.py contains VALUE = {index + 20}."
                    ),
                ),
                origin_surface="11111111-1111-4111-8111-111111111111",
                scratch_root=scratch,
            )
        except task_review_runner.TaskReviewError as exc:
            check(
                f"missing {relative} fails before review effects",
                "verified LLM Obsidian vault" in str(exc),
                str(exc),
            )
        else:
            check(f"missing {relative} fails before review effects", False)
        finally:
            task_review_current._run_review = original
        check(
            f"missing {relative} leaves no gate, operation, prompt, or provider effect",
            len(captures) == captures_before_incomplete
            and not (
                incomplete
                / ".vault-meta/harness/current-review"
                / incomplete_key
                / "active.json"
            ).exists()
            and not (incomplete / ".vault-meta/harness").exists()
            and set(scratch.iterdir()) == scratch_entries_before,
        )

print("\nAll external current-review tests passed.")
