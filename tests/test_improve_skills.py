#!/usr/bin/env python3
"""Tests for the deterministic improve-skills structural audit."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "improve-skills" / "scripts" / "audit_skills.py"
spec = importlib.util.spec_from_file_location("improve_skills_audit_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

VERIFY_SCRIPT = ROOT / "references" / "upstream-skills" / "verify_snapshots.py"
verify_spec = importlib.util.spec_from_file_location("snapshot_verifier_test", VERIFY_SCRIPT)
verify_module = importlib.util.module_from_spec(verify_spec)
assert verify_spec.loader is not None
sys.modules[verify_spec.name] = verify_module
verify_spec.loader.exec_module(verify_module)


rows = module.audit_directory(ROOT / "skills")
assert len(rows) == len(list((ROOT / "skills").glob("*/SKILL.md")))
assert not [finding for row in rows for finding in row["findings"]], rows
print("OK   repository skill inventory passes structural audit")


valid_verdicts = {
    "schema_version": 1,
    "records": [
        {
            "skill": "fixture",
            "verdict": "fix",
            "passes": {
                "invocation": "pass",
                "hierarchy": "pass",
                "steering": "F-001",
                "pruning": "pass",
                "goal_preservation": "F-001",
            },
            "overall_input": "The approved task and its Outcome Contract.",
            "overall_outcome": "Deliver the declared observable result.",
            "local_subgoal": "Make this skill's bounded step predictable.",
            "completion_proxies": ["The focused test is green."],
            "required_outcome_evidence": [
                "The declared success-evidence item is independently established."
            ],
            "evidence": "skills/fixture/SKILL.md:10",
            "change": "Sharpen the completion boundary.",
            "behavior_proof": "Invocation and authority remain unchanged.",
        }
    ],
}
assert module.validate_verdict_records(valid_verdicts, ("fixture",)) == valid_verdicts

missing_goal_pass = json.loads(json.dumps(valid_verdicts))
del missing_goal_pass["records"][0]["passes"]["goal_preservation"]
try:
    module.validate_verdict_records(missing_goal_pass, ("fixture",))
except ValueError as exc:
    assert "five named passes" in str(exc)
else:
    raise AssertionError("missing goal-preservation pass was accepted")

missing_skill = json.loads(json.dumps(valid_verdicts))
try:
    module.validate_verdict_records(missing_skill, ("fixture", "other"))
except ValueError as exc:
    assert "same skill names" in str(exc)
else:
    raise AssertionError("incomplete verdict inventory was accepted")
print("OK   verdict records require exhaustive goal-preservation evidence")


with tempfile.TemporaryDirectory(prefix="improve-skills-verdicts.") as raw:
    skills = Path(raw) / "skills"
    fixture = skills / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "SKILL.md").write_text(
        "---\nname: fixture\ndescription: Verdict fixture.\n---\n\n# Fixture\n",
        encoding="utf-8",
    )
    verdict_path = Path(raw) / "verdicts.json"
    verdict_path.write_text(json.dumps(valid_verdicts), encoding="utf-8")
    command = [
        sys.executable,
        str(SCRIPT),
        "--skills-dir",
        str(skills),
        "--verdicts",
        str(verdict_path),
        "--strict",
        "--json",
    ]
    validated = subprocess.run(command, check=False, capture_output=True, text=True)
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["verdicts_validated"] is True

    verdict_path.write_text(json.dumps(missing_goal_pass), encoding="utf-8")
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "five named passes" in json.loads(rejected.stdout)["verdicts_error"]
    print("OK   CLI validates the same exhaustive verdict record contract")


with tempfile.TemporaryDirectory(prefix="improve-skills-test.") as raw:
    skills = Path(raw) / "skills"
    good = skills / "good"
    good.mkdir(parents=True)
    (good / "references").mkdir()
    (good / "agents").mkdir()
    (good / "references" / "details.md").write_text("# Details\n", encoding="utf-8")
    (good / "agents" / "openai.yaml").write_text(
        "policy:\n  allow_implicit_invocation: false\n",
        encoding="utf-8",
    )
    (good / "SKILL.md").write_text(
        "---\n"
        "name: good\n"
        "description: Explicit fixture.\n"
        "disable-model-invocation: true\n"
        "---\n\n"
        "# Good\n\nRead [details](references/details.md).\n",
        encoding="utf-8",
    )

    bad = skills / "bad"
    bad.mkdir()
    (bad / "agents").mkdir()
    (bad / "agents" / "openai.yaml").write_text(
        "policy:\n  allow_implicit_invocation: false\n",
        encoding="utf-8",
    )
    (bad / "SKILL.md").write_text(
        "---\n"
        "name: wrong-name\n"
        "description: Broken fixture.\n"
        "---\n\n"
        "# Bad\n\nTODO: read [missing](references/missing.md).\n",
        encoding="utf-8",
    )

    fixture_rows = {row["skill"]: row for row in module.audit_directory(skills)}
    assert fixture_rows["good"]["findings"] == []
    codes = {finding["code"] for finding in fixture_rows["bad"]["findings"]}
    assert codes == {"name-mismatch", "invocation-parity", "placeholder", "broken-link"}, codes
    print("OK   name, invocation, placeholder, and reference drift detected")


snapshot_check = subprocess.run(
    [sys.executable, str(VERIFY_SCRIPT)],
    cwd=ROOT,
    check=False,
    capture_output=True,
    text=True,
)
assert snapshot_check.returncode == 0, snapshot_check.stderr
assert "All 2 upstream snapshots match manifest.json." in snapshot_check.stdout
print("OK   pinned upstream snapshots match their reproducible manifest")


with tempfile.TemporaryDirectory(prefix="snapshot-verifier-test.") as raw:
    repo = Path(raw)
    snapshot_root = repo / "references" / "upstream-skills"
    snapshot = snapshot_root / "tiny"
    snapshot.mkdir(parents=True)
    payload = snapshot / "payload.txt"
    payload.write_text("pinned\n", encoding="utf-8")
    files, byte_count, digest = verify_module.inspect_tree(snapshot)
    manifest = {
        "schema_version": 1,
        "sources": {
            "tiny": {
                "local_path": "references/upstream-skills/tiny",
                "files": files,
                "bytes": byte_count,
                "tree_sha256": digest,
            }
        },
    }
    manifest_path = snapshot_root / "manifest.json"

    def write_manifest() -> None:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_fixture() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--manifest", str(manifest_path)],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )

    write_manifest()
    assert run_fixture().returncode == 0

    payload.write_text("tampered\n", encoding="utf-8")
    tampered = run_fixture()
    assert tampered.returncode == 1
    assert "FAIL tiny: bytes=" in tampered.stderr
    assert "tree_sha256=" in tampered.stderr
    payload.write_text("pinned\n", encoding="utf-8")

    link = snapshot / "linked.txt"
    link.symlink_to(payload.name)
    linked = run_fixture()
    assert linked.returncode == 1
    assert "unsupported snapshot entries: linked.txt" in linked.stderr
    link.unlink()

    manifest["sources"]["tiny"]["local_path"] = "outside"
    write_manifest()
    escaped = run_fixture()
    assert escaped.returncode == 1
    assert "local_path escapes" in escaped.stderr

    manifest["sources"]["tiny"]["local_path"] = "references/upstream-skills/missing"
    write_manifest()
    missing = run_fixture()
    assert missing.returncode == 1
    assert "snapshot directory is missing or not a direct child" in missing.stderr
    print("OK   snapshot verifier fails closed on tamper, symlink, escape, and missing trees")


print("\nAll improve-skills tests passed.")
