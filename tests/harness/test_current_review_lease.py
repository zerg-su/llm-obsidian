#!/usr/bin/env python3
"""Deterministic state matrix for the current-review observational lease."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from current_review_lease import (  # noqa: E402
    CurrentReviewLeaseError,
    create_review_lease,
    enforce_current_review_lease,
    evaluate_review_lease,
    lease_attention_payload,
)
from review_target import resolve_target  # noqa: E402
from task_review_identity import _review_resolution_path  # noqa: E402
from task_review_shared import TaskReviewError  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "review@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Lease Test"], cwd=path, check=True)
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path.resolve()


with tempfile.TemporaryDirectory(prefix="current-review-lease.") as raw:
    base = Path(raw)
    product = repo(base / "product")
    target = resolve_target(product)
    lease = create_review_lease(target)
    check("lease binds one clean exact HEAD", lease["head"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=product, text=True).strip())
    check("lease binds target identity", lease["target_key"] == target.target_key)
    held, rebound = evaluate_review_lease(lease, target, gate_status="reviewing")
    check("unchanged active target retains its lease", not rebound and held == lease)

    dirty_cases = (
        ("index", lambda root: ((root / "staged.py").write_text("STAGED = 1\n", encoding="utf-8"), subprocess.run(["git", "add", "staged.py"], cwd=root, check=True))),
        ("worktree", lambda root: (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")),
        ("untracked", lambda root: (root / "untracked.py").write_text("NEW = 1\n", encoding="utf-8")),
    )
    for name, mutate in dirty_cases:
        candidate = repo(base / f"dirty-{name}")
        candidate_target = resolve_target(candidate)
        candidate_lease = create_review_lease(candidate_target)
        mutate(candidate)
        try:
            evaluate_review_lease(candidate_lease, candidate_target, gate_status="reviewing")
        except CurrentReviewLeaseError as exc:
            check(f"{name} drift is rejected while review is active", name in exc.reasons, repr(exc.reasons))
        else:
            check(f"{name} drift is rejected while review is active", False)

    (product / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    try:
        evaluate_review_lease(lease, target, gate_status="changes-requested")
    except CurrentReviewLeaseError as exc:
        check("resolution phase still requires a clean resume", "worktree" in exc.reasons)
    else:
        check("resolution phase still requires a clean resume", False)
    subprocess.run(["git", "add", "app.py"], cwd=product, check=True)
    subprocess.run(["git", "commit", "-m", "resolve finding"], cwd=product, check=True, capture_output=True)
    rebound_lease, rebound = evaluate_review_lease(lease, target, gate_status="changes-requested")
    check("clean changed HEAD rebinds only after changes-requested", rebound and rebound_lease["head"] != lease["head"])
    try:
        evaluate_review_lease(lease, target, gate_status="reviewing")
    except CurrentReviewLeaseError as exc:
        check("clean HEAD drift is stale during active review", exc.reasons == ("head",))
    else:
        check("clean HEAD drift is stale during active review", False)

    range_product = repo(base / "range-product")
    range_base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=range_product, text=True
    ).strip()
    (range_product / "app.py").write_text("VALUE = 20\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=range_product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "range change"],
        cwd=range_product,
        check=True,
        capture_output=True,
    )
    range_target = resolve_target(range_product)
    range_lease = create_review_lease(range_target, base_sha=range_base)
    check(
        "range lease binds an immutable exact base",
        range_lease["schema_version"] == 2
        and range_lease["base"] == range_base,
    )
    (range_product / "app.py").write_text("VALUE = 21\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=range_product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "range resolution"],
        cwd=range_product,
        check=True,
        capture_output=True,
    )
    rebound_range, range_changed = evaluate_review_lease(
        range_lease, range_target, gate_status="changes-requested"
    )
    check(
        "committed resolution retains the original review base",
        range_changed and rebound_range["base"] == range_base,
    )

    attention = lease_attention_payload(
        task_id="11111111-1111-4111-8111-111111111111",
        lease=lease,
        reasons=("index", "worktree"),
        observed_head=rebound_lease["head"],
    )
    check("stale lease produces typed attention", attention == {
        "schema_version": 1,
        "status": "attention-required",
        "reason": "current-review-target-drift",
        "task_id": "11111111-1111-4111-8111-111111111111",
        "target_key": target.target_key,
        "expected_head": lease["head"],
        "observed_head": rebound_lease["head"],
        "drift": ["index", "worktree"],
    })

    gate_path = base / "owner-gate/review-gate.json"
    gate_path.parent.mkdir()
    gate_path.write_text('{"status":"reviewing"}\n', encoding="utf-8")
    try:
        enforce_current_review_lease(
            {
                "lifecycle": "current-checkout",
                "review_lease": lease,
            },
            product,
            gate_path,
            "11111111-1111-4111-8111-111111111111",
        )
    except TaskReviewError:
        pass
    else:
        check("provider admission rejects drift with owner attention", False)
    admission_attention = json.loads(
        (gate_path.parent / "review-lease-attention.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "provider admission rejects drift with owner attention",
        admission_attention["status"] == "attention-required"
        and admission_attention["drift"] == ["head"],
    )

    runtime = base / "owner-runtime"
    current_meta = {"lifecycle": "current-checkout"}
    dispatched_meta = {"lifecycle": "dispatch"}
    check("current resolution transport is owner-only", _review_resolution_path(current_meta, product, runtime) == runtime.resolve() / "inputs/task-review-resolution.json")
    check("dispatched resolution transport remains product-owned", _review_resolution_path(dispatched_meta, product, runtime) == product / ".task-review-resolution.json")

print("\nAll current-review lease tests passed.")
