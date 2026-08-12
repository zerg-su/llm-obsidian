from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_reap_lifecycle import prepared_source_plan  # noqa: E402


class SafeReapPlanTest(unittest.TestCase):
    def test_final_reap_preserves_a_concurrent_pending_edit(self) -> None:
        approved = "---\nstatus: pending\n---\n\n# Plan\n"
        edited = approved + "\nUser-authored concurrent change.\n"

        rendered, status = prepared_source_plan(
            {"reap_policy": {"mode": "final"}},
            edited,
            approved_sha256=hashlib.sha256(approved.encode()).hexdigest(),
            today="2026-08-12",
            result_link="[[Task result]]",
            exec_session="executor",
            label="wiki/plans/approved.md",
        )

        self.assertEqual(status, "conflict")
        self.assertEqual(rendered, edited)

    def test_exact_final_close_and_shared_retention_remain_distinct(self) -> None:
        approved = "---\nstatus: pending\nupdated: 2026-08-11\nsessions: []\n---\n\n# Plan\n"
        digest = hashlib.sha256(approved.encode()).hexdigest()
        closed, closed_status = prepared_source_plan(
            {"reap_policy": {"mode": "final"}},
            approved,
            approved_sha256=digest,
            today="2026-08-12",
            result_link="[[Task result]]",
            exec_session="executor",
            label="wiki/plans/approved.md",
        )
        retained, retained_status = prepared_source_plan(
            {"reap_policy": {"mode": "shared"}},
            approved,
            approved_sha256=digest,
            today="2026-08-12",
            result_link="[[Task result]]",
            exec_session="executor",
            label="wiki/plans/approved.md",
        )

        self.assertEqual(closed_status, "closed")
        self.assertIn("status: executed", closed)
        self.assertEqual(retained_status, "retained")
        self.assertEqual(retained, approved)


if __name__ == "__main__":
    unittest.main()
