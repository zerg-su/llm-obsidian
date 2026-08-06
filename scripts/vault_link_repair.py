#!/usr/bin/env python3
"""Plan one deterministic, optimistic unresolved-wikilink repair transaction."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vault_schema import build_wiki_catalog, rewrite_wikilinks, validate_schema


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RepairPlan:
    repair_id: str
    paths: tuple[str, ...]
    link_count: int
    payload: dict


class ExactBindingError(ValueError):
    """One explicit display-target/context-path binding is invalid."""


@dataclass(frozen=True)
class ExactBinding:
    display_target: str
    context_path: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "display_target": self.display_target,
            "context_path": self.context_path,
        }


def parse_exact_binding(value: object) -> ExactBinding:
    """Parse the strict optional binding carried by planner and writer."""

    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "display_target",
        "context_path",
    }:
        raise ExactBindingError("exact binding has an invalid shape")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ExactBindingError("exact binding schema_version must be 1")
    display_target = value.get("display_target")
    context_path = value.get("context_path")
    if (
        not isinstance(display_target, str)
        or display_target != display_target.strip()
        or not display_target
        or len(display_target) > 200
        or any(token in display_target for token in ("\0", "\n", "\r", "[", "]", "|", "#", "^"))
    ):
        raise ExactBindingError("exact binding display_target is invalid")
    if (
        not isinstance(context_path, str)
        or context_path != context_path.strip()
        or not context_path
        or len(context_path) > 500
    ):
        raise ExactBindingError("exact binding context_path is invalid")
    return ExactBinding(display_target, context_path)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validator_page(path: Path) -> bool:
    return not (
        path.stem.startswith("lint-report-")
        or path.stem.startswith("log-archive-")
        or path.name == "_index.md"
    )


def _bound_target_stem(repo_root: Path, binding: ExactBinding) -> str:
    relative = PurePosixPath(binding.context_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != binding.context_path
        or len(relative.parts) < 2
        or relative.parts[0] != "wiki"
        or ".." in relative.parts
        or any(part in {"", "."} for part in relative.parts)
        or relative.suffix != ".md"
    ):
        raise ExactBindingError(
            "exact binding context_path must be a repo-relative wiki/*.md path"
        )

    cursor = repo_root
    for part in relative.parts:
        if not cursor.is_dir() or part not in {child.name for child in cursor.iterdir()}:
            raise ExactBindingError("exact binding target does not exist exactly")
        cursor = cursor / part
        if cursor.is_symlink():
            raise ExactBindingError("exact binding target path cannot contain symlinks")
    target = cursor.resolve()
    wiki = (repo_root / "wiki").resolve()
    if target == wiki or wiki not in target.parents or not target.is_file():
        raise ExactBindingError("exact binding target must be a file inside wiki")

    stem_matches = [
        path.resolve()
        for path in (repo_root / "wiki").rglob("*.md")
        if path.stem.casefold() == relative.stem.casefold()
    ]
    if stem_matches != [target]:
        raise ExactBindingError("exact binding target filename stem is ambiguous")
    return relative.stem


def build_repair_plan(
    repo_root: Path = ROOT,
    *,
    exact_binding: ExactBinding | None = None,
) -> RepairPlan | None:
    """Return one complete vault-write payload, or fail closed with ``None``."""

    repo_root = repo_root.resolve()
    wiki = repo_root / "wiki"
    bound_stem = (
        _bound_target_stem(repo_root, exact_binding)
        if exact_binding is not None
        else ""
    )
    failures = [issue for issue in validate_schema(repo_root) if issue.level == "fail"]
    if not failures:
        if exact_binding is not None:
            raise ExactBindingError(
                "exact binding display target is not unresolved"
            )
        return None
    if any(issue.code != "wikilink" for issue in failures):
        if exact_binding is not None:
            raise ExactBindingError(
                "exact binding cannot repair unrelated vault failures"
            )
        return None

    catalog = build_wiki_catalog(wiki)
    updates: list[dict[str, str]] = []
    link_count = 0
    blocked = False
    bound_hits = 0

    for path in sorted(wiki.rglob("*.md")):
        if not _validator_page(path):
            continue
        text = path.read_text(encoding="utf-8")
        changed_here = 0

        def replace(link):
            nonlocal blocked, bound_hits, changed_here
            if not link.target or catalog.resolves(link.target):
                return None
            if exact_binding is not None:
                if link.target != exact_binding.display_target:
                    raise ExactBindingError(
                        "exact binding left an unresolved target unbound"
                    )
                if link.embed or link.suffix:
                    raise ExactBindingError(
                        "exact binding target must be one plain prose wikilink"
                    )
                bound_hits += 1
                if bound_hits > 1:
                    raise ExactBindingError(
                        "exact binding display target is duplicated"
                    )
                changed_here += 1
                return (
                    f"[[{bound_stem}|{exact_binding.display_target}]]"
                )
            candidates = catalog.repair_targets(link.target)
            if link.embed or len(candidates) != 1:
                blocked = True
                return None
            changed_here += 1
            return link.with_target(candidates[0])

        rewritten = rewrite_wikilinks(text, replace)
        if rewritten.malformed:
            if exact_binding is not None:
                raise ExactBindingError(
                    "exact binding cannot repair malformed wikilink prose"
                )
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

    if exact_binding is not None and bound_hits != 1:
        raise ExactBindingError(
            "exact binding display target must occur exactly once"
        )
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
    if exact_binding is not None:
        payload["exact_binding"] = exact_binding.payload()
    return RepairPlan(
        repair_id=repair_id,
        paths=tuple(item["path"] for item in updates),
        link_count=link_count,
        payload=payload,
    )


def main(argv: list[str]) -> int:
    exact_binding: ExactBinding | None = None
    if argv:
        if argv != ["--exact-binding"]:
            print(
                "vault-link-repair: only --exact-binding is supported",
                file=sys.stderr,
            )
            return 3
        try:
            exact_binding = parse_exact_binding(json.load(sys.stdin))
        except (ExactBindingError, json.JSONDecodeError) as exc:
            print(f"vault-link-repair: invalid exact binding: {exc}", file=sys.stderr)
            return 3
    try:
        plan = build_repair_plan(ROOT, exact_binding=exact_binding)
    except ExactBindingError as exc:
        print(f"vault-link-repair: exact binding rejected: {exc}", file=sys.stderr)
        return 3
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
