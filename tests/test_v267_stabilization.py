#!/usr/bin/env python3
"""Independent expectations for the 2.6.7 stabilization denominator.

Covers E267.RC1.SUBJECT_DIGEST and E267.RC1.RELEASE_STOP: subject
inclusion/exclusion, digest determinism and reset behavior, streak
ordering/freshness, and the three-class release stop rule.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/v267_stabilization.py"
CONFIG_PATH = ROOT / "config/v267-stabilization-subject.json"
MANIFEST_PATH = ROOT / "config/acceptance-cells.toml"
sys.path.insert(0, str(ROOT / "scripts"))

import v267_stabilization as stab


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _seed_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    files = {
        "scripts/harness/cli.py": "print('cli')\n",
        "config/model-routing.toml": "[routes]\n",
        "hooks/hooks.json": "{}\n",
        "schemas/lifecycle-transition-v1.json": "{}\n",
        "skills/dispatch/SKILL.md": "# dispatch\n",
        "docs/skill-references/failure-repair-contract.md": "contract\n",
        "docs/dragonscale-guide.md": "guide\n",
        "docs/acceptance/evidence/v2.6.7/rc1-receipt.json": "{}\n",
        "tests/test_sample.py": "assert True\n",
        "wiki/log.md": "log\n",
        "references/obsidian-markdown.md": "ref\n",
        "README.md": "readme\n",
        "CLAUDE.md": "claude\n",
        "Makefile": "test:\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seed")


def _commit(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", relative)
    _git(root, "commit", "-qm", f"edit {relative}")


EVID_TMP = Path(tempfile.mkdtemp(prefix="v267-evidence-"))


def _evidence_file(relative: str, content: str) -> dict[str, str]:
    path = EVID_TMP / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}


def _material(sequence: int) -> dict[str, object]:
    prefix = f"docs/acceptance/evidence/v2.6.7/rc1-run-{sequence}"
    return {
        "findings_artifact": _evidence_file(
            f"{prefix}-findings.json", '{"findings": 1}'
        ),
        "fix_head": "f" * 40,
        "refreshed_summary_artifact": _evidence_file(
            f"{prefix}-refreshed-summary.json", '{"summary": "refreshed"}'
        ),
        "second_verification_artifact": _evidence_file(
            f"{prefix}-verify-2.json", '{"verify": 2}'
        ),
        "re_review_artifact": _evidence_file(
            f"{prefix}-re-review.json", '{"review": "approve"}'
        ),
    }


def _receipt(sequence: int, digest: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 2,
        "run_id": f"rc1-run-{sequence}",
        "sequence": sequence,
        "cell_id": f"rc1-corridor-run-{sequence}",
        "corridor": "engineering/change",
        "lifecycle_subject_sha256": digest,
        "request_id": f"request-{sequence}",
        "owner_id": f"owner-{sequence}",
        "store_id": f"store-{sequence}",
        "worktree_id": f"worktree-{sequence}",
        "provider_session_ids": [f"session-{sequence}"],
        "executor_route": {"runtime": "claude", "model": "fable", "effort": "high"},
        "review_route": {
            "mode": "simple",
            "runtime": "claude",
            "model": "fable",
            "effort": "high",
        },
        "result": "success",
        "material_cycle": _material(sequence) if sequence == 2 else None,
        "resource_free": True,
        "coordinator_recovery": False,
    }
    value.update(overrides)
    return value


def _defect(defect_id: str, root_cause_class: str, seam: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "defect_id": defect_id,
        "root_cause_class": root_cause_class,
        "seam": seam,
        "alias_of": None,
        "reproducer": f"tests/harness/test_lifecycle_crash_matrix.py::{defect_id}",
        "durable_pre_state": "accepted callback persisted",
        "expected_owner": "runtime_worker_review_bridge",
        "expected_transition": "review-findings-published",
        "observed_post_state": "ingestion pending after restart",
        "effect_ambiguity": "none",
        "focused_regression": "tests/harness/test_callback_submit_recovery.py",
        "disposition": "recorded",
    }
    value.update(overrides)
    return value


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


config = stab.load_subject_config(CONFIG_PATH)
check("subject config loads with schema_version 1", config.schema_version == 1)
check("subject config binds release 2.6.7", config.release == "2.6.7")
check(
    "known defect seam is the callback/finalization seam",
    config.known_defect_seam == "callback-finalization",
)
check("release stop needs three new classes", config.release_stop_class_limit == 3)
check("streak target is three fresh runs", config.streak_target == 3)

INCLUDED = [
    "scripts/harness/cli.py",
    "scripts/v267_stabilization.py",
    "hooks/hooks.json",
    "schemas/lifecycle-transition-v1.json",
    "config/model-routing.toml",
    "config/v267-stabilization-subject.json",
    "bin/setup-dcg.sh",
    "skills/dispatch/SKILL.md",
    "skills/review/SKILL.md",
    "agents/reviewer.md",
    ".claude/hooks/stop.sh",
    ".claude/skill-rules.json",
    ".codex-plugin/plugin.json",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/skill-references/failure-repair-contract.md",
    "docs/runtime-capabilities.md",
    "docs/task-sessions.md",
    # Unknown future roots fail safe: they reset the streak rather than
    # silently preserving it.
    "newroot/module.py",
    "toolchain.cfg",
]
EXCLUDED = [
    "wiki/log.md",
    "wiki/plans/2026-08-09-plan.md",
    "docs/dragonscale-guide.md",
    "docs/acceptance/v2.6.7-stabilization-contract.md",
    "docs/acceptance/evidence/v2.6.7/rc1-run-1.json",
    "tests/harness/test_store.py",
    "tests/test_v267_stabilization.py",
    "references/obsidian-markdown.md",
    "evals/sample.json",
    "examples/example.md",
    "prototypes/sketch.py",
    "_templates/page.md",
    ".obsidian/app.json",
    ".raw/dump.txt",
    ".vault-meta/index.jsonl",
    ".claude-memory/MEMORY.md",
    ".github/workflows/ci.yml",
    ".mcp-profiles/heavy.json",
    ".mcp.json.example",
    "README.md",
    "README.ru.md",
    "CHANGELOG.md",
    "ATTRIBUTION.md",
    "LICENSE",
    "Makefile",
    ".gitignore",
    ".gitattributes",
]
for relative in INCLUDED:
    check(f"subject includes {relative}", stab.classify_path(relative, config) is True)
for relative in EXCLUDED:
    check(f"subject excludes {relative}", stab.classify_path(relative, config) is False)


with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw) / "repo"
    repo.mkdir()
    _seed_repo(repo)
    base = stab.lifecycle_subject_sha256(repo, config)
    check(
        "digest is 64 lowercase hex characters",
        len(base) == 64 and base == base.lower() and set(base) <= set("0123456789abcdef"),
    )
    check(
        "digest is deterministic across repeated computation",
        stab.lifecycle_subject_sha256(repo, config) == base,
    )

    # Excluded edits must preserve the digest: wiki, ordinary docs,
    # release evidence, tests, and root markdown.
    for relative, content in (
        ("wiki/log.md", "log grew\n"),
        ("docs/dragonscale-guide.md", "guide v2\n"),
        ("docs/acceptance/evidence/v2.6.7/rc1-receipt.json", "{\"run\": 1}\n"),
        ("tests/test_sample.py", "assert 1 + 1 == 2\n"),
        ("README.md", "readme v2\n"),
        ("Makefile", "test:\n\ttrue\n"),
    ):
        _commit(repo, relative, content)
        check(
            f"excluded edit {relative} preserves the digest",
            stab.lifecycle_subject_sha256(repo, config) == base,
        )

    # Behavioral edits must each change the digest: runtime code, config,
    # schema, hook, skill, and behavioral documents.
    current = base
    for relative, content in (
        ("scripts/harness/cli.py", "print('cli v2')\n"),
        ("config/model-routing.toml", "[routes]\nprimary = 'fable-high'\n"),
        ("schemas/lifecycle-transition-v1.json", "{\"v\": 2}\n"),
        ("hooks/hooks.json", "{\"Stop\": []}\n"),
        ("skills/dispatch/SKILL.md", "# dispatch v2\n"),
        ("docs/skill-references/failure-repair-contract.md", "contract v2\n"),
        ("CLAUDE.md", "claude v2\n"),
        ("scripts/new_owner.py", "print('new')\n"),
    ):
        _commit(repo, relative, content)
        changed = stab.lifecycle_subject_sha256(repo, config)
        check(f"behavioral edit {relative} changes the digest", changed != current)
        current = changed

    # Dirty behavioral state fails closed; dirty excluded state does not.
    (repo / "wiki/log.md").write_text("uncommitted log\n", encoding="utf-8")
    check(
        "dirty wiki state keeps the digest computable",
        stab.lifecycle_subject_sha256(repo, config) == current,
    )
    (repo / "scripts/harness/cli.py").write_text("print('dirty')\n", encoding="utf-8")
    try:
        stab.lifecycle_subject_sha256(repo, config)
    except stab.StabilizationError:
        print("OK   dirty behavioral tracked state fails closed")
    else:
        raise AssertionError("dirty behavioral tracked state fails closed")
    _git(repo, "checkout", "--", "scripts/harness/cli.py")
    (repo / "scripts/untracked_owner.py").write_text("print('x')\n", encoding="utf-8")
    try:
        stab.lifecycle_subject_sha256(repo, config)
    except stab.StabilizationError:
        print("OK   untracked behavioral path fails closed")
    else:
        raise AssertionError("untracked behavioral path fails closed")
    (repo / "scripts/untracked_owner.py").unlink()

    cli_digest = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "digest",
            "--root",
            str(repo),
            "--config",
            str(CONFIG_PATH),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("digest CLI exits 0 on a clean fixture repo", cli_digest.returncode == 0)
    payload = json.loads(cli_digest.stdout)
    check(
        "digest CLI reports the module digest",
        payload["lifecycle_subject_sha256"] == current,
    )
    check(
        "digest CLI reports a positive subject path count",
        payload["subject_path_count"] > 0,
    )


# --- Streak validation -----------------------------------------------------

gate = stab.load_rc1_gate(MANIFEST_PATH)
check(
    "gate streak target matches the stabilization denominator",
    gate.streak_target == config.streak_target,
)

good = [_receipt(1, DIGEST_A), _receipt(2, DIGEST_A), _receipt(3, DIGEST_A)]
verdict = stab.validate_streak(
    good, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
)
check("three fresh successes complete the streak", verdict["complete"] is True)
check("complete streak counts three runs", verdict["streak"] == 3)
check(
    "material finding cycle is present in the window",
    verdict["material_finding_cycle"] is True,
)

reset = [
    _receipt(1, DIGEST_A),
    _receipt(2, DIGEST_A),
    _receipt(3, DIGEST_B, material_cycle=_material(3)),
]
verdict = stab.validate_streak(
    reset, expected_digest=DIGEST_B, config=config, gate=gate, root=EVID_TMP
)
check("behavioral digest change resets the streak", verdict["streak"] == 1)
check("streak after digest change is incomplete", verdict["complete"] is False)

stale = stab.validate_streak(
    good, expected_digest=DIGEST_B, config=config, gate=gate, root=EVID_TMP
)
check(
    "streak on a superseded digest counts zero",
    stale["streak"] == 0 and stale["complete"] is False,
)

failed_middle = [
    _receipt(1, DIGEST_A),
    _receipt(2, DIGEST_A, result="failed"),
    _receipt(3, DIGEST_A, material_cycle=_material(3)),
]
verdict = stab.validate_streak(
    failed_middle, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
)
check("failed run resets the consecutive count", verdict["streak"] == 1)

recovered = [
    _receipt(1, DIGEST_A),
    _receipt(2, DIGEST_A, coordinator_recovery=True),
    _receipt(3, DIGEST_A, material_cycle=_material(3)),
]
verdict = stab.validate_streak(
    recovered, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
)
check("coordinator recovery invalidates the run", verdict["streak"] == 1)

leaked = [
    _receipt(1, DIGEST_A),
    _receipt(2, DIGEST_A, resource_free=False),
    _receipt(3, DIGEST_A, material_cycle=_material(3)),
]
verdict = stab.validate_streak(
    leaked, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
)
check("non-resource-free run invalidates the run", verdict["streak"] == 1)

no_material = [
    _receipt(1, DIGEST_A),
    _receipt(2, DIGEST_A, material_cycle=None),
    _receipt(3, DIGEST_A),
]
verdict = stab.validate_streak(
    no_material, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
)
check(
    "streak without a material finding cycle is incomplete",
    verdict["streak"] == 3 and verdict["complete"] is False,
)

for field in ("request_id", "owner_id", "store_id", "worktree_id"):
    repeated = [
        _receipt(1, DIGEST_A),
        _receipt(2, DIGEST_A),
        _receipt(3, DIGEST_A),
    ]
    repeated[1][field] = repeated[0][field]
    try:
        stab.validate_streak(
            repeated, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
        )
    except stab.StabilizationError:
        print(f"OK   repeated {field} across runs fails closed")
    else:
        raise AssertionError(f"repeated {field} across runs fails closed")

shared_session = [
    _receipt(1, DIGEST_A),
    _receipt(2, DIGEST_A, provider_session_ids=["session-1"]),
    _receipt(3, DIGEST_A),
]
try:
    stab.validate_streak(
        shared_session, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
    )
except stab.StabilizationError:
    print("OK   repeated provider session across runs fails closed")
else:
    raise AssertionError("repeated provider session across runs fails closed")

unordered = [_receipt(2, DIGEST_A), _receipt(1, DIGEST_A), _receipt(3, DIGEST_A)]
try:
    stab.validate_streak(
        unordered, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
    )
except stab.StabilizationError:
    print("OK   out-of-order receipts fail closed")
else:
    raise AssertionError("out-of-order receipts fail closed")

malformed = [_receipt(1, "not-a-digest")]
try:
    stab.validate_streak(
        malformed, expected_digest=DIGEST_A, config=config, gate=gate, root=EVID_TMP
    )
except stab.StabilizationError:
    print("OK   malformed digest in a receipt fails closed")
else:
    raise AssertionError("malformed digest in a receipt fails closed")


# --- Release stop rule -----------------------------------------------------

two_classes = {
    "schema_version": 1,
    "defects": [
        _defect("D267-001", "worker-restart-divergence", "other"),
        _defect("D267-002", "ledger-reservation-race", "other"),
        _defect("D267-003", "callback-identity-drift", "callback-finalization"),
    ],
}
verdict = stab.release_stop(two_classes, config=config)
check("two new classes do not stop the release", verdict["stop"] is False)
check(
    "known-seam defects do not count toward the stop rule",
    verdict["new_class_count"] == 2,
)

three_classes = {
    "schema_version": 1,
    "defects": [
        _defect("D267-001", "worker-restart-divergence", "other"),
        _defect("D267-002", "ledger-reservation-race", "other"),
        _defect("D267-003", "projection-owner-leak", "other"),
        _defect("D267-004", "callback-identity-drift", "callback-finalization"),
    ],
}
verdict = stab.release_stop(three_classes, config=config)
check("three new classes stop the release", verdict["stop"] is True)
check("stop verdict names three classes", verdict["new_class_count"] == 3)

aliased = {
    "schema_version": 1,
    "defects": [
        _defect("D267-001", "worker-restart-divergence", "other"),
        _defect("D267-002", "ledger-reservation-race", "other"),
        _defect(
            "D267-003",
            "worker-restart-divergence",
            "other",
            alias_of="D267-001",
        ),
    ],
}
verdict = stab.release_stop(aliased, config=config)
check("symptom aliases do not inflate the stop count", verdict["stop"] is False)
check("aliased ledger counts two classes", verdict["new_class_count"] == 2)

dangling = {
    "schema_version": 1,
    "defects": [
        _defect("D267-001", "worker-restart-divergence", "other", alias_of="D267-404"),
    ],
}
try:
    stab.release_stop(dangling, config=config)
except stab.StabilizationError:
    print("OK   alias to an unknown defect fails closed")
else:
    raise AssertionError("alias to an unknown defect fails closed")

incomplete = {
    "schema_version": 1,
    "defects": [
        {
            "defect_id": "D267-001",
            "root_cause_class": "worker-restart-divergence",
            "seam": "other",
        }
    ],
}
try:
    stab.release_stop(incomplete, config=config)
except stab.StabilizationError:
    print("OK   defect record without a reproducer fails closed")
else:
    raise AssertionError("defect record without a reproducer fails closed")


# --- CLI validation entrypoints --------------------------------------------

with tempfile.TemporaryDirectory() as raw:
    streak_path = Path(raw) / "receipts.json"
    streak_path.write_text(
        json.dumps({"schema_version": 1, "receipts": good}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "streak",
            "--receipts",
            str(streak_path),
            "--expected-digest",
            DIGEST_A,
            "--config",
            str(CONFIG_PATH),
            "--root",
            str(EVID_TMP),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("streak CLI exits 0 on a complete streak", result.returncode == 0)
    check(
        "streak CLI reports completion",
        json.loads(result.stdout)["complete"] is True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "streak",
            "--receipts",
            str(streak_path),
            "--expected-digest",
            DIGEST_B,
            "--config",
            str(CONFIG_PATH),
            "--root",
            str(EVID_TMP),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("streak CLI exits 1 on an incomplete streak", result.returncode == 1)

    ledger_path = Path(raw) / "ledger.json"
    ledger_path.write_text(json.dumps(three_classes), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "ledger",
            "--ledger",
            str(ledger_path),
            "--config",
            str(CONFIG_PATH),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check("ledger CLI exits 2 when the stop rule fires", result.returncode == 2)
    check(
        "ledger CLI reports the stop verdict",
        json.loads(result.stdout)["stop"] is True,
    )

print("v267 stabilization tests passed")
