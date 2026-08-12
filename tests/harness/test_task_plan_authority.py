from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from approved_plan_snapshot import bind_approved_plan_snapshot  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
import task_escalation_records  # noqa: E402
from task_escalation_records import record_path  # noqa: E402
from task_plan_authority import (  # noqa: E402
    PlanAuthorityError,
    record_plan_amendment,
    resolve_plan_authority,
)
from task_review_finalization_attempt import (  # noqa: E402
    attempt_binding,
    finalization_ledger,
)
from wiki_summary_contract import WikiSummaryError, validate_summary_for_task  # noqa: E402


def plan(outcome: str, evidence: str) -> bytes:
    return (
        "---\ntype: plan\nstatus: pending\n---\n\n# Plan\n\n"
        "## Outcome Contract\n\n```json\n"
        + json.dumps(
            {
                "schema_version": 1,
                "desired_outcome": outcome,
                "success_evidence": [
                    {"evidence_id": evidence, "observable": "It is visible."}
                ],
                "non_goals": ["No foreign task mutation."],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n```\n"
    ).encode()


TASK_ID = "11111111-1111-4111-8111-111111111111"


class TaskPlanAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="task-plan-authority.")
        root = Path(self.temp.name).resolve()
        self.vault = root / "vault"
        self.worktree = root / "worktree"
        self.source = self.vault / "wiki/plans/approved.md"
        self.source.parent.mkdir(parents=True)
        (self.vault / ".vault-meta").mkdir()
        self.worktree.mkdir()
        self.base = plan("Base outcome.", "base")
        self.source.write_bytes(self.base)
        bound = bind_approved_plan_snapshot(
            {"vault_root": self.vault, "plan_file": self.source}
        )
        self.meta = {
            "version": 4,
            "project_id": "project-a",
            "task_id": TASK_ID,
            "task_name": "authority-fixture",
            "origin_session": "session-a",
            "worktree": str(self.worktree),
            "vault_root": str(self.vault),
            "plan_file": str(self.source),
            "plan_snapshot_file": str(bound["_approved_plan_file"]),
            "approved_plan_sha256": bound["_approved_plan_sha256"],
            "outcome_contract_sha256": extract_from_bytes(self.base).sha256,
            "finalization_policy": {
                "max_cycles": 5,
                "add_independent_model_after": 3,
                "execution": "ephemeral",
                "primary_route_alias": "finalization-primary",
                "independent_route_alias": "finalization-independent",
            },
        }
        (self.worktree / ".task-meta.json").write_text(
            json.dumps(self.meta, sort_keys=True) + "\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def amendment_source(self, name: str, content: bytes) -> Path:
        path = self.vault / "drafts" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return path

    def test_ordered_amendments_are_the_only_active_plan_mutation(self) -> None:
        first_bytes = plan("First amended outcome.", "first")
        second_bytes = plan("Second amended outcome.", "second")
        first = record_plan_amendment(
            self.worktree,
            self.amendment_source("first", first_bytes),
            decision="approve the first exact change",
        )
        second = record_plan_amendment(
            self.worktree,
            self.amendment_source("second", second_bytes),
            decision="supersede with the second exact change",
        )

        authority = resolve_plan_authority(self.meta, self.worktree)

        self.assertEqual(authority.content, second_bytes)
        self.assertEqual(authority.plan_sha256, hashlib.sha256(second_bytes).hexdigest())
        self.assertEqual(authority.outcome_sha256, extract_from_bytes(second_bytes).sha256)
        self.assertEqual(authority.amendments, (first, second))
        self.assertEqual(first.payload["task_id"], TASK_ID)
        self.assertEqual(first.payload["root_operation_id"], TASK_ID)
        self.assertEqual(first.payload["prior_plan_sha256"], self.meta["approved_plan_sha256"])
        self.assertEqual(first.payload["prior_amendment_id"], "")
        self.assertEqual(second.payload["prior_plan_sha256"], first.payload["new_plan_sha256"])
        self.assertEqual(second.payload["prior_amendment_id"], first.record_id)
        self.assertEqual(second.payload["prior_amendment_sha256"], first.sha256)

    def test_source_edit_without_amendment_does_not_change_authority(self) -> None:
        self.source.write_bytes(plan("Concurrent source edit.", "source-edit"))

        authority = resolve_plan_authority(self.meta, self.worktree)

        self.assertEqual(authority.content, self.base)
        self.assertEqual(authority.plan_sha256, self.meta["approved_plan_sha256"])

    def test_public_amendment_retry_recovers_its_exact_prepointer_record(self) -> None:
        amended = plan("Crash-safe amended outcome.", "crash-safe")
        source = self.amendment_source("crash-safe", amended)
        original_write_pointer = task_escalation_records._write_pointer
        pointer_attempts = 0

        def fail_first_pointer(worktree: Path, record: object) -> None:
            nonlocal pointer_attempts
            pointer_attempts += 1
            if pointer_attempts == 1:
                raise OSError("simulated pointer publication failure")
            original_write_pointer(worktree, record)

        with mock.patch.object(
            task_escalation_records,
            "_write_pointer",
            side_effect=fail_first_pointer,
        ):
            with self.assertRaises(PlanAuthorityError):
                record_plan_amendment(
                    self.worktree,
                    source,
                    decision="approve the crash-safe exact change",
                )
            with self.assertRaises(PlanAuthorityError):
                record_plan_amendment(
                    self.worktree,
                    source,
                    decision="a foreign retry must not adopt the orphan",
                )
            self.assertFalse(
                (self.worktree / ".task-needs-attention.json").exists()
            )
            recovered = record_plan_amendment(
                self.worktree,
                source,
                decision="approve the crash-safe exact change",
            )

        authority = resolve_plan_authority(self.meta, self.worktree)
        self.assertEqual(pointer_attempts, 2)
        self.assertEqual(authority.content, amended)
        self.assertEqual(authority.amendments, (recovered,))

    def test_summary_evidence_uses_the_explicit_amended_outcome(self) -> None:
        amended = plan("Amended evidence outcome.", "amended")
        record_plan_amendment(
            self.worktree,
            self.amendment_source("summary", amended),
            decision="approve amended summary evidence",
        )
        summary = {
            "schema_version": 2,
            "type": "repo-touch",
            "title": "Authority result",
            "session": "session-a",
            "body": "The amended evidence is established.",
            "outcome_disposition": "achieved",
            "outcome_evidence_ids": ["amended"],
            "residual_gap_pointers": [],
        }

        self.assertEqual(
            validate_summary_for_task(summary, self.meta), summary
        )
        with self.assertRaises(WikiSummaryError):
            validate_summary_for_task(
                {**summary, "outcome_evidence_ids": ["base"]}, self.meta
            )

    def test_amendment_preserves_ledger_lineage_and_rebinds_next_attempt(self) -> None:
        ledger = finalization_ledger(
            self.meta, self.vault, TASK_ID, self.worktree
        )
        attempt_id = "22222222-2222-4222-8222-222222222222"
        ledger.reserve(
            attempt_id=attempt_id,
            exact_head="a" * 40,
            task_id=TASK_ID,
            worktree=str(self.worktree),
            provider_policy={
                "routes": ["finalization-primary"],
                "reason": "primary-only",
            },
        )
        amended = plan("Ledger-safe amended outcome.", "ledger-amended")
        record_plan_amendment(
            self.worktree,
            self.amendment_source("ledger", amended),
            decision="approve the ledger-safe exact change",
        )

        reopened = finalization_ledger(
            self.meta, self.vault, TASK_ID, self.worktree
        )
        lineage = reopened.snapshot()
        _, cycle, active_plan, active_outcome = attempt_binding(
            self.meta, TASK_ID, self.worktree, cycle=1
        )

        self.assertEqual(len(lineage["cycles"]), 1)
        self.assertEqual(lineage["cycles"][0]["attempt_id"], attempt_id)
        self.assertEqual(
            lineage["plan_sha256"], self.meta["approved_plan_sha256"]
        )
        self.assertEqual(cycle, 1)
        self.assertEqual(active_plan, hashlib.sha256(amended).hexdigest())
        self.assertEqual(active_outcome, extract_from_bytes(amended).sha256)

    def test_stale_predecessor_and_mixed_outcome_fail_closed(self) -> None:
        changed = plan("Changed outcome.", "changed")
        record = record_plan_amendment(
            self.worktree,
            self.amendment_source("changed", changed),
            decision="approve the exact change",
        )
        path = record_path(self.worktree, record.record_id)
        forged = json.loads(path.read_text(encoding="utf-8"))
        forged["payload"]["prior_plan_sha256"] = "f" * 64
        raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        path.write_bytes(raw)
        (self.worktree / ".task-needs-attention.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "record_id": record.record_id,
                    "record_sha256": hashlib.sha256(raw).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(PlanAuthorityError):
            resolve_plan_authority(self.meta, self.worktree)

        self.temp.cleanup()
        self.setUp()
        record = record_plan_amendment(
            self.worktree,
            self.amendment_source("mixed", changed),
            decision="approve the exact change",
        )
        snapshot = Path(record.payload["new_plan_snapshot_file"])
        snapshot.write_bytes(plan("Foreign bytes.", "foreign"))
        with self.assertRaisesRegex(PlanAuthorityError, "snapshot"):
            resolve_plan_authority(self.meta, self.worktree)


if __name__ == "__main__":
    unittest.main()
