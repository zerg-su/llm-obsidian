#!/usr/bin/env python3
"""RC1 gate facade: production preflight over the three configured cells.

Regression tests for the accepted Sol High finding rc1-gate-unreachable:
one production contract/preflight facade must consume the exact three
configured RC1 cells, sequence them strictly under coordinator
authorization, bind runs to lifecycle_subject_sha256, emit
streak-consumable receipts, preserve the additive legacy four-cell
release path, and reject unknown/skipped/reordered/unbound cells.
Execution is exercised only through injected corridor drivers: these
tests never launch a live provider cell.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FACADE_PATH = ROOT / "scripts/live_acceptance_rc1_gate.py"
MANIFEST_PATH = ROOT / "config/acceptance-cells.toml"
CONFIG_PATH = ROOT / "config/v267-stabilization-subject.json"
sys.path.insert(0, str(ROOT / "scripts"))

import v267_stabilization as stab


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


def check_rejects(name: str, thunk) -> None:
    try:
        thunk()
    except stab.StabilizationError:
        print(f"OK   {name}")
    else:
        raise AssertionError(name)


def _load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
EVIDENCE_TMP = Path(tempfile.mkdtemp(prefix="rc1-facade-evidence-"))
for _args in (
    ("init", "-q"),
    ("config", "user.email", "test@example.com"),
    ("config", "user.name", "test"),
    ("commit", "--allow-empty", "-qm", "fix"),
):
    subprocess.run(["git", "-C", str(EVIDENCE_TMP), *_args], check=True)
FIX_HEAD = subprocess.run(
    ["git", "-C", str(EVIDENCE_TMP), "rev-parse", "HEAD"],
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()


def _evidence_file(relative: str, content: str) -> dict[str, str]:
    path = EVIDENCE_TMP / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}


def _typed_record(kind: str, cell_id: str, head: str, **extra: object) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "type": kind,
            "cell_id": cell_id,
            "head_sha": head,
            **extra,
        }
    )


def _material_artifacts(sequence: int, cell_id: str) -> dict[str, object]:
    prefix = f"docs/acceptance/evidence/v2.6.7/rc1-run-{sequence}"
    return {
        "findings_artifact": _evidence_file(
            f"{prefix}-findings.json",
            _typed_record("findings", cell_id, FIX_HEAD),
        ),
        "fix_head": FIX_HEAD,
        "refreshed_summary_artifact": _evidence_file(
            f"{prefix}-refreshed-summary.json",
            _typed_record("refreshed-summary", cell_id, FIX_HEAD),
        ),
        "second_verification_artifact": _evidence_file(
            f"{prefix}-verify-2.json",
            _typed_record("second-verification", cell_id, FIX_HEAD),
        ),
        "re_review_artifact": _evidence_file(
            f"{prefix}-re-review.json",
            _typed_record("re-review", cell_id, FIX_HEAD, verdict="approve"),
        ),
    }


def _fill_template(
    template: dict[str, object], sequence: int, *, material: bool = False
) -> dict[str, object]:
    """Complete a facade receipt template with run identities."""

    receipt = dict(template)
    receipt.update(
        {
            "run_id": f"rc1-run-{sequence}",
            "request_id": f"request-{sequence}",
            "owner_id": f"owner-{sequence}",
            "store_id": f"store-{sequence}",
            "worktree_id": f"worktree-{sequence}",
            "provider_session_ids": [
                f"executor-session-{sequence}",
                f"reviewer-session-{sequence}",
            ],
            "result": "success",
            "material_cycle": _material_artifacts(
                sequence, str(template["cell_id"])
            )
            if material
            else None,
            "resource_free": True,
            "coordinator_recovery": False,
        }
    )
    return receipt


# --- The facade exists as production code -----------------------------------

check(
    "the RC1 gate facade module exists in production scripts",
    FACADE_PATH.is_file(),
)
facade = _load_script("live_acceptance_rc1_gate", "scripts/live_acceptance_rc1_gate.py")
check("the facade exposes load_gate", hasattr(facade, "load_gate"))

gate = facade.load_gate(ROOT)
check(
    "the facade consumes the exact three configured RC1 cells",
    [cell.cell_id for cell in gate.cells]
    == [f"rc1-corridor-run-{index}" for index in (1, 2, 3)],
)

# --- Strict sequencing over streak-consumable receipts -----------------------

plan = gate.plan([], expected_digest=DIGEST_A, evidence_root=EVIDENCE_TMP)
check(
    "an empty receipt list plans the first configured cell",
    plan["next_cell"] == "rc1-corridor-run-1" and plan["complete"] is False,
)

template_1 = gate.receipt_template("rc1-corridor-run-1", expected_digest=DIGEST_A)
check(
    "receipt templates bind cell, corridor, digest, and routes",
    template_1["cell_id"] == "rc1-corridor-run-1"
    and template_1["sequence"] == 1
    and template_1["corridor"] == "engineering/change"
    and template_1["lifecycle_subject_sha256"] == DIGEST_A
    and template_1["executor_route"]
    == {"runtime": "claude", "model": "fable", "effort": "high"}
    and template_1["review_route"]
    == {"mode": "simple", "runtime": "claude", "model": "fable", "effort": "high"},
)

receipt_1 = _fill_template(template_1, 1)
check_rejects(
    "a schema-valid caller receipt cannot advance the gate",
    lambda: gate.plan(
        [receipt_1], expected_digest=DIGEST_A, evidence_root=EVIDENCE_TMP
    ),
)

receipt_2 = _fill_template(
    gate.receipt_template("rc1-corridor-run-2", expected_digest=DIGEST_A),
    2,
    material=True,
)
receipt_3 = _fill_template(
    gate.receipt_template("rc1-corridor-run-3", expected_digest=DIGEST_A), 3
)
check_rejects(
    "three self-authored receipts cannot complete the gate",
    lambda: gate.plan(
        [receipt_1, receipt_2, receipt_3],
        expected_digest=DIGEST_A,
        evidence_root=EVIDENCE_TMP,
    ),
)

check_rejects(
    "a skipped cell fails closed",
    lambda: gate.plan(
        [receipt_1, receipt_3], expected_digest=DIGEST_A, evidence_root=EVIDENCE_TMP
    ),
)
check_rejects(
    "reordered receipts fail closed",
    lambda: gate.plan(
        [receipt_2, receipt_1], expected_digest=DIGEST_A, evidence_root=EVIDENCE_TMP
    ),
)
check_rejects(
    "an unbound receipt fails closed",
    lambda: gate.plan(
        [
            {
                "schema_version": 1,
                "run_id": "junk",
                "sequence": 1,
                "lifecycle_subject_sha256": DIGEST_A,
                "request_id": "junk",
                "owner_id": "junk",
                "store_id": "junk",
                "worktree_id": "junk",
                "provider_session_ids": [],
                "result": "success",
                "material_finding_cycle": True,
                "resource_free": True,
                "coordinator_recovery": False,
            }
        ],
        expected_digest=DIGEST_A,
        evidence_root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "an unknown cell template request fails closed",
    lambda: gate.receipt_template("rc1-corridor-run-9", expected_digest=DIGEST_A),
)

# --- Coordinator authorization boundary --------------------------------------

check_rejects(
    "an unauthorized run authorization fails closed",
    lambda: gate.authorize("rc1-corridor-run-1", coordinator_authorized=False),
)
authorized = gate.authorize("rc1-corridor-run-1", coordinator_authorized=True)
check(
    "an authorized run yields the bound cell contract without launching",
    authorized["cell_id"] == "rc1-corridor-run-1"
    and authorized["corridor"] == "engineering/change",
)
check_rejects(
    "authorizing an unknown cell fails closed",
    lambda: gate.authorize("rc1-corridor-run-9", coordinator_authorized=True),
)

# --- The legacy four-cell release path stays additive -------------------------

release = _load_script("_release_acceptance_facade", "scripts/release-acceptance.py")
legacy = release.load_manifest(ROOT)
check(
    "the legacy release manifest still loads beside the facade",
    set(legacy.get("required_cells", []))
    == {
        "claude-lifecycle",
        "codex-lifecycle",
        "cross-runtime-composition",
        "deep-review",
    },
)
runner = _load_script("_live_acceptance_runner_facade", "scripts/live-acceptance-runner.py")
check(
    "the legacy live runner cell identities are unchanged",
    runner.CELL_IDS
    == (
        "claude-lifecycle",
        "codex-lifecycle",
        "cross-runtime-composition",
        "deep-review",
    ),
)

# --- Production execution path: reserve, launch, record ----------------------

STATE_DIR = Path(tempfile.mkdtemp(prefix="rc1-facade-state-"))
STATE_PATH = STATE_DIR / "rc1-streak-state.json"
LAUNCHES: list[str] = []


def _spec_file(name: str, request_id: str) -> Path:
    spec = STATE_DIR / name
    spec.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "pipeline": "engineering/change",
                "executor": {"runtime": "claude", "model": "fable", "effort": "high"},
                "review": {
                    "mode": "simple",
                    "runtime": "claude",
                    "model": "fable",
                    "effort": "high",
                },
            }
        ),
        encoding="utf-8",
    )
    return spec


def _fake_launcher(
    root: Path, contract: dict, *, timeout: int, spec_path: Path, **_: object
) -> dict:
    LAUNCHES.append(contract["cell_id"])
    request_id = json.loads(spec_path.read_text(encoding="utf-8"))["request_id"]
    return {
        "schema_version": 1,
        "status": "launched",
        "request_id": request_id,
        "worktree": str(EVIDENCE_TMP),
        "harness": {
            "owner_id": request_id,
            "operation_id": request_id,
            "lane_id": f"lane-{request_id}",
            "run_id": f"run-{request_id}",
        },
    }


def _failing_launcher(root: Path, contract: dict, *, timeout: int, **_: object) -> dict:
    LAUNCHES.append(contract["cell_id"])
    raise RuntimeError("corridor crashed after reservation")


check_rejects(
    "an unauthorized run is contained before any launch or state effect",
    lambda: gate.reserve_and_launch(
        coordinator_authorized=False,
        expected_digest=DIGEST_A,
        state_path=STATE_PATH,
        launcher=_fake_launcher,
        spec_path=_spec_file("spec-unauthorized.json", "request-x"),
        evidence_root=EVIDENCE_TMP,
    ),
)
check(
    "containment invoked no launcher and wrote no state",
    LAUNCHES == [] and not STATE_PATH.exists(),
)

spec = _spec_file("spec-1.json", "request-11")
report = gate.reserve_and_launch(
    coordinator_authorized=True,
    expected_digest=DIGEST_A,
    state_path=STATE_PATH,
    launcher=_fake_launcher,
    spec_path=spec,
    evidence_root=EVIDENCE_TMP,
)
check(
    "the next cell preserves the structured dispatch identity",
    report["cell_id"] == "rc1-corridor-run-1"
    and report["status"] == "launched"
    and report["launch"]["harness"]["operation_id"] == "request-11",
)
fresh = _fill_template({**report["receipt_template"]}, 11)
fresh.update(
    {
        "request_id": "request-11",
        "owner_id": report["launch"]["harness"]["owner_id"],
        "run_id": report["launch"]["harness"]["run_id"],
        "worktree_id": report["launch"]["worktree"],
    }
)
unaccepted_state = STATE_PATH.read_bytes()
check_rejects(
    "a launched identity cannot mint a receipt without durable acceptance",
    lambda: gate.record_receipt(
        fresh,
        expected_digest=DIGEST_A,
        state_path=STATE_PATH,
        evidence_root=EVIDENCE_TMP,
    ),
)
check(
    "rejected self-authored receipt leaves the launched reservation unchanged",
    STATE_PATH.read_bytes() == unaccepted_state and LAUNCHES == ["rc1-corridor-run-1"],
)

# Restart safety: a crash after reservation resumes only the identical
# dispatch identity; a competing spec is rejected.
RESTART_STATE = STATE_DIR / "rc1-restart-state.json"
LAUNCHES.clear()
RESTART_SPEC = _spec_file("spec-restart.json", "request-21")
try:
    gate.reserve_and_launch(
        coordinator_authorized=True,
        expected_digest=DIGEST_A,
        state_path=RESTART_STATE,
        launcher=_failing_launcher,
        spec_path=RESTART_SPEC,
        evidence_root=EVIDENCE_TMP,
    )
except RuntimeError:
    print("OK   a crashed launch leaves the durable reservation behind")
else:
    raise AssertionError("a crashed launch leaves the durable reservation behind")
check_rejects(
    "a failed launch cannot be recorded as a successful cell",
    lambda: gate.record_receipt(
        _fill_template(
            {**gate.receipt_template("rc1-corridor-run-1", expected_digest=DIGEST_A)},
            21,
        ),
        expected_digest=DIGEST_A,
        state_path=RESTART_STATE,
        evidence_root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "a competing dispatch identity cannot resume the reserved cell",
    lambda: gate.reserve_and_launch(
        coordinator_authorized=True,
        expected_digest=DIGEST_A,
        state_path=RESTART_STATE,
        launcher=_fake_launcher,
        spec_path=_spec_file("spec-competing.json", "request-99"),
        evidence_root=EVIDENCE_TMP,
    ),
)
resumed = gate.reserve_and_launch(
    coordinator_authorized=True,
    expected_digest=DIGEST_A,
    state_path=RESTART_STATE,
    launcher=_fake_launcher,
    spec_path=RESTART_SPEC,
    evidence_root=EVIDENCE_TMP,
)
check(
    "a restarted run resumes the reserved cell with the identical identity",
    resumed["cell_id"] == "rc1-corridor-run-1"
    and LAUNCHES == ["rc1-corridor-run-1", "rc1-corridor-run-1"],
)
check_rejects(
    "a launched, unrecorded cell refuses any further run",
    lambda: gate.reserve_and_launch(
        coordinator_authorized=True,
        expected_digest=DIGEST_A,
        state_path=RESTART_STATE,
        launcher=_fake_launcher,
        spec_path=RESTART_SPEC,
        evidence_root=EVIDENCE_TMP,
    ),
)
drift_state = RESTART_STATE.read_bytes()
drift_launches = list(LAUNCHES)
check_rejects(
    "caller digest drift cannot replace or relaunch an active reservation",
    lambda: gate.reserve_and_launch(
        coordinator_authorized=True,
        expected_digest=DIGEST_B,
        state_path=RESTART_STATE,
        launcher=_fake_launcher,
        spec_path=RESTART_SPEC,
        evidence_root=EVIDENCE_TMP,
    ),
)
check(
    "rejected digest drift leaves reservation bytes and launch count unchanged",
    RESTART_STATE.read_bytes() == drift_state and LAUNCHES == drift_launches,
)

# Receipt authority: fabricated or tampered receipts are rejected and
# nothing persists.
foreign = _fill_template(
    {**gate.receipt_template("rc1-corridor-run-1", expected_digest=DIGEST_A)},
    31,
)
check_rejects(
    "a receipt with a foreign request identity is rejected",
    lambda: gate.record_receipt(
        foreign,
        expected_digest=DIGEST_A,
        state_path=RESTART_STATE,
        evidence_root=EVIDENCE_TMP,
    ),
)
tampered = _fill_template(
    {**gate.receipt_template("rc1-corridor-run-1", expected_digest=DIGEST_A)},
    32,
)
tampered["request_id"] = "request-21"
tampered["executor_route"] = {"runtime": "claude", "model": "sonnet", "effort": "low"}
check_rejects(
    "a tampered receipt is rejected by the streak authority",
    lambda: gate.record_receipt(
        tampered,
        expected_digest=DIGEST_A,
        state_path=RESTART_STATE,
        evidence_root=EVIDENCE_TMP,
    ),
)
state_after = json.loads(RESTART_STATE.read_text(encoding="utf-8"))
check(
    "the rejected receipts left the durable state untouched",
    state_after["receipts"] == [] and state_after["reservation"] is not None,
)

# Concurrency: the claim is linearizable, so two callers with two
# route-compatible specs cannot both launch the same cell.
import threading

RACE_STATE = STATE_DIR / "rc1-race-state.json"
race_launches: list[str] = []
race_errors: list[str] = []
race_barrier = threading.Barrier(2, timeout=10)


def _race_launcher(root: Path, contract: dict, *, timeout: int, **options: object) -> dict:
    spec_path = Path(str(options["spec_path"]))
    race_launches.append(str(spec_path))
    request_id = json.loads(spec_path.read_text(encoding="utf-8"))["request_id"]
    return {
        "schema_version": 1,
        "status": "launched",
        "request_id": request_id,
        "worktree": str(EVIDENCE_TMP),
        "harness": {
            "owner_id": request_id,
            "operation_id": request_id,
            "lane_id": f"lane-{request_id}",
            "run_id": f"run-{request_id}",
        },
    }


def _race(spec_name: str, request_id: str) -> None:
    spec = _spec_file(spec_name, request_id)
    race_barrier.wait()
    try:
        gate.reserve_and_launch(
            coordinator_authorized=True,
            expected_digest=DIGEST_A,
            state_path=RACE_STATE,
            launcher=_race_launcher,
            spec_path=spec,
            evidence_root=EVIDENCE_TMP,
        )
    except stab.StabilizationError as exc:
        race_errors.append(str(exc))


threads = [
    threading.Thread(target=_race, args=("spec-race-a.json", "request-41")),
    threading.Thread(target=_race, args=("spec-race-b.json", "request-42")),
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
race_state = json.loads(RACE_STATE.read_text(encoding="utf-8"))
check(
    "two concurrent callers produce exactly one launch and one reservation",
    len(race_launches) == 1
    and len(race_errors) == 1
    and race_state["reservation"] is not None
    and race_state["reservation"]["status"] == "launched",
)

# The production dispatch driver binds the spec to the reserved cell before
# any launch effect.
SPEC_PATH = STATE_DIR / "dispatch-spec.json"
SPEC_PATH.write_text(
    json.dumps(
        {
            "request_id": "request-51",
            "pipeline": "research/deep",
            "executor": {"runtime": "claude", "model": "fable", "effort": "high"},
            "review": {
                "mode": "simple",
                "runtime": "claude",
                "model": "fable",
                "effort": "high",
            },
        }
    ),
    encoding="utf-8",
)
contract_1 = gate.authorize("rc1-corridor-run-1", coordinator_authorized=True)
check_rejects(
    "the dispatch driver rejects a spec whose pipeline drifts from the corridor",
    lambda: facade.dispatch_corridor_driver(
        ROOT, contract_1, timeout=5, spec_path=SPEC_PATH, approval_token="t"
    ),
)
SPEC_PATH.write_text(
    json.dumps(
        {
            "request_id": "request-52",
            "pipeline": "engineering/change",
            "executor": {"runtime": "codex", "model": "gpt", "effort": "low"},
            "review": {
                "mode": "simple",
                "runtime": "claude",
                "model": "fable",
                "effort": "high",
            },
        }
    ),
    encoding="utf-8",
)
check_rejects(
    "the dispatch driver rejects a spec whose executor route drifts",
    lambda: facade.dispatch_corridor_driver(
        ROOT, contract_1, timeout=5, spec_path=SPEC_PATH, approval_token="t"
    ),
)

# The CLI help no longer claims the command is effect-free.
help_result = subprocess.run(
    [sys.executable, str(FACADE_PATH), "--help"],
    text=True,
    capture_output=True,
    check=False,
)
check(
    "the CLI help distinguishes read-only preflight from effectful run/record",
    help_result.returncode == 0
    and "never launches a provider cell" not in help_result.stdout
    and "read-only" in help_result.stdout
    and "coordinator-authorized" in help_result.stdout,
)

# --- Preflight CLI ------------------------------------------------------------

result = subprocess.run(
    [sys.executable, str(FACADE_PATH), "preflight", "--json"],
    text=True,
    capture_output=True,
    check=False,
)
check("the facade preflight CLI exits 0", result.returncode == 0)
payload = json.loads(result.stdout)
check(
    "the preflight CLI reports the three cells and the next cell",
    [cell["cell_id"] for cell in payload["cells"]]
    == [f"rc1-corridor-run-{index}" for index in (1, 2, 3)]
    and payload["next_cell"] == "rc1-corridor-run-1"
    and payload["corridor"] == "engineering/change",
)

print("rc1 gate facade regression tests passed")
