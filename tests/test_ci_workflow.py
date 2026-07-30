#!/usr/bin/env python3
"""Static invariants for the shallow macOS GitHub Actions checkout."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

assert "fetch-depth: 2" in workflow
assert "git diff --check HEAD^ HEAD" in workflow
assert "git show --check --oneline --no-renames HEAD" not in workflow

print("OK   CI fetches the release/merge parent")
print("OK   whitespace gate checks only introduced lines")

for snapshot in ("mattpocock-skills", "obra-superpowers"):
    assert f"references/upstream-skills/{snapshot}/** -whitespace" in attributes

print("OK   byte-exact upstream snapshots retain their original whitespace")
