#!/usr/bin/env python3
"""Hermetic regression tests for strict vault frontmatter and link schema."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from vault_schema import (  # noqa: E402
    FrontmatterError,
    neutralize_unresolved_wikilinks,
    parse_frontmatter,
    validate_schema,
)


passed = 0


def ok(name: str) -> None:
    global passed
    passed += 1
    print(f"OK   {name}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    ok(name)


def assert_raises(name: str, block: str, needle: str) -> None:
    try:
        parse_frontmatter(block)
    except FrontmatterError as exc:
        assert_true(name, needle in str(exc))
        return
    raise AssertionError(f"{name}: FrontmatterError not raised")


def page(title: str, body: str = "", address: str = "c-000001") -> str:
    return f'''---
type: concept
title: "{title}"
address: {address}
status: developing
created: 2026-07-09
updated: 2026-07-09
tags: [test]
sessions: []
---

# {title}

{body}
'''


fm = parse_frontmatter(
    """type: concept
tags:
  - alpha
sessions:
  - id: session-1
    date: 2026-07-09
related: [\"[[One]]\", '[[Two]]']"""
)
assert_true("strict parser scalar", fm["type"] == "concept")
assert_true("strict parser block list", fm["tags"] == ["alpha"])
assert_true("strict parser nested list", fm["sessions"][0]["id"] == "session-1")
assert_true("strict parser flow list", fm["related"] == ["[[One]]", "[[Two]]"])
assert_raises("joined fields rejected", "weight: 27tags:", "colon")
assert_raises("duplicate keys rejected", "type: a\ntype: b", "duplicate")
assert_raises("unexpected indentation rejected", "type: a\n   stray: b", "indent")

with tempfile.TemporaryDirectory(prefix="vault-schema-") as tmp:
    root = Path(tmp)
    wiki = root / "wiki"
    meta = root / ".vault-meta"
    (wiki / "concepts").mkdir(parents=True)
    meta.mkdir()
    (wiki / "concepts" / "One.md").write_text(
        page(
            "One",
            "[[Alias Two]] [[Map]] [[dashboard]] [[Two\\|label]]\n"
            "```md\n[[Ignored Code Link]]\n```",
        ),
        encoding="utf-8",
    )
    two = page("Two", address="c-000002").replace(
        "tags: [test]", 'aliases: ["Alias Two"]\ntags: [test]'
    )
    (wiki / "concepts" / "Two.md").write_text(two, encoding="utf-8")
    (wiki / "Map.canvas").write_text("{}\n", encoding="utf-8")
    (wiki / "dashboard.base").write_text("filters: {}\n", encoding="utf-8")
    (meta / "address-counter.txt").write_text("3\n", encoding="utf-8")
    (meta / "address-map.tsv").write_text(
        "c-000001\twiki/concepts/One.md\n"
        "c-000002\twiki/concepts/Two.md\n",
        encoding="utf-8",
    )
    issues = validate_schema(root)
    assert_true("canvas/base/alias/escaped links resolve", not [i for i in issues if i.level == "fail"])
    cleaned, neutralized = neutralize_unresolved_wikilinks(
        wiki,
        "Keep [[Two]] and `[[Inline Example]]`; strip [[Missing Plan|the plan]].\n"
        "```md\n[[Fenced Example]]\n```\n",
    )
    assert_true(
        "summary neutralizer preserves valid links and code",
        "[[Two]]" in cleaned
        and "`[[Inline Example]]`" in cleaned
        and "[[Fenced Example]]" in cleaned,
    )
    assert_true(
        "summary neutralizer de-links only unresolved prose",
        "strip the plan" in cleaned
        and "[[Missing Plan" not in cleaned
        and neutralized == ["Missing Plan"],
    )

    (meta / "address-counter.txt").write_text("5\n", encoding="utf-8")
    issues = validate_schema(root)
    assert_true("counter-ahead gap does not fail", not [i for i in issues if i.level == "fail"])
    assert_true("counter-ahead gap warns", any(i.level == "warn" and i.code == "address" for i in issues))

    (meta / "address-counter.txt").write_text("2\n", encoding="utf-8")
    issues = validate_schema(root)
    assert_true("counter behind highest address fails", any(i.level == "fail" and i.code == "address" for i in issues))
    (meta / "address-counter.txt").write_text("3\n", encoding="utf-8")

    one_path = wiki / "concepts" / "One.md"
    one_path.write_text(page("One", "[[Missing Page]]"), encoding="utf-8")
    issues = validate_schema(root)
    assert_true("dead link fails", any(i.code == "wikilink" for i in issues))

    reports = wiki / "meta" / "reports"
    reports.mkdir(parents=True)
    report_path = reports / "lint-report-2026-07-19.md"
    report_path.write_text(
        page("Lint Report", "Validator evidence: [[Missing Page]]", address="c-000003"),
        encoding="utf-8",
    )
    (meta / "address-counter.txt").write_text("4\n", encoding="utf-8")
    (meta / "address-map.tsv").write_text(
        "c-000001\twiki/concepts/One.md\n"
        "c-000002\twiki/concepts/Two.md\n"
        "c-000003\twiki/meta/reports/lint-report-2026-07-19.md\n",
        encoding="utf-8",
    )
    issues = validate_schema(root)
    assert_true(
        "lint report source links are excluded",
        not [i for i in issues if i.code == "wikilink" and "lint-report-" in i.message],
    )
    report_path.unlink()
    (meta / "address-counter.txt").write_text("3\n", encoding="utf-8")
    (meta / "address-map.tsv").write_text(
        "c-000001\twiki/concepts/One.md\n"
        "c-000002\twiki/concepts/Two.md\n",
        encoding="utf-8",
    )

    one_path.write_text(page("One"), encoding="utf-8")
    (wiki / "other").mkdir()
    (wiki / "other" / "one.md").write_text(
        page("Duplicate One", address="c-000003"), encoding="utf-8"
    )
    (meta / "address-counter.txt").write_text("4\n", encoding="utf-8")
    (meta / "address-map.tsv").write_text(
        "c-000001\twiki/concepts/One.md\n"
        "c-000002\twiki/concepts/Two.md\n"
        "c-000003\twiki/other/one.md\n",
        encoding="utf-8",
    )
    issues = validate_schema(root)
    assert_true("case-insensitive duplicate filename fails", any(i.code == "filename" for i in issues))

live_fails = [issue for issue in validate_schema(ROOT) if issue.level == "fail"]
assert_true("live vault strict schema", not live_fails)
print(f"\nPassed: {passed}")
