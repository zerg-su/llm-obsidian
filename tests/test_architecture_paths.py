#!/usr/bin/env python3
"""Behavior tests for the Architecture Workflow project-path gate."""

from __future__ import annotations

import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from architecture_paths import (  # noqa: E402
    ArchitecturePathError,
    artifact_destination,
    collision_key,
    validate_artifact_title,
    validate_project_key,
)


class Suite:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, detail: object = "") -> None:
        if condition:
            print(f"OK   {label}")
        else:
            print(f"FAIL {label}: {detail}")
            self.failures.append(label)

    def rejects(self, label: str, fn) -> None:
        try:
            fn()
        except ArchitecturePathError:
            self.check(label, True)
        else:
            self.check(label, False, "unsafe value was accepted")


def page(title: str, *, aliases: tuple[str, ...] = ()) -> str:
    alias_lines = "\n".join(f'  - "{alias}"' for alias in aliases)
    aliases_block = f"aliases:\n{alias_lines}\n" if aliases else ""
    return f'''---
type: project
title: "{title}"
artifact_role: architecture
project_key: atlas
project_display_name: Atlas
artifact_revision: 1
status: accepted
created: 2026-08-17
updated: 2026-08-17
tags: [project, test]
sessions: []
address: c-123456
{aliases_block}---

# {title}
'''


def main() -> int:
    suite = Suite()

    for valid in ("a", "atlas", "atlas-2", "a" * 64):
        suite.check(
            f"valid project key {valid[:12]}", validate_project_key(valid) == valid
        )
    for invalid in (
        "",
        "Atlas",
        "-atlas",
        "atlas-",
        "atlas_2",
        "atlas/other",
        ".",
        "..",
        "a" * 65,
    ):
        suite.rejects(
            f"reject project key {invalid[:12]!r}",
            lambda value=invalid: validate_project_key(value),
        )

    suite.check(
        "hub title is valid",
        validate_artifact_title("Atlas", "Atlas") == "Atlas",
    )
    suite.check(
        "project-prefixed artifact title is valid",
        validate_artifact_title("Atlas", "Atlas WI-001 — Recovery")
        == "Atlas WI-001 — Recovery",
    )
    for invalid in (
        "",
        "Other Architecture",
        "Atlas/Architecture",
        "Atlas\\Architecture",
        "Atlas .. Architecture",
        "Atlas: Architecture",
        "Atlas [Architecture]",
        " Atlas Architecture",
        "Atlas Architecture ",
        "Atlas\nArchitecture",
        "Atlas" + "x" * 116,
    ):
        suite.rejects(
            f"reject artifact title {invalid[:18]!r}",
            lambda value=invalid: validate_artifact_title("Atlas", value),
        )

    composed = "Atlas Spéc — Recovery"
    decomposed = unicodedata.normalize("NFD", composed).upper()
    suite.check(
        "collision key is NFC plus casefold",
        collision_key(composed) == collision_key(decomposed),
    )

    with tempfile.TemporaryDirectory(prefix="architecture-paths.") as raw:
        wiki = Path(raw).resolve() / "wiki"
        atlas = wiki / "projects" / "atlas"
        other = wiki / "projects" / "other"
        atlas.mkdir(parents=True)
        other.mkdir(parents=True)

        own = atlas / "Atlas Architecture.md"
        own.write_text(page("Atlas Architecture"), encoding="utf-8")
        alias_owner = other / "Other Contract.md"
        alias_owner.write_text(
            page("Other Contract", aliases=("Atlas Contract — Boundary",)),
            encoding="utf-8",
        )
        normalized_owner = other / "Existing Normalized.md"
        normalized_owner.write_text(page(decomposed), encoding="utf-8")

        destination = artifact_destination(
            wiki,
            project_key="atlas",
            project_display_name="Atlas",
            artifact_role="work-item",
            artifact_title="Atlas WI-001 — Recovery",
        )
        suite.check(
            "role mapping resolves beneath selected project root",
            destination == atlas / "work" / "Atlas WI-001 — Recovery.md",
            destination,
        )

        suite.check(
            "an update excludes only its own catalog identities",
            artifact_destination(
                wiki,
                project_key="atlas",
                project_display_name="Atlas",
                artifact_role="architecture",
                artifact_title="Atlas Architecture",
                current_path=own,
            )
            == own,
        )
        suite.rejects(
            "cross-project stem collision fails closed",
            lambda: artifact_destination(
                wiki,
                project_key="other",
                project_display_name="Atlas",
                artifact_role="architecture",
                artifact_title="Atlas Architecture",
            ),
        )
        suite.rejects(
            "cross-project current owner cannot authorize a duplicate",
            lambda: artifact_destination(
                wiki,
                project_key="other",
                project_display_name="Atlas",
                artifact_role="architecture",
                artifact_title="Atlas Architecture",
                current_path=own,
            ),
        )
        suite.rejects(
            "title-to-alias collision fails closed",
            lambda: artifact_destination(
                wiki,
                project_key="atlas",
                project_display_name="Atlas",
                artifact_role="contract",
                artifact_title="Atlas Contract — Boundary",
            ),
        )
        suite.rejects(
            "normalization and case collision fails closed",
            lambda: artifact_destination(
                wiki,
                project_key="atlas",
                project_display_name="Atlas",
                artifact_role="spec",
                artifact_title=composed,
            ),
        )
        suite.rejects(
            "unknown role fails closed",
            lambda: artifact_destination(
                wiki,
                project_key="atlas",
                project_display_name="Atlas",
                artifact_role="plan",
                artifact_title="Atlas Plan",
            ),
        )

        outside = Path(raw).resolve() / "outside"
        outside.mkdir()
        work = atlas / "work"
        work.symlink_to(outside, target_is_directory=True)
        suite.rejects(
            "resolved symlink destination cannot escape selected project root",
            lambda: artifact_destination(
                wiki,
                project_key="atlas",
                project_display_name="Atlas",
                artifact_role="work-graph",
                artifact_title="Atlas Work Graph",
            ),
        )

        redirected_wiki = Path(raw).resolve() / "redirected-wiki"
        redirected_projects = redirected_wiki / "projects"
        beta = redirected_projects / "beta"
        beta.mkdir(parents=True)
        (redirected_projects / "alpha").symlink_to(
            beta, target_is_directory=True
        )
        suite.rejects(
            "project-key symlink cannot redirect to another in-vault project",
            lambda: artifact_destination(
                redirected_wiki,
                project_key="alpha",
                project_display_name="Atlas",
                artifact_role="spec",
                artifact_title="Atlas Spec",
            ),
        )

        outside_wiki = Path(raw).resolve() / "outside-wiki"
        outside_wiki.mkdir()
        outside_redirect_wiki = Path(raw).resolve() / "outside-redirect-wiki"
        outside_redirect_projects = outside_redirect_wiki / "projects"
        outside_redirect_projects.mkdir(parents=True)
        (outside_redirect_projects / "alpha").symlink_to(
            outside_wiki, target_is_directory=True
        )
        suite.rejects(
            "project-key symlink cannot redirect outside the wiki",
            lambda: artifact_destination(
                outside_redirect_wiki,
                project_key="alpha",
                project_display_name="Atlas",
                artifact_role="spec",
                artifact_title="Atlas Spec",
            ),
        )

        projects_target_wiki = Path(raw).resolve() / "projects-target-wiki"
        projects_target = projects_target_wiki / "project-store"
        (projects_target / "alpha").mkdir(parents=True)
        (projects_target_wiki / "projects").symlink_to(
            projects_target, target_is_directory=True
        )
        suite.rejects(
            "projects-directory symlink cannot redirect the project namespace",
            lambda: artifact_destination(
                projects_target_wiki,
                project_key="alpha",
                project_display_name="Atlas",
                artifact_role="spec",
                artifact_title="Atlas Spec",
            ),
        )

        role_redirects = (
            ("design-link", "design", "specs", "design", "Atlas Design Redirect"),
            ("work-link", "work", ".", "work-item", "Atlas Work Redirect"),
            ("contracts-link", "contracts", "design", "contract", "Atlas Contract Redirect"),
        )
        for project_key, link_name, target_name, role, title in role_redirects:
            role_project = wiki / "projects" / project_key
            role_project.mkdir(parents=True)
            target = role_project if target_name == "." else role_project / target_name
            target.mkdir(exist_ok=True)
            (role_project / link_name).symlink_to(target, target_is_directory=True)
            suite.rejects(
                f"role-directory symlink {link_name} to {target_name} fails closed",
                lambda project_key=project_key, role=role, title=title: artifact_destination(
                    wiki,
                    project_key=project_key,
                    project_display_name="Atlas",
                    artifact_role=role,
                    artifact_title=title,
                ),
            )

        clean_project = wiki / "projects" / "clean-role"
        (clean_project / "design").mkdir(parents=True)
        clean_destination = artifact_destination(
            wiki,
            project_key="clean-role",
            project_display_name="Atlas",
            artifact_role="design",
            artifact_title="Atlas Clean Architecture",
        )
        suite.check(
            "canonical real role directory remains accepted",
            clean_destination
            == clean_project / "design" / "Atlas Clean Architecture.md",
            clean_destination,
        )

    return int(bool(suite.failures))


if __name__ == "__main__":
    raise SystemExit(main())
