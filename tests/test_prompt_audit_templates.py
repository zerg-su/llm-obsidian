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

from vault_schema import parse_frontmatter, split_frontmatter


def render_template(skill: str, section: str, replacements: dict[str, str]) -> str:
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    section_text = text.split(section, 1)[1]
    match = re.search(
        r"(?m)^(?P<fence>`{3,4})(?:markdown|yaml)\n(?P<page>---\n[\s\S]*?)^(?P=fence)$",
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


def supporting_page(title: str, address: str, kind: str = "concept") -> str:
    provenance = ""
    if kind == "source":
        provenance = (
            "source_class: internal\nverified_at: 2026-09-28\n"
            f"content_sha256: {'a' * 64}\n"
        )
    return (
        f'---\ntype: {kind}\ntitle: "{title}"\naddress: {address}\n'
        "status: developing\ncreated: 2026-09-28\nupdated: 2026-09-28\n"
        f"tags: [test]\nsessions: [prompt-audit-test]\n{provenance}---\n\n# {title}\n"
    )


class QueryTemplateContracts(unittest.TestCase):
    def test_u_t3_query_answer(self) -> None:
        page = render_template("wiki-query", "## Filing Answers Back", {
            "YYYY-MM-DD": "2026-09-28",
            "<domain>": "testing",
            "<from ./scripts/allocate-address.sh>": "c-000001",
            "<./scripts/current-session-id.sh>": "prompt-audit-test",
        })
        code, output = validate_pages({
            "wiki/questions/Answer.md": page + "\n# Answer\nA supported answer.\n",
            "wiki/concepts/Page referenced in answer.md": supporting_page("Page referenced in answer", "c-000002"),
            "wiki/sources/Relevant Source.md": supporting_page("Relevant Source", "c-000003", "source"),
        })
        self.assertEqual(code, 0, output)
        self.assertIn("0 FAIL, 0 WARN", output)
        print(f"U-T3 query answer: {output.strip()} [explicit TMPDIR root]")

    def test_u_t3_query_sub_index(self) -> None:
        page = render_template("wiki-query", "## Domain Sub-Index Format", {
            "YYYY-MM-DD": "2026-09-28",
            "<SESSION_ID>": "prompt-audit-test",
        })
        code, output = validate_pages({"wiki/entities/_index.md": page})
        self.assertEqual(code, 0, output)
        self.assertIn("0 FAIL, 0 WARN", output)
        print(f"U-T3 query sub-index: {output.strip()} [explicit TMPDIR root]")


def render_save_template(kind: str) -> str:
    replacements = {
        "<synthesis|concept|source|decision|session|service|incident|runbook|question|goal|...>": kind,
        "c-NNNNNN": "c-000001",
        "YYYY-MM-DD": "2026-09-28",
        "<relevant-tag>": "testing",
        "<SESSION_ID>": "prompt-audit-test",
        "official|internal|third-party": "internal",
        "<lowercase sha256 of the source content>": "a" * 64,
    }
    page = render_template("save", "## Frontmatter Template", replacements)
    frontmatter = split_frontmatter(page)
    assert frontmatter is not None
    text = (ROOT / "skills/save/SKILL.md").read_text(encoding="utf-8")
    section = text.split("## Frontmatter Template", 1)[1].split("## Writing Style", 1)[0]
    for match in re.finditer(r"(?m)^(For [\s\S]*?)\n```yaml\n([\s\S]*?)\n```", section):
        if f"`{kind}`" in match.group(1):
            additions = match.group(2)
            for placeholder, value in replacements.items():
                additions = additions.replace(placeholder, value)
            # Override documented scalar fields without parsing, reserializing,
            # stripping comments, or repairing the template before validation.
            for line in additions.splitlines():
                field = re.match(r"([a-z_]+):", line)
                if field:
                    pattern = rf"(?m)^{field.group(1)}:.*$"
                    if re.search(pattern, frontmatter):
                        frontmatter = re.sub(pattern, lambda _: line, frontmatter, count=1)
                        continue
                frontmatter += "\n" + line
    return f"---\n{frontmatter}\n---\n\n# Saved Note\nSupported content.\n"


class SaveTemplateContracts(unittest.TestCase):
    def test_save_yaml_has_no_comments(self) -> None:
        text = (ROOT / "skills/save/SKILL.md").read_text(encoding="utf-8")
        section = text.split("## Frontmatter Template", 1)[1].split("## Writing Style", 1)[0]
        for index, block in enumerate(re.findall(r"```yaml\n([\s\S]*?)\n```", section)):
            with self.subTest(block=index):
                self.assertNotIn("#", block, "YAML explanations belong in adjacent prose")

    def test_d_u6k_save_templates(self) -> None:
        for kind, folder in (("question", "questions"), ("synthesis", "questions"), ("source", "sources")):
            with self.subTest(kind=kind):
                page = render_save_template(kind)
                code, output = validate_pages({
                    f"wiki/{folder}/Saved Note.md": page,
                    "wiki/concepts/Any Wiki Page Mentioned.md": supporting_page("Any Wiki Page Mentioned", "c-000002"),
                    "wiki/sources/Relevant Source.md": supporting_page("Relevant Source", "c-000003", "source"),
                })
                self.assertEqual(code, 0, output)
                self.assertIn("0 FAIL, 0 WARN", output)
                fields = parse_frontmatter(split_frontmatter(page))
                self.assertEqual(fields["sources"], ["[[Relevant Source]]"])
                self.assertEqual(fields["source_path"], ".raw/source-if-applicable.md")
                if kind in {"question", "synthesis"}:
                    self.assertIn(fields["status"], {"open", "answered"})
                print(f"D-U6k / U-T3 save {kind}: {output.strip()} [explicit TMPDIR root]")


if __name__ == "__main__":
    unittest.main()
