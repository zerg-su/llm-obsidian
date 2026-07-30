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
