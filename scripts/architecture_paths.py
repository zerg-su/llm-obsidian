#!/usr/bin/env python3
"""Fail-closed project artifact naming, collision, and containment checks."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from vault_schema import (
    CONTENT_EXTENSIONS,
    FrontmatterError,
    build_wiki_catalog,
    parse_frontmatter,
    split_document,
    split_frontmatter,
)


PROJECT_KEY_RX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
FORBIDDEN_TITLE_CHARACTERS = frozenset("/\\:|#^[]")
ROLE_DIRECTORIES = {
    "hub": "",
    "vision": "",
    "architecture": "",
    "design": "design",
    "spec": "specs",
    "contract": "contracts",
    "work-graph": "work",
    "work-item": "work",
}


class ArchitecturePathError(ValueError):
    """A project identity or destination is outside the artifact contract."""


def collision_key(value: str) -> str:
    """Return the vault-wide identity key required by the artifact contract."""

    return unicodedata.normalize("NFC", value).casefold()


def validate_project_key(project_key: str) -> str:
    """Validate a lowercase ASCII project directory key."""

    if not isinstance(project_key, str) or not PROJECT_KEY_RX.fullmatch(project_key):
        raise ArchitecturePathError(
            "project key must be 1-64 lowercase ASCII letters, digits, or hyphens "
            "with no leading or trailing hyphen"
        )
    return project_key


def validate_artifact_title(project_display_name: str, artifact_title: str) -> str:
    """Validate one project-prefixed, path-safe artifact title."""

    if not isinstance(project_display_name, str) or not project_display_name:
        raise ArchitecturePathError("project display name must be non-empty")
    if not isinstance(artifact_title, str) or not artifact_title:
        raise ArchitecturePathError("artifact title must be non-empty")
    if len(artifact_title) > 120:
        raise ArchitecturePathError("artifact title must be at most 120 characters")
    if artifact_title[0] in ". " or artifact_title[-1] in ". ":
        raise ArchitecturePathError(
            "artifact title must not start or end with a dot or space"
        )
    if ".." in artifact_title:
        raise ArchitecturePathError("artifact title must not contain '..'")
    if any(char in FORBIDDEN_TITLE_CHARACTERS for char in artifact_title):
        raise ArchitecturePathError(
            "artifact title contains a forbidden path or wikilink character"
        )
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in artifact_title):
        raise ArchitecturePathError("artifact title must not contain control characters")
    if not (
        artifact_title == project_display_name
        or artifact_title.startswith(f"{project_display_name} ")
    ):
        raise ArchitecturePathError(
            "artifact title must start with the exact project display name"
        )
    return artifact_title


def _frontmatter_identity_keys(path: Path) -> set[str]:
    keys = {collision_key(path.stem)}
    if path.suffix.lower() != ".md":
        return keys
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return keys
    block = split_frontmatter(text)
    if block is not None:
        try:
            frontmatter = parse_frontmatter(block)
        except FrontmatterError:
            frontmatter = {}
        title = frontmatter.get("title")
        if isinstance(title, str) and title.strip():
            keys.add(collision_key(title.strip()))
        aliases = frontmatter.get("aliases")
        if isinstance(aliases, list):
            keys.update(
                collision_key(alias.strip())
                for alias in aliases
                if isinstance(alias, str) and alias.strip()
            )
    document = split_document(text)
    if document is not None:
        heading = re.search(r"^#\s+(.+?)\s*#*\s*$", document[1], flags=re.MULTILINE)
        if heading is not None:
            keys.add(collision_key(heading.group(1)))
    return keys


def _catalog_namespace(wiki_root: Path) -> set[str]:
    catalog = build_wiki_catalog(wiki_root)
    names = {
        collision_key(name)
        for name in (*catalog.by_stem.keys(), *catalog.aliases, *catalog.repair_names)
    }
    names.update(collision_key(Path(name).name) for name in catalog.exact)
    return names


def _identity_owners(wiki_root: Path, key: str) -> set[Path]:
    owners: set[Path] = set()
    for path in sorted(wiki_root.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in CONTENT_EXTENSIONS
            and key in _frontmatter_identity_keys(path)
        ):
            owners.add(path.resolve())
    return owners


def assert_title_available(
    wiki_root: Path, artifact_title: str, *, current_path: Path | None = None
) -> None:
    """Reject a title colliding with any WikiCatalog stem, title, or alias."""

    wiki = wiki_root.resolve()
    if not wiki.is_dir():
        raise ArchitecturePathError(f"wiki root does not exist: {wiki_root}")
    key = collision_key(artifact_title)
    if key not in _catalog_namespace(wiki):
        return

    current = current_path.resolve() if current_path is not None else None
    if current is not None and wiki not in current.parents:
        raise ArchitecturePathError("current artifact path is outside the wiki root")
    owners = _identity_owners(wiki, key)
    if current is not None and owners == {current}:
        return
    owner_text = ", ".join(
        str(path.relative_to(wiki)) for path in sorted(owners)
    ) or "an existing WikiCatalog identity"
    raise ArchitecturePathError(
        f"artifact title collides vault-wide with {owner_text}: {artifact_title!r}"
    )


def artifact_destination(
    wiki_root: Path,
    *,
    project_key: str,
    project_display_name: str,
    artifact_role: str,
    artifact_title: str,
    current_path: Path | None = None,
) -> Path:
    """Validate identity and return a contained canonical project page path."""

    key = validate_project_key(project_key)
    title = validate_artifact_title(project_display_name, artifact_title)
    if artifact_role not in ROLE_DIRECTORIES:
        raise ArchitecturePathError(
            f"artifact role must be one of {sorted(ROLE_DIRECTORIES)}"
        )
    wiki = wiki_root.absolute()
    projects_root = wiki / "projects"
    project_root = projects_root / key
    folder = ROLE_DIRECTORIES[artifact_role]
    role_root = project_root / folder if folder else project_root
    destination = role_root / f"{title}.md"
    lexical_chain = dict.fromkeys((wiki, projects_root, project_root, role_root))
    try:
        redirected = any(
            path.is_symlink() or path.resolve() != path for path in lexical_chain
        )
        destination_redirected = (
            destination.is_symlink() or destination.resolve() != destination
        )
    except OSError as exc:
        raise ArchitecturePathError(
            "canonical artifact destination cannot be resolved"
        ) from exc
    if redirected or destination_redirected:
        raise ArchitecturePathError(
            "artifact destination must use the canonical non-symlink project role path"
        )
    if current_path is not None:
        try:
            current = current_path.resolve(strict=True)
        except OSError as exc:
            raise ArchitecturePathError("current artifact path is unavailable") from exc
        if current_path.is_symlink() or not current.is_file() or current != destination:
            raise ArchitecturePathError(
                "current artifact path must be the existing canonical destination"
            )
    assert_title_available(wiki, title, current_path=current_path)
    return destination


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--project-display-name", required=True)
    parser.add_argument("--artifact-role", required=True)
    parser.add_argument("--artifact-title", required=True)
    parser.add_argument("--current-path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        destination = artifact_destination(
            args.wiki_root,
            project_key=args.project_key,
            project_display_name=args.project_display_name,
            artifact_role=args.artifact_role,
            artifact_title=args.artifact_title,
            current_path=args.current_path,
        )
    except ArchitecturePathError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "path": str(destination)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
