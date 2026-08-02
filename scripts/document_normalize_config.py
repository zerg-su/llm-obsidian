"""Shared configuration and stable helpers for document normalization."""

from __future__ import annotations

import hashlib
import json
import os
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

# Bump whenever stable artifact semantics change so old cache entries cannot be
# mistaken for results produced by the current profile.
PROFILE_VERSION = 4
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
PDF_TEXT_THRESHOLD = 40
PDF_RASTER_THRESHOLD = 0.50
MAX_REPAIR_SEGMENTS = 20
MAX_REPAIR_SEGMENT_CHARACTERS = 2_000
MAX_REPAIR_TOTAL_CHARACTERS = 20_000
MAX_REPAIR_DOCUMENT_RATIO = 0.15
EXIT_OK = 0
EXIT_RUNTIME_UNAVAILABLE = 2
EXIT_LOW_QUALITY = 3
EXIT_CONVERSION_FAILURE = 4
EXIT_NEEDS_SEMANTIC_CLEANUP = 5
EXIT_NEEDS_USER_ACTION = 6
INSTALL_COMMAND = "python3 scripts/install-docling.py install"
CHECK_COMMAND = "python3 scripts/document-normalize.py check"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_cache_root() -> Path:
    value = os.environ.get("LLM_OBSIDIAN_DOCUMENT_CACHE", "")
    return Path(value).expanduser() if value else ROOT / ".vault-meta" / "document-cache"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
