#!/usr/bin/env python3
"""Behavior tests for deterministic one-shot wikilink repair planning."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from vault_link_repair import (  # noqa: E402
    ExactBindingError,
    build_repair_plan,
    parse_exact_binding,
)
from vault_write_contract import ConflictError, PayloadError  # noqa: E402
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


with tempfile.TemporaryDirectory(prefix="vault-link-repair-log-") as raw:
    root = Path(raw)
    wiki = fixture(root)
    (wiki / "concepts" / "canonical.md").write_text(
        page("Canonical Display"), encoding="utf-8"
    )
    log = wiki / "log.md"
    log.write_text(
        page("Log", "[[Canonical Display]]"), encoding="utf-8"
    )
    plan = build_repair_plan(root)
    assert plan is not None
    check(
        "canonical planner includes a writer-owned log repair",
        plan.paths == ("wiki/log.md",),
    )
    applied = MutationPlanner(root).plan(plan.payload, "2026-08-05")
    check(
        "sole writer accepts its exact deterministic log repair",
        applied.writes == [(log.resolve(), plan.payload["pages"][0]["content"])],
    )
    forged = {
        **plan.payload,
        "pages": [
            {
                **plan.payload["pages"][0],
                "content": plan.payload["pages"][0]["content"] + "\nforged\n",
            }
        ],
    }
    try:
        MutationPlanner(root).plan(forged, "2026-08-05")
    except PayloadError:
        check("forged writer-owned repair remains rejected", True)
    else:
        raise AssertionError("forged writer-owned repair remains rejected")


def binding(display_target: str, context_path: str):
    return parse_exact_binding(
        {
            "schema_version": 1,
            "display_target": display_target,
            "context_path": context_path,
        }
    )


def binding_rejected(name: str, action) -> None:
    try:
        action()
    except ExactBindingError:
        check(name, True)
    else:
        raise AssertionError(name)


with tempfile.TemporaryDirectory(prefix="vault-link-repair-binding-") as raw:
    root = Path(raw)
    wiki = fixture(root)
    (wiki / "plans").mkdir()
    target = wiki / "plans" / "2026-exact-context.md"
    target.write_text(
        page("Different durable title", heading="Different durable heading"),
        encoding="utf-8",
    )
    log = wiki / "log.md"
    log.write_text(
        page("Log", "[[Human display target]]"), encoding="utf-8"
    )
    exact = binding(
        "Human display target",
        "wiki/plans/2026-exact-context.md",
    )
    check(
        "title/stem mismatch remains noop without an exact binding",
        build_repair_plan(root) is None,
    )
    exact_plan = build_repair_plan(root, exact_binding=exact)
    assert exact_plan is not None
    check(
        "exact binding derives only the filename stem and display alias",
        exact_plan.paths == ("wiki/log.md",)
        and "[[2026-exact-context|Human display target]]"
        in exact_plan.payload["pages"][0]["content"]
        and exact_plan.payload["exact_binding"]
        == {
            "schema_version": 1,
            "display_target": "Human display target",
            "context_path": "wiki/plans/2026-exact-context.md",
        },
    )
    applied = MutationPlanner(root).plan(exact_plan.payload, "2026-08-05")
    check(
        "sole writer accepts only the byte-identical bound plan",
        applied.writes
        == [(log.resolve(), exact_plan.payload["pages"][0]["content"])],
    )

    binding_rejected(
        "exact binding rejects path traversal",
        lambda: build_repair_plan(
            root,
            exact_binding=binding(
                "Human display target", "wiki/plans/../escape.md"
            ),
        ),
    )
    binding_rejected(
        "exact binding rejects non-wiki paths",
        lambda: build_repair_plan(
            root,
            exact_binding=binding(
                "Human display target", "docs/2026-exact-context.md"
            ),
        ),
    )
    binding_rejected(
        "exact binding rejects non-Markdown paths",
        lambda: build_repair_plan(
            root,
            exact_binding=binding(
                "Human display target", "wiki/plans/2026-exact-context.txt"
            ),
        ),
    )
    binding_rejected(
        "exact binding rejects a missing exact target",
        lambda: build_repair_plan(
            root,
            exact_binding=binding(
                "Human display target", "wiki/plans/missing.md"
            ),
        ),
    )
    binding_rejected(
        "exact binding rejects non-exact path casing",
        lambda: build_repair_plan(
            root,
            exact_binding=binding(
                "Human display target",
                "wiki/plans/2026-Exact-Context.md",
            ),
        ),
    )
    symlink = wiki / "plans" / "linked-context.md"
    symlink.symlink_to(target)
    binding_rejected(
        "exact binding rejects a symlink target",
        lambda: build_repair_plan(
            root,
            exact_binding=binding(
                "Human display target", "wiki/plans/linked-context.md"
            ),
        ),
    )
    symlink.unlink()

    log.write_text(
        page("Log", "[[Human display target]] [[Human display target]]"),
        encoding="utf-8",
    )
    binding_rejected(
        "exact binding rejects duplicate unresolved display targets",
        lambda: build_repair_plan(root, exact_binding=exact),
    )
    log.write_text(
        page("Log", "[[Human display target]] [[Unbound missing target]]"),
        encoding="utf-8",
    )
    binding_rejected(
        "exact binding rejects any unbound repair",
        lambda: build_repair_plan(root, exact_binding=exact),
    )
    log.write_text(
        page("Log", "[[Human display target]]"), encoding="utf-8"
    )
    duplicate = wiki / "concepts" / "2026-exact-context.md"
    duplicate.write_text(page("Duplicate stem"), encoding="utf-8")
    binding_rejected(
        "exact binding rejects an ambiguous filename stem",
        lambda: build_repair_plan(root, exact_binding=exact),
    )
    duplicate.unlink()

    stable_plan = build_repair_plan(root, exact_binding=exact)
    assert stable_plan is not None
    log.write_text(
        log.read_text(encoding="utf-8") + "\nconcurrent\n",
        encoding="utf-8",
    )
    try:
        MutationPlanner(root).plan(stable_plan.payload, "2026-08-05")
    except (ConflictError, PayloadError):
        check("bound repair rejects source/hash drift", True)
    else:
        raise AssertionError("bound repair rejects source/hash drift")


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
