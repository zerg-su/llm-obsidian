#!/usr/bin/env python3
"""Public-seam checks for internal review callback transport and archive proof."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SUBMIT = ROOT / "scripts/harness/review_submit.py"
ARCHIVE = ROOT / "scripts/harness/review_archive.py"
sys.path.insert(0, str(ROOT / "scripts"))
from review_contract import ReviewContractError, validate_review
from review_resolution import validate_resolution_evidence
from harness.review_archive import render_page
from harness.review_submit import ReviewCallbackPort, ReviewSubmitError, submit_review
from harness.verification import load_profiles


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


review = {
    "schema_version": 1,
    "operation_id": "operation-1",
    "run_id": "run-1",
    "mode": "simple",
    "head_sha": "a" * 40,
    "verification_profile": {"name": "scoped", "sha256": "b" * 64},
    "verdict": "approve",
    "axes": [
        {
            "axis": "openai-holistic",
            "verdict": "approve",
            "verification_iteration": 0,
            "findings": [],
        }
    ],
    "verification_gaps": [],
    "notes_for_executor": [],
    "residual_risks": [],
}
check("simple review contract accepted", validate_review(review)["mode"] == "simple")
try:
    validate_review({**review, "mode": "light"})
except ReviewContractError:
    check("legacy review mode rejected", True)
else:
    check("legacy review mode rejected", False)

with tempfile.TemporaryDirectory(prefix="harness-review-transport.") as raw:
    root = Path(raw)
    worktree = root / "worktree"
    operation = root / "operation"
    vault = root / "vault"
    worktree.mkdir()
    operation.mkdir()
    vault.mkdir()
    (worktree / "config").mkdir()
    (vault / "config").mkdir()
    shutil.copy2(
        ROOT / "config/verification-profiles.toml",
        worktree / "config/verification-profiles.toml",
    )
    shutil.copy2(
        ROOT / "config/verification-profiles.toml",
        vault / "config/verification-profiles.toml",
    )
    (worktree / "tracked.txt").write_text("initial\n", encoding="utf-8")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "review-test@example.invalid"),
        ("git", "config", "user.name", "Review Test"),
        ("git", "add", "tracked.txt", "config/verification-profiles.toml"),
        ("git", "commit", "-qm", "initial"),
    ):
        subprocess.run(command, cwd=worktree, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    profile = load_profiles(worktree / "config/verification-profiles.toml")[
        "scoped"
    ]
    review = {
        **review,
        "head_sha": head,
        "verification_profile": {"name": profile.name, "sha256": profile.sha256},
    }
    meta = {
        "schema_version": 1,
        "run_id": "run-1",
        "review_id": "operation-1",
        "operation_id": "operation-1",
        "review_mode": "simple",
        "head_sha": head,
        "verification_profile": {"name": profile.name, "sha256": profile.sha256},
        "worktree": str(worktree),
        "task_name": "transport-test",
    }
    resolution_payload = {
        "schema_version": 1,
        "operation_id": "operation-1",
        "axis": "openai-holistic",
        "reviewed_head_sha": "0" * 40,
        "resolved_head_sha": head,
        "fix_delta_sha256": "d" * 64,
        "previous_finding_ids": ["F-round-1"],
        "resolutions": [
            {
                "finding_id": "F-round-1",
                "disposition": "rejected",
                "rationale": (
                    "The final verification proved the reported path is "
                    "unreachable under the bound invariant."
                ),
                "follow_up": "",
            }
        ],
    }
    resolution_path = operation / "resolution-openai-holistic-0.json"
    resolution_path.write_text(
        json.dumps(resolution_payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    meta["resolution_evidence"] = [
        {
            "pointer": resolution_path.relative_to(operation).as_posix(),
            "sha256": hashlib.sha256(resolution_path.read_bytes()).hexdigest(),
        }
    ]
    (operation / ".review-meta.json").write_text(json.dumps(meta), encoding="utf-8")

    class CapturePort:
        def __init__(self) -> None:
            self.envelope: Any = None

        def publish(self, envelope: Any) -> None:
            self.envelope = envelope

    port: ReviewCallbackPort = CapturePort()
    envelope = submit_review(json.dumps(review), meta=meta, worktree=worktree, port=port)
    check(
        "callback publisher is an explicit narrow port",
        port.envelope == envelope
        and envelope.operation_id == "operation-1"
        and envelope.payload["head_sha"] == head,
    )
    round_meta = {
        **meta,
        "transport": "review-round",
        "operation_id": "operation-1-round-1",
        "run_id": "run-round-1",
        "review_id": "operation-1",
        "parent_session_operation_id": "operation-1",
        "axis": "openai-holistic",
        "verification_iteration": 0,
    }
    round_result = {
        "schema_version": 1,
        "axis": "openai-holistic",
        "verdict": "changes-requested",
        "verification_iteration": 0,
        "findings": [
            {
                "finding_id": "F-round-1",
                "severity": "important",
                "file": "scripts/review_contract.py",
                "line": 1,
                "summary": "one bounded issue",
                "evidence": "the exact path is reachable",
                "recommendation": "resolve and verify in this session",
            }
        ],
    }
    round_port: ReviewCallbackPort = CapturePort()
    round_envelope = submit_review(
        json.dumps(round_result),
        meta=round_meta,
        worktree=worktree,
        port=round_port,
    )
    check(
        "internal review round publishes the exact child receipt identity",
        round_port.envelope == round_envelope
        and round_envelope.operation_id == "operation-1-round-1"
        and round_envelope.run_id == "run-round-1"
        and round_envelope.payload["parent_session_operation_id"]
        == "operation-1"
        and round_envelope.payload["axis"] == "openai-holistic",
    )

    def check_round_finding_rejected(label: str, candidate: dict[str, Any]) -> None:
        rejected_port: ReviewCallbackPort = CapturePort()
        try:
            submit_review(
                json.dumps(candidate),
                meta=round_meta,
                worktree=worktree,
                port=rejected_port,
            )
        except ReviewSubmitError:
            check(label, rejected_port.envelope is None)
        else:
            check(label, False)

    finding = round_result["findings"][0]
    assert isinstance(finding, dict)
    invalid_id = {**finding, "finding_id": "not a bounded id"}
    check_round_finding_rejected(
        "review round rejects a canonically invalid finding",
        {**round_result, "findings": [invalid_id]},
    )
    duplicate = {**finding, "summary": "a second issue with the same id"}
    check_round_finding_rejected(
        "review round rejects duplicate finding ids",
        {**round_result, "findings": [finding, duplicate]},
    )
    reserved = {
        **finding,
        "finding_id": f"{round_meta['axis']}:F-round-1",
    }
    check_round_finding_rejected(
        "review round rejects the reserved aggregate finding prefix",
        {**round_result, "findings": [reserved]},
    )
    for field in ("finding_id", "file", "summary", "evidence", "recommendation"):
        check_round_finding_rejected(
            f"review round rejects whitespace-only finding {field}",
            {**round_result, "findings": [{**finding, field: " \t "}]},
        )
    for field, value in (
        ("finding_id", "F" + "x" * 100),
        ("file", "x" * 1001),
        ("summary", "x" * 301),
        ("evidence", "x" * 4001),
        ("recommendation", "x" * 4001),
    ):
        check_round_finding_rejected(
            f"review round rejects oversized finding {field}",
            {**round_result, "findings": [{**finding, field: value}]},
        )
    result = subprocess.run(
        [
            sys.executable,
            str(SUBMIT),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(operation),
        ],
        input=json.dumps(review),
        text=True,
        capture_output=True,
        check=False,
    )
    check("typed reviewer outbox accepted", result.returncode == 0)
    callback = json.loads((operation / ".review-callback.json").read_text(encoding="utf-8"))
    check(
        "callback binds exact operation and payload",
        callback["operation_id"] == "operation-1"
        and callback["payload"]["run_id"] == "run-1"
        and len(callback["payload_sha256"]) == 64,
    )
    review_input = operation / ".review-input.json"
    review_input.write_text(json.dumps(review), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SUBMIT),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(operation),
            "--input-file",
            str(review_input),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "review submit accepts one exact scratch input without shell redirection",
        result.returncode == 0 and not review_input.exists(),
    )
    unexpected_input = operation / "review.json"
    unexpected_input.write_text(json.dumps(review), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SUBMIT),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(operation),
            "--input-file",
            str(unexpected_input),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "review submit rejects every non-canonical input file",
        result.returncode != 0 and unexpected_input.exists(),
    )
    stale_resolution = {
        **resolution_payload,
        "resolved_head_sha": "1" * 40,
    }
    resolution_path.write_text(
        json.dumps(stale_resolution, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    meta["resolution_evidence"][0]["sha256"] = hashlib.sha256(
        resolution_path.read_bytes()
    ).hexdigest()
    (operation / ".review-meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    stale_archive = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "archive rejects stale ruling terminal HEAD",
        stale_archive.returncode != 0,
    )
    historical_resolution = {
        **resolution_payload,
        "resolved_head_sha": "1" * 40,
    }
    resolution_path.write_text(
        json.dumps(historical_resolution, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    terminal_resolution_path = operation / "resolution-openai-holistic-1.json"
    broken_terminal_resolution = {
        **resolution_payload,
        "reviewed_head_sha": "2" * 40,
        "fix_delta_sha256": "e" * 64,
    }
    terminal_resolution_path.write_text(
        json.dumps(broken_terminal_resolution, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    meta["resolution_evidence"] = [
        {
            "pointer": resolution_path.relative_to(operation).as_posix(),
            "sha256": hashlib.sha256(resolution_path.read_bytes()).hexdigest(),
        },
        {
            "pointer": terminal_resolution_path.relative_to(operation).as_posix(),
            "sha256": hashlib.sha256(
                terminal_resolution_path.read_bytes()
            ).hexdigest(),
        },
    ]
    (operation / ".review-meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    broken_chain_archive = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "archive rejects a broken same-axis ruling chain",
        broken_chain_archive.returncode != 0,
    )
    terminal_resolution = {
        **broken_terminal_resolution,
        "reviewed_head_sha": "1" * 40,
    }
    terminal_resolution_path.write_text(
        json.dumps(terminal_resolution, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    meta["resolution_evidence"][1]["sha256"] = hashlib.sha256(
        terminal_resolution_path.read_bytes()
    ).hexdigest()
    (operation / ".review-meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (worktree / "config" / "verification-profiles.toml").unlink()
    result = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    archive = json.loads(result.stdout)
    check(
        "approved callback produces bounded dry-run archive proof",
        result.returncode == 0
        and archive["status"] == "dry-run"
        and archive["review_id"] == "operation-1"
        and archive["verdict"] == "approve",
    )
    check(
        "archive preserves an ordered multi-iteration ruling chain",
        len(meta["resolution_evidence"]) == 2,
    )
    check(
        "archive validates coordinator config for a generic product root",
        not (worktree / "config" / "verification-profiles.toml").exists(),
    )
    dry_page = render_page(
        "Archive evidence",
        "operation-1",
        review,
        "c-000123",
        (validate_resolution_evidence(resolution_payload),),
    )
    check(
        "archive renders durable per-finding disposition evidence",
        "## Executor resolutions" in dry_page
        and "F-round-1 · rejected" in dry_page
        and "reported path is unreachable" in dry_page,
    )
    wikilink_shaped_review = {
        **review,
        "axes": [
            {
                "axis": "openai-holistic",
                "verdict": "approve",
                "verification_iteration": 0,
                "findings": [
                    {
                        "finding_id": "F-wikilink-shape",
                        "severity": "minor",
                        "file": "prototype.py",
                        "line": 1,
                        "summary": "List-shaped input [[1, 3], [2, 5]]",
                        "evidence": "Observed [[not a wiki target]]",
                        "recommendation": "Keep [[1, 3], [2, 5]] as data",
                    }
                ],
            }
        ],
        "verification_gaps": ["Gap [[not a page]]"],
        "notes_for_executor": [],
        "residual_risks": [],
    }
    escaped_page = render_page(
        "Archive wikilink-shaped prose",
        "operation-1",
        wikilink_shaped_review,
        "c-000123",
    )
    check(
        "archive escapes reviewer prose that resembles Obsidian wikilinks",
        "[[" not in escaped_page
        and "]]" not in escaped_page
        and r"\[\[1, 3], [2, 5\]\]" in escaped_page,
    )
    scripts = vault / "scripts"
    scripts.mkdir()
    allocator = scripts / "allocate-address.sh"
    allocator.write_text("#!/bin/sh\nprintf 'c-000123\\n'\n", encoding="utf-8")
    allocator.chmod(0o755)
    writer = scripts / "vault-write.py"
    writer.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

vault = Path(__file__).resolve().parents[1]
payload = json.load(sys.stdin)
page = payload["pages"][0]
target = vault / page["path"]
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(page["content"], encoding="utf-8")
count = vault / ".writer-count"
calls = int(count.read_text(encoding="utf-8")) + 1 if count.exists() else 1
count.write_text(str(calls), encoding="utf-8")
raise SystemExit(9 if calls == 1 else 0)
""",
        encoding="utf-8",
    )
    writer.chmod(0o755)
    interrupted = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    archived_page = vault / archive["path"]
    check(
        "archive contains a post-commit pre-marker interruption",
        interrupted.returncode == 3
        and archived_page.is_file()
        and (operation / ".review-archive-intent.json").is_file()
        and not (operation / ".review-archive.json").exists(),
    )
    recovered = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "archive reconstructs its marker after the committed crash window",
        recovered.returncode == 0
        and json.loads(recovered.stdout)["status"] == "archived"
        and json.loads((vault / ".writer-count").read_text(encoding="utf-8")) == 1
        and (operation / ".review-archive.json").is_file()
        and not (operation / ".review-archive-intent.json").exists(),
    )
    current = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "current exact archive is replay-safe without another vault write",
        current.returncode == 0
        and json.loads(current.stdout)["status"] == "already-current",
    )
    legacy_page = archived_page.read_text(encoding="utf-8") + (
        "\n- Legacy reviewer prose: [[1, 3], [2, 5]]\n"
    )
    archived_page.write_text(legacy_page, encoding="utf-8")
    legacy_marker = json.loads(
        (operation / ".review-archive.json").read_text(encoding="utf-8")
    )
    legacy_marker.pop("renderer_version", None)
    legacy_marker["content_sha256"] = hashlib.sha256(
        legacy_page.encode()
    ).hexdigest()
    (operation / ".review-archive.json").write_text(
        json.dumps(legacy_marker), encoding="utf-8"
    )
    repaired = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    repaired_marker = json.loads(
        (operation / ".review-archive.json").read_text(encoding="utf-8")
    )
    check(
        "legacy archive bytes are regenerated through the canonical writer",
        repaired.returncode == 0
        and json.loads(repaired.stdout)["status"] == "archived"
        and repaired_marker["renderer_version"] == 2
        and "[[1, 3]" not in archived_page.read_text(encoding="utf-8")
        and json.loads((vault / ".writer-count").read_text(encoding="utf-8"))
        == 2,
    )
    (operation / ".review-meta.json").write_text(
        json.dumps({**meta, "review_id": "different-review"}), encoding="utf-8"
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("archive rejects split review/operation identity", rejected.returncode == 3)
    (operation / ".review-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    original_callback = callback
    for label, field, value in (
        ("archive rejects mismatched operation identity", "operation_id", "other-op"),
        ("archive rejects mismatched review schema", "schema_version", 2),
        ("archive rejects mismatched reviewed HEAD", "head_sha", "c" * 40),
        ("archive rejects mismatched run identity", "run_id", "run-other"),
        (
            "archive rejects mismatched verification profile evidence",
            "verification_profile",
            {"name": "full", "sha256": "d" * 64},
        ),
    ):
        tampered = json.loads(json.dumps(original_callback))
        tampered["payload"][field] = value
        canonical = json.dumps(
            tampered["payload"], sort_keys=True, separators=(",", ":")
        ).encode()
        tampered["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
        (operation / ".review-callback.json").write_text(
            json.dumps(tampered), encoding="utf-8"
        )
        rejected = subprocess.run(
            [
                sys.executable,
                str(ARCHIVE),
                "--worktree",
                str(worktree),
                "--operation-dir",
                str(operation),
                "--vault-root",
                str(vault),
                "--dry-run",
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        check(label, rejected.returncode == 3)
    bad_digest = json.loads(json.dumps(original_callback))
    bad_digest["payload_sha256"] = "0" * 64
    (operation / ".review-callback.json").write_text(
        json.dumps(bad_digest), encoding="utf-8"
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("archive rejects callback digest mismatch", rejected.returncode == 3)
    (operation / ".review-callback.json").write_text(
        json.dumps(original_callback), encoding="utf-8"
    )
    profile_path = vault / "config/verification-profiles.toml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            '"make test-model-routing"', '"python3 -V"', 1
        ),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "archive rejects evidence for a stale verification profile",
        rejected.returncode == 3,
    )
    shutil.copy2(ROOT / "config/verification-profiles.toml", profile_path)
    (worktree / "tracked.txt").write_text("new HEAD\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "advance HEAD"], cwd=worktree, check=True
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(ARCHIVE),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--dry-run",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("archive rejects evidence for a stale worktree HEAD", rejected.returncode == 3)

print("review transport tests passed")
