from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


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

    def test_concurrent_publishers_expose_only_complete_snapshot_bytes(self) -> None:
        first_at_publish = threading.Event()
        allow_first_publish = threading.Event()
        original_link = os.link
        calls = 0
        failures: list[BaseException] = []

        def ordered_link(source: object, target: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_at_publish.set()
                if not allow_first_publish.wait(2):
                    raise RuntimeError("second publisher did not run")
            original_link(source, target, **kwargs)

        def capture() -> None:
            try:
                bind_approved_plan_snapshot(self.request)
            except BaseException as exc:  # capture the worker assertion seam
                failures.append(exc)

        with mock.patch("approved_plan_snapshot.os.link", side_effect=ordered_link):
            first = threading.Thread(target=capture)
            first.start()
            self.assertTrue(first_at_publish.wait(2))
            second = threading.Thread(target=capture)
            second.start()
            second.join(2)
            allow_first_publish.set()
            first.join(2)

        digest = hashlib.sha256(PLAN).hexdigest()
        snapshot = (
            self.vault
            / ".vault-meta/approved-plan-snapshots"
            / f"{digest}.md"
        )
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(snapshot.read_bytes(), PLAN)

    def test_crash_left_temporary_file_cannot_poison_snapshot_retry(self) -> None:
        digest = hashlib.sha256(PLAN).hexdigest()
        root = self.vault / ".vault-meta/approved-plan-snapshots"
        root.mkdir(mode=0o700)
        leftover = root / f".{digest}.crash.tmp"
        leftover.write_bytes(PLAN[:12])
        leftover.chmod(0o600)

        bound = bind_approved_plan_snapshot(self.request)

        snapshot = bound["_approved_plan_file"]
        self.assertEqual(snapshot.read_bytes(), PLAN)
        self.assertEqual(leftover.read_bytes(), PLAN[:12])

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
