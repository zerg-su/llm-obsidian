#!/usr/bin/env python3
"""U-T3: render skill templates and run the real validator on a TMPDIR vault."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def render_template(skill: str, section: str, replacements: dict[str, str]) -> str:
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    section_text = text.split(section, 1)[1]
    match = re.search(
        r"(?m)^(?P<fence>`{3,4})markdown\n(?P<page>---\n[\s\S]*?)^(?P=fence)$",
        section_text,
    )
    if match is None:
        raise AssertionError(f"Missing page template: {skill} / {section}")
    page = match.group("page")
    for placeholder, value in replacements.items():
        page = page.replace(placeholder, value)
    return page


def validate_pages(pages: dict[str, str]) -> tuple[int, str]:
    """Set every validator root explicitly; never validate the actual vault."""
    scratch_parent = os.environ.get("TMPDIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="prompt-audit-templates-", dir=scratch_parent) as tmp:
        root = Path(tmp).resolve()
        assert not root.is_relative_to(ROOT.resolve()), "Templates must live under TMPDIR"
        meta = root / ".vault-meta"
        meta.mkdir()
        addresses = []
        for relative, content in pages.items():
            path = root / relative
            assert path.resolve().is_relative_to(root / "wiki")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            address = re.search(r"(?m)^address: (c-\d{6})$", content)
            if address:
                addresses.append((address.group(1), relative))
        highest = max((int(address[2:]) for address, _ in addresses), default=0)
        (meta / "address-counter.txt").write_text(f"{highest + 1}\n", encoding="utf-8")
        (meta / "address-map.tsv").write_text(
            "".join(f"{address}\t{path}\n" for address, path in sorted(addresses)),
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location("template_validator", ROOT / "scripts/validate-vault.py")
        assert spec and spec.loader
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        validator.REPO_ROOT = root
        validator.WIKI = root / "wiki"
        validator.HOT_FILE = validator.WIKI / "hot.md"
        validator.LOG_FILE = validator.WIKI / "log.md"
        validator.INDEX_FILE = validator.WIKI / "index.md"
        validator.GUIDE_FILE = validator.WIKI / "meta/daily-pipeline-guide.md"
        validator.FOLD_SCRIPT = root / "scripts/fold-log.py"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validator.main(["--checks", "schema,questions"])
        return code, output.getvalue()


class LintTemplateContracts(unittest.TestCase):
    def test_u_t3_lint_templates(self) -> None:
        replacements = {"YYYY-MM-DD": "2026-09-28", "<SESSION_ID>": "prompt-audit-test"}
        for section, path in (
            ("## Lint Report Format", "wiki/meta/reports/lint-report-2026-09-28.md"),
            ("## Dataview Dashboard", "wiki/meta/dashboard.md"),
        ):
            with self.subTest(template=section):
                page = render_template("wiki-lint", section, replacements)
                code, output = validate_pages({path: page})
                self.assertEqual(code, 0, output)
                self.assertIn("0 FAIL, 0 WARN", output)
                print(f"U-T3 {section}: {output.strip()} [explicit TMPDIR root]")


if __name__ == "__main__":
    unittest.main()
