#!/usr/bin/env python3
"""RC1 streak binding: bound receipts, artifact-derived material cycles.

Regression tests for the accepted Sol High findings
rc1-corridor-trace-incomplete and rc1-streak-accepts-unbound-receipts:
the declared corridor trace must represent the complete material-cycle
branch, material-cycle evidence must derive from durable artifacts, and
streak receipts must bind to the exact configured cell, sequence,
corridor, and resolved executor/reviewer routes with non-empty provider
session identities.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/v267_stabilization.py"
CONFIG_PATH = ROOT / "config/v267-stabilization-subject.json"
MANIFEST_PATH = ROOT / "config/acceptance-cells.toml"
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


DIGEST_A = "a" * 64
FULL_TRACE = [
    "dispatch",
    "summary",
    "scoped-verify",
    "simple-review",
    "findings",
    "fix",
    "refreshed-summary",
    "scoped-verify-2",
    "re-review-approve",
    "reap",
    "cleanup",
]
MATERIAL_BRANCH = [
    "findings",
    "fix",
    "refreshed-summary",
    "scoped-verify-2",
    "re-review-approve",
]
FABLE_EXECUTOR = {"runtime": "claude", "model": "fable", "effort": "high"}
FABLE_REVIEW = {
    "mode": "simple",
    "runtime": "claude",
    "model": "fable",
    "effort": "high",
}


def _material_artifacts(sequence: int) -> dict[str, str]:
    prefix = f"docs/acceptance/evidence/v2.6.7/rc1-run-{sequence}"
    return {
        "findings_artifact": f"{prefix}-findings.json",
        "fix_head": "f" * 40,
        "refreshed_summary_artifact": f"{prefix}-refreshed-summary.json",
        "second_verification_artifact": f"{prefix}-verify-2.json",
        "re_review_artifact": f"{prefix}-re-review.json",
    }


def _bound_receipt(sequence: int, digest: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 2,
        "run_id": f"rc1-run-{sequence}",
        "sequence": sequence,
        "cell_id": f"rc1-corridor-run-{sequence}",
        "corridor": "engineering/change",
        "lifecycle_subject_sha256": digest,
        "request_id": f"request-{sequence}",
        "owner_id": f"owner-{sequence}",
        "store_id": f"store-{sequence}",
        "worktree_id": f"worktree-{sequence}",
        "provider_session_ids": [
            f"executor-session-{sequence}",
            f"reviewer-session-{sequence}",
        ],
        "executor_route": dict(FABLE_EXECUTOR),
        "review_route": dict(FABLE_REVIEW),
        "result": "success",
        "material_cycle": _material_artifacts(sequence) if sequence == 2 else None,
        "resource_free": True,
        "coordinator_recovery": False,
    }
    value.update(overrides)
    return value


def _unbound_v1_receipt(sequence: int, digest: str) -> dict[str, object]:
    """A pass-0 style receipt: shape-only, self-asserted, unbound."""

    return {
        "schema_version": 1,
        "run_id": f"anything-goes-{sequence}",
        "sequence": sequence,
        "lifecycle_subject_sha256": digest,
        "request_id": f"junk-req-{sequence}",
        "owner_id": f"junk-own-{sequence}",
        "store_id": f"junk-store-{sequence}",
        "worktree_id": f"junk-wt-{sequence}",
        "provider_session_ids": [],
        "result": "success",
        "material_finding_cycle": sequence == 1,
        "resource_free": True,
        "coordinator_recovery": False,
    }


# --- Finding 2, config half: the declared trace carries the full branch ----

manifest = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
rc1 = manifest["rc1"]
for name in sorted(rc1["cells"]):
    trace = rc1["cells"][name]["expected"]
    check(
        f"{name} declares the complete supported corridor trace",
        trace == FULL_TRACE,
    )
    check(
        f"{name} represents the material-cycle branch after simple-review",
        all(stage in trace for stage in MATERIAL_BRANCH)
        and trace.index("findings") > trace.index("simple-review"),
    )

# --- Findings 2+3, contract half: gate-bound streak validation -------------

config = stab.load_subject_config(CONFIG_PATH)
check(
    "v267_stabilization exposes an RC1 gate declaration loader",
    hasattr(stab, "load_rc1_gate"),
)
gate = stab.load_rc1_gate(MANIFEST_PATH)
check(
    "gate declaration binds the engineering/change corridor",
    gate.corridor == "engineering/change",
)
check(
    "gate declares the three configured cells in strict sequence",
    [cell.cell_id for cell in gate.cells]
    == [f"rc1-corridor-run-{index}" for index in (1, 2, 3)]
    and [cell.sequence for cell in gate.cells] == [1, 2, 3],
)
check(
    "gate streak target matches the stabilization denominator",
    gate.streak_target == config.streak_target,
)

good = [
    _bound_receipt(1, DIGEST_A),
    _bound_receipt(2, DIGEST_A),
    _bound_receipt(3, DIGEST_A),
]
verdict = stab.validate_streak(
    good, expected_digest=DIGEST_A, config=config, gate=gate
)
check(
    "three bound fresh successes complete the streak",
    verdict["complete"] is True and verdict["streak"] == 3,
)
check(
    "material cycle verdict derives from durable artifacts",
    verdict["material_finding_cycle"] is True,
)

# Self-asserted material evidence is rejected (finding 2).
check_rejects(
    "a v1 self-asserted material_finding_cycle flag fails closed",
    lambda: stab.validate_streak(
        [_unbound_v1_receipt(1, DIGEST_A)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "a bare-boolean material_cycle fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, material_cycle=True)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
for missing in (
    "findings_artifact",
    "fix_head",
    "refreshed_summary_artifact",
    "second_verification_artifact",
    "re_review_artifact",
):
    partial = _material_artifacts(2)
    del partial[missing]
    check_rejects(
        f"a material cycle without {missing} fails closed",
        lambda partial=partial: stab.validate_streak(
            [
                _bound_receipt(1, DIGEST_A),
                _bound_receipt(2, DIGEST_A, material_cycle=partial),
                _bound_receipt(3, DIGEST_A),
            ],
            expected_digest=DIGEST_A,
            config=config,
            gate=gate,
        ),
    )

first_pass_only = [
    _bound_receipt(1, DIGEST_A),
    _bound_receipt(2, DIGEST_A, material_cycle=None),
    _bound_receipt(3, DIGEST_A),
]
verdict = stab.validate_streak(
    first_pass_only, expected_digest=DIGEST_A, config=config, gate=gate
)
check(
    "a streak of first-pass approvals is incomplete without a material cycle",
    verdict["streak"] == 3
    and verdict["material_finding_cycle"] is False
    and verdict["complete"] is False,
)

# Binding requirements (finding 3).
check_rejects(
    "empty provider session identities fail closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, provider_session_ids=[])],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "a sequence gap across configured cells fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A), _bound_receipt(3, DIGEST_A)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "a receipt starting past the first configured cell fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(2, DIGEST_A)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "an unknown cell identity fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, cell_id="rc1-corridor-run-9")],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "a receipt sequence that contradicts its configured cell fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, cell_id="rc1-corridor-run-2")],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "corridor drift fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, corridor="research/deep")],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "executor route drift fails closed",
    lambda: stab.validate_streak(
        [
            _bound_receipt(
                1,
                DIGEST_A,
                executor_route={
                    "runtime": "claude",
                    "model": "sonnet",
                    "effort": "low",
                },
            )
        ],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "review route drift fails closed",
    lambda: stab.validate_streak(
        [
            _bound_receipt(
                1,
                DIGEST_A,
                review_route={
                    "mode": "deep",
                    "runtime": "claude",
                    "model": "fable",
                    "effort": "high",
                },
            )
        ],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)
check_rejects(
    "a missing route binding fails closed",
    lambda: stab.validate_streak(
        [
            {
                key: value
                for key, value in _bound_receipt(1, DIGEST_A).items()
                if key != "executor_route"
            }
        ],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
    ),
)

# --- CLI: the streak subcommand enforces the binding -------------------------

with tempfile.TemporaryDirectory() as raw:
    unbound_path = Path(raw) / "unbound.json"
    unbound_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "receipts": [
                    _unbound_v1_receipt(1, DIGEST_A),
                    _unbound_v1_receipt(5, DIGEST_A),
                    _unbound_v1_receipt(9, DIGEST_A),
                ],
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "streak",
            "--receipts",
            str(unbound_path),
            "--expected-digest",
            DIGEST_A,
            "--config",
            str(CONFIG_PATH),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "streak CLI rejects unbound self-asserted receipts",
        result.returncode == 3,
    )

    bound_path = Path(raw) / "bound.json"
    bound_path.write_text(
        json.dumps({"schema_version": 1, "receipts": good}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "streak",
            "--receipts",
            str(bound_path),
            "--expected-digest",
            DIGEST_A,
            "--config",
            str(CONFIG_PATH),
            "--manifest",
            str(MANIFEST_PATH),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "streak CLI accepts a fully bound complete streak",
        result.returncode == 0 and json.loads(result.stdout)["complete"] is True,
    )

print("rc1 streak binding regression tests passed")
