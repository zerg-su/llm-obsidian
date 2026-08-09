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

import hashlib
import json
import re
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


EVIDENCE_TMP = Path(tempfile.mkdtemp(prefix="rc1-streak-evidence-"))


def _evidence_file(relative: str, content: str) -> dict[str, str]:
    path = EVIDENCE_TMP / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}


def _material_artifacts(sequence: int) -> dict[str, object]:
    prefix = f"docs/acceptance/evidence/v2.6.7/rc1-run-{sequence}"
    return {
        "findings_artifact": _evidence_file(
            f"{prefix}-findings.json", '{"findings": 1}'
        ),
        "fix_head": "f" * 40,
        "refreshed_summary_artifact": _evidence_file(
            f"{prefix}-refreshed-summary.json", '{"summary": "refreshed"}'
        ),
        "second_verification_artifact": _evidence_file(
            f"{prefix}-verify-2.json", '{"verify": 2}'
        ),
        "re_review_artifact": _evidence_file(
            f"{prefix}-re-review.json", '{"review": "approve"}'
        ),
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
    good, expected_digest=DIGEST_A, config=config, gate=gate, root=EVIDENCE_TMP
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
        root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "a bare-boolean material_cycle fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, material_cycle=True)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
        root=EVIDENCE_TMP,
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
            root=EVIDENCE_TMP,
        ),
    )

first_pass_only = [
    _bound_receipt(1, DIGEST_A),
    _bound_receipt(2, DIGEST_A, material_cycle=None),
    _bound_receipt(3, DIGEST_A),
]
verdict = stab.validate_streak(
    first_pass_only, expected_digest=DIGEST_A, config=config, gate=gate, root=EVIDENCE_TMP
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
        root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "a sequence gap across configured cells fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A), _bound_receipt(3, DIGEST_A)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
        root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "a receipt starting past the first configured cell fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(2, DIGEST_A)],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
        root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "an unknown cell identity fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, cell_id="rc1-corridor-run-9")],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
        root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "a receipt sequence that contradicts its configured cell fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, cell_id="rc1-corridor-run-2")],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
        root=EVIDENCE_TMP,
    ),
)
check_rejects(
    "corridor drift fails closed",
    lambda: stab.validate_streak(
        [_bound_receipt(1, DIGEST_A, corridor="research/deep")],
        expected_digest=DIGEST_A,
        config=config,
        gate=gate,
        root=EVIDENCE_TMP,
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
        root=EVIDENCE_TMP,
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
        root=EVIDENCE_TMP,
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
        root=EVIDENCE_TMP,
    ),
)

# Material-cycle evidence must be real, contained, and content-bound.
GOOD_SHA = "0" * 64


def _forged_material(**overrides: object) -> dict[str, object]:
    value = _material_artifacts(1)
    value.update(overrides)
    return value


for label, forged in (
    (
        "a nonexistent findings artifact fails closed",
        _forged_material(
            findings_artifact={
                "path": "docs/acceptance/evidence/v2.6.7/does-not-exist.json",
                "sha256": GOOD_SHA,
            }
        ),
    ),
    (
        "a traversal artifact path fails closed",
        _forged_material(
            findings_artifact={
                "path": "docs/acceptance/evidence/v2.6.7/../../../escape.json",
                "sha256": GOOD_SHA,
            }
        ),
    ),
    (
        "an absolute artifact path fails closed",
        _forged_material(
            findings_artifact={"path": "/etc/passwd", "sha256": GOOD_SHA}
        ),
    ),
    (
        "an artifact outside the evidence root fails closed",
        _forged_material(
            findings_artifact=_evidence_file("docs/other/outside.json", "{}")
            | {"path": "docs/other/outside.json"}
        ),
    ),
    (
        "a tampered artifact content hash fails closed",
        _forged_material(
            findings_artifact=_evidence_file(
                "docs/acceptance/evidence/v2.6.7/tampered.json", '{"a": 1}'
            )
            | {"sha256": GOOD_SHA}
        ),
    ),
    (
        "a malformed fix_head object id fails closed",
        _forged_material(fix_head="not-a-git-oid"),
    ),
    (
        "a legacy bare-string artifact reference fails closed",
        _forged_material(
            findings_artifact="docs/acceptance/evidence/v2.6.7/rc1-run-1-findings.json"
        ),
    ),
    (
        "an empty artifact file fails closed",
        _forged_material(
            findings_artifact=_evidence_file(
                "docs/acceptance/evidence/v2.6.7/empty.json", ""
            )
        ),
    ),
):
    check_rejects(
        label,
        lambda forged=forged: stab.validate_streak(
            [_bound_receipt(1, DIGEST_A, material_cycle=forged)],
            expected_digest=DIGEST_A,
            config=config,
            gate=gate,
            root=EVIDENCE_TMP,
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
            "--root",
            str(EVIDENCE_TMP),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "streak CLI accepts a fully bound complete streak",
        result.returncode == 0 and json.loads(result.stdout)["complete"] is True,
    )


# --- Operator contract doc stays bound to the published schema --------------

DOC = ROOT / "docs/acceptance/v2.6.7-stabilization-contract.md"
doc_text = DOC.read_text(encoding="utf-8")
fences = re.findall(r"```json\n(.*?)```", doc_text, flags=re.DOTALL)
documented = next(
    (
        json.loads(block)
        for block in fences
        if '"schema_version": 2' in block and '"cell_id"' in block
    ),
    None,
)
check("the operator contract documents a schema-2 receipt example", documented is not None)
check(
    "the documented receipt carries exactly the required fields",
    sorted(documented) == sorted(stab.RECEIPT_REQUIRED_FIELDS),
)
check(
    "the documented material cycle names exactly the durable artifacts",
    sorted(documented["material_cycle"])
    == sorted(stab.MATERIAL_CYCLE_ARTIFACT_FIELDS),
)
check(
    "the operator contract no longer documents schema-1 streak receipts",
    "schema_version` 1): `run_id`" not in doc_text
    and "material_finding_cycle" not in doc_text,
)
for command_ref in (
    "scripts/live_acceptance_rc1_gate.py preflight",
    "scripts/live_acceptance_rc1_gate.py run",
    "scripts/live_acceptance_rc1_gate.py record",
    "--manifest config/acceptance-cells.toml",
):
    check(
        f"the operator contract documents `{command_ref}`",
        command_ref in doc_text,
    )
check(
    "the documented gate facade exists",
    (ROOT / "scripts/live_acceptance_rc1_gate.py").is_file(),
)

print("rc1 streak binding regression tests passed")
