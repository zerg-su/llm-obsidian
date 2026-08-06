#!/usr/bin/env python3
"""Build one validated, side-effect-free mutation plan for vault-write."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from plan_lifecycle import PlanCloseError, render_plan_close
from vault_write_contract import CapViolation, ConflictError, PayloadError, sha256_text
from vault_write_pages import PageMutationValidator, canonical_source_url
from vault_write_rendering import apply_hot, apply_log


HOT_MUTATION_KEYS = {
    "hot_bullet",
    "hot_recent_remove_addresses",
    "hot_narrative",
    "hot_threads",
}


@dataclass(frozen=True)
class MutationPlan:
    writes: list[tuple[Path, str]]
    deletes: list[tuple[Path, str]]
    warnings: list[str]


class MutationPlanner:
    """Own validation order and the complete pre-commit write/delete set."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.hot_file = self.repo_root / "wiki" / "hot.md"
        self.log_file = self.repo_root / "wiki" / "log.md"
        self.pages = PageMutationValidator(self.repo_root)

    def plan(self, payload: dict, today: str) -> MutationPlan:
        writes, deletes = self.pages.page_mutations(
            payload.get("pages"),
            allow_writer_owned_link_repair=(
                self._exact_writer_owned_link_repair(payload)
            ),
        )
        move_writes, move_deletes = self.pages.page_moves(payload.get("moves"))
        writes.extend(move_writes)
        deletes.extend(move_deletes)
        warnings: list[str] = []

        self.pages.validate_unique_source_urls(writes, deletes)

        manifest = self.manifest_write(payload.get("manifest_update"))
        if manifest:
            writes.append(manifest)

        if payload.keys() & HOT_MUTATION_KEYS:
            hot_text = self.hot_file.read_text(encoding="utf-8")
            rendered_hot, hot_warnings = apply_hot(payload, hot_text, today)
            writes.append((self.hot_file, rendered_hot))
            warnings.extend(hot_warnings)

        if payload.get("log_entry"):
            log_text = self.log_file.read_text(encoding="utf-8")
            writes.append(
                (self.log_file, apply_log(payload["log_entry"], log_text, today))
            )

        if payload.get("plan_close"):
            writes.append(self.apply_plan_close(payload["plan_close"], today))

        self.ensure_unique_writes(writes, deletes)
        if not writes and not deletes:
            raise PayloadError("payload produced no writes")
        return MutationPlan(writes=writes, deletes=deletes, warnings=warnings)

    def _exact_writer_owned_link_repair(self, payload: dict) -> bool:
        """Authorize only the planner's current deterministic repair bytes."""

        allowed = {"schema_version", "request_id", "actor", "pages"}
        raw_binding = payload.get("exact_binding")
        binding = None
        if raw_binding is not None:
            allowed.add("exact_binding")
        if (
            payload.get("actor") != "stop-hook-link-repair"
            or set(payload) != allowed
        ):
            return False
        from vault_link_repair import (
            ExactBindingError,
            build_repair_plan,
            parse_exact_binding,
        )

        try:
            if raw_binding is not None:
                binding = parse_exact_binding(raw_binding)
            repair = build_repair_plan(
                self.repo_root,
                exact_binding=binding,
            )
        except ExactBindingError:
            return False
        return repair is not None and payload == repair.payload

    def manifest_write(self, spec: object) -> tuple[Path, str] | None:
        if spec is None:
            return None
        if not isinstance(spec, dict):
            raise PayloadError("manifest_update must be an object")
        unknown = set(spec) - {"path", "expected_sha256", "merge"}
        if unknown:
            raise PayloadError(f"manifest_update unknown keys: {sorted(unknown)}")
        rel = str(spec.get("path") or ".raw/.manifest.json")
        if rel != ".raw/.manifest.json":
            raise PayloadError("manifest_update.path must be .raw/.manifest.json")
        path = self.pages.repo_path(rel)
        expected = str(spec.get("expected_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise PayloadError("manifest_update requires lowercase expected_sha256")
        merge = spec.get("merge")
        if not isinstance(merge, dict):
            raise PayloadError("manifest_update.merge must be an object")

        initial_text = '{"address_map":{},"sources":{}}\n'
        if not path.is_file():
            if expected != sha256_text(initial_text):
                raise ConflictError(f"manifest target missing: {rel}")
            current_text = initial_text
        else:
            current_text = path.read_text(encoding="utf-8")
            actual = sha256_text(current_text)
            if actual != expected:
                raise ConflictError(
                    f"manifest conflict: {actual}, expected {expected}"
                )
        try:
            current = json.loads(current_text)
        except json.JSONDecodeError as exc:
            raise PayloadError(f"manifest is invalid JSON: {exc}") from exc
        if not isinstance(current, dict):
            raise PayloadError("manifest root must be an object")
        self._validate_manifest_sources(current, merge)
        return (
            path,
            json.dumps(
                deep_merge(current, merge),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    @staticmethod
    def _validate_manifest_sources(current: dict, merge: dict) -> None:
        source_patch = merge.get("sources")
        if source_patch is None:
            return
        if not isinstance(source_patch, dict):
            raise PayloadError("manifest_update.merge.sources must be an object")
        current_sources = current.get("sources")
        if current_sources is not None and not isinstance(current_sources, dict):
            raise PayloadError("manifest sources must be an object")
        known_urls: dict[str, str] = {}
        for key in current_sources or {}:
            if isinstance(key, str) and key.startswith(("http://", "https://")):
                known_urls.setdefault(
                    canonical_source_url(key, context="manifest source key"), key
                )
        for key in source_patch:
            if not isinstance(key, str):
                raise PayloadError("manifest source keys must be strings")
            if not key.startswith(("http://", "https://")):
                continue
            canonical = canonical_source_url(key, context="manifest source key")
            if key != canonical:
                raise PayloadError(
                    "URL manifest source keys must use the canonical fragment-free URL"
                )
            prior = known_urls.get(canonical)
            if prior is not None and prior != key:
                raise PayloadError(
                    "URL manifest source identity conflicts with an existing canonical key"
                )
            known_urls[canonical] = key

    def ensure_unique_writes(
        self,
        writes: list[tuple[Path, str]],
        deletes: list[tuple[Path, str]] | None = None,
    ) -> None:
        seen: set[Path] = set()
        for path, _ in writes:
            if path in seen:
                raise PayloadError(
                    f"payload writes {path.relative_to(self.repo_root)} more than once"
                )
            seen.add(path)
        for path, _ in deletes or []:
            if path in seen:
                raise PayloadError(
                    f"payload mutates {path.relative_to(self.repo_root)} more than once"
                )
            seen.add(path)

    def apply_plan_close(self, spec: dict, today: str) -> tuple[Path, str]:
        if not isinstance(spec, dict):
            raise CapViolation(
                "plan_close must be an object {file, result_link, exec_session}"
            )
        rel = str(spec.get("file") or "")
        result_link = str(spec.get("result_link") or "").strip()
        exec_session = spec.get("exec_session") or None
        if not result_link:
            raise CapViolation(
                "plan_close.result_link is required, e.g. '[[Page Title]]'"
            )

        path = (self.repo_root / rel).resolve()
        plans_dir = (self.repo_root / "wiki" / "plans").resolve()
        if plans_dir not in path.parents:
            raise CapViolation(
                f"plan_close.file must live in wiki/plans/ (got {rel!r})"
            )
        if not path.is_file():
            raise CapViolation(f"plan_close.file not found: {rel}")

        expected = spec.get("expected_sha256")
        if expected is not None:
            if (
                not isinstance(expected, str)
                or len(expected) != 64
                or any(char not in "0123456789abcdef" for char in expected)
            ):
                raise CapViolation(
                    "plan_close.expected_sha256 must be a lowercase SHA-256"
                )
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise ConflictError(
                    f"plan_close conflict for {rel}: expected {expected}, got {actual}"
                )

        try:
            new_text = render_plan_close(
                path.read_text(encoding="utf-8"),
                today=today,
                result_link=result_link,
                exec_session=exec_session,
                label=rel,
            )
        except PlanCloseError as exc:
            raise CapViolation(str(exc)) from exc
        return path, new_text


def deep_merge(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
