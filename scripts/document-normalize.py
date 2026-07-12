#!/usr/bin/env python3
"""Normalize local documents into stable, cacheable Markdown artifacts.

Text-like inputs use only the Python standard library. Binary document formats
are converted by a versioned, local Docling runtime prepared by
``scripts/install-docling.py``. The converter never accepts URLs and disables
Docling remote services and external plugins.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "document-tools.json").read_text(encoding="utf-8"))
DOCLING_VERSION = str(CONFIG["docling"]["version"])
DOCLING_PYTHON_VERSION = str(CONFIG["docling"]["python"])
OCR_LANGUAGES = [str(value) for value in CONFIG["docling"]["ocr_languages"]]
DOCLING_MODELS = [str(value) for value in CONFIG["docling"]["models"]]
EASYOCR_REQUIRED_FILES = [
    str(value) for value in CONFIG["docling"]["easyocr_required_files"]
]
PROFILE_VERSION = 1
TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv"}
HTML_EXTENSIONS = {".html", ".htm"}
DOCLING_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
}
DEFAULT_MAX_BYTES = int(CONFIG["limits"]["max_bytes"])
DEFAULT_MAX_PAGES = int(CONFIG["limits"]["max_pages"])
DEFAULT_TIMEOUT_SECONDS = int(CONFIG["limits"]["timeout_seconds"])
MIN_USEFUL_CHARACTERS = 20
LOW_CONFIDENCE_THRESHOLD = 0.50
INSTALL_COMMAND = "python3 scripts/install-docling.py install"
CHECK_COMMAND = "python3 scripts/document-normalize.py check"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_cache_root() -> Path:
    value = os.environ.get("LLM_OBSIDIAN_DOCUMENT_CACHE", "")
    return Path(value).expanduser() if value else ROOT / ".vault-meta" / "document-cache"


def default_docling_home() -> Path:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_HOME", "")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".local" / "share" / "llm-obsidian" / "docling" / DOCLING_VERSION


def default_models_path() -> Path:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_MODELS", "")
    if value:
        return Path(value).expanduser()
    return default_docling_home() / "models"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def emit(payload: dict[str, Any], *, json_output: bool, code: int = 0) -> NoReturn:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        status = payload.get("status", "unknown")
        message = payload.get("message") or payload.get("reason") or ""
        print(f"{status}: {message}".rstrip())
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, dict) and artifacts.get("markdown"):
            print(f"markdown: {artifacts['markdown']}")
        action = payload.get("action")
        if isinstance(action, dict):
            if action.get("install_command"):
                print(f"install: {action['install_command']}")
            if action.get("check_command"):
                print(f"check: {action['check_command']}")
    raise SystemExit(code)


def needs_docling_payload(reason: str) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "needs_user_action",
        "reason": reason,
        "action": {
            "kind": "install_or_repair_docling",
            "message": "Install the pinned local Docling runtime, then retry the ingest.",
            "install_command": INSTALL_COMMAND,
            "check_command": CHECK_COMMAND,
            "native_model_fallback_requires_confirmation": True,
        },
    }


def custom_docling_command() -> Path | None:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_CMD", "").strip()
    return Path(value).expanduser() if value else None


def docling_command() -> Path:
    custom = custom_docling_command()
    if custom is not None:
        return custom
    return default_docling_home() / "venv" / "bin" / "docling"


def runtime_version() -> str | None:
    override = os.environ.get("LLM_OBSIDIAN_DOCLING_VERSION", "").strip()
    if override:
        return override
    python = default_docling_home() / "venv" / "bin" / "python"
    if not python.is_file():
        return None
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('docling'))",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def models_ready() -> bool:
    if custom_docling_command() is not None:
        return True
    marker = default_docling_home() / "models-manifest.json"
    if not marker.is_file() or not default_models_path().is_dir():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        data.get("docling_version") == DOCLING_VERSION
        and data.get("python_version") == DOCLING_PYTHON_VERSION
        and data.get("languages") == OCR_LANGUAGES
        and data.get("easyocr_required_files") == EASYOCR_REQUIRED_FILES
        and data.get("models") == DOCLING_MODELS
        and any(default_models_path().iterdir())
        and all(
            (default_models_path() / "EasyOcr" / name).is_file()
            for name in EASYOCR_REQUIRED_FILES
        )
    )


def runtime_status() -> dict[str, Any]:
    command = docling_command()
    version = runtime_version()
    command_ok = command.is_file() and os.access(command, os.X_OK)
    model_ok = models_ready()
    ok = command_ok and version == DOCLING_VERSION and model_ok
    payload: dict[str, Any] = {
        "version": 1,
        "status": "ok" if ok else "dependency_missing",
        "docling": {
            "command": str(command),
            "expected_version": DOCLING_VERSION,
            "actual_version": version,
            "command_ready": command_ok,
            "models_path": str(default_models_path()),
            "models_ready": model_ok,
            "ocr_languages": OCR_LANGUAGES,
        },
    }
    if not ok:
        payload.update(needs_docling_payload("Pinned Docling runtime or ru/en models are missing."))
        payload["docling"] = {
            "command": str(command),
            "expected_version": DOCLING_VERSION,
            "actual_version": version,
            "command_ready": command_ok,
            "models_path": str(default_models_path()),
            "models_ready": model_ok,
            "ocr_languages": OCR_LANGUAGES,
        }
    return payload


class MarkdownHTMLParser(HTMLParser):
    """Small networkless HTML-to-Markdown converter for local documents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.pre_depth = 0
        self.list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "header", "footer", "table", "tr"}:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            self.list_depth += 1
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n" + "  " * max(0, self.list_depth - 1) + "- ")
        elif tag == "pre":
            self.pre_depth += 1
            self.parts.append("\n\n```text\n")
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)
        elif tag == "pre":
            self.parts.append("\n```\n")
            self.pre_depth = max(0, self.pre_depth - 1)
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")
        elif tag in {"p", "div", "section", "article", "table", "tr"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.pre_depth:
            self.parts.append(data)
        else:
            self.parts.append(re.sub(r"\s+", " ", data))

    def markdown(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip() + "\n"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is not valid UTF-8: {exc}") from exc


def builtin_markdown(path: Path) -> str:
    suffix = path.suffix.lower()
    raw = read_text(path)
    if suffix in {".md", ".markdown", ".txt"}:
        return raw if raw.endswith("\n") else raw + "\n"
    if suffix == ".json":
        try:
            value = json.dumps(json.loads(raw), ensure_ascii=False, indent=2, sort_keys=True)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        return f"# {path.name}\n\n```json\n{value}\n```\n"
    if suffix in {".yaml", ".yml"}:
        return f"# {path.name}\n\n```yaml\n{raw.rstrip()}\n```\n"
    if suffix == ".csv":
        # Parse one row to reject binary/malformed NUL input while preserving the
        # original delimiter and numeric precision for downstream synthesis.
        if "\x00" in raw:
            raise ValueError("CSV contains NUL bytes")
        try:
            next(csv.reader(raw.splitlines()), None)
        except csv.Error as exc:
            raise ValueError(f"invalid CSV: {exc}") from exc
        return f"# {path.name}\n\n```csv\n{raw.rstrip()}\n```\n"
    if suffix in HTML_EXTENSIONS:
        parser = MarkdownHTMLParser()
        parser.feed(raw)
        parser.close()
        return parser.markdown()
    raise ValueError(f"unsupported text format: {suffix or '<none>'}")


def collect_confidences(value: Any, output: list[float]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if "confidence" in str(key).lower() and isinstance(item, (int, float)):
                number = float(item)
                if 0.0 <= number <= 1.0:
                    output.append(number)
            else:
                collect_confidences(item, output)
    elif isinstance(value, list):
        for item in value:
            collect_confidences(item, output)


def page_count(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    pages = value.get("pages")
    if isinstance(pages, (list, dict)):
        return len(pages)
    for key in ("document", "doc"):
        found = page_count(value.get(key))
        if found is not None:
            return found
    return None


def docling_convert(source: Path, output: Path, timeout_seconds: int) -> tuple[Path, Path, dict[str, Any]]:
    command = docling_command()
    args = [
        str(command),
        "convert",
        "--to",
        "md",
        "--to",
        "json",
        "--output",
        str(output),
        "--image-export-mode",
        "referenced",
        "--html-image-fetch",
        "none",
        "--pipeline",
        "standard",
        "--ocr",
        "--no-force-ocr",
        "--tables",
        "--ocr-engine",
        "easyocr",
        "--ocr-lang",
        ",".join(OCR_LANGUAGES),
        "--table-mode",
        "accurate",
        "--artifacts-path",
        str(default_models_path()),
        "--no-enable-remote-services",
        "--no-allow-external-plugins",
        "--document-timeout",
        str(timeout_seconds),
        "--page-batch-size",
        "4",
        "--device",
        "auto",
        str(source),
    ]
    env = os.environ.copy()
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "DOCLING_ARTIFACTS_PATH": str(default_models_path()),
            "EASYOCR_MODULE_PATH": str(default_models_path() / "EasyOcr"),
        }
    )
    try:
        result = subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds + 30,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Docling exceeded the {timeout_seconds}s document timeout") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"Docling conversion failed: {tail[:500]}")

    markdown_candidates = sorted(output.glob("*.md"))
    json_candidates = sorted(path for path in output.glob("*.json") if path.name != "manifest.json")
    if not markdown_candidates or not json_candidates:
        raise RuntimeError("Docling completed without Markdown and JSON artifacts")
    markdown = markdown_candidates[0]
    document_json = json_candidates[0]
    try:
        structured = json.loads(document_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Docling JSON is invalid: {exc}") from exc
    return markdown, document_json, structured


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def cached_payload(target: Path, source_hash: str, profile_hash: str) -> dict[str, Any] | None:
    manifest = target / "manifest.json"
    markdown = target / "document.md"
    if not manifest.is_file() or not markdown.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("source", {}).get("sha256") != source_hash:
        return None
    if payload.get("processor", {}).get("profile_sha256") != profile_hash:
        return None
    payload["cached"] = True
    payload["status"] = "cached" if payload.get("quality", {}).get("accepted") else "low_quality"
    payload["artifacts"] = {
        "root": str(target),
        "markdown": str(markdown),
        "docling_json": str(target / "document.docling.json")
        if (target / "document.docling.json").is_file()
        else None,
    }
    return payload


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
            status["source"] = {"path": str(source), "size": size, "format": suffix.lstrip(".")}
            emit(status, json_output=args.json, code=2)
        processor_version = runtime_version() or DOCLING_VERSION
    else:
        emit(
            {
                "version": 1,
                "status": "unsupported",
                "reason": f"unsupported file extension: {suffix or '<none>'}",
                "supported": sorted(TEXT_EXTENSIONS | HTML_EXTENSIONS | DOCLING_EXTENSIONS),
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
                emit(cached, json_output=args.json, code=0 if cached["status"] == "cached" else 3)

        work = Path(tempfile.mkdtemp(prefix=f".{cache_key}.tmp-", dir=cache_root))
        try:
            structured: dict[str, Any] | None = None
            if processor == "builtin":
                try:
                    markdown_text = builtin_markdown(source)
                except (OSError, ValueError) as exc:
                    emit(
                        {"version": 1, "status": "conversion_failed", "reason": str(exc)},
                        json_output=args.json,
                        code=4,
                    )
                (work / "document.md").write_text(markdown_text, encoding="utf-8")
            else:
                raw_dir = work / "docling-output"
                raw_dir.mkdir()
                try:
                    markdown_path, json_path, structured = docling_convert(source, raw_dir, args.timeout)
                except RuntimeError as exc:
                    emit(
                        {"version": 1, "status": "conversion_failed", "reason": str(exc)},
                        json_output=args.json,
                        code=4,
                    )
                for child in raw_dir.iterdir():
                    if child in {markdown_path, json_path}:
                        continue
                    destination = work / child.name
                    if child.is_dir():
                        shutil.copytree(child, destination)
                    else:
                        shutil.copy2(child, destination)
                shutil.copy2(markdown_path, work / "document.md")
                shutil.copy2(json_path, work / "document.docling.json")
                shutil.rmtree(raw_dir)

            markdown_text = (work / "document.md").read_text(encoding="utf-8")
            nonspace_characters = len(re.sub(r"\s+", "", markdown_text))
            pages = page_count(structured) if structured is not None else None
            if pages is not None and pages > args.max_pages:
                emit(
                    {
                        "version": 1,
                        "status": "unsupported",
                        "reason": f"document has {pages} pages; configured limit is {args.max_pages}",
                    },
                    json_output=args.json,
                    code=4,
                )
            confidences: list[float] = []
            if structured is not None:
                collect_confidences(structured, confidences)
            confidence_average = sum(confidences) / len(confidences) if confidences else None
            minimum_characters = MIN_USEFUL_CHARACTERS if processor == "docling" else 1
            enough_text = nonspace_characters >= minimum_characters
            confidence_ok = (
                processor != "docling"
                or confidence_average is None
                or confidence_average >= LOW_CONFIDENCE_THRESHOLD
            )
            accepted = enough_text and confidence_ok
            status_name = "ok" if accepted else "low_quality"
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
                },
            }
            if not accepted:
                manifest_payload["reason"] = "normalized output is empty/short or confidence is below threshold; inspect before ingest"
            write_manifest(work / "manifest.json", manifest_payload)
            if target.exists():
                shutil.rmtree(target)
            os.replace(work, target)
            manifest_payload["artifacts"] = {
                "root": str(target),
                "markdown": str(target / "document.md"),
                "docling_json": str(target / "document.docling.json")
                if (target / "document.docling.json").is_file()
                else None,
            }
            emit(manifest_payload, json_output=args.json, code=0 if accepted else 3)
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    normalize_parser = subparsers.add_parser("normalize", help="normalize one local file")
    normalize_parser.add_argument("source")
    normalize_parser.add_argument("--cache-root", default=str(default_cache_root()))
    normalize_parser.add_argument("--max-bytes", type=positive_int, default=DEFAULT_MAX_BYTES)
    normalize_parser.add_argument("--max-pages", type=positive_int, default=DEFAULT_MAX_PAGES)
    normalize_parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT_SECONDS)
    normalize_parser.add_argument("--force", action="store_true")
    normalize_parser.add_argument("--json", action="store_true")
    check_parser = subparsers.add_parser("check", help="validate the pinned Docling runtime")
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
        emit(payload, json_output=args.json, code=0 if payload.get("status") == "ok" else 2)
    normalize(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
