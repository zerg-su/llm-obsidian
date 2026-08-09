"""Production corridor constructors with fake provider/process/cmux/clock ports.

The world builds a real vault/worktree/OperationStore/task-meta fixture for the
supported ``engineering/change`` corridor and starts the production runtime
worker against it.  Model turns are world actions (files written by the test),
and the only fake seams are the provider stub process, the cmux transcript
recorder, the review provider port, and the injected verification runner.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import (  # noqa: E402
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.pipeline_builtins import (  # noqa: E402
    builtin_definitions,
    builtin_registry,
)
from harness.pipelines import compile_pipeline  # noqa: E402
from harness.adapters.process import ProcessAdapter  # noqa: E402
from harness.runtime_worker import run as run_worker  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import review_round_payload  # noqa: E402
from harness.workflows.review_results import (  # noqa: E402
    ReviewFinding,
    ReviewResult,
)
from outcome_contract import extract_from_bytes  # noqa: E402
from review_contract import VERIFY_BUDGETS  # noqa: E402


PROJECT = "cccc0267-0000-4000-8000-000000000000"
ORIGIN_SURFACE = "00000000-0000-4000-8000-00000000c0de"
TASK_SURFACE = "00000000-0000-4000-8000-00000000beef"
COORDINATOR_SESSION = "coordinator-session"

_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "corridor_task_review_runner", ROOT / "scripts" / "task-review-runner.py"
)
assert _RUNNER_SPEC and _RUNNER_SPEC.loader
_RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(_RUNNER)
run_task_review = _RUNNER.run_task_review

PROVIDER_STUB = """#!/usr/bin/env python3
import json, os, pathlib, sys, time
summary = pathlib.Path(sys.argv[1])
payload = sys.argv[2]
if not summary.exists():
    temporary = summary.with_name(f".{summary.name}.stub")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, summary)
time.sleep(0.3)
"""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class SimulatedWorkerCrash(RuntimeError):
    """The volatile worker process stopped at one armed durable boundary."""


class CrashPlan:
    """Arm one seam occurrence; consuming it kills the worker generation."""

    def __init__(self) -> None:
        self.seam = ""
        self.occurrence = 0
        self.observed: dict[str, int] = {}

    def arm(self, seam: str, *, occurrence: int = 1) -> None:
        if self.seam:
            raise RuntimeError("a corridor failpoint is already armed")
        if not seam or occurrence < 1:
            raise ValueError("corridor failpoint identity is invalid")
        self.seam = seam
        self.occurrence = occurrence

    def observe(self, seam: str) -> None:
        count = self.observed.get(seam, 0) + 1
        self.observed[seam] = count
        if seam == self.seam and count == self.occurrence:
            self.seam = ""
            self.occurrence = 0
            raise SimulatedWorkerCrash(seam)

    def assert_consumed(self) -> None:
        if self.seam:
            raise AssertionError(f"unconsumed corridor failpoint: {self.seam}")


class TranscriptCmux:
    """Fake cmux port: records worker wake transport instead of sending it."""

    def __init__(self, crashes: CrashPlan | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []
        self.crashes = crashes

    def _observe(self, text: str) -> None:
        if self.crashes is None:
            return
        if "Typed review findings are ready" in text:
            self.crashes.observe("findings-notification-send")
        elif "Refresh .task-summary.json" in text:
            self.crashes.observe("refresh-notification-send")
        elif "reap-runner.py" in text:
            self.crashes.observe("reap-notification-send")

    def send(self, surface_id: str, text: str) -> None:
        self._observe(text)
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))


@dataclass(frozen=True)
class FakeReviewSessionResult:
    record: object
    checkpoint: str
    checkpoint_sha256: str = ""


class CorridorReviewRuntime:
    """Deterministic review provider port; the real gate owns all transitions."""

    def __init__(
        self,
        store: OperationStore,
        owner_id: str,
        crashes: CrashPlan | None = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.crashes = crashes
        self.started = 0

    def start(self, request: object, *, on_surface_opened=None):
        if self.crashes is not None:
            self.crashes.observe("review-session-start")
        self.started += 1
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        record = replace(
            record,
            resources=OwnedResources(surface_id=TASK_SURFACE),
            revision=record.revision + 1,
        )
        self.store.save(record, expected_revision=record.revision - 1)
        result = FakeReviewSessionResult(record, "checkpoint-corridor")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def status(self, owner_id: str, operation_id: str):
        return FakeReviewSessionResult(
            self.store.read(owner_id, operation_id), "checkpoint-corridor"
        )

    def hydrate_durable_checkpoint(
        self, owner_id: str, operation_id: str, _lane_id: str
    ):
        return FakeReviewSessionResult(
            self.store.read(owner_id, operation_id),
            "checkpoint-corridor",
            checkpoint_sha256=hashlib.sha256(b"checkpoint-corridor").hexdigest(),
        )

    def register_callback_target(self, *_args: object) -> None:
        return None

    def accept_callback(self, envelope: object) -> object:
        if self.crashes is not None:
            self.crashes.observe("review-callback-acceptance:before")
        acceptance = CallbackBroker(self.store, self.owner_id).accept(envelope)
        if self.crashes is not None:
            self.crashes.observe("review-callback-acceptance")
        return acceptance

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state in {"complete", "failed", "cancelled"}:
            return record
        if record.state in {"created", "preflight", "starting", "attention-required"}:
            self.store.transition(owner_id, operation_id, "cancelling")
        elif record.state != "finalizing":
            self.store.transition(owner_id, operation_id, "finalizing")
        self.store.transition(owner_id, operation_id, "exiting")
        return self.store.read(owner_id, operation_id)

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state == "exiting":
            self.store.transition(owner_id, operation_id, "complete")
        completed = self.store.read(owner_id, operation_id)
        if completed.resources != OwnedResources():
            updated = replace(
                completed,
                resources=OwnedResources(),
                revision=completed.revision + 1,
            )
            self.store.save(updated, expected_revision=completed.revision)
            completed = updated
        return completed


def executor_summary(body: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "type": "session",
        "title": "Corridor Result",
        "session": "executor-session",
        "body": body,
        "outcome_disposition": "achieved",
        "outcome_evidence_ids": ["corridor-green"],
        "residual_gap_pointers": [],
    }


@dataclass
class CorridorWorld:
    root: Path
    vault: Path
    worktree: Path
    store: OperationStore
    task_id: str
    owner_id: str
    profile_sha: str
    meta: dict[str, object]
    spec_path: Path
    state_root: Path
    summary_path: Path
    cmux: TranscriptCmux
    review_runtime: CorridorReviewRuntime
    crashes: CrashPlan = field(default_factory=CrashPlan)
    worker_generations: int = 0
    worker_faults: list[BaseException] = field(default_factory=list)
    _worker_thread: threading.Thread | None = None

    def worker_alive(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def gate_root(self) -> Path:
        return (
            self.store.root / "review-data" / self.task_id / self.task_id
        )

    @property
    def review_runtime_root(self) -> Path:
        return self.store.root / "review-runtime" / self.task_id

    def record(self):
        return self.store.read(self.owner_id, self.task_id)

    def gate_state(self) -> dict[str, object]:
        path = self.gate_root / "review-gate.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def head(self) -> str:
        return git(self.worktree, "rev-parse", "HEAD")

    def publish_summary(self, body: str) -> dict[str, object]:
        """World action standing in for the executor model's summary turn."""

        summary = executor_summary(body)
        write_json(self.summary_path, summary)
        return summary

    def active_round(self, *, verification_iteration: int = 0) -> dict[str, object]:
        state = self.gate_state()
        lanes = [
            lane
            for lane in state.get("lanes", [])
            if lane.get("verification_iteration") == verification_iteration
        ]
        if len(lanes) != 1:
            raise AssertionError(
                f"expected one review lane at iteration {verification_iteration}: "
                f"{state.get('lanes')}"
            )
        lane = lanes[0]
        rounds = [
            record
            for record in self.store.list(self.task_id)
            if record.spec.kind == "review-round"
            and record.spec.parent_operation_id == lane["operation_id"]
        ]
        if len(rounds) != 1:
            raise AssertionError(
                f"expected one review round for lane {lane['operation_id']}"
            )
        return {
            "axis": lane["axis"],
            "operation_id": rounds[0].spec.operation_id,
            "run_id": rounds[0].run_id,
            "parent_operation_id": lane["operation_id"],
        }

    def publish_review_callback(
        self,
        *,
        verdict: str,
        findings: tuple[dict[str, object], ...] = (),
        verification_iteration: int = 0,
    ) -> dict[str, object]:
        """World action standing in for the reviewer model's callback turn."""

        round_state = self.active_round(
            verification_iteration=verification_iteration
        )
        axis = str(round_state["axis"])
        result = ReviewResult(
            axis,
            verdict,
            tuple(
                ReviewFinding(
                    finding_id=str(finding["finding_id"]),
                    axis=axis,
                    severity=str(finding["severity"]),
                    summary=str(finding["summary"]),
                    evidence=str(finding["evidence"]),
                    file=str(finding.get("file") or ""),
                    line=finding.get("line"),
                    recommendation=str(finding.get("recommendation") or ""),
                )
                for finding in findings
            ),
            verification_iteration,
        )
        payload = review_round_payload(
            str(round_state["parent_operation_id"]), result
        )
        digest = canonical_sha256(payload)
        envelope = {
            "schema_version": 1,
            "callback_id": f"review-{digest[:24]}",
            "operation_id": round_state["operation_id"],
            "run_id": round_state["run_id"],
            "kind": "review",
            "payload": payload,
            "payload_sha256": digest,
        }
        write_json(
            self.review_runtime_root
            / "callbacks"
            / axis
            / ".review-callback.json",
            envelope,
        )
        return envelope

    def resolve_findings(self, *, commit_message: str) -> dict[str, object]:
        """World action standing in for the executor's fix + resolution turn."""

        packet_path = self.worktree / ".task-review.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        (self.worktree / "resolution.txt").write_text(
            f"{commit_message}\n", encoding="utf-8"
        )
        git(self.worktree, "add", "resolution.txt")
        git(self.worktree, "commit", "-m", commit_message)
        resolution = {
            "schema_version": 1,
            "operation_id": self.task_id,
            "review_identity_sha256": packet["review_identity_sha256"],
            "reviewed_head_sha": packet["reviewed_head_sha"],
            "resolved_head_sha": self.head(),
            "resolutions": [
                {
                    "finding_id": finding_id,
                    "disposition": "applied",
                    "rationale": "The committed correction is on the resolved HEAD.",
                    "follow_up": "",
                }
                for finding_id in packet["material_finding_ids"]
            ],
        }
        write_json(self.worktree / ".task-review-resolution.json", resolution)
        return resolution

    def run_worker_generation(
        self,
        *,
        verification_runner: Callable[..., object],
        review_launcher: Callable[[Path, Path], None] | None = None,
        during: Callable[["CorridorWorld"], None] | None = None,
        timeout: float = 60.0,
    ) -> int | None:
        """Run one production worker process lifetime against the fixture."""

        self.worker_generations += 1
        launcher = review_launcher or (
            lambda _vault, worktree: run_task_review(
                Path(worktree), runtime_manager=self.review_runtime
            )
        )
        result: list[int] = []
        faults: list[BaseException] = []

        def target() -> None:
            try:
                result.append(
                    run_worker(
                        self.spec_path,
                        poll_seconds=0.02,
                        checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
                        cmux_adapter=self.cmux,
                        review_launcher=launcher,
                        verification_runner=verification_runner,
                    )
                )
            except BaseException as exc:  # crash boundary evidence
                faults.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        self._worker_thread = thread
        thread.start()
        try:
            if during is not None:
                during(self)
        finally:
            thread.join(timeout=timeout)
            self._worker_thread = None
        self.worker_faults.extend(faults)
        if thread.is_alive():
            return None
        if faults:
            raise faults[0]
        return result[0] if result else None

    def await_condition(
        self,
        label: str,
        condition: Callable[[], bool],
        *,
        timeout: float = 30.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError(f"corridor condition timed out: {label}")


def passing_verification_runner(
    calls: list[tuple[str, ...]],
    crashes: CrashPlan | None = None,
):
    def runner(argv: list[str], **kwargs: object):
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        if crashes is not None:
            crashes.observe("verification-command")
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    return runner


MATERIAL_CORRIDOR_FINDING = {
    "finding_id": "F-corridor-material",
    "severity": "important",
    "file": "product.txt",
    "line": 1,
    "summary": "Material corridor finding",
    "evidence": "The original content is incomplete.",
    "recommendation": "Commit the exact correction.",
}
REFRESHED_SUMMARY_BODY = (
    "The corridor evidence is established.\n\n"
    "Resolved the material review finding at the final HEAD."
)


def corridor_autopilot(
    world: CorridorWorld,
    *,
    initial_head: str,
    timeout: float = 60.0,
) -> None:
    """Perform the next pending model turn from durable state only.

    The autopilot is restart-safe: it derives every decision from the same
    durable artifacts a real executor or reviewer session would read, so a
    crashed worker generation can be resumed by running it again.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not world.worker_alive():
            return
        record = world.record()
        # attention-required is not terminal here: a restarted worker may
        # recover it from its durable latch; a latched worker exits on its
        # own and ends the loop through worker_alive instead.
        if record.accepted_callback_kind == "wiki-summary" or record.state in {
            "complete",
            "failed",
            "cancelled",
        }:
            return
        state = world.gate_state()
        attempt = state.get("attempt") if isinstance(state, dict) else None
        packet_path = world.worktree / ".task-review.json"
        resolution_path = world.worktree / ".task-review-resolution.json"
        refresh_marker = world.state_root / "pipeline-summary-refresh-notify.json"
        resolution_pending = False
        if packet_path.is_file():
            if not resolution_path.is_file():
                resolution_pending = True
            else:
                # The worker materializes an unresolved template beside the
                # findings packet; only a committed resolved HEAD counts as
                # the executor's completed fix turn.
                resolution = json.loads(
                    resolution_path.read_text(encoding="utf-8")
                )
                resolved = str(resolution.get("resolved_head_sha") or "")
                resolution_pending = not resolved or resolved == str(
                    resolution.get("reviewed_head_sha") or ""
                )
        if resolution_pending:
            world.resolve_findings(commit_message="resolve corridor finding")
        elif (
            refresh_marker.is_file()
            and json.loads(world.summary_path.read_text(encoding="utf-8")).get(
                "body"
            )
            != REFRESHED_SUMMARY_BODY
        ):
            world.publish_summary(REFRESHED_SUMMARY_BODY)
        elif (
            isinstance(attempt, dict)
            and attempt.get("status") == "awaiting-callback"
            and state.get("status") == "reviewing"
        ):
            identity = attempt.get("identity") or {}
            reviewed_head = str(identity.get("exact_head_sha") or "")
            axis_lanes = state.get("lanes", [])
            live_callback = any(
                (
                    world.review_runtime_root
                    / "callbacks"
                    / str(lane.get("axis"))
                    / ".review-callback.json"
                ).is_file()
                for lane in axis_lanes
            )
            if not live_callback:
                if reviewed_head == initial_head:
                    world.publish_review_callback(
                        verdict="changes-requested",
                        findings=(MATERIAL_CORRIDOR_FINDING,),
                        verification_iteration=0,
                    )
                else:
                    world.publish_review_callback(
                        verdict="approve",
                        findings=(),
                        verification_iteration=0,
                    )
        time.sleep(0.02)
    raise AssertionError("corridor autopilot timed out")


def build_corridor_world(
    root: Path,
    task_id: str,
    *,
    shared_vault: Path | None = None,
    owner_id: str = "owner-1",
    executor_runtime: str = "codex",
    executor_model: str = "gpt-5.6-sol",
    executor_effort: str = "high",
    review_runtime: str = "codex",
    review_model: str = "sol",
    review_effort: str = "high",
) -> CorridorWorld:
    vault = shared_vault if shared_vault is not None else root / f"vault-{task_id}"
    worktree = root / f"worktree-{task_id}"
    (vault / "wiki" / "plans").mkdir(parents=True, exist_ok=True)
    (vault / "scripts").mkdir(exist_ok=True)
    (vault / "config").mkdir(exist_ok=True)
    for name in ("verification-profiles.toml", "model-routing.toml"):
        shutil.copy2(ROOT / "config" / name, vault / "config" / name)
    (vault / "skills" / "review").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / "skills" / "review" / "SKILL.md",
        vault / "skills" / "review" / "SKILL.md",
    )
    (vault / "scripts" / "reap-runner.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "corridor@example.invalid")
    git(worktree, "config", "user.name", "Corridor World")
    (worktree / "product.txt").write_text("ready\n", encoding="utf-8")
    git(worktree, "add", "product.txt")
    git(worktree, "commit", "-m", "ready")

    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text(
        "# Approved\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Complete the corridor fixture.",'
        '"success_evidence":[{"evidence_id":"corridor-green",'
        '"observable":"The corridor accepts the exact typed summary."}],'
        '"non_goals":["No authority expansion."]}\n```\n',
        encoding="utf-8",
    )
    write_json(
        vault
        / ".vault-meta"
        / "task-sessions"
        / "session-bindings"
        / COORDINATOR_SESSION
        / "binding.json",
        {
            "session_id": COORDINATOR_SESSION,
            "project_id": PROJECT,
            "task_id": task_id,
        },
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    pipeline = compile_pipeline(
        builtin_definitions()["engineering/change"],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    store.create(
        OperationSpec(
            task_id,
            f"key-{task_id}",
            "dispatch",
            owner_id,
            RuntimeRoute(
                executor_runtime,
                executor_model,
                executor_effort,
                "executor",
                "a" * 64,
            ),
            "packets/task.json",
            "scoped",
            contract_sha256=pipeline.definition_sha256,
        ),
        lane_id="lane-1",
        run_id=f"run-{task_id}",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner_id, task_id, state)

    profile_sha = load_profiles(
        vault / "config" / "verification-profiles.toml"
    )["scoped"].sha256
    meta: dict[str, object] = {
        "version": 4,
        "project_id": PROJECT,
        "task_id": task_id,
        "task_name": "Corridor engineering change",
        "origin_session": COORDINATOR_SESSION,
        "executor_runtime": executor_runtime,
        "interaction_policy": "unattended",
        "pipeline_policy": {
            "name": "engineering/change",
            "definition_sha256": pipeline.definition_sha256,
            "completion_policy": "attention",
            "total_pass_limit": 2,
        },
        "routing": {
            "session": {
                "runtime": executor_runtime,
                "model": executor_model,
                "effort": executor_effort,
            }
        },
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "outcome_contract_sha256": extract_from_bytes(plan.read_bytes()).sha256,
        "vault_root": str(vault),
        "review_policy": {
            "mode": "simple",
            "cross_model": False,
            "runtime": review_runtime,
            "model": review_model,
            "effort": review_effort,
            "max_verify_iterations": VERIFY_BUDGETS["simple"],
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
        },
        "finalization_policy": {
            "max_cycles": 5,
            "add_independent_model_after": 3,
            "execution": "ephemeral",
            "primary_route_alias": "finalization-primary",
            "independent_route_alias": "finalization-independent",
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "Corridor Result",
        },
        "surface_policy": {"auto_close": True, "placement": "split"},
        "watchdog_policy": {
            "enabled": True,
            "poll_seconds": 30,
            "warn_after_seconds": 900,
            "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
        "task_surface": TASK_SURFACE,
        "worktree": str(worktree),
    }
    write_json(worktree / ".task-meta.json", meta)
    (worktree / ".task-prompt.md").write_text(
        "Complete the approved corridor task and write the canonical summary.",
        encoding="utf-8",
    )
    provider = root / f"provider-{task_id}.py"
    provider.write_text(PROVIDER_STUB, encoding="utf-8")
    summary_path = worktree / ".task-summary.json"
    initial_summary = executor_summary(
        "The declared corridor evidence is established."
    )
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(summary_path),
            json.dumps(initial_summary, sort_keys=True) + "\n",
        ),
        cwd=worktree,
        state_root=root / f"state-{task_id}",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=summary_path,
        store_root=store.root,
        owner_id=owner_id,
        operation_id=task_id,
        run_id=f"run-{task_id}",
        surface_id=TASK_SURFACE,
        runtime=executor_runtime,
        callback_mode="task-summary",
        task_summary_pointer=summary_path,
        origin_surface=ORIGIN_SURFACE,
    )
    crashes = CrashPlan()
    return CorridorWorld(
        root=root,
        vault=vault,
        worktree=worktree,
        store=store,
        task_id=task_id,
        owner_id=owner_id,
        profile_sha=profile_sha,
        meta=meta,
        spec_path=launch.spec_path,
        state_root=launch.spec_path.parent,
        summary_path=summary_path,
        cmux=TranscriptCmux(crashes),
        review_runtime=CorridorReviewRuntime(store, task_id, crashes),
        crashes=crashes,
    )
