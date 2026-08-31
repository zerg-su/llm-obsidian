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
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_review_current  # noqa: E402
from review_target import resolve_target  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
from task_review_context import _context  # noqa: E402
from task_review_request import _prompt  # noqa: E402
from task_review_transport import _callback_wake  # noqa: E402


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
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
        second_result = task_review_runner.run_current_review(
            second,
            vault_root=vault,
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
    synthetic_plan = first_runtime / "inputs/current-review-scope.md"
    check(
        "default current review packages its real Outcome Contract",
        outcome_input["role"] == "outcome"
        and outcome_input["sha256"] == first_meta["outcome_contract_sha256"]
        and hashlib.sha256(outcome_bytes).hexdigest()
        == first_meta["outcome_contract_sha256"]
        and extract_from_bytes(synthetic_plan.read_bytes()).sha256
        == first_meta["outcome_contract_sha256"]
        and bool(json.loads(outcome_bytes)["success_evidence"]),
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
    task_review_current._run_review = stop_before_provider
    try:
        rebound_result = task_review_runner.run_current_review(
            first,
            vault_root=vault,
            base=review_base,
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
        "committed resolution rebinds synthetic range scope and outcome",
        rebound_result["status"] == "prepared"
        and original_review_head != rebound_head
        and f"{review_base}..{rebound_head}"
        in rebound_plan.read_text(encoding="utf-8")
        and original_review_head not in rebound_plan.read_text(encoding="utf-8")
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
    check("callback retains the exact review base", f"--base {review_base}" in wake, wake)

    public = task_review_runner.parser().parse_args(
        [
            "current",
            "--target",
            str(first),
            "--vault-root",
            str(vault),
            "--base",
            review_base,
        ]
    )
    legacy = task_review_runner.parser().parse_args(
        ["current", "--worktree", str(first), "--vault-root", str(vault)]
    )
    check(
        "public target and legacy worktree forms resolve identically",
        public.target == legacy.worktree == first and public.base == review_base,
    )
    defaulted = task_review_runner.parser().parse_args(
        ["current", "--vault-root", str(vault)]
    )
    check("current facade permits the current Git root default", defaulted.target is None and defaulted.worktree is None)

    dirty = product(base / "products/dirty", 3)
    (dirty / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    dirty_key = resolve_target(dirty).target_key
    captures_before_dirty = len(captures)
    task_review_current._run_review = stop_before_provider
    try:
        task_review_runner.run_current_review(
            dirty,
            vault_root=vault,
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
