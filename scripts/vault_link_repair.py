#!/usr/bin/env python3
"""Plan one deterministic, optimistic unresolved-wikilink repair transaction."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from vault_schema import build_wiki_catalog, rewrite_wikilinks, validate_schema


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RepairPlan:
    repair_id: str
    paths: tuple[str, ...]
    link_count: int
    payload: dict


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validator_page(path: Path) -> bool:
    return not (
        path.stem.startswith("lint-report-")
        or path.stem.startswith("log-archive-")
        or path.name == "_index.md"
    )


def build_repair_plan(repo_root: Path = ROOT) -> RepairPlan | None:
    """Return one complete vault-write payload, or fail closed with ``None``."""

    repo_root = repo_root.resolve()
    wiki = repo_root / "wiki"
    failures = [issue for issue in validate_schema(repo_root) if issue.level == "fail"]
    if not failures or any(issue.code != "wikilink" for issue in failures):
        return None

    catalog = build_wiki_catalog(wiki)
    updates: list[dict[str, str]] = []
    link_count = 0
    blocked = False

    for path in sorted(wiki.rglob("*.md")):
        if not _validator_page(path):
            continue
        text = path.read_text(encoding="utf-8")
        changed_here = 0

        def replace(link):
            nonlocal blocked, changed_here
            if not link.target or catalog.resolves(link.target):
                return None
            candidates = catalog.repair_targets(link.target)
            if link.embed or len(candidates) != 1:
                blocked = True
                return None
            changed_here += 1
            return link.with_target(candidates[0])

        rewritten = rewrite_wikilinks(text, replace)
        if rewritten.malformed:
            blocked = True
        if changed_here:
            rel = str(path.relative_to(repo_root))
            updates.append(
                {
                    "op": "update",
                    "path": rel,
                    "content": rewritten.text,
                    "expected_sha256": _sha256(text),
                }
            )
            link_count += changed_here

    if blocked or not updates:
        return None

    material = "\n".join(
        f"{item['path']}:{item['expected_sha256']}:{_sha256(item['content'])}"
        for item in updates
    )
    repair_id = "wikilink-" + _sha256(material)[:16]
    payload = {
        "schema_version": 1,
        "request_id": repair_id,
        "actor": "stop-hook-link-repair",
        "pages": updates,
    }
    return RepairPlan(
        repair_id=repair_id,
        paths=tuple(item["path"] for item in updates),
        link_count=link_count,
        payload=payload,
    )


def main(argv: list[str]) -> int:
    if argv:
        print("vault-link-repair: no arguments are supported", file=sys.stderr)
        return 3
    plan = build_repair_plan(ROOT)
    if plan is None:
        print(json.dumps({"schema_version": 1, "status": "noop"}, sort_keys=True))
        return 0
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "planned",
                "repair_id": plan.repair_id,
                "paths": list(plan.paths),
                "link_count": plan.link_count,
                "payload": plan.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
