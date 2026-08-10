#!/usr/bin/env python3
"""Bounded callback/schema self-healing loop for rejected review inputs.

The live RC3 defect: a fresh cycle-2 review round required
``verification_iteration=0``, the compiled prompt never stated the exact
integer, the reviewer inferred ``1`` from the prior fix cycle, and
``review_submit`` rejected the callback with no recovery path.  This suite
proves the three batched repairs: the exact iteration is compiled into the
round prompt, a rejection writes one durable keyed receipt, and the reviewer
worker drives at most ``attempt_limit`` idempotent same-session corrections
while every dead/mismatched/changed/exhausted shape stays fail-closed and no
finalization cycle or verification iteration is consumed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.review_submit import (  # noqa: E402
    ReviewSubmitError,
    round_schema_lines,
)
from harness.runtime_worker_control import (  # noqa: E402
    RuntimeWorkerControlMixin,
)
from harness.store import OperationStore  # noqa: E402


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def check_round_prompt_states_exact_iteration() -> None:
    lines = round_schema_lines(verification_iteration=0)
    stated = [line for line in lines if "`verification_iteration` is exactly `0`" in line]
    default = round_schema_lines()
    check(
        "the round prompt states the exact authoritative iteration",
        len(stated) == 1
        and not any("`verification_iteration` is exactly" in line for line in default),
        (stated, default),
    )


def run_submit(state_dir: Path, worktree: Path, payload: object) -> int:
    input_path = state_dir / ".review-input.json"
    write_json(input_path, payload)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/harness/review_submit.py"),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(state_dir),
            "--input-file",
            str(input_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode


def check_rejection_receipts_are_keyed_and_idempotent(root: Path) -> None:
    state_dir = root / "callbacks" / "openai-holistic"
    state_dir.mkdir(parents=True)
    worktree = root / "submit-product"
    worktree.mkdir()
    write_json(
        state_dir / ".review-meta.json",
        {
            "schema_version": 1,
            "transport": "review-round",
            "operation_id": "round-op",
            "run_id": "round-run",
            "review_id": "review-op",
            "parent_session_operation_id": "parent-op",
            "review_mode": "simple",
            "axis": "openai-holistic",
            "verification_iteration": 0,
            "worktree": str(worktree),
            "head_sha": "a" * 40,
            "verification_profile": {"name": "scoped", "sha256": "5" * 64},
        },
    )
    invalid = {
        "schema_version": 1,
        "axis": "openai-holistic",
        "verdict": "approve",
        "verification_iteration": 1,
        "findings": [],
    }
    first = run_submit(state_dir, worktree, invalid)
    rejections = state_dir / ".review-submit-rejections"
    receipts = sorted(rejections.glob("*.json"))
    check(
        "an invalid iteration writes one typed keyed rejection receipt",
        first == 3 and len(receipts) == 1,
        (first, receipts),
    )
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    check(
        "the receipt carries actionable expected and actual values",
        receipt["error_code"] == "verification-iteration-mismatch"
        and receipt["expected"] == 0
        and receipt["actual"] == 1
        and receipt["attempt"] == 1
        and receipt["operation_id"] == "round-op",
        receipt,
    )
    duplicate = run_submit(state_dir, worktree, invalid)
    check(
        "resubmitting identical bytes reuses the receipt idempotently",
        duplicate == 3 and len(sorted(rejections.glob("*.json"))) == 1,
        duplicate,
    )
    second = dict(invalid)
    second["verification_iteration"] = 2
    run_submit(state_dir, worktree, second)
    names = [path.name for path in sorted(rejections.glob("*.json"))]
    check(
        "a different invalid input receives the next attempt number",
        len(names) == 2 and names[1].endswith("-a2.json"),
        names,
    )
    valid = dict(invalid)
    valid["verification_iteration"] = 0
    accepted = run_submit(state_dir, worktree, valid)
    check(
        "a corrected input publishes the callback and continues automatically",
        accepted == 0 and (state_dir / ".review-callback.json").is_file(),
        accepted,
    )


class RecordingCmux:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.keys: list[str] = []

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append(text)

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append(key)


class AliveProcess:
    def __init__(self, status: str = "alive") -> None:
        self._status = status

    def process_status(self, group: int, identity: str = "") -> str:
        return self._status


def correction_worker(
    root: Path,
    name: str,
    *,
    receipts: int = 1,
    process_status: str = "alive",
    head_drift: bool = False,
    identity_mismatch: bool = False,
) -> tuple[object, RecordingCmux, list, OperationStore, Path]:
    vault = root / f"vault-{name}"
    store = OperationStore(vault / ".vault-meta" / "harness")
    owner = "owner-1"
    op = f"{name}-review-parent"
    spec = OperationSpec(
        op,
        f"key-{op}",
        "simple-review-holistic",
        owner,
        RuntimeRoute("claude", "opus", "medium", "reviewer-callback", "4" * 64),
        "packets/review.json",
        "scoped",
    )
    store.create(spec, lane_id=f"lane-{name}", run_id=f"run-{name}")
    for step in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner, op, step)
    worktree = root / f"product-{name}"
    worktree.mkdir()
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "x@example.invalid"),
        ("config", "user.name", "X"),
    ):
        subprocess.run(
            ["git", "-C", str(worktree), *argv], check=True, capture_output=True
        )
    (worktree / "f.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "f.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "c"],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    state = root / f"state-{name}"
    callbacks = state / "callbacks" / "anthropic-holistic"
    callbacks.mkdir(parents=True)
    write_json(
        state / "callback-target.json",
        {
            "schema_version": 1,
            "generation": 1,
            "operation_id": "round-op",
            "run_id": "round-run",
            "callback_pointer": str(callbacks / ".review-callback.json"),
        },
    )
    write_json(
        callbacks / ".review-meta.json",
        {
            "schema_version": 1,
            "operation_id": "round-op" if not identity_mismatch else "foreign",
            "run_id": "round-run",
            "axis": "anthropic-holistic",
            "verification_iteration": 0,
            "worktree": str(worktree),
            "head_sha": ("b" * 40) if head_drift else head,
        },
    )
    for index in range(1, receipts + 1):
        write_json(
            callbacks / ".review-submit-rejections" / f"{'c' * 12}{index}-a{index}.json",
            {
                "schema_version": 1,
                "status": "rejected",
                "operation_id": "round-op",
                "run_id": "round-run",
                "axis": "anthropic-holistic",
                "input_sha256": ("d" * 63) + str(index),
                "attempt": index,
                "error_code": "verification-iteration-mismatch",
                "error": "review round iteration does not match metadata",
                "expected": 0,
                "actual": 1,
            },
        )
    attention: list = []
    cmux = RecordingCmux()

    class Worker(RuntimeWorkerControlMixin):
        def __init__(self) -> None:
            self.spec_path = state / "runtime.json"
            self.spec = {
                "operation_id": op,
                "owner_id": owner,
                "run_id": f"run-{name}",
                "cwd": state,
                "surface_id": "22222222-2222-2222-2222-222222222222",
                "callback_registration": state / "callback-target.json",
            }
            self.store = store
            self.operation = store.read(owner, op)
            self.handle = SimpleNamespace(
                process_group=os.getpid(), process_identity="e" * 64
            )
            self.process = AliveProcess(process_status)
            self.cmux_adapter = cmux
            self.trusted_vault = ROOT

        def summary_attention(self, status, reason=None, **_kw) -> None:
            attention.append((status, reason))

    return Worker(), cmux, attention, store, callbacks


def check_correction_loop(root: Path) -> None:
    worker, cmux, attention, store, callbacks = correction_worker(
        root, "correct"
    )
    ledger_dir = store.root / "finalization-ledger"
    worker.inspect_submit_rejections()
    record = store.read("owner-1", "correct-review-parent")
    check(
        "a live rejection drives one same-session correction",
        len(cmux.sent) == 1
        and cmux.keys == ["Enter"]
        and attention == []
        and "verification-iteration-mismatch" in cmux.sent[0]
        and "Expected 0" in cmux.sent[0]
        and "actual 1" in cmux.sent[0]
        and ".review-input.json" in cmux.sent[0]
        and "review_submit.py" in cmux.sent[0]
        and record.attempt == 1,
        (cmux.sent, attention, record.attempt),
    )
    check(
        "the correction never resends the review prompt or starts a cycle",
        "Return exactly one review-round JSON object" not in cmux.sent[0]
        and not ledger_dir.exists(),
        cmux.sent[0][:120],
    )
    worker.inspect_submit_rejections()
    check(
        "a duplicate rejection receipt is idempotent",
        len(cmux.sent) == 1
        and store.read("owner-1", "correct-review-parent").attempt == 1,
        (len(cmux.sent),),
    )
    meta = json.loads(
        (callbacks / ".review-meta.json").read_text(encoding="utf-8")
    )
    check(
        "corrections consume no verification iteration",
        meta["verification_iteration"] == 0,
        meta,
    )


def check_correction_fail_closed(root: Path) -> None:
    worker, cmux, attention, store, _ = correction_worker(
        root, "exhausted", receipts=3
    )
    worker.inspect_submit_rejections()
    check(
        "the third rejection exhausts the attempt budget fail-closed",
        cmux.sent == []
        and attention
        and attention[0][0] == "review-submit-rejections-exhausted"
        and attention[0][1] == AttentionReason.RETRY_EXHAUSTED,
        (cmux.sent, attention),
    )
    worker, cmux, attention, store, _ = correction_worker(
        root, "dead", process_status="dead"
    )
    worker.inspect_submit_rejections()
    check(
        "a dead reviewer session stays fail-closed",
        cmux.sent == []
        and attention
        and attention[0][0] == "review-submit-correction-session-dead",
        (cmux.sent, attention),
    )
    worker, cmux, attention, store, _ = correction_worker(
        root, "mismatch", identity_mismatch=True
    )
    worker.inspect_submit_rejections()
    check(
        "a mismatched reviewer identity stays fail-closed",
        cmux.sent == []
        and attention
        and attention[0][0] == "review-submit-correction-identity",
        (cmux.sent, attention),
    )
    worker, cmux, attention, store, _ = correction_worker(
        root, "drift", head_drift=True
    )
    worker.inspect_submit_rejections()
    check(
        "a changed HEAD stays fail-closed",
        cmux.sent == []
        and attention
        and attention[0][0] == "review-submit-correction-head-drift",
        (cmux.sent, attention),
    )


def check_launch_in_progress_classification(root: Path) -> None:
    from harness.runtime_worker_review_bridge import (
        RuntimeWorkerReviewBridgeMixin,
    )

    vault = root / "vault-prerace"
    store = OperationStore(vault / ".vault-meta" / "harness")
    owner = "owner-1"
    parent_op = "prerace-review-parent"
    spec = OperationSpec(
        parent_op,
        "key-prerace",
        "simple-review-holistic",
        owner,
        RuntimeRoute("claude", "opus", "medium", "reviewer-callback", "4" * 64),
        "packets/review.json",
        "scoped",
    )
    store.create(spec, lane_id="lane-prerace", run_id="run-prerace")
    store.transition(owner, parent_op, "preflight")
    store.transition(owner, parent_op, "starting")
    product = root / "product-prerace"
    product.mkdir()
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "x@example.invalid"),
        ("config", "user.name", "X"),
    ):
        subprocess.run(
            ["git", "-C", str(product), *argv], check=True, capture_output=True
        )
    (product / "f.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(product), "add", "f.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(product), "commit", "-m", "c"],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(product), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    gate_root = vault / ".vault-meta" / "harness" / "review-data" / "T" / "T"
    write_json(
        gate_root / "review-gate.json",
        {
            "schema_version": 1,
            "dispatch_operation_id": "prerace-task",
            "owner_id": owner,
            "status": "reviewing",
            "context": {"head_sha": head},
            "attempt": {
                "status": "awaiting-callback",
                "identity": {"exact_head_sha": head},
            },
            "lanes": [
                {
                    "operation_id": parent_op,
                    "run_id": "run-prerace",
                    "lane_id": "lane-prerace",
                    "verification_iteration": 0,
                }
            ],
        },
    )
    state = root / "state-prerace"
    state.mkdir()

    class Worker(RuntimeWorkerReviewBridgeMixin):
        def __init__(self) -> None:
            self.spec_path = state / "runtime.json"
            self.spec = {
                "operation_id": "prerace-task",
                "owner_id": owner,
                "cwd": product,
                "surface_id": "22222222-2222-2222-2222-222222222222",
            }
            self.store = store
            self.review = SimpleNamespace(gate_root=gate_root)

    worker = Worker()
    check(
        "a bound reviewer launch-in-progress is waiting without ready.json",
        worker._durable_review_in_progress() is True,
        "pre-ready launch window",
    )
    ready_path = (
        store.root / "owners" / owner / "runtime" / parent_op / "ready.json"
    )
    write_json(ready_path, {"schema_version": 1, "status": "failed"})
    check(
        "a failed early handshake inside the launch window stays fail-closed",
        worker._durable_review_in_progress() is False,
        "failed ready",
    )


with tempfile.TemporaryDirectory(prefix="review-submit-corrections.") as raw:
    root = Path(raw)
    check_round_prompt_states_exact_iteration()
    check_rejection_receipts_are_keyed_and_idempotent(root)
    check_correction_loop(root)
    check_correction_fail_closed(root)
    check_launch_in_progress_classification(root)

print("review submit correction tests passed")
