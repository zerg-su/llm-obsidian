#!/usr/bin/env python3
"""Behavior matrix for the code-owned plan review facade."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def outcome(observable: str = "the exact behavior is established") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "purpose": "review the plan safely",
            "desired_outcome": "the plan is reviewed as intent",
            "success_evidence": [
                {"evidence_id": "E-plan", "observable": observable}
            ],
            "non_goals": ["launch an implementation review"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def plan_text(
    *,
    outcome_headings: int = 1,
    disposition_headings: int = 1,
    evidence_headings: int = 1,
    design: str = "Implement the bounded facade.",
) -> str:
    parts = ["---", "type: plan", "status: pending", "---", "", "# Plan", ""]
    for _ in range(outcome_headings):
        parts.extend(("## Outcome Contract", "", "```json", outcome(), "```", ""))
    parts.extend(("## Design", "", design, ""))
    for _ in range(disposition_headings):
        parts.extend(
            (
                "## Capability Dispositions and Defect Ledger",
                "",
                "| Capability | Disposition |",
                "|---|---|",
                "| plan facade | included |",
                "",
            )
        )
    for _ in range(evidence_headings):
        parts.extend(
            (
                "## Success Evidence Map",
                "",
                "| Evidence | Check |",
                "|---|---|",
                "| E-plan | facade matrix |",
                "",
            )
        )
    return "\n".join(parts)


with tempfile.TemporaryDirectory(prefix="plan-review-facade.") as raw:
    tmp = Path(raw)
    worktree = tmp / "repo"
    plan_dir = worktree / "wiki/plans"
    plan_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "plan-review@example.invalid"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Plan Review Test"],
        cwd=worktree,
        check=True,
    )
    (worktree / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan = plan_dir / "approved.md"
    plan.write_text(plan_text(), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add approved plan"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        plan_review = importlib.import_module("task_review_plan")
        plan_rebind = importlib.import_module("task_review_plan_rebind")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "RED: task_review_plan must provide the code-owned compiler/facade"
        ) from exc

    compiled = plan_review.compile_plan_review(worktree, plan)

    class SessionCounter:
        starts = 0

        def start(self, *_args: object, **_kwargs: object) -> object:
            self.starts += 1
            raise AssertionError("invalid plan boundary reached provider start")

    sessions = SessionCounter()
    check(
        "1 valid plan compiles four independent artifacts",
        set(compiled.artifacts)
        == {"outcome", "design", "capability_dispositions", "success_evidence"}
        and len(set(compiled.artifact_sha256.values())) == 4,
        compiled,
    )
    check(
        "2 design artifact replaces every protected region with its digest",
        b"the plan is reviewed as intent" not in compiled.artifacts["design"]
        and b"plan facade | included" not in compiled.artifacts["design"]
        and b"E-plan | facade matrix" not in compiled.artifacts["design"]
        and all(
            digest.encode() in compiled.artifacts["design"]
            for name, digest in compiled.artifact_sha256.items()
            if name != "design"
        ),
    )

    def rejected(label: str, text: str, **kwargs: object) -> None:
        candidate = plan_dir / f"{label}.md"
        candidate.write_text(text, encoding="utf-8")
        try:
            plan_review.run_plan_review(
                worktree,
                plan_file=candidate,
                capability_dispositions=str(
                    kwargs.get("capability_dispositions") or ""
                ),
                success_evidence_map=str(
                    kwargs.get("success_evidence_map") or ""
                ),
                runtime_manager=sessions,
                apply_finalizing_recovery=lambda **_ignored: {},
            )
        except plan_review.PlanReviewError as exc:
            guarded = (
                exc.code == "plan-review-artifact-boundary-invalid"
                and sessions.starts == 0
            )
        else:
            guarded = False
        check(label, guarded)

    rejected("3 missing Outcome heading fails closed", plan_text(outcome_headings=0))
    rejected("4 duplicated Outcome heading fails closed", plan_text(outcome_headings=2))
    rejected(
        "5 missing dispositions without explicit pointer fails closed",
        plan_text(disposition_headings=0),
    )
    rejected(
        "6 missing evidence map without explicit pointer fails closed",
        plan_text(evidence_headings=0),
    )

    disposition = worktree / "wiki/dispositions.md"
    evidence = worktree / "wiki/evidence.md"
    disposition.write_text("# dispositions\n\nplan facade: included\n", encoding="utf-8")
    evidence.write_text("# evidence\n\nE-plan: facade matrix\n", encoding="utf-8")
    explicit_plan = plan_dir / "explicit.md"
    explicit_plan.write_text(
        plan_text(disposition_headings=0, evidence_headings=0),
        encoding="utf-8",
    )
    explicit = plan_review.compile_plan_review(
        worktree,
        explicit_plan,
        capability_dispositions="wiki/dispositions.md",
        success_evidence_map="wiki/evidence.md",
    )
    check(
        "7 absent inline sections accept two distinct exact pointers",
        explicit.artifacts["capability_dispositions"] == disposition.read_bytes()
        and explicit.artifacts["success_evidence"] == evidence.read_bytes(),
    )
    rejected(
        "8 overlapping explicit artifacts fail closed",
        plan_text(disposition_headings=0, evidence_headings=0),
        capability_dispositions="wiki/dispositions.md",
        success_evidence_map="wiki/dispositions.md",
    )
    alias_root = worktree / "wiki-alias"
    alias_root.symlink_to(worktree / "wiki", target_is_directory=True)
    rejected(
        "9 symlinked parent aliases cannot duplicate a protected artifact",
        plan_text(disposition_headings=0, evidence_headings=0),
        capability_dispositions="wiki/dispositions.md",
        success_evidence_map="wiki-alias/dispositions.md",
    )
    alias_sessions = SessionCounter()
    try:
        plan_review.run_plan_review(
            worktree,
            plan_file=alias_root / "plans/approved.md",
            runtime_manager=alias_sessions,
            apply_finalizing_recovery=lambda **_ignored: {},
        )
    except plan_review.PlanReviewError as exc:
        aliased_plan_rejected = (
            exc.code == "plan-review-artifact-boundary-invalid"
            and alias_sessions.starts == 0
        )
    except AssertionError:
        aliased_plan_rejected = False
    else:
        aliased_plan_rejected = False
    check(
        "10 a plan reached through a symlinked parent is rejected before launch",
        aliased_plan_rejected,
    )

    base_sha, head_sha = plan_review.resolve_plan_oids(worktree, compiled)
    check(
        "11 single-parent exact plan commit derives HEAD parent",
        base_sha == baseline
        and head_sha
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        (base_sha, head_sha),
    )
    committed_plan = plan.read_bytes()
    plan.write_text(
        plan_text(design="Dirty bytes must never reach a reviewer."),
        encoding="utf-8",
    )
    dirty_sessions = SessionCounter()
    try:
        plan_review.run_plan_review(
            worktree,
            plan_file=plan,
            runtime_manager=dirty_sessions,
            apply_finalizing_recovery=lambda **_ignored: {},
        )
    except plan_review.PlanReviewError as exc:
        dirty_plan_rejected = (
            exc.code == "plan-review-base-invalid"
            and dirty_sessions.starts == 0
        )
    except AssertionError:
        dirty_plan_rejected = False
    else:
        dirty_plan_rejected = False
    plan.write_bytes(committed_plan)
    check(
        "12 dirty plan bytes are rejected before provider launch",
        dirty_plan_rejected,
    )
    packet_root = tmp / "packet-scratch"
    boundary = plan_review.materialize_plan_review(
        packet_root,
        compiled,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    inspection = json.loads(
        (packet_root / "inputs/plan-review-inspection.json").read_text(
            encoding="utf-8"
        )
    )
    from harness.workflows.review import ReviewContext
    from task_review_request import _prompt

    prompt_pointer = _prompt(
        vault=ROOT,
        worktree=worktree,
        runtime_root=packet_root,
        context=ReviewContext(
            "packets/example/manifest.json",
            head_sha,
            "scoped",
            "a" * 64,
            purpose="intent",
            boundary_input_sha256=boundary.input_sha256,
        ),
        axis="openai-holistic",
        verification=False,
    )
    prompt = (packet_root / prompt_pointer).read_text(encoding="utf-8")
    check(
        "plan packet and prompt expose exact OIDs and four literal inspect commands",
        inspection["base_sha"] == base_sha
        and inspection["head_sha"] == head_sha
        and len(inspection["commands"]) == 4
        and all(command in prompt for command in inspection["commands"])
        and f"Exact review base: `{base_sha}`" in prompt
        and f"Exact product HEAD: `{head_sha}`" in prompt,
        (inspection, prompt),
    )
    try:
        plan_review.run_plan_review(
            worktree,
            plan_file=plan,
            base="HEAD",
            runtime_manager=sessions,
            apply_finalizing_recovery=lambda **_ignored: {},
        )
    except plan_review.PlanReviewError as exc:
        symbolic_rejected = (
            exc.code == "plan-review-base-invalid" and sessions.starts == 0
        )
    else:
        symbolic_rejected = False
    check("13 symbolic base is rejected before launch", symbolic_rejected)

    subprocess.run(
        [
            "git",
            "add",
            explicit_plan.relative_to(worktree),
            disposition.relative_to(worktree),
            evidence.relative_to(worktree),
        ],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add explicit protected artifacts"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    committed_explicit = plan_review.compile_plan_review(
        worktree,
        explicit_plan,
        capability_dispositions="wiki/dispositions.md",
        success_evidence_map="wiki/evidence.md",
    )
    disposition_bytes = disposition.read_bytes()
    disposition.write_text(
        "# dispositions\n\nplan facade: dirty\n", encoding="utf-8"
    )
    explicit_sessions = SessionCounter()
    try:
        plan_review.run_plan_review(
            worktree,
            plan_file=explicit_plan,
            base=head_sha,
            capability_dispositions="wiki/dispositions.md",
            success_evidence_map="wiki/evidence.md",
            runtime_manager=explicit_sessions,
            apply_finalizing_recovery=lambda **_ignored: {},
        )
    except plan_review.PlanReviewError as exc:
        dirty_explicit_rejected = (
            exc.code == "plan-review-base-invalid"
            and explicit_sessions.starts == 0
        )
    except AssertionError:
        dirty_explicit_rejected = False
    else:
        dirty_explicit_rejected = False
    disposition.write_bytes(disposition_bytes)
    check(
        "14 dirty explicit protected bytes are rejected before provider launch",
        dirty_explicit_rejected
        and committed_explicit.explicit_artifact_paths
        == {
            "capability_dispositions": "wiki/dispositions.md",
            "success_evidence": "wiki/evidence.md",
        },
    )

    (worktree / "README.md").write_text("baseline\nunrelated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "unrelated head"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        plan_review.resolve_plan_oids(worktree, compiled)
    except plan_review.PlanReviewError as exc:
        missing_base_rejected = exc.code == "plan-review-base-invalid"
    else:
        missing_base_rejected = False
    check("15 current plan review without a safe base fails closed", missing_base_rejected)

    plan.write_text(
        plan_text(design="Plan and product changed together."), encoding="utf-8"
    )
    (worktree / "README.md").write_text(
        "baseline\nunrelated\nmixed\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", plan.relative_to(worktree), "README.md"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "mixed plan and product head"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    mixed_sessions = SessionCounter()
    try:
        plan_review.run_plan_review(
            worktree,
            plan_file=plan,
            runtime_manager=mixed_sessions,
            apply_finalizing_recovery=lambda **_ignored: {},
        )
    except plan_review.PlanReviewError as exc:
        mixed_base_rejected = (
            exc.code == "plan-review-base-invalid" and mixed_sessions.starts == 0
        )
    except AssertionError:
        mixed_base_rejected = False
    else:
        mixed_base_rejected = False
    check(
        "16 mixed plan and product HEAD requires an explicit trusted base",
        mixed_base_rejected,
    )

    legacy = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/task-review-runner.py"),
            "current",
            "--worktree",
            str(worktree),
            "--plan",
            str(plan),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "17 legacy current --plan rejects the ambiguous implementation default",
        legacy.returncode == 3
        and "use the plan facade" in legacy.stderr
        and not (worktree / ".vault-meta").exists(),
        (legacy.stdout, legacy.stderr),
    )

    plan.write_text(plan_text(), encoding="utf-8")
    subprocess.run(["git", "add", plan.relative_to(worktree)], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "restore approved plan"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    reviewed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan.write_text(
        plan_text(design="Implement the bounded facade and retain both lanes."),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", plan.relative_to(worktree)], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve plan design"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    resolved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    resolved = plan_review.compile_plan_review(worktree, plan)
    delta = plan_review.validate_design_rebind(
        worktree,
        compiled,
        resolved,
        reviewed_head=reviewed_head,
        resolved_head=resolved_head,
    )
    check(
        "design-only rebind binds reviewed/resolved plan digests and exact Git delta",
        delta["reviewed_plan_sha256"] == compiled.plan_sha256
        and delta["resolved_plan_sha256"] == resolved.plan_sha256
        and delta["changed_paths"] == [compiled.plan_relative_path]
        and len(delta["git_delta_sha256"]) == 64,
        delta,
    )
    active_runtime = tmp / "active-review-scratch"
    old_boundary = plan_review.materialize_plan_review(
        active_runtime,
        compiled,
        base_sha=baseline,
        head_sha=reviewed_head,
    )
    boundary_path = active_runtime / "inputs/review-boundary-input.json"
    boundary_path.write_text(
        json.dumps(old_boundary.payload(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    active_path = tmp / "active.json"
    task_id = "11111111-1111-4111-8111-111111111111"
    candidate = {
        "task_id": task_id,
        "runtime_root": str(active_runtime),
        "review_boundary_input_file": str(boundary_path),
        "plan_review": {
            "schema_version": 1,
            "base_sha": baseline,
            "head_sha": reviewed_head,
            "plan_relative_path": compiled.plan_relative_path,
            "artifact_root": "runtime",
        },
    }
    gate_state = {
        "status": "awaiting-resolution",
        "context": {"head_sha": reviewed_head},
    }
    requested_policy = {
        "purpose": "intent",
        "boundary_input_sha256": resolved.boundary().input_sha256,
    }
    (worktree / ".task-review-resolution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": task_id,
                "reviewed_head_sha": reviewed_head,
                "resolved_head_sha": resolved_head,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    foreign_boundary = tmp / "foreign-boundary.json"
    foreign_boundary.write_text(boundary_path.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        plan_review.rebind_active_plan_review(
            worktree,
            active_path,
            {**candidate, "review_boundary_input_file": str(foreign_boundary)},
            gate_state,
            requested_policy,
            resolved,
            requested_base_sha=reviewed_head,
            requested_head_sha=resolved_head,
        )
    except plan_review.PlanReviewError:
        foreign_rejected = True
    else:
        foreign_rejected = False
    rebound = plan_review.rebind_active_plan_review(
        worktree,
        active_path,
        candidate,
        gate_state,
        requested_policy,
        resolved,
        requested_base_sha=reviewed_head,
        requested_head_sha=resolved_head,
    )
    rebound_boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    check(
        "retained-lane rebind updates exact scratch without a new session",
        foreign_rejected
        and rebound["task_id"] == task_id
        and rebound["plan_review"]["reviewed_plan_sha256"]
        == compiled.plan_sha256
        and rebound["plan_review"]["resolved_plan_sha256"]
        == resolved.plan_sha256
        and rebound_boundary["design_sha256"]
        == resolved.artifact_sha256["design"]
        and sessions.starts == 0,
        rebound,
    )

    class RebindCrash(RuntimeError):
        pass

    crash_replay: dict[str, bool] = {}
    original_atomic_json = plan_review._atomic_json
    original_atomic_bytes = plan_review._atomic_bytes
    original_rebind_atomic_json = plan_rebind._atomic_json
    original_rebind_atomic_bytes = plan_rebind._atomic_bytes
    for stage in ("boundary", "current-review", "active"):
        crash_runtime = tmp / f"crash-{stage}-scratch"
        crash_boundary = plan_review.materialize_plan_review(
            crash_runtime,
            compiled,
            base_sha=baseline,
            head_sha=reviewed_head,
        )
        crash_boundary_path = (
            crash_runtime / "inputs/review-boundary-input.json"
        )
        original_atomic_json(crash_boundary_path, crash_boundary.payload())
        crash_active_path = tmp / f"crash-{stage}-active.json"
        crash_candidate = {
            **candidate,
            "runtime_root": str(crash_runtime),
            "review_boundary_input_file": str(crash_boundary_path),
        }
        original_atomic_json(
            crash_runtime / "current-review.json", crash_candidate
        )
        original_atomic_json(crash_active_path, crash_candidate)
        target = {
            "boundary": crash_boundary_path,
            "current-review": crash_runtime / "current-review.json",
            "active": crash_active_path,
        }[stage].resolve()
        crashed = [False]

        def crash_after_json(path: Path, value: object) -> None:
            original_rebind_atomic_json(path, value)
            if path.resolve() == target and not crashed[0]:
                crashed[0] = True
                raise RebindCrash(stage)

        def crash_after_bytes(path: Path, value: bytes) -> None:
            original_rebind_atomic_bytes(path, value)
            if path.resolve() == target and not crashed[0]:
                crashed[0] = True
                raise RebindCrash(stage)

        plan_rebind._atomic_json = crash_after_json
        plan_rebind._atomic_bytes = crash_after_bytes
        try:
            plan_review.rebind_active_plan_review(
                worktree,
                crash_active_path,
                crash_candidate,
                gate_state,
                requested_policy,
                resolved,
                requested_base_sha=reviewed_head,
                requested_head_sha=resolved_head,
            )
        except RebindCrash:
            pass
        finally:
            plan_rebind._atomic_json = original_rebind_atomic_json
            plan_rebind._atomic_bytes = original_rebind_atomic_bytes
        try:
            replayed = plan_review.rebind_active_plan_review(
                worktree,
                crash_active_path,
                crash_candidate,
                gate_state,
                requested_policy,
                resolved,
                requested_base_sha=reviewed_head,
                requested_head_sha=resolved_head,
            )
            replay_boundary = json.loads(
                crash_boundary_path.read_text(encoding="utf-8")
            )
            replay_current = json.loads(
                (crash_runtime / "current-review.json").read_text(
                    encoding="utf-8"
                )
            )
            replay_active = json.loads(
                crash_active_path.read_text(encoding="utf-8")
            )
            transaction = json.loads(
                (crash_runtime / "plan-review-rebind.json").read_text(
                    encoding="utf-8"
                )
            )
            crash_replay[stage] = (
                crashed[0]
                and replay_boundary["design_sha256"]
                == resolved.artifact_sha256["design"]
                and replay_current == replayed
                and replay_active == replayed
                and transaction["stage"] == "complete"
            )
        except (OSError, ValueError, plan_review.PlanReviewError):
            crash_replay[stage] = False
    check(
        "retained plan rebind replays every authoritative publication crash",
        crash_replay
        == {"boundary": True, "current-review": True, "active": True},
        crash_replay,
    )

    mutations = {
        "outcome": plan_text(
            design="Implement the bounded facade and retain both lanes."
        ).replace("the exact behavior is established", "changed protected outcome"),
        "capability_dispositions": plan_text(
            design="Implement the bounded facade and retain both lanes."
        ).replace("plan facade | included", "plan facade | deferred"),
        "success_evidence": plan_text(
            design="Implement the bounded facade and retain both lanes."
        ).replace("E-plan | facade matrix", "E-plan | changed matrix"),
    }
    observed_changes: dict[str, tuple[str, ...]] = {}
    active_guards: dict[str, bool] = {}
    for name, text in mutations.items():
        mutated_path = plan_dir / f"protected-{name}.md"
        mutated_path.write_text(text, encoding="utf-8")
        mutated = plan_review.compile_plan_review(worktree, mutated_path)
        observed_changes[name] = plan_review.protected_artifact_changes(
            resolved, mutated
        )
        try:
            plan_review.guard_active_protected_artifacts(
                active_runtime,
                rebound,
                mutated,
            )
        except plan_review.PlanReviewError as exc:
            active_guards[name] = (
                exc.code == "plan-review-protected-artifact-changed"
                and "amendment and fresh boundary" in str(exc)
            )
        else:
            active_guards[name] = False
    check(
        "Outcome dispositions and evidence-map deltas remain protected",
        observed_changes
        == {
            "outcome": ("outcome",),
            "capability_dispositions": ("capability_dispositions",),
            "success_evidence": ("success_evidence",),
        },
        observed_changes,
    )
    check(
        "every active protected delta requires amendment plus fresh boundary",
        active_guards
        == {
            "outcome": True,
            "capability_dispositions": True,
            "success_evidence": True,
        },
        active_guards,
    )

print("\nPlan review facade RED/green matrix passed.")
