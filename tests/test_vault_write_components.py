#!/usr/bin/env python3
"""Focused behavior tests for the vault-write planning and journal seams."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from vault_write_mutations import MutationPlanner
from vault_write_transaction import TransactionJournal, atomic_write


class Suite:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool) -> None:
        if condition:
            print(f"OK   {label}")
        else:
            print(f"FAIL {label}")
            self.failures.append(label)

    def finish(self) -> int:
        return int(bool(self.failures))


def page(title: str) -> str:
    return f"""---
type: concept
title: "{title}"
status: evergreen
created: 2026-08-02
updated: 2026-08-02
address: c-123456
tags: [test]
sessions: []
---

# {title}
"""


def hot_fixture() -> str:
    return """---
type: meta
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
sessions: []
---

# Hot

## Last Updated

Nothing yet.

## Key Recent Facts

- seed

## Recent Changes

- 2026-01-01: [[Seed]] — seed (`c-000001`)

## Active Threads

- seed
"""


def main() -> int:
    suite = Suite()
    with tempfile.TemporaryDirectory(prefix="vault-write-components.") as raw:
        root = Path(raw).resolve()
        (root / "wiki" / "concepts").mkdir(parents=True)
        (root / ".vault-meta").mkdir()
        (root / "wiki" / "hot.md").write_text(hot_fixture(), encoding="utf-8")
        (root / "wiki" / "log.md").write_text(
            "---\nupdated: 2026-01-01\n---\n\n# Log\n", encoding="utf-8"
        )

        planner = MutationPlanner(root)
        plan = planner.plan(
            {
                "pages": [
                    {
                        "op": "create",
                        "path": "wiki/concepts/Planned.md",
                        "content": page("Planned"),
                    }
                ],
                "hot_bullet": "2026-08-02: [[Planned]] — added (`c-123456`)",
                "log_entry": "## [2026-08-02] test | Planned\n\n- added",
            },
            "2026-08-02",
        )
        planned_paths = {path.relative_to(root).as_posix() for path, _ in plan.writes}
        suite.check(
            "planner owns one complete write set",
            planned_paths
            == {"wiki/concepts/Planned.md", "wiki/hot.md", "wiki/log.md"},
        )
        suite.check(
            "planning is side-effect free",
            not (root / "wiki" / "concepts" / "Planned.md").exists(),
        )

        journal_path = root / ".vault-meta" / ".vault-write-journal.json"
        target = root / "wiki" / "concepts" / "Recovered.md"
        attempts = 0

        def interrupted_write(path: Path, text: str) -> None:
            nonlocal attempts
            attempts += 1
            if path == journal_path:
                atomic_write(path, text)
                return
            raise RuntimeError("simulated process interruption")

        interrupted = TransactionJournal(root, journal_path, interrupted_write)
        try:
            interrupted.commit([(target, page("Recovered"))], [])
        except RuntimeError:
            pass
        suite.check(
            "interrupted commit leaves durable journal only",
            attempts == 2 and journal_path.is_file() and not target.exists(),
        )

        recovered = TransactionJournal(root, journal_path, atomic_write).recover()
        suite.check(
            "recovery rolls the exact write forward",
            recovered == 1 and "# Recovered" in target.read_text(encoding="utf-8"),
        )
        suite.check("recovery clears journal", not journal_path.exists())

        plans = root / "wiki/plans"
        plans.mkdir()
        source_plan = plans / "approved.md"
        approved = "---\nstatus: pending\n---\n\n# Approved\n"
        edited = approved + "\nConcurrent user edit.\n"
        source_plan.write_text(edited, encoding="utf-8")
        conflict_result = root / "wiki/concepts/Conflict result.md"
        conflict_plan = planner.plan(
            {
                "actor": "reap",
                "pages": [
                    {
                        "op": "create",
                        "path": "wiki/concepts/Conflict result.md",
                        "content": page("Conflict result"),
                    }
                ],
                "plan_close": {
                    "file": "wiki/plans/approved.md",
                    "result_link": "[[Conflict result]]",
                    "exec_session": "executor",
                    "expected_sha256": hashlib.sha256(approved.encode()).hexdigest(),
                    "on_conflict": "preserve",
                },
            },
            "2026-08-12",
        )
        suite.check(
            "reap conflict skips only the stale plan close",
            [path for path, _text in conflict_plan.writes] == [conflict_result]
            and conflict_plan.warnings
            == ["plan_close conflict preserved for wiki/plans/approved.md"],
        )
        TransactionJournal(root, journal_path, atomic_write).commit(
            conflict_plan.writes, conflict_plan.deletes
        )
        suite.check(
            "reap conflict records the result without overwriting the user plan",
            conflict_result.is_file()
            and source_plan.read_text(encoding="utf-8") == edited,
        )

    return suite.finish()


if __name__ == "__main__":
    raise SystemExit(main())
