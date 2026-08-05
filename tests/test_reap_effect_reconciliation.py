#!/usr/bin/env python3
"""Durable recovery checks for a completed pending reap effect."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import EffectOutcome, RuntimeRoute, to_dict  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.workflows.dispatch import DispatchRequest, run_dispatch  # noqa: E402
from harness.workflows.reap import summary_callback  # noqa: E402
from reap_effect_reconciliation import (  # noqa: E402
    ReapEffectRecoveryError,
    parse_recovery_request,
    reconcile_completed_reap_effect,
)


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"OK   {name}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


class Fixture:
    task_id = "11111111-1111-4111-8111-111111111111"
    callback_id = "wiki-summary-fixture"
    address = "c-000125"
    task_name = "bounded-reap-fixture"
    title = "Bounded reap fixture"
    session = "22222222-2222-4222-8222-222222222222"

    def __init__(self, root: Path, *, effect: str = "reap-finalize") -> None:
        self.root = root
        self.vault = root / "vault"
        self.worktree = root / "worktree"
        self.store_root = self.vault / ".vault-meta" / "harness"
        self.result = self.vault / "wiki" / "repos" / f"{self.title}.md"
        self.plan = self.vault / "wiki" / "plans" / "approved.md"
        (self.vault / "wiki" / "repos").mkdir(parents=True)
        self.plan.parent.mkdir(parents=True)
        (self.vault / ".vault-meta").mkdir()
        self.worktree.mkdir()
        (self.vault / ".gitignore").write_text(
            ".vault-meta/harness/\n", encoding="utf-8"
        )
        self.plan.write_text("---\nstatus: pending\n---\n", encoding="utf-8")
        self.result.write_text(
            "---\n"
            "type: repo\n"
            f'title: "{self.title}"\n'
            f"address: {self.address}\n"
            "status: active\n"
            "created: 2026-08-05\n"
            "updated: 2026-08-05\n"
            "tags: [reap]\n"
            "sessions: []\n"
            "---\n\n"
            f"# {self.title}\n",
            encoding="utf-8",
        )
        result_link = f"[[{self.title}]]"
        (self.vault / "wiki" / "log.md").write_text(
            f"## [2026-08-05] reap | {self.task_name}\n\n"
            f"`{self.address}` {result_link}. complete\n",
            encoding="utf-8",
        )
        (self.vault / "wiki" / "hot.md").write_text(
            "## Recent Changes\n\n"
            f"- 2026-08-05: {result_link} — finalized task result "
            f"(`{self.address}`)\n",
            encoding="utf-8",
        )
        (self.vault / ".vault-meta" / "address-map.tsv").write_text(
            f"{self.address}\twiki/repos/{self.title}.md\n",
            encoding="utf-8",
        )
        git(self.vault, "init", "-b", "main")
        git(self.vault, "config", "user.email", "reap@example.invalid")
        git(self.vault, "config", "user.name", "Reap Test")
        git(self.vault, "add", ".gitignore", ".vault-meta/address-map.tsv", "wiki")
        git(self.vault, "commit", "-m", "committed reap fixture")

        self.summary = {
            "schema_version": 2,
            "type": "repo-touch",
            "title": self.title,
            "session": self.session,
            "body": "complete",
            "outcome_disposition": "achieved",
            "outcome_evidence_ids": ["fixture"],
            "residual_gap_pointers": [],
        }
        self.summary_path = self.worktree / ".task-summary.json"
        self.summary_path.write_text(
            json.dumps(self.summary, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.meta_path = self.worktree / ".task-meta.json"
        self.meta_path.write_text(
            json.dumps(
                {
                    "version": 4,
                    "task_id": self.task_id,
                    "task_name": self.task_name,
                    "vault_root": str(self.vault),
                    "worktree": str(self.worktree),
                    "plan_file": str(self.plan),
                    "reap_policy": {"title": self.title, "mode": "shared"},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.prepared_path = self.worktree / ".task-reap-prepared.json"
        prepared = {
            "version": 1,
            "task_name": self.task_name,
            "current_session": self.session,
            "result_path": str(self.result),
            "result_link": result_link,
            "vault_root": str(self.vault),
            "summary_sha256": sha256(self.summary_path),
            "meta_sha256": sha256(self.meta_path),
            "approved_plan_sha256": sha256(self.plan),
            "closed_plan_sha256": sha256(self.plan),
            "plan_path": str(self.plan),
            "review_archives": [],
            "prepared_date": "2026-08-05",
            "prepared_at": "2026-08-05T12:00:00Z",
            "exec_session": self.session,
        }
        self.prepared_path.write_text(
            json.dumps(prepared, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.complete_path = self.worktree / ".task-reap-complete.json"
        complete = {
            "version": 1,
            "task_name": self.task_name,
            "current_session": self.session,
            "result_path": str(self.result),
            "vault_root": str(self.vault),
            "summary_sha256": sha256(self.summary_path),
            "meta_sha256": sha256(self.meta_path),
            "plan_path": str(self.plan),
            "closed_plan_sha256": sha256(self.plan),
            "result_sha256": sha256(self.result),
            "validated": True,
            "completed_at": "2026-08-05T12:01:00Z",
            "task_session_status": "archived",
        }
        self.complete_path.write_text(
            json.dumps(complete, sort_keys=True) + "\n", encoding="utf-8"
        )

        route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
        launched = run_dispatch(
            DispatchRequest(
                self.task_id,
                self.task_id,
                "b" * 64,
                "packets/reap/manifest.json",
                route,
            ),
            self.store_root,
            launch=lambda _record: {"status": "launched"},
            persist_result=lambda _record, _result: None,
        )
        self.run_id = launched.record.run_id
        envelope = summary_callback(
            callback_id=self.callback_id,
            operation_id=self.task_id,
            run_id=self.run_id,
            summary=self.summary,
        )
        self.callback_sha256 = envelope.payload_sha256
        self.store = OperationStore(self.store_root)
        CallbackBroker(self.store, self.task_id).accept(envelope)
        self.store.begin_effect(self.task_id, self.task_id, effect)
        callback = (
            self.store_root
            / "owners"
            / self.task_id
            / "runtime"
            / self.task_id
            / "callback-receipt.json"
        )
        callback.parent.mkdir(parents=True, exist_ok=True)
        callback.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "accepted",
                    "callback_id": self.callback_id,
                    "operation_id": self.task_id,
                    "run_id": self.run_id,
                    "payload_sha256": self.callback_sha256,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.callback_path = callback
        self.request_value = {
            "schema_version": 1,
            "owner_id": self.task_id,
            "operation_id": self.task_id,
            "run_id": self.run_id,
            "accepted_callback_id": self.callback_id,
            "accepted_callback_sha256": self.callback_sha256,
            "prepared_receipt_sha256": sha256(self.prepared_path),
            "completion_receipt_sha256": sha256(self.complete_path),
            "committed_vault_head": git(self.vault, "rev-parse", "HEAD"),
            "result_address": self.address,
        }

    def request(self):
        return parse_recovery_request(self.request_value)

    def reconcile(self):
        return reconcile_completed_reap_effect(
            self.vault,
            self.worktree,
            self.request(),
        )


with tempfile.TemporaryDirectory(prefix="reap-effect-reconcile-") as raw:
    fixture = Fixture(Path(raw))
    before = fixture.store.read(fixture.task_id, fixture.task_id)
    result = fixture.reconcile()
    after = fixture.store.read(fixture.task_id, fixture.task_id)
    check(
        "completed receipt resolves only the exact pending reap effect",
        result["status"] == "reconciled"
        and before.state == after.state == "finalizing"
        and before.pending_effect == "reap-finalize"
        and after.pending_effect == ""
        and after.effect_outcome == EffectOutcome.SUCCEEDED
        and after.resources == before.resources,
    )
    revision = after.revision
    replay = fixture.reconcile()
    check(
        "same completed receipt replay is idempotent",
        replay["status"] == "already-reconciled"
        and fixture.store.read(fixture.task_id, fixture.task_id).revision == revision,
    )
    request_file = fixture.root / "recovery-request.json"
    request_file.write_text(
        json.dumps(fixture.request_value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "reap-effect-reconcile.py"),
            "--vault-root",
            str(fixture.vault),
            "--worktree",
            str(fixture.worktree),
            "--request-file",
            str(request_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "public reconciler CLI preserves exact-receipt idempotence",
        cli.returncode == 0
        and json.loads(cli.stdout)["status"] == "already-reconciled"
        and fixture.store.read(fixture.task_id, fixture.task_id).revision == revision,
    )


def rejected(name: str, mutate, *, effect: str = "reap-finalize") -> None:
    with tempfile.TemporaryDirectory(prefix="reap-effect-reject-") as raw:
        fixture = Fixture(Path(raw), effect=effect)
        mutate(fixture)
        before = to_dict(fixture.store.read(fixture.task_id, fixture.task_id))
        try:
            fixture.reconcile()
        except ReapEffectRecoveryError:
            after = to_dict(fixture.store.read(fixture.task_id, fixture.task_id))
            check(name, after == before)
        else:
            raise AssertionError(name)


rejected(
    "missing completion receipt fails without state mutation",
    lambda fixture: fixture.complete_path.unlink(),
)
rejected(
    "wrong run identity fails without state mutation",
    lambda fixture: fixture.request_value.update(
        {"run_id": "33333333-3333-4333-8333-333333333333"}
    ),
)
rejected(
    "accepted callback digest mismatch fails without state mutation",
    lambda fixture: fixture.request_value.update(
        {"accepted_callback_sha256": "f" * 64}
    ),
)
rejected(
    "prepared receipt digest drift fails without state mutation",
    lambda fixture: fixture.prepared_path.write_text(
        fixture.prepared_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    ),
)
rejected(
    "completion receipt schema drift fails without state mutation",
    lambda fixture: fixture.complete_path.write_text(
        json.dumps(
            {
                **json.loads(fixture.complete_path.read_text(encoding="utf-8")),
                "unexpected": True,
            }
        )
        + "\n",
        encoding="utf-8",
    ),
)
rejected(
    "cross-task receipt fails without state mutation",
    lambda fixture: fixture.request_value.update(
        {"operation_id": "44444444-4444-4444-8444-444444444444"}
    ),
)
rejected(
    "wrong pending effect fails without state mutation",
    lambda _fixture: None,
    effect="other-effect",
)


def duplicate_log(fixture: Fixture) -> None:
    log = fixture.vault / "wiki" / "log.md"
    log.write_text(
        log.read_text(encoding="utf-8")
        + f"\n## [2026-08-05] reap | {fixture.task_name}\n",
        encoding="utf-8",
    )
    git(fixture.vault, "add", "wiki/log.md")
    git(fixture.vault, "commit", "-m", "duplicate cardinality fixture")
    fixture.request_value["committed_vault_head"] = git(
        fixture.vault, "rev-parse", "HEAD"
    )


rejected(
    "duplicate reap cardinality fails without state mutation",
    duplicate_log,
)
rejected(
    "wrong committed vault head fails without state mutation",
    lambda fixture: fixture.request_value.update(
        {"committed_vault_head": "e" * 40}
    ),
)

try:
    parse_recovery_request({"schema_version": 1})
except ReapEffectRecoveryError:
    check("typed recovery request rejects missing identities", True)
else:
    raise AssertionError("typed recovery request rejects missing identities")

print(f"\nPassed: {passed}")
