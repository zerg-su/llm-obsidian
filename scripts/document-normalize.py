#!/usr/bin/env python3
"""Normalize local documents into stable, cacheable Markdown artifacts.

Text-like inputs use only the Python standard library. Binary document formats
are converted by a versioned, local Docling runtime prepared by
``scripts/install-docling.py``. The converter never accepts URLs and disables
Docling remote services and external plugins.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, NoReturn


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# The conversion collaborator enforces the source-audited offline invariant
# ``"HF_HUB_OFFLINE": "1"`` before invoking Docling.
# These imports intentionally re-export the original module-level interface.
from document_normalize_cache import cached_payload, status_exit_code
from document_normalize_config import (
    CHECK_COMMAND,
    CONFIG,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PAGES,
    DEFAULT_TIMEOUT_SECONDS,
    DOCLING_EXTENSIONS,
    DOCLING_MODELS,
    DOCLING_PYTHON_VERSION,
    DOCLING_VERSION,
    EASYOCR_REQUIRED_FILES,
    EXIT_CONVERSION_FAILURE,
    EXIT_LOW_QUALITY,
    EXIT_NEEDS_SEMANTIC_CLEANUP,
    EXIT_NEEDS_USER_ACTION,
    EXIT_OK,
    EXIT_RUNTIME_UNAVAILABLE,
    HTML_EXTENSIONS,
    INSTALL_COMMAND,
    LOW_CONFIDENCE_THRESHOLD,
    MAX_REPAIR_DOCUMENT_RATIO,
    MAX_REPAIR_SEGMENT_CHARACTERS,
    MAX_REPAIR_SEGMENTS,
    MAX_REPAIR_TOTAL_CHARACTERS,
    MIN_USEFUL_CHARACTERS,
    OCR_LANGUAGES,
    PDF_RASTER_THRESHOLD,
    PDF_TEXT_THRESHOLD,
    PROFILE_VERSION,
    ROOT,
    TEXT_EXTENSIONS,
    default_cache_root,
    emit,
    sha256_file,
    stable_hash,
    utc_now,
    write_manifest,
)
from document_normalize_conversion import (
    MarkdownHTMLParser,
    builtin_markdown,
    collect_confidences,
    docling_convert,
    page_count,
    read_text,
    stable_docling_text,
)
from document_normalize_quality import (
    FENCED_CODE_RE,
    INLINE_ENUM_RE,
    LOWERCASE_START_RE,
    MIXED_WORD_RE,
    PAGE_MARKER_RE,
    TERMINAL_RE,
    deterministic_cleanup,
    issue_counts,
    normalize_visible_punctuation,
    page_for_offset,
    quality_issues,
    repair_bundle,
    restore_inline_numbered_list,
    safe_text_join,
    sequential_enumerator_positions,
    structural_block,
    suspicious_mixed_words,
)
from document_normalize_runtime import (
    custom_docling_adapter,
    default_docling_home,
    default_models_path,
    docling_adapter,
    docling_python,
    models_ready,
    needs_docling_payload,
    runtime_status,
    runtime_version,
)


def normalize(args: argparse.Namespace) -> NoReturn:
    source = Path(args.source).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    else:
        source = source.resolve()
    if not source.is_file():
        emit({"version": 1, "status": "unsupported", "reason": f"not a local file: {source}"}, json_output=args.json, code=4)
    size = source.stat().st_size
    if size > args.max_bytes:
        emit(
            {
                "version": 1,
                "status": "unsupported",
                "reason": f"file is {size} bytes; configured limit is {args.max_bytes}",
            },
            json_output=args.json,
            code=4,
        )
    suffix = source.suffix.lower()
    if suffix in TEXT_EXTENSIONS or suffix in HTML_EXTENSIONS:
        processor = "builtin"
        processor_version = str(PROFILE_VERSION)
    elif suffix in DOCLING_EXTENSIONS:
        processor = "docling"
        status = runtime_status()
        if status.get("status") != "ok":
            status["source"] = {
                "path": str(source),
                "size": size,
                "format": suffix.lstrip("."),
            }
            emit(status, json_output=args.json, code=2)
        processor_version = runtime_version() or DOCLING_VERSION
    else:
        emit(
            {
                "version": 1,
                "status": "unsupported",
                "reason": f"unsupported file extension: {suffix or '<none>'}",
                "supported": sorted(
                    TEXT_EXTENSIONS | HTML_EXTENSIONS | DOCLING_EXTENSIONS
                ),
            },
            json_output=args.json,
            code=4,
        )

    source_hash = sha256_file(source)
    profile = {
        "version": PROFILE_VERSION,
        "processor": processor,
        "processor_version": processor_version,
        "ocr_engine": "easyocr" if processor == "docling" else None,
        "ocr_languages": OCR_LANGUAGES if processor == "docling" else [],
        "table_mode": "accurate" if processor == "docling" else None,
        "pdf_text_threshold": PDF_TEXT_THRESHOLD if suffix == ".pdf" else None,
        "pdf_raster_threshold": PDF_RASTER_THRESHOLD if suffix == ".pdf" else None,
        "cleanup": "deterministic-v2" if processor == "docling" else None,
        "max_pages": args.max_pages,
    }
    profile_hash = stable_hash(profile)
    cache_key = f"{source_hash}-{profile_hash[:16]}"
    cache_root = Path(args.cache_root).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / f".{cache_key}.lock"
    target = cache_root / cache_key
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not args.force:
            cached = cached_payload(target, source_hash, profile_hash)
            if cached is not None:
                emit(
                    cached,
                    json_output=args.json,
                    code=status_exit_code(str(cached["status"])),
                )

        work = Path(tempfile.mkdtemp(prefix=f".{cache_key}.tmp-", dir=cache_root))
        try:
            structured: dict[str, Any] | None = None
            adapter_metadata: dict[str, Any] | None = None
            if processor == "builtin":
                try:
                    markdown_text = builtin_markdown(source)
                except (OSError, ValueError) as exc:
                    emit(
                        {
                            "version": 1,
                            "status": "conversion_failed",
                            "reason": str(exc),
                        },
                        json_output=args.json,
                        code=4,
                    )
                (work / "document.md").write_text(markdown_text, encoding="utf-8")
                (work / "document.raw.md").write_text(markdown_text, encoding="utf-8")
            else:
                raw_dir = work / "docling-output"
                raw_dir.mkdir()
                try:
                    markdown_path, json_path, structured, adapter_metadata = (
                        docling_convert(
                            source,
                            raw_dir,
                            args.timeout,
                            args.max_pages,
                            args.max_bytes,
                        )
                    )
                except RuntimeError as exc:
                    emit(
                        {
                            "version": 1,
                            "status": "conversion_failed",
                            "reason": str(exc),
                        },
                        json_output=args.json,
                        code=4,
                    )
                for child in raw_dir.iterdir():
                    if child in {markdown_path, json_path, raw_dir / "adapter.json"}:
                        continue
                    destination = work / child.name
                    if child.is_dir():
                        shutil.copytree(child, destination)
                    else:
                        shutil.copy2(child, destination)
                try:
                    stable_markdown = stable_docling_text(markdown_path, raw_dir)
                    stable_json = stable_docling_text(json_path, raw_dir)
                except OSError as exc:
                    emit(
                        {
                            "version": 1,
                            "status": "conversion_failed",
                            "reason": str(exc),
                        },
                        json_output=args.json,
                        code=4,
                    )
                clean_markdown = deterministic_cleanup(stable_markdown)
                (work / "document.raw.md").write_text(
                    stable_markdown, encoding="utf-8"
                )
                (work / "document.md").write_text(clean_markdown, encoding="utf-8")
                (work / "document.docling.json").write_text(
                    stable_json, encoding="utf-8"
                )
                write_manifest(work / "document.adapter.json", adapter_metadata)
                shutil.rmtree(raw_dir)

            markdown_text = (work / "document.md").read_text(encoding="utf-8")
            quality_text = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.DOTALL)
            nonspace_characters = len(re.sub(r"\s+", "", quality_text))
            pages = (
                int(adapter_metadata["pages"])
                if adapter_metadata is not None
                and adapter_metadata.get("pages") is not None
                else page_count(structured)
                if structured is not None
                else None
            )
            if pages is not None and pages > args.max_pages:
                emit(
                    {
                        "version": 1,
                        "status": "unsupported",
                        "reason": (
                            f"document has {pages} pages; configured limit is "
                            f"{args.max_pages}"
                        ),
                    },
                    json_output=args.json,
                    code=4,
                )
            confidences: list[float] = []
            if structured is not None:
                collect_confidences(structured, confidences)
            confidence_average = (
                sum(confidences) / len(confidences) if confidences else None
            )
            minimum_characters = MIN_USEFUL_CHARACTERS if processor == "docling" else 1
            enough_text = nonspace_characters >= minimum_characters
            confidence_ok = (
                processor != "docling"
                or confidence_average is None
                or confidence_average >= LOW_CONFIDENCE_THRESHOLD
            )
            accepted = enough_text and confidence_ok
            issues = (
                quality_issues(markdown_text, adapter_metadata)
                if processor == "docling"
                else []
            )
            clean_hash = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()
            bundle, over_cap = repair_bundle(
                source_hash, clean_hash, markdown_text, issues
            )
            if bundle is not None:
                write_manifest(work / "repair-bundle.json", bundle)
            embedded_issues = (
                list(bundle["segments"])
                if bundle is not None
                else issues[:MAX_REPAIR_SEGMENTS]
            )
            if not accepted:
                status_name = "low_quality"
            elif over_cap:
                status_name = "needs_user_action"
            elif bundle is not None:
                status_name = "needs_semantic_cleanup"
            else:
                status_name = "ok"
            manifest_payload: dict[str, Any] = {
                "version": 1,
                "status": status_name,
                "cached": False,
                "created_at": utc_now(),
                "cache_key": cache_key,
                "source": {
                    "path": str(source),
                    "name": source.name,
                    "format": suffix.lstrip("."),
                    "size": size,
                    "sha256": source_hash,
                },
                "processor": {
                    **profile,
                    "profile_sha256": profile_hash,
                    "network_allowed": False,
                    "external_plugins_allowed": False,
                },
                "quality": {
                    "accepted": accepted,
                    "nonspace_characters": nonspace_characters,
                    "pages": pages,
                    "confidence_samples": len(confidences),
                    "confidence_average": confidence_average,
                    "minimum_characters": minimum_characters,
                    "minimum_confidence": LOW_CONFIDENCE_THRESHOLD,
                    "issues": embedded_issues,
                    "issues_total": len(issues),
                    "issue_counts": issue_counts(issues),
                    "issues_truncated": len(embedded_issues) < len(issues),
                    "repair_segments": len(bundle["segments"]) if bundle else 0,
                },
            }
            if not accepted:
                manifest_payload["reason"] = (
                    "normalized output is empty/short or confidence is below "
                    "threshold; inspect before ingest"
                )
            elif over_cap:
                manifest_payload["reason"] = (
                    "semantic cleanup exceeds automatic repair limits"
                )
                manifest_payload["action"] = {
                    "kind": "confirm_large_semantic_cleanup",
                    "message": (
                        "Inspect the repair bundle and ask before processing the document."
                    ),
                }
            elif bundle is not None:
                manifest_payload["reason"] = (
                    "bounded semantic cleanup is required before synthesis"
                )
            write_manifest(work / "manifest.json", manifest_payload)
            if target.exists():
                shutil.rmtree(target)
            os.replace(work, target)
            manifest_payload["artifacts"] = {
                "root": str(target),
                "markdown": str(target / "document.md"),
                "raw_markdown": str(target / "document.raw.md"),
                "docling_json": (
                    str(target / "document.docling.json")
                    if (target / "document.docling.json").is_file()
                    else None
                ),
                "adapter_metadata": (
                    str(target / "document.adapter.json")
                    if (target / "document.adapter.json").is_file()
                    else None
                ),
                "repair_bundle": (
                    str(target / "repair-bundle.json")
                    if (target / "repair-bundle.json").is_file()
                    else None
                ),
            }
            emit(
                manifest_payload,
                json_output=args.json,
                code=status_exit_code(status_name),
            )
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    normalize_parser = subparsers.add_parser(
        "normalize", help="normalize one local file"
    )
    normalize_parser.add_argument("source")
    normalize_parser.add_argument("--cache-root", default=str(default_cache_root()))
    normalize_parser.add_argument(
        "--max-bytes", type=positive_int, default=DEFAULT_MAX_BYTES
    )
    normalize_parser.add_argument(
        "--max-pages", type=positive_int, default=DEFAULT_MAX_PAGES
    )
    normalize_parser.add_argument(
        "--timeout", type=positive_int, default=DEFAULT_TIMEOUT_SECONDS
    )
    normalize_parser.add_argument("--force", action="store_true")
    normalize_parser.add_argument("--json", action="store_true")
    check_parser = subparsers.add_parser(
        "check", help="validate the pinned Docling runtime"
    )
    check_parser.add_argument("--json", action="store_true")
    return parser


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "check":
        payload = runtime_status()
        emit(
            payload,
            json_output=args.json,
            code=0 if payload.get("status") == "ok" else 2,
        )
    normalize(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
