#!/usr/bin/env python3
"""Behavior tests for the registered one-shot reap log-block repair."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from vault_reap_log_repair import (  # noqa: E402
    ReapLogRepairError,
    build_reap_log_repair_plan,
    parse_reap_log_binding,
)
from vault_write_contract import PayloadError  # noqa: E402
from vault_write_mutations import MutationPlanner  # noqa: E402


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"OK   {name}")


LOG_FRONT = '''---
type: meta
title: "Operation Log"
created: 2026-07-05
updated: 2026-08-10
tags:
  - meta
  - log
status: evergreen
sessions:
  - "public-template-v2"
---

# Operation Log

Append-only intro prose with a healthy [[index]] link.

'''

TASK = "v267-rc1-cell-1f-interval-merge"
HEADING = f"## [2026-08-10] reap | {TASK}"
MALFORMED_BLOCK = (
    f"{HEADING}\n\n"
    "`c-000150` [[Result Page]]. Implemented the corridor.\n\n"
    "Review archive: [[Cross-model review — f20f7\n\n"
)
DISPATCH_BLOCK = (
    f"## [2026-08-10 15:39] dispatch | {TASK}\n\n"
    "Spawned the corridor with plan [[Cell Plan]].\n\n"
)
OLDER_BLOCK = "## [2026-08-09] reap | older-task\n\n`c-000140` [[Older Result]]. done.\n"
REPLACEMENT = (
    f"{HEADING}\n\n"
    "`c-000150` [[Result Page]]. Implemented the corridor.\n\n"
    "Review archive:"
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def binding_for(log_text: str, *, task: str = TASK, replacement: str = REPLACEMENT):
    return parse_reap_log_binding(
        {
            "schema_version": 1,
            "task_name": task,
            "expected_log_sha256": sha(log_text),
            "replacement_entry": replacement,
        }
    )


def fixture(root: Path, log_text: str) -> Path:
    log = root / "wiki" / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(log_text, encoding="utf-8")
    return log


with tempfile.TemporaryDirectory(prefix="reap-log-repair-") as raw:
    root = Path(raw)
    log_text = LOG_FRONT + MALFORMED_BLOCK + DISPATCH_BLOCK + OLDER_BLOCK
    log = fixture(root, log_text)

    plan = build_reap_log_repair_plan(root, binding_for(log_text))
    repaired = plan.payload["pages"][0]["content"]
    check(
        "repair replaces exactly the malformed top reap block",
        repaired == LOG_FRONT + REPLACEMENT + "\n\n" + DISPATCH_BLOCK + OLDER_BLOCK,
    )
    check(
        "repair preserves every unrelated byte",
        repaired.startswith(LOG_FRONT)
        and repaired.endswith(DISPATCH_BLOCK + OLDER_BLOCK),
    )
    check(
        "repair payload is one optimistic writer-owned update",
        plan.payload["actor"] == "reap-log-repair"
        and plan.payload["pages"]
        == [
            {
                "op": "update",
                "path": "wiki/log.md",
                "content": repaired,
                "expected_sha256": sha(log_text),
            }
        ],
    )
    check(
        "repair identity is deterministic",
        build_reap_log_repair_plan(root, binding_for(log_text)).payload
        == plan.payload,
    )

    planner_plan = MutationPlanner(root).plan(dict(plan.payload), "2026-08-10")
    check(
        "planner authorizes the exact recomputed repair payload",
        [(path.name, content) for path, content in planner_plan.writes]
        == [("log.md", repaired)],
    )

    forged = dict(plan.payload)
    forged["pages"] = [dict(plan.payload["pages"][0], content=repaired + "tamper")]
    try:
        MutationPlanner(root).plan(forged, "2026-08-10")
        raise AssertionError("forged repair content must stay writer-owned")
    except PayloadError:
        check("forged repair content is rejected as writer-owned", True)

    unregistered = {
        "schema_version": 1,
        "request_id": "manual",
        "actor": "save",
        "pages": [dict(plan.payload["pages"][0])],
    }
    try:
        MutationPlanner(root).plan(unregistered, "2026-08-10")
        raise AssertionError("unregistered log update must stay writer-owned")
    except PayloadError:
        check("unregistered direct log update stays writer-owned", True)

    log.write_text(repaired, encoding="utf-8")
    try:
        MutationPlanner(root).plan(dict(plan.payload), "2026-08-10")
        raise AssertionError("second application must fail closed")
    except PayloadError:
        check("repair is one-shot: reapplying after success fails closed", True)
    for name, exc_check in {
        "stale binding is rejected after the log advanced": lambda: (
            build_reap_log_repair_plan(root, binding_for(log_text))
        ),
        "healthy top block is not repairable": lambda: (
            build_reap_log_repair_plan(root, binding_for(repaired))
        ),
    }.items():
        try:
            exc_check()
            raise AssertionError(name)
        except ReapLogRepairError:
            check(name, True)

    cases = {
        "missing matching reap block fails closed": (
            LOG_FRONT + DISPATCH_BLOCK + OLDER_BLOCK,
            {},
        ),
        "duplicate matching reap blocks fail closed": (
            LOG_FRONT + MALFORMED_BLOCK + MALFORMED_BLOCK + OLDER_BLOCK,
            {},
        ),
        "matching block below the top fails closed": (
            LOG_FRONT + DISPATCH_BLOCK + MALFORMED_BLOCK + OLDER_BLOCK,
            {},
        ),
        "unrelated malformed bytes elsewhere fail closed": (
            LOG_FRONT
            + MALFORMED_BLOCK
            + DISPATCH_BLOCK
            + "## [2026-08-09] reap | older-task\n\n`c-000140` [[broken\n",
            {},
        ),
        "replacement with a drifted heading fails closed": (
            log_text,
            {"replacement": REPLACEMENT.replace("cell-1f", "cell-9z")},
        ),
        "replacement with unmatched brackets fails closed": (
            log_text,
            {"replacement": REPLACEMENT + " [[still-broken"},
        ),
        "replacement smuggling a second block fails closed": (
            log_text,
            {"replacement": REPLACEMENT + "\n\n## [2026-08-10] reap | extra"},
        ),
    }
    for name, (text, overrides) in cases.items():
        fixture(root, text)
        try:
            build_reap_log_repair_plan(root, binding_for(text, **overrides))
            raise AssertionError(name)
        except ReapLogRepairError:
            check(name, True)

    fixture(root, log_text)
    for name, shape in {
        "binding rejects unknown keys": {
            "schema_version": 1,
            "task_name": TASK,
            "expected_log_sha256": sha(log_text),
            "replacement_entry": REPLACEMENT,
            "extra": 1,
        },
        "binding rejects a malformed digest": {
            "schema_version": 1,
            "task_name": TASK,
            "expected_log_sha256": "not-a-digest",
            "replacement_entry": REPLACEMENT,
        },
        "binding rejects an empty task name": {
            "schema_version": 1,
            "task_name": "",
            "expected_log_sha256": sha(log_text),
            "replacement_entry": REPLACEMENT,
        },
    }.items():
        try:
            parse_reap_log_binding(shape)
            raise AssertionError(name)
        except ReapLogRepairError:
            check(name, True)

print(f"All {passed} reap log repair tests passed.")
