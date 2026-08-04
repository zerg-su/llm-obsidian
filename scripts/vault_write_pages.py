#!/usr/bin/env python3
"""Page/source validation and optimistic page mutation policy."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from vault_schema import (
    ADDRESS_CUTOFF,
    ADDRESS_EXEMPT_TYPES,
    ADDRESS_RX,
    DATE_RX,
    FrontmatterError,
    REQUIRED_KEYS,
    parse_frontmatter,
    split_frontmatter,
)
from vault_write_contract import ConflictError, PayloadError, safe_repo_path, sha256_text


class PageMutationValidator:
    """Own optimistic page/source invariants for one repository root."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.hot_file = (self.repo_root / "wiki" / "hot.md").resolve()
        self.log_file = (self.repo_root / "wiki" / "log.md").resolve()

    def repo_path(self, rel: str, *, prefix: str | None = None) -> Path:
        return safe_repo_path(self.repo_root, rel, prefix=prefix)

    def validate_page_content(self, rel: str, content: str) -> None:
        if not rel.endswith(".md"):
            return
        block = split_frontmatter(content)
        if block is None:
            raise PayloadError(f"{rel}: missing or unclosed frontmatter")
        try:
            frontmatter = parse_frontmatter(block)
        except FrontmatterError as exc:
            raise PayloadError(f"{rel}: invalid frontmatter: {exc}") from exc
        missing = [key for key in REQUIRED_KEYS if key not in frontmatter]
        if missing:
            raise PayloadError(
                f"{rel}: missing required frontmatter: {', '.join(missing)}"
            )
        if not isinstance(frontmatter.get("tags"), list) or not frontmatter["tags"]:
            raise PayloadError(f"{rel}: tags must be a non-empty list")
        if not isinstance(frontmatter.get("sessions"), list):
            raise PayloadError(f"{rel}: sessions must be a list")
        for key in ("created", "updated"):
            if not DATE_RX.fullmatch(str(frontmatter.get(key) or "")):
                raise PayloadError(f"{rel}: {key} must be YYYY-MM-DD")
        address = frontmatter.get("address")
        if address is not None:
            match = ADDRESS_RX.fullmatch(str(address))
            if match is None or int(match.group(1)) == 0:
                raise PayloadError(f"{rel}: invalid non-zero c-NNNNNN address")
        created = str(frontmatter.get("created") or "")
        requires_address = (
            str(frontmatter.get("type") or "") not in ADDRESS_EXEMPT_TYPES
            and DATE_RX.fullmatch(created)
            and time.strptime(created, "%Y-%m-%d")[:3]
            >= (ADDRESS_CUTOFF.year, ADDRESS_CUTOFF.month, ADDRESS_CUTOFF.day)
        )
        if requires_address and address is None:
            raise PayloadError(f"{rel}: post-rollout content page requires address")
        if str(frontmatter.get("type") or "") == "source":
            source_class = str(frontmatter.get("source_class") or "")
            if source_class not in {"official", "internal", "third-party"}:
                raise PayloadError(
                    f"{rel}: source_class must be official|internal|third-party"
                )
            if not DATE_RX.fullmatch(str(frontmatter.get("verified_at") or "")):
                raise PayloadError(f"{rel}: source verified_at must be YYYY-MM-DD")
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(frontmatter.get("content_sha256") or "")
            ):
                raise PayloadError(
                    f"{rel}: source content_sha256 must be lowercase SHA-256"
                )
            source_url = str(frontmatter.get("source_url") or "").strip()
            if source_url:
                canonical_source_url(source_url, context=rel)

    def source_page_identity(self, content: str, *, context: str) -> str | None:
        block = split_frontmatter(content)
        if block is None:
            return None
        try:
            frontmatter = parse_frontmatter(block)
        except FrontmatterError:
            return None
        if str(frontmatter.get("type") or "") != "source":
            return None
        source_url = str(frontmatter.get("source_url") or "").strip()
        return canonical_source_url(source_url, context=context) if source_url else None

    def validate_unique_source_urls(
        self, writes: list[tuple[Path, str]], deletes: list[tuple[Path, str]]
    ) -> None:
        pending: list[tuple[Path, str]] = []
        replaced = {path.resolve() for path, _ in writes + deletes}
        for path, content in writes:
            if path.suffix != ".md" or self.repo_root / "wiki" not in path.parents:
                continue
            identity = self.source_page_identity(
                content, context=str(path.relative_to(self.repo_root))
            )
            if identity:
                pending.append((path.resolve(), identity))
        if not pending:
            return

        owners: dict[str, Path] = {}
        for path in (self.repo_root / "wiki").rglob("*.md"):
            resolved = path.resolve()
            if path.is_symlink() or resolved in replaced or not path.is_file():
                continue
            try:
                identity = self.source_page_identity(
                    path.read_text(encoding="utf-8"),
                    context=str(path.relative_to(self.repo_root)),
                )
            except (OSError, UnicodeError):
                continue
            if identity:
                owners.setdefault(identity, resolved)
        for path, identity in pending:
            owner = owners.get(identity)
            if owner is not None and owner != path:
                raise PayloadError(
                    f"{path.relative_to(self.repo_root)}: source_url already belongs to "
                    f"{owner.relative_to(self.repo_root)}; update that canonical source page"
                )
            owners[identity] = path

    def page_mutations(
        self,
        specs: object,
        *,
        allow_writer_owned_link_repair: bool = False,
    ) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
        if specs is None:
            return [], []
        if not isinstance(specs, list):
            raise PayloadError("pages must be an array")
        writes: list[tuple[Path, str]] = []
        deletes: list[tuple[Path, str]] = []
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                raise PayloadError(f"pages[{index}] must be an object")
            unknown = set(spec) - {"op", "path", "content", "expected_sha256"}
            if unknown:
                raise PayloadError(f"pages[{index}] unknown keys: {sorted(unknown)}")
            op = spec.get("op")
            rel = str(spec.get("path") or "")
            content = spec.get("content")
            if op not in {"create", "update", "delete"}:
                raise PayloadError(f"pages[{index}].op must be create|update|delete")
            path = self.repo_path(rel, prefix="wiki/")
            if path in {self.hot_file, self.log_file} and not (
                allow_writer_owned_link_repair and op == "update"
            ):
                key = "hot_*" if path == self.hot_file else "log_entry"
                raise PayloadError(
                    f"pages[{index}].path {rel!r} is writer-owned; "
                    f"use the dedicated {key} payload"
                )
            if op == "delete":
                if content is not None:
                    raise PayloadError(f"pages[{index}]: delete must not carry content")
                if not path.is_file():
                    raise ConflictError(f"delete target missing: {rel}")
                expected = str(spec.get("expected_sha256") or "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise PayloadError(
                        f"pages[{index}]: delete requires lowercase SHA-256"
                    )
                actual = sha256_text(path.read_text(encoding="utf-8"))
                if actual != expected:
                    raise ConflictError(
                        f"delete conflict: {rel} is {actual}, expected {expected}"
                    )
                deletes.append((path, expected))
                continue
            if not isinstance(content, str):
                raise PayloadError(f"pages[{index}].content must be a string")
            self.validate_page_content(rel, content)
            if op == "create":
                if path.exists():
                    raise ConflictError(f"create collision: {rel} already exists")
                if spec.get("expected_sha256") is not None:
                    raise PayloadError(
                        f"pages[{index}]: create must not carry expected_sha256"
                    )
            else:
                if not path.is_file():
                    raise ConflictError(f"update target missing: {rel}")
                expected = str(spec.get("expected_sha256") or "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise PayloadError(
                        f"pages[{index}]: update requires lowercase SHA-256"
                    )
                actual = sha256_text(path.read_text(encoding="utf-8"))
                if actual != expected:
                    raise ConflictError(
                        f"update conflict: {rel} is {actual}, expected {expected}"
                    )
            writes.append((path, content))
        return writes, deletes

    def page_moves(
        self, specs: object
    ) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
        """Render optimistic renames as destination writes plus source deletes."""

        if specs is None:
            return [], []
        if not isinstance(specs, list):
            raise PayloadError("moves must be an array")
        writes: list[tuple[Path, str]] = []
        deletes: list[tuple[Path, str]] = []
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                raise PayloadError(f"moves[{index}] must be an object")
            unknown = set(spec) - {"from", "to", "expected_sha256"}
            if unknown:
                raise PayloadError(f"moves[{index}] unknown keys: {sorted(unknown)}")
            source_rel = str(spec.get("from") or "")
            target_rel = str(spec.get("to") or "")
            source = self.repo_path(source_rel, prefix="wiki/")
            target = self.repo_path(target_rel, prefix="wiki/")
            if source in {self.hot_file, self.log_file} or target in {
                self.hot_file,
                self.log_file,
            }:
                raise PayloadError(
                    f"moves[{index}] cannot rename writer-owned log/hot files"
                )
            if source == target:
                raise PayloadError(f"moves[{index}] source and target are identical")
            if not source.is_file():
                raise ConflictError(f"move source missing: {source_rel}")
            if target.exists():
                raise ConflictError(f"move target exists: {target_rel}")
            expected = str(spec.get("expected_sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PayloadError(f"moves[{index}] requires lowercase expected_sha256")
            content = source.read_text(encoding="utf-8")
            actual = sha256_text(content)
            if actual != expected:
                raise ConflictError(
                    f"move conflict: {source_rel} is {actual}, expected {expected}"
                )
            self.validate_page_content(target_rel, content)
            writes.append((target, content))
            deletes.append((source, expected))
        return writes, deletes


def canonical_source_url(raw: str, *, context: str) -> str:
    """Return the stable network-resource identity used for deduplication."""

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise PayloadError(f"{context}: invalid source URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise PayloadError(f"{context}: source_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise PayloadError(f"{context}: source_url must not contain credentials")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
