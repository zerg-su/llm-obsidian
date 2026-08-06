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
    denied = reserve(ledger, 21)
    check(
        "approval closes the lineage without another cycle",
        approved.terminal_disposition == "approved"
        and denied.reason == "approved"
        and not denied.allowed
        and ledger.path.read_bytes() == before_retry,
    )

print("\nAll finalization ledger tests passed.")
