#!/usr/bin/env python3
"""Behavior matrix for the bounded finalization lineage ledger."""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.finalization_ledger import (  # noqa: E402
    FinalizationLedger,
    FinalizationLedgerError,
    predecessor_bound_attempt_id,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def attempt(number: int) -> str:
    return str(uuid.UUID(int=number))


def reserve(
    ledger: FinalizationLedger,
    number: int,
    *,
    task_number: int | None = None,
    worktree: str | None = None,
    routes: tuple[str, ...] = ("finalization-primary",),
):
    return ledger.reserve(
        attempt_id=attempt(number),
        exact_head=f"{number:040x}",
        task_id=attempt(task_number or number),
        worktree=worktree or f"/tmp/finalization-task-{number}",
        provider_policy={
            "routes": list(routes),
            "reason": "primary-only" if len(routes) == 1 else "independent-available",
        },
    )


with tempfile.TemporaryDirectory(prefix="finalization-ledger.") as raw:
    root = Path(raw)
    ledger = FinalizationLedger(
        root,
        lineage_id=attempt(100),
        origin_task_id=attempt(101),
        plan_sha256="a" * 64,
        outcome_contract_sha256="b" * 64,
        max_cycles=5,
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent = list(pool.map(lambda _: reserve(ledger, 1), range(8)))
    check(
        "concurrent duplicate reservation is linearizable",
        {item.cycle_number for item in concurrent} == {1}
        and sum(item.created for item in concurrent) == 1
        and sum(item.allowed for item in concurrent) == 1
        and {item.reason for item in concurrent}
        == {"reserved", "already-reserved"},
    )

    first = concurrent[0]
    try:
        reserve(ledger, 2)
    except FinalizationLedgerError as exc:
        check(
            "a distinct attempt cannot overlap the active reservation",
            "active reservation" in str(exc),
        )
    else:
        check("a distinct attempt cannot overlap the active reservation", False)

    terminal = ledger.record_terminal(
        attempt_id=attempt(1), terminal_result="changes-requested"
    )
    check(
        "the first terminal attempt preserves its exact reservation",
        terminal.cycle_number == first.cycle_number
        and terminal.terminal_result == "changes-requested"
        and terminal.terminal_disposition == "",
    )
    before_terminal_replay = ledger.path.read_bytes()
    replay = ledger.record_terminal(
        attempt_id=attempt(1), terminal_result="changes-requested"
    )
    check(
        "an identical terminal replay is idempotent",
        replay.cycle_number == terminal.cycle_number
        and replay.terminal_result == terminal.terminal_result
        and replay.reason == "already-terminal"
        and ledger.path.read_bytes() == before_terminal_replay,
    )
    try:
        ledger.record_terminal(
            attempt_id=attempt(1), terminal_result="approved"
        )
    except FinalizationLedgerError as exc:
        check(
            "a terminal attempt is immutable",
            "terminal result is immutable" in str(exc),
        )
    else:
        check("a terminal attempt is immutable", False)

    for number in range(2, 6):
        decision = reserve(
            ledger,
            number,
            task_number=200 + number,
            worktree=f"/tmp/recreated-worktree-{number}",
            routes=(
                ("finalization-primary", "finalization-independent")
                if number >= 4
                else ("finalization-primary",)
            ),
        )
        check(
            f"cycle {number} survives changed task/worktree/provider inputs",
            decision.allowed and decision.created and decision.cycle_number == number,
        )
        completed = ledger.record_terminal(
            attempt_id=attempt(number), terminal_result="changes-requested"
        )

    check(
        "the fifth unsuccessful terminal attempt exhausts the lineage",
        completed.cycle_number == 5
        and completed.terminal_disposition == "finalization-budget-exhausted",
    )
    before_sixth = ledger.path.read_bytes()
    sixth = reserve(ledger, 6)
    after_sixth = ledger.path.read_bytes()
    check(
        "the sixth attempt is denied with zero ledger effect",
        not sixth.allowed
        and not sixth.created
        and sixth.cycle_number is None
        and sixth.reason == "finalization-budget-exhausted"
        and before_sixth == after_sixth,
    )

    persisted = json.loads(ledger.path.read_text(encoding="utf-8"))
    check(
        "lineage identity does not reset across mutable execution identity",
        persisted["lineage_id"] == attempt(100)
        and persisted["origin_task_id"] == attempt(101)
        and len(persisted["cycles"]) == 5
        and {cycle["task_id"] for cycle in persisted["cycles"]}
        == {attempt(1), *(attempt(200 + number) for number in range(2, 6))},
    )

with tempfile.TemporaryDirectory(prefix="finalization-ledger-approved.") as raw:
    ledger = FinalizationLedger(
        Path(raw),
        lineage_id=attempt(300),
        origin_task_id=attempt(301),
        plan_sha256="c" * 64,
        outcome_contract_sha256="d" * 64,
    )
    reserve(ledger, 20)
    approved = ledger.record_terminal(
        attempt_id=attempt(20), terminal_result="approved"
    )
    before_retry = ledger.path.read_bytes()
    denied = ledger.reserve(
        attempt_id=attempt(21),
        exact_head=f"{20:040x}",
        task_id=attempt(21),
        worktree="/tmp/finalization-task-21",
        provider_policy={
            "routes": ["finalization-primary"],
            "reason": "primary-only",
        },
    )
    check(
        "approval closes only its exact HEAD",
        approved.terminal_disposition == "approved"
        and denied.reason == "approved"
        and not denied.allowed
        and ledger.path.read_bytes() == before_retry,
    )
    changed_head = reserve(ledger, 22)
    snapshot = ledger.snapshot()
    check(
        "a changed HEAD reopens the bounded lineage after approval",
        changed_head.allowed
        and changed_head.created
        and changed_head.cycle_number == 2
        and snapshot["terminal_disposition"] == ""
        and snapshot["cycles"][0]["terminal_result"] == "approved"
        and snapshot["cycles"][1]["terminal_result"] == "",
    )

with tempfile.TemporaryDirectory(
    prefix="finalization-ledger-amended-boundary."
) as raw:
    ledger = FinalizationLedger(
        Path(raw),
        lineage_id=attempt(310),
        origin_task_id=attempt(311),
        plan_sha256="c" * 64,
        outcome_contract_sha256="d" * 64,
    )
    reserve(ledger, 30)
    ledger.record_terminal(
        attempt_id=attempt(30), terminal_result="approved"
    )

    amended = ledger.reserve(
        attempt_id=attempt(31),
        exact_head=f"{30:040x}",
        task_id=attempt(31),
        worktree="/tmp/finalization-task-amended",
        provider_policy={
            "routes": ["finalization-primary"],
            "reason": "primary-only",
        },
        supersedes_approved_attempt_id=attempt(30),
    )
    snapshot = ledger.snapshot()
    check(
        "an explicit amended boundary reopens approved same-HEAD lineage",
        amended.allowed
        and amended.created
        and amended.cycle_number == 2
        and snapshot["terminal_disposition"] == ""
        and snapshot["cycles"][0]["terminal_result"] == "approved"
        and snapshot["cycles"][1]["attempt_id"] == attempt(31),
    )


# --- Mechanism-neutral attempt accounting (E267.RC1.PRODUCT_BUDGET) ---------
#
# Only a material product outcome (changes-requested or approved) may consume
# one of the five product cycles.  Mechanism outcomes (attention-required,
# blocked) release the reserved cycle into an immutable attempt receipt.

with tempfile.TemporaryDirectory(prefix="finalization-ledger-mechanism.") as raw:
    ledger = FinalizationLedger(
        Path(raw),
        lineage_id=attempt(400),
        origin_task_id=attempt(401),
        plan_sha256="e" * 64,
        outcome_contract_sha256="f" * 64,
    )
    reserve(ledger, 30)
    mechanism = ledger.record_terminal(
        attempt_id=attempt(30), terminal_result="attention-required"
    )
    snapshot = ledger.snapshot()
    check(
        "a mechanism outcome releases its product-cycle reservation",
        mechanism.reason == "mechanism-recorded"
        and mechanism.terminal_result == "attention-required"
        and snapshot["cycles"] == []
        and len(snapshot["attempts"]) == 1
        and snapshot["attempts"][0]["attempt_id"] == attempt(30)
        and snapshot["attempts"][0]["classification"] == "attention-required"
        and snapshot["attempts"][0]["cycle_number"] == 1,
    )
    before_replay = ledger.path.read_bytes()
    replay = ledger.record_terminal(
        attempt_id=attempt(30), terminal_result="attention-required"
    )
    check(
        "a mechanism outcome replay is idempotent",
        replay.reason == "already-mechanism"
        and ledger.path.read_bytes() == before_replay,
    )
    before_unbound_retry = ledger.path.read_bytes()
    try:
        reserve(ledger, 31)
    except FinalizationLedgerError:
        pass
    else:
        raise AssertionError("a predecessor-free retry reopened a released cycle")
    check(
        "a released product cycle requires its exact predecessor generation",
        ledger.path.read_bytes() == before_unbound_retry,
    )
    retry_id = predecessor_bound_attempt_id(
        lineage_id=ledger.lineage_id,
        predecessor_attempt_id=attempt(30),
        exact_head="a" * 40,
        cycle_number=1,
    )
    retry = ledger.reserve_from_policy_matrix(
        attempt_id=retry_id,
        exact_head="a" * 40,
        task_id=attempt(1031),
        worktree="/tmp/finalization-task-31",
        provider_policies={
            cycle: {
                "routes": ["finalization-primary"],
                "reason": "primary-only",
            }
            for cycle in range(1, ledger.max_cycles + 1)
        },
        predecessor_attempt_id=attempt(30),
    )
    check(
        "the released cycle number is reserved again by the retry",
        retry.allowed and retry.created and retry.cycle_number == 1,
    )
    before_retry_replay = ledger.path.read_bytes()
    retry_replay = ledger.reserve_from_policy_matrix(
        attempt_id=retry_id,
        exact_head="a" * 40,
        task_id=attempt(1031),
        worktree="/tmp/finalization-task-31",
        provider_policies={
            cycle: {
                "routes": ["finalization-primary"],
                "reason": "primary-only",
            }
            for cycle in range(1, ledger.max_cycles + 1)
        },
        predecessor_attempt_id=attempt(30),
    )
    check(
        "autonomous continuation repeat ticks do not duplicate a product cycle",
        retry_replay.reason == "already-reserved"
        and not retry_replay.allowed
        and not retry_replay.created
        and retry_replay.cycle_number == 1
        and ledger.path.read_bytes() == before_retry_replay,
    )
    blocked = ledger.record_terminal(
        attempt_id=retry_id, terminal_result="blocked"
    )
    snapshot = ledger.snapshot()
    check(
        "a blocked review is mechanism evidence, not a product cycle",
        blocked.reason == "mechanism-recorded"
        and snapshot["cycles"] == []
        and [row["classification"] for row in snapshot["attempts"]]
        == ["attention-required", "blocked"],
    )
    second_retry_id = predecessor_bound_attempt_id(
        lineage_id=ledger.lineage_id,
        predecessor_attempt_id=retry_id,
        exact_head=f"{32:040x}",
        cycle_number=1,
    )
    ledger.reserve_from_policy_matrix(
        attempt_id=second_retry_id,
        exact_head=f"{32:040x}",
        task_id=attempt(32),
        worktree="/tmp/finalization-task-32",
        provider_policies={
            cycle: {
                "routes": ["finalization-primary"],
                "reason": "primary-only",
            }
            for cycle in range(1, ledger.max_cycles + 1)
        },
        predecessor_attempt_id=retry_id,
    )
    ledger.record_terminal(
        attempt_id=second_retry_id, terminal_result="changes-requested"
    )
    for number in range(33, 37):
        reserve(ledger, number)
        ledger.record_terminal(
            attempt_id=attempt(number), terminal_result="changes-requested"
        )
    snapshot = ledger.snapshot()
    check(
        "five material failures exhaust the lineage despite mechanism receipts",
        [cycle["terminal_result"] for cycle in snapshot["cycles"]]
        == ["changes-requested"] * 5
        and snapshot["terminal_disposition"] == "finalization-budget-exhausted"
        and len(snapshot["attempts"]) == 2,
    )
    before_sixth = ledger.path.read_bytes()
    sixth = reserve(ledger, 37)
    check(
        "mechanism receipts do not reopen the exhausted lineage",
        not sixth.allowed
        and sixth.reason == "finalization-budget-exhausted"
        and ledger.path.read_bytes() == before_sixth,
    )

with tempfile.TemporaryDirectory(
    prefix="finalization-ledger-accepted-cleanup-recovery."
) as raw:
    ledger = FinalizationLedger(
        Path(raw),
        lineage_id=attempt(430),
        origin_task_id=attempt(431),
        plan_sha256="1" * 64,
        outcome_contract_sha256="2" * 64,
    )
    reserve(ledger, 432)
    ledger.record_terminal(
        attempt_id=attempt(432), terminal_result="attention-required"
    )
    recovered = ledger.reserve(
        attempt_id=attempt(432),
        exact_head=f"{432:040x}",
        task_id=attempt(432),
        worktree="/tmp/finalization-task-432",
        provider_policy={
            "routes": ["finalization-primary"],
            "reason": "primary-only",
        },
        recover_attention_attempt=True,
    )
    recovered_snapshot = ledger.snapshot()
    check(
        "exact accepted-callback cleanup restores its released reservation",
        not recovered.allowed
        and recovered.created
        and recovered.reason == "attention-recovered"
        and recovered.cycle_number == 1
        and recovered_snapshot["attempts"] == []
        and recovered_snapshot["cycles"][0]["attempt_id"] == attempt(432)
        and recovered_snapshot["cycles"][0]["terminal_result"] == "",
    )
    approved_after_cleanup = ledger.record_terminal(
        attempt_id=attempt(432), terminal_result="approved"
    )
    check(
        "recovered callback records its actual terminal review verdict",
        approved_after_cleanup.terminal_result == "approved"
        and ledger.snapshot()["terminal_disposition"] == "approved",
    )

with tempfile.TemporaryDirectory(
    prefix="finalization-ledger-predecessor-retry."
) as raw:
    lineage_id = attempt(450)
    ledger = FinalizationLedger(
        Path(raw),
        lineage_id=lineage_id,
        origin_task_id=attempt(451),
        plan_sha256="1" * 64,
        outcome_contract_sha256="2" * 64,
        max_cycles=5,
    )
    for number in range(1, 5):
        reservation = reserve(ledger, 450 + number)
        check(
            f"predecessor fixture reserves product cycle {number}",
            reservation.cycle_number == number,
        )
        ledger.record_terminal(
            attempt_id=attempt(450 + number),
            terminal_result="changes-requested",
        )
    failed_attempt_id = attempt(455)
    failed = reserve(ledger, 455)
    check(
        "predecessor fixture reserves the bounded fifth product cycle",
        failed.cycle_number == 5,
    )
    mechanism = ledger.record_terminal(
        attempt_id=failed_attempt_id,
        terminal_result="attention-required",
    )
    replacement_head = "f" * 40
    replacement_id = predecessor_bound_attempt_id(
        lineage_id=lineage_id,
        predecessor_attempt_id=failed_attempt_id,
        exact_head=replacement_head,
        cycle_number=5,
    )
    policies = {
        cycle: {
            "routes": ["finalization-primary"],
            "reason": "primary-only",
        }
        for cycle in range(1, 6)
    }
    replacement = ledger.reserve_from_policy_matrix(
        attempt_id=replacement_id,
        exact_head=replacement_head,
        task_id=attempt(456),
        worktree="/tmp/finalization-task-456",
        provider_policies=policies,
        predecessor_attempt_id=failed_attempt_id,
    )
    snapshot = ledger.snapshot()
    check(
        "a predecessor-bound retry retains product cycle five",
        mechanism.cycle_number == 5
        and replacement.allowed
        and replacement.created
        and replacement.cycle_number == 5
        and [cycle["number"] for cycle in snapshot["cycles"]]
        == [1, 2, 3, 4, 5]
        and snapshot["cycles"][-1]["attempt_id"] == replacement_id
        and snapshot["attempts"][-1]["attempt_id"] == failed_attempt_id,
    )
    replacement_replay = ledger.reserve_from_policy_matrix(
        attempt_id=replacement_id,
        exact_head=replacement_head,
        task_id=attempt(456),
        worktree="/tmp/finalization-task-456",
        provider_policies=policies,
        predecessor_attempt_id=failed_attempt_id,
    )
    check(
        "a predecessor-bound retry reservation replays idempotently",
        not replacement_replay.allowed
        and not replacement_replay.created
        and replacement_replay.reason == "already-reserved"
        and replacement_replay.cycle_number == 5,
    )
    wrong_predecessor = attempt(457)
    before_rejection = ledger.path.read_bytes()
    try:
        ledger.reserve_from_policy_matrix(
            attempt_id=predecessor_bound_attempt_id(
                lineage_id=lineage_id,
                predecessor_attempt_id=wrong_predecessor,
                exact_head=replacement_head,
                cycle_number=5,
            ),
            exact_head=replacement_head,
            task_id=attempt(456),
            worktree="/tmp/finalization-task-456",
            provider_policies=policies,
            predecessor_attempt_id=wrong_predecessor,
        )
    except FinalizationLedgerError:
        pass
    else:
        raise AssertionError("an unrecorded predecessor authorized a retry")
    check(
        "a missing predecessor fails closed with zero ledger effect",
        ledger.path.read_bytes() == before_rejection,
    )
    try:
        predecessor_bound_attempt_id(
            lineage_id=lineage_id,
            predecessor_attempt_id=failed_attempt_id,
            exact_head=replacement_head,
            cycle_number=6,
        )
    except FinalizationLedgerError:
        pass
    else:
        raise AssertionError("a sixth product cycle identity was synthesized")
    check("a retry generation cannot synthesize product cycle six", True)

with tempfile.TemporaryDirectory(prefix="finalization-ledger-legacy.") as raw:
    legacy = FinalizationLedger(
        Path(raw),
        lineage_id=attempt(500),
        origin_task_id=attempt(501),
        plan_sha256="a" * 64,
        outcome_contract_sha256="b" * 64,
    )
    reserve(legacy, 40)
    legacy.record_terminal(
        attempt_id=attempt(40), terminal_result="changes-requested"
    )
    # A pre-2.6.7 ledger has no current mechanism-attempt authority.
    stored = json.loads(legacy.path.read_text(encoding="utf-8"))
    stored.pop("attempts")
    legacy.path.write_text(
        json.dumps(stored, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    try:
        legacy.snapshot()
    except FinalizationLedgerError:
        pass
    else:
        raise AssertionError("a legacy ledger without attempts was accepted")
    check("a legacy ledger without attempt receipts fails closed", True)

with tempfile.TemporaryDirectory(prefix="finalization-ledger-bound.") as raw:
    bounded = FinalizationLedger(
        Path(raw),
        lineage_id=attempt(600),
        origin_task_id=attempt(601),
        plan_sha256="c" * 64,
        outcome_contract_sha256="d" * 64,
    )
    mechanism_attempt_id = attempt(50)
    reserve(bounded, 50)
    for number in range(50, 50 + 25):
        if number != 50:
            next_attempt_id = predecessor_bound_attempt_id(
                lineage_id=bounded.lineage_id,
                predecessor_attempt_id=mechanism_attempt_id,
                exact_head=f"{number:040x}",
                cycle_number=1,
            )
            bounded.reserve_from_policy_matrix(
                attempt_id=next_attempt_id,
                exact_head=f"{number:040x}",
                task_id=attempt(number),
                worktree=f"/tmp/finalization-task-{number}",
                provider_policies={
                    cycle: {
                        "routes": ["finalization-primary"],
                        "reason": "primary-only",
                    }
                    for cycle in range(1, bounded.max_cycles + 1)
                },
                predecessor_attempt_id=mechanism_attempt_id,
            )
            mechanism_attempt_id = next_attempt_id
        bounded.record_terminal(
            attempt_id=mechanism_attempt_id,
            terminal_result="attention-required",
        )
    try:
        final_attempt_id = predecessor_bound_attempt_id(
            lineage_id=bounded.lineage_id,
            predecessor_attempt_id=mechanism_attempt_id,
            exact_head=f"{99:040x}",
            cycle_number=1,
        )
        bounded.reserve_from_policy_matrix(
            attempt_id=final_attempt_id,
            exact_head=f"{99:040x}",
            task_id=attempt(99),
            worktree="/tmp/finalization-task-99",
            provider_policies={
                cycle: {
                    "routes": ["finalization-primary"],
                    "reason": "primary-only",
                }
                for cycle in range(1, bounded.max_cycles + 1)
            },
            predecessor_attempt_id=mechanism_attempt_id,
        )
        bounded.record_terminal(
            attempt_id=final_attempt_id,
            terminal_result="attention-required",
        )
    except FinalizationLedgerError as exc:
        check(
            "mechanism recovery evidence is separately bounded",
            "mechanism" in str(exc),
        )
    else:
        check("mechanism recovery evidence is separately bounded", False)

print("\nAll finalization ledger tests passed.")
