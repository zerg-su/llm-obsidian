from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from approved_plan_snapshot import (  # noqa: E402
    PlanSnapshotError,
    bind_approved_plan_snapshot,
    validate_approved_plan_snapshot,
)


PLAN = b"""---
type: plan
status: pending
---

# Approved plan

## Outcome Contract

```json
{"schema_version":1,"desired_outcome":"Keep the approved bytes stable.","success_evidence":[{"evidence_id":"snapshot","observable":"The snapshot remains exact."}],"non_goals":["No mutable authority."]}
```
"""


class ApprovedPlanSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="approved-plan-snapshot.")
        self.vault = Path(self.temp.name).resolve()
        self.plan = self.vault / "wiki/plans/approved.md"
        self.plan.parent.mkdir(parents=True)
        (self.vault / ".vault-meta").mkdir()
        self.plan.write_bytes(PLAN)
        self.request = {
            "vault_root": self.vault,
            "plan_file": self.plan,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capture_is_content_addressed_owner_only_and_source_independent(self) -> None:
        bound = bind_approved_plan_snapshot(self.request)
        digest = hashlib.sha256(PLAN).hexdigest()
        snapshot = bound["_approved_plan_file"]

        self.assertEqual(
            snapshot,
            self.vault
            / ".vault-meta/approved-plan-snapshots"
            / f"{digest}.md",
        )
        self.assertEqual(bound["_approved_plan_sha256"], digest)
        self.assertEqual(snapshot.read_bytes(), PLAN)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(snapshot.stat().st_uid, os.getuid())

        meta = {
            "vault_root": str(self.vault),
            "plan_file": str(self.plan),
            "plan_snapshot_file": str(snapshot),
            "approved_plan_sha256": digest,
        }
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8") + "\nConcurrent user edit.\n",
            encoding="utf-8",
        )

        validated = validate_approved_plan_snapshot(meta)

        self.assertEqual(validated.path, snapshot)
        self.assertEqual(validated.sha256, digest)
        self.assertEqual(validated.content, PLAN)

    def test_repeated_capture_is_byte_idempotent(self) -> None:
        first = bind_approved_plan_snapshot(self.request)
        snapshot = first["_approved_plan_file"]
        before = snapshot.read_bytes()

        second = bind_approved_plan_snapshot(self.request)

        self.assertEqual(second["_approved_plan_file"], snapshot)
        self.assertEqual(snapshot.read_bytes(), before)

    def test_tampering_and_noncanonical_identity_fail_closed(self) -> None:
        bound = bind_approved_plan_snapshot(self.request)
        snapshot = bound["_approved_plan_file"]
        digest = bound["_approved_plan_sha256"]
        meta = {
            "vault_root": str(self.vault),
            "plan_file": str(self.plan),
            "plan_snapshot_file": str(snapshot),
            "approved_plan_sha256": digest,
        }

        snapshot.write_bytes(PLAN + b"tampered\n")
        with self.assertRaisesRegex(PlanSnapshotError, "digest"):
            validate_approved_plan_snapshot(meta)

        snapshot.write_bytes(PLAN)
        alias = snapshot.with_name("alias.md")
        alias.write_bytes(PLAN)
        with self.assertRaisesRegex(PlanSnapshotError, "canonical"):
            validate_approved_plan_snapshot(
                {**meta, "plan_snapshot_file": str(alias)}
            )

    def test_symlinked_snapshot_inventory_is_rejected(self) -> None:
        digest = hashlib.sha256(PLAN).hexdigest()
        outside = self.vault / "outside"
        outside.mkdir()
        root = self.vault / ".vault-meta/approved-plan-snapshots"
        root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(PlanSnapshotError, "symlink"):
            bind_approved_plan_snapshot(self.request)


if __name__ == "__main__":
    unittest.main()
