from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.context import ContextBuilder  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
from task_escalation_records import (  # noqa: E402
    append_amendment,
    append_raise,
    append_resolution,
)
from task_review_context import (  # noqa: E402
    _amendment_evidence,
    _bounded_review_diff,
)
from task_review_shared import TaskReviewError  # noqa: E402


class ReviewDiffBoundaryTest(unittest.TestCase):
    def test_normalizes_invalid_utf8(self) -> None:
        normalized = _bounded_review_diff(b"review fixture\n\xd0\n")

        self.assertIn("\ufffd", normalized.decode("utf-8"))

    def test_truncates_on_utf8_character_boundary(self) -> None:
        raw = ("a" * 64_999 + "Ж" * 1_000).encode("utf-8")

        bounded = _bounded_review_diff(raw)

        self.assertLessEqual(len(bounded), 65_536)
        self.assertTrue(
            bounded.decode("utf-8").endswith(
                "[diff truncated; inspect product HEAD]\n"
            )
        )


class ReviewAmendmentEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="review-amendment.")
        self.worktree = Path(self.temp.name).resolve()
        self.plan = (
            b"# Plan\n\n```json\n"
            b'{"schema_version":1,"desired_outcome":"Keep it exact.",'
            b'"success_evidence":[{"evidence_id":"e1",'
            b'"observable":"The boundary is independently visible."}],'
            b'"non_goals":["No unrelated scope."]}\n```\n'
        )
        self.plan_sha = hashlib.sha256(self.plan).hexdigest()
        self.outcome_sha = extract_from_bytes(self.plan).sha256
        self.meta = {
            "approved_plan_sha256": self.plan_sha,
            "outcome_contract_sha256": self.outcome_sha,
        }
        (self.worktree / ".task-meta.json").write_text(
            json.dumps(
                {
                    **self.meta,
                    "task_name": "fixture",
                    "worktree": str(self.worktree),
                    "project_id": "project",
                    "task_id": "task",
                    "origin_session": "session",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append(self, decision: str = "authorize exact repair"):
        return append_amendment(
            self.worktree,
            plan_sha256=self.plan_sha,
            outcome_sha256=self.outcome_sha,
            decision=decision,
        )

    def test_matching_amendment_binds_exact_record_into_manifest(self) -> None:
        amendment = self.append()

        evidence = _amendment_evidence(self.meta, self.worktree)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        inputs, metadata = evidence
        manifest = ContextBuilder(self.worktree / "packets").build(
            "review", inputs, metadata=metadata
        )
        payload = json.loads(
            (
                self.worktree
                / "packets"
                / manifest.packet_id
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(metadata["amendment_record_id"], amendment.record_id)
        self.assertEqual(metadata["amendment_record_sha256"], amendment.sha256)
        self.assertEqual(payload["metadata"], metadata)
        self.assertEqual(payload["inputs"][0]["sha256"], amendment.sha256)
        self.assertEqual(payload["inputs"][0]["role"], "outcome")

    def test_no_chain_requires_no_amendment_input(self) -> None:
        self.assertIsNone(_amendment_evidence(self.meta, self.worktree))

    def test_digest_mismatched_amendment_is_rejected(self) -> None:
        self.append()
        stale = {**self.meta, "approved_plan_sha256": "a" * 64}

        with self.assertRaisesRegex(TaskReviewError, "does not match"):
            _amendment_evidence(stale, self.worktree)

    def test_ordered_superseding_amendments_select_terminal_authority(self) -> None:
        first = self.append("authorize first exact repair")
        terminal = self.append("supersede with second exact repair")

        evidence = _amendment_evidence(self.meta, self.worktree)

        self.assertIsNotNone(evidence)
        assert evidence is not None
        inputs, metadata = evidence
        self.assertEqual(
            tuple(item.content_sha256 for item in inputs),
            (first.sha256, terminal.sha256),
        )
        self.assertEqual(
            tuple(item.name for item in inputs),
            ("approved-amendment-001.json", "approved-amendment-002.json"),
        )
        self.assertEqual(metadata["amendment_record_id"], terminal.record_id)
        self.assertEqual(metadata["amendment_record_sha256"], terminal.sha256)
        self.assertEqual(metadata["amendment_chain_length"], "2")

    def test_resolved_decision_between_amendments_preserves_order(self) -> None:
        first = self.append("authorize first exact repair")
        raised = append_raise(
            self.worktree,
            {
                "id": "mechanism-interleave",
                "status": "pending",
                "category": "mechanism-failure",
                "worktree": str(self.worktree),
            },
        )
        append_resolution(
            self.worktree,
            "resume the exact approved boundary",
            expected_record_sha256=raised.sha256,
        )
        terminal = self.append("authorize second exact repair")

        evidence = _amendment_evidence(self.meta, self.worktree)

        self.assertIsNotNone(evidence)
        assert evidence is not None
        inputs, metadata = evidence
        self.assertEqual(
            tuple(item.content_sha256 for item in inputs),
            (first.sha256, terminal.sha256),
        )
        self.assertEqual(metadata["amendment_record_id"], terminal.record_id)

    def test_orphan_sibling_amendment_is_rejected(self) -> None:
        first = self.append("authorize first exact repair")
        self.append("persisted sibling before pointer publication")
        (self.worktree / ".task-needs-attention.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "record_id": first.record_id,
                    "record_sha256": first.sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.append("competing terminal amendment")

        with self.assertRaisesRegex(TaskReviewError, "invalid"):
            _amendment_evidence(self.meta, self.worktree)

    def test_mixed_amendment_bindings_are_rejected(self) -> None:
        self.append("authorize matching repair")
        append_amendment(
            self.worktree,
            plan_sha256=self.plan_sha,
            outcome_sha256="f" * 64,
            decision="authorize foreign outcome repair",
        )

        with self.assertRaisesRegex(TaskReviewError, "does not match"):
            _amendment_evidence(self.meta, self.worktree)

    def test_missing_or_tampered_authoritative_record_is_rejected(self) -> None:
        amendment = self.append()
        amendment.path.unlink()
        with self.assertRaisesRegex(TaskReviewError, "invalid"):
            _amendment_evidence(self.meta, self.worktree)

        self.temp.cleanup()
        self.setUp()
        amendment = self.append()
        amendment.path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(TaskReviewError, "invalid"):
            _amendment_evidence(self.meta, self.worktree)


if __name__ == "__main__":
    unittest.main()
