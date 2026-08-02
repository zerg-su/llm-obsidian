"""Canonical source and test inventory for harness quality audits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class AuditManifestError(ValueError):
    """Raised when the canonical harness audit inventory is invalid."""


@dataclass(frozen=True)
class AuditManifest:
    source_roots: tuple[str, ...]
    entrypoints: tuple[str, ...]
    coverage_test_roots: tuple[str, ...]
    coverage_tests: tuple[str, ...]
    excluded_entrypoints: tuple[tuple[str, str], ...]

    def source_paths(self, root: Path) -> tuple[Path, ...]:
        paths = set()
        for relative in self.source_roots:
            paths.update((root / relative).rglob("*.py"))
        paths.update(root / relative for relative in self.entrypoints)
        return tuple(sorted(path.resolve() for path in paths))

    def test_paths(self, root: Path) -> tuple[Path, ...]:
        paths = set()
        for relative in self.coverage_test_roots:
            paths.update((root / relative).glob("test_*.py"))
        paths.update(root / relative for relative in self.coverage_tests)
        return tuple(sorted(path.resolve() for path in paths))


def _strings(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise AuditManifestError(f"{label} must be unique non-empty paths")
    return tuple(value)


def load_audit_manifest(root: Path, path: Path | None = None) -> AuditManifest:
    source = path or root / "config/harness-audit-manifest.json"
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditManifestError("harness audit manifest is unavailable") from exc
    expected = {
        "schema_version",
        "source_roots",
        "entrypoints",
        "coverage_test_roots",
        "coverage_tests",
        "excluded_entrypoints",
    }
    if not isinstance(raw, dict) or set(raw) != expected or raw["schema_version"] != 1:
        raise AuditManifestError("harness audit manifest fields are not exact")
    excluded = raw["excluded_entrypoints"]
    if not isinstance(excluded, list) or any(
        not isinstance(item, dict)
        or set(item) != {"path", "reason"}
        or not isinstance(item["path"], str)
        or not item["path"]
        or not isinstance(item["reason"], str)
        or not item["reason"]
        for item in excluded
    ):
        raise AuditManifestError("harness audit exclusions are invalid")
    manifest = AuditManifest(
        _strings(raw["source_roots"], "source_roots"),
        _strings(raw["entrypoints"], "entrypoints"),
        _strings(raw["coverage_test_roots"], "coverage_test_roots"),
        _strings(raw["coverage_tests"], "coverage_tests"),
        tuple((item["path"], item["reason"]) for item in excluded),
    )
    for candidate in (*manifest.source_paths(root), *manifest.test_paths(root)):
        if not candidate.is_file() or root.resolve() not in candidate.parents:
            raise AuditManifestError("harness audit manifest path is unavailable")
    return manifest
