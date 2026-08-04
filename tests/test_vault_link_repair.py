#!/usr/bin/env python3
"""Behavior tests for deterministic one-shot wikilink repair planning."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from vault_link_repair import build_repair_plan  # noqa: E402
from vault_write_contract import ConflictError  # noqa: E402
from vault_write_mutations import MutationPlanner  # noqa: E402


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"OK   {name}")


def page(title: str, body: str = "", *, heading: str | None = None) -> str:
    return f'''---
type: concept
title: "{title}"
status: developing
created: 2026-01-01
updated: 2026-01-01
tags: [test]
sessions: []
---

# {heading or title}

{body}
'''


def fixture(root: Path) -> Path:
    wiki = root / "wiki"
    meta = root / ".vault-meta"
    (wiki / "concepts").mkdir(parents=True)
    meta.mkdir()
    (meta / "address-counter.txt").write_text("1\n", encoding="utf-8")
    (meta / "address-map.tsv").write_text("", encoding="utf-8")
    return wiki


with tempfile.TemporaryDirectory(prefix="vault-link-repair-") as raw:
    root = Path(raw)
    wiki = fixture(root)
    (wiki / "concepts" / "alpha.md").write_text(
        page("Alpha Canonical"), encoding="utf-8"
    )
    (wiki / "concepts" / "beta.md").write_text(
        page("Beta Title", heading="Beta Heading"), encoding="utf-8"
    )
    source = wiki / "source.md"
    source.write_text(
        page(
            "Source",
            "[[Alpha Canonical|alpha alias]] [[Beta Heading#Details]]\n"
            "[[Alpha Canonical\\|escaped label]]\n"
            "`[[Alpha Canonical]]`\n"
            "``[[Alpha Canonical]]``\n"
            "```one ` two `` [[Beta Heading]]```\n"
            "```md\n[[Beta Heading]]\n```\n"
            "~~~~md\n[[Alpha Canonical]]\n~~~~\n"
            "````md\n```\n[[Beta Heading]]\n```\n````\n",
        ),
        encoding="utf-8",
    )

    plan = build_repair_plan(root)
    check("unique title and H1 produce one source update", plan is not None and plan.link_count == 3)
    assert plan is not None
    check("repair payload uses one optimistic page update", len(plan.payload["pages"]) == 1)
    rendered = plan.payload["pages"][0]["content"]
    check(
        "alias anchor and escaped pipe are preserved",
        "[[alpha|alpha alias]]" in rendered
        and "[[beta#Details]]" in rendered
        and "[[alpha\\|escaped label]]" in rendered,
    )
    check(
        "inline and fenced examples remain byte-stable",
        "`[[Alpha Canonical]]`" in rendered
        and "``[[Alpha Canonical]]``" in rendered
        and "```one ` two `` [[Beta Heading]]```" in rendered
        and "```md\n[[Beta Heading]]\n```" in rendered
        and "~~~~md\n[[Alpha Canonical]]\n~~~~" in rendered
        and "````md\n```\n[[Beta Heading]]\n```\n````" in rendered,
    )
    check("repair report paths are bounded repo-relative paths", plan.paths == ("wiki/source.md",))
    check("repair identity is deterministic", build_repair_plan(root).repair_id == plan.repair_id)

    source.write_text(source.read_text(encoding="utf-8") + "\nconcurrent\n", encoding="utf-8")
    try:
        MutationPlanner(root).plan(plan.payload, "2026-08-04")
    except ConflictError:
        check("concurrent source change is rejected by sole writer", True)
    else:
        raise AssertionError("concurrent source change is rejected by sole writer")


def no_plan(name: str, source_body: str, targets: list[tuple[str, str, str | None]]) -> None:
    with tempfile.TemporaryDirectory(prefix="vault-link-repair-control-") as raw:
        root = Path(raw)
        wiki = fixture(root)
        for filename, title, heading in targets:
            (wiki / "concepts" / filename).write_text(
                page(title, heading=heading), encoding="utf-8"
            )
        (wiki / "source.md").write_text(page("Source", source_body), encoding="utf-8")
        check(name, build_repair_plan(root) is None)


no_plan(
    "ambiguous title has zero repair plan",
    "[[Shared Title]]",
    [("one.md", "Shared Title", None), ("two.md", "Shared Title", None)],
)
no_plan(
    "missing target has zero repair plan",
    "[[Missing Target]]",
    [("one.md", "One", None)],
)
no_plan(
    "unsupported embed has zero repair plan",
    "![[Alpha Canonical]]",
    [("alpha.md", "Alpha Canonical", None)],
)
no_plan(
    "malformed prose link has zero repair plan",
    "[[Alpha Canonical",
    [("alpha.md", "Alpha Canonical", None)],
)
no_plan(
    "already valid filename needs no mutation",
    "[[alpha]]",
    [("alpha.md", "Alpha Canonical", None)],
)
no_plan(
    "double-backtick code span needs no mutation",
    "``[[Alpha Canonical]]``",
    [("alpha.md", "Alpha Canonical", None)],
)
no_plan(
    "long code span containing shorter runs needs no mutation",
    "```one ` two `` [[Alpha Canonical]]```",
    [("alpha.md", "Alpha Canonical", None)],
)

with tempfile.TemporaryDirectory(prefix="vault-link-repair-fenced-h1-") as raw:
    root = Path(raw)
    wiki = fixture(root)
    target = page("Target Title", heading="Real Heading").replace(
        "# Real Heading",
        "````md\n# Fenced Heading\n````\n\n# Real Heading",
    )
    (wiki / "concepts" / "target.md").write_text(target, encoding="utf-8")
    (wiki / "source.md").write_text(
        page("Source", "[[Fenced Heading]]"), encoding="utf-8"
    )
    check("fenced H1 is not a repair candidate", build_repair_plan(root) is None)

print(f"\nPassed: {passed}")
