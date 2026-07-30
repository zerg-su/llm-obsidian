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


def custom_docling_adapter() -> Path | None:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_ADAPTER", "").strip()
    return Path(value).expanduser() if value else None


def docling_python() -> Path:
    return default_docling_home() / "venv" / "bin" / "python"


def docling_adapter() -> Path:
    return custom_docling_adapter() or ROOT / "scripts" / "docling-adapter.py"


def runtime_version() -> str | None:
    override = os.environ.get("LLM_OBSIDIAN_DOCLING_VERSION", "").strip()
    if override:
        return override
    python = docling_python()
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
    if custom_docling_adapter() is not None:
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
    adapter = docling_adapter()
    python = docling_python()
    version = runtime_version()
    if custom_docling_adapter() is not None:
        command_ok = adapter.is_file() and os.access(adapter, os.X_OK)
        command = adapter
    else:
        command_ok = python.is_file() and adapter.is_file()
        command = python
    model_ok = models_ready()
    ok = command_ok and version == DOCLING_VERSION and model_ok
    payload: dict[str, Any] = {
        "version": 1,
        "status": "ok" if ok else "dependency_missing",
        "docling": {
            "command": str(command),
            "adapter": str(adapter),
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
            "adapter": str(adapter),
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


def docling_convert(
    source: Path,
    output: Path,
    timeout_seconds: int,
    max_pages: int,
    max_bytes: int,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    custom = custom_docling_adapter()
    command = [str(custom)] if custom is not None else [str(docling_python()), str(docling_adapter())]
    args = command + [
        "--source",
        str(source),
        "--output",
        str(output),
        "--models-path",
        str(default_models_path()),
        "--ocr-languages",
        ",".join(OCR_LANGUAGES),
        "--timeout",
        str(timeout_seconds),
        "--max-pages",
        str(max_pages),
        "--max-bytes",
        str(max_bytes),
        "--pdf-text-threshold",
        str(PDF_TEXT_THRESHOLD),
        "--pdf-raster-threshold",
        str(PDF_RASTER_THRESHOLD),
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

    markdown = output / "document.md"
    document_json = output / "document.json"
    adapter_json = output / "adapter.json"
    if not markdown.is_file() or not document_json.is_file() or not adapter_json.is_file():
        raise RuntimeError("Docling adapter completed without Markdown, JSON, and metadata artifacts")
    try:
        structured = json.loads(document_json.read_text(encoding="utf-8"))
        adapter_metadata = json.loads(adapter_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Docling adapter JSON is invalid: {exc}") from exc
    for required, expected in (
        ("offline", True),
        ("remote_services_allowed", False),
        ("external_plugins_allowed", False),
    ):
        if adapter_metadata.get(required) is not expected:
            raise RuntimeError(f"Docling adapter did not prove isolation invariant: {required}")
    return markdown, document_json, structured, adapter_metadata


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def stable_docling_text(path: Path, raw_dir: Path) -> str:
    """Rewrite Docling's temporary absolute artifact paths as cache-relative paths."""
    text = path.read_text(encoding="utf-8")
    prefix = raw_dir.resolve().as_posix().rstrip("/") + "/"
    stable = text.replace(prefix, "")
    if prefix in stable:
        raise RuntimeError(f"Docling artifact still references temporary output: {path.name}")
    return stable


PAGE_MARKER_RE = re.compile(r"^<!-- llm-obsidian-page: (\d+) -->$")
MIXED_WORD_RE = re.compile(r"(?u)\b\w+\b")
INLINE_ENUM_RE = re.compile(r"(?<![\w.])(\d{1,2})[.)]\s+")
TERMINAL_RE = re.compile(r"[.!?…:;»”'\")\]]$")
LOWERCASE_START_RE = re.compile(r"^[«\"'([]*[a-zа-яё]")
FENCED_CODE_RE = re.compile(
    r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*(?=\n|$)"
)


def normalize_visible_punctuation(value: str) -> str:
    parts = re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]*`|<!--.*?-->)", value, flags=re.DOTALL)
    for index in range(0, len(parts), 2):
        text = parts[index]
        text = re.sub(r"[ \t]+([,.;:!?…%»\)])", r"\1", text)
        text = re.sub(r"([«\(])[ \t]+", r"\1", text)
        text = re.sub(r'"[ \t]+([^"\n]+?)[ \t]+"', r'"\1"', text)
        text = re.sub(r"\b(из|по|во|кое)[ \t]+-[ \t]*(?=[а-яё])", r"\1-", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<=[а-яё])[ \t]+-[ \t]*(то|либо|нибудь)\b", r"-\1", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<=\S)[ \t]+-[ \t]+(?=\S)", " — ", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        parts[index] = text
    return "".join(parts)


def sequential_enumerator_positions(value: str) -> list[int]:
    matches = list(INLINE_ENUM_RE.finditer(value))
    selected: set[int] = set()
    run: list[re.Match[str]] = []
    previous: int | None = None
    for match in matches:
        number = int(match.group(1))
        if previous is not None and number == previous + 1:
            run.append(match)
        else:
            if len(run) >= 3:
                selected.update(item.start() for item in run)
            run = [match]
        previous = number
    if len(run) >= 3:
        selected.update(item.start() for item in run)
    return sorted(selected)


def restore_inline_numbered_list(value: str) -> str:
    positions = sequential_enumerator_positions(value)
    if not positions:
        return value
    output = value
    for position in reversed(positions):
        prefix = "" if position == 0 or output[position - 1] == "\n" else "\n"
        output = output[:position] + prefix + output[position:]
    return output


def structural_block(value: str) -> bool:
    stripped = value.lstrip()
    lines = stripped.splitlines()
    if not lines:
        return True
    return any(
        line.startswith(("#", "![[", "![", "- ", "* ", "+ ", ">", "|", "```"))
        or line.startswith(("~~~", "<!-- llm-obsidian-preserved-code:"))
        or re.match(r"^\d{1,3}[.)]\s+", line) is not None
        for line in lines
    )


def safe_text_join(previous: str, following: str) -> bool:
    if structural_block(previous) or structural_block(following):
        return False
    previous_text = previous.rstrip()
    following_text = following.lstrip()
    return bool(
        previous_text
        and following_text
        and not TERMINAL_RE.search(previous_text)
        and LOWERCASE_START_RE.search(following_text)
    )


def deterministic_cleanup(markdown: str) -> str:
    preserved: dict[str, str] = {}

    def preserve_code(match: re.Match[str]) -> str:
        code = match.group(0)
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
        placeholder = f"<!-- llm-obsidian-preserved-code:{len(preserved)}:{digest} -->"
        preserved[placeholder] = code.rstrip("\n")
        return placeholder

    protected = FENCED_CODE_RE.sub(preserve_code, markdown)
    normalized = normalize_visible_punctuation(protected.replace("\u00ad\n", ""))
    raw_blocks = [
        block.strip()
        for block in re.split(r"\n{2,}", normalized)
        if block.strip() and re.fullmatch(r"[.,;:]+", block.strip()) is None
    ]
    blocks: list[str] = []
    index = 0
    while index < len(raw_blocks):
        block = raw_blocks[index]
        marker = PAGE_MARKER_RE.fullmatch(block)
        if marker and blocks and index + 1 < len(raw_blocks):
            following = raw_blocks[index + 1]
            if not structural_block(following):
                following = restore_inline_numbered_list(following)
            if safe_text_join(blocks[-1], following):
                blocks[-1] = f"{blocks[-1]} {block} {following}"
                index += 2
                continue
        if not structural_block(block):
            block = restore_inline_numbered_list(block)
        if not structural_block(block):
            block = re.sub(r"(?<!  )\n(?!\n)", " ", block)
        if blocks and safe_text_join(blocks[-1], block):
            blocks[-1] = f"{blocks[-1]} {block}"
        else:
            blocks.append(block)
        index += 1
    result = "\n\n".join(blocks).strip() + "\n"
    for placeholder, code in preserved.items():
        result = result.replace(placeholder, code)
    return result


def page_for_offset(markdown: str, offset: int) -> int | None:
    page: int | None = None
    for match in re.finditer(r"<!-- llm-obsidian-page: (\d+) -->", markdown[:offset]):
        page = int(match.group(1))
    return page


def suspicious_mixed_words(value: str) -> list[str]:
    found: list[str] = []
    for match in MIXED_WORD_RE.finditer(value):
        word = match.group(0)
        if re.search(r"[A-Za-z]", word) and re.search(r"[А-Яа-яЁё]", word):
            found.append(word)
    return found


def quality_issues(markdown: str, adapter: dict[str, Any] | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(
        code: str,
        start: int,
        end: int,
        *,
        provenance: str = "native",
        paragraph_target: bool = False,
    ) -> None:
        boundary = markdown.rfind("\n\n", 0, start)
        paragraph_start = boundary + 2 if boundary >= 0 else 0
        paragraph_end = markdown.find("\n\n", end)
        if paragraph_end < 0:
            paragraph_end = len(markdown)
        target_start = paragraph_start if paragraph_target else start
        target_end = paragraph_end if paragraph_target else end
        text = markdown[target_start:target_end].strip()
        if not text:
            return
        issues.append(
            {
                "code": code,
                "page": page_for_offset(markdown, start),
                "provenance": provenance,
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text[: MAX_REPAIR_SEGMENT_CHARACTERS - 600],
                "truncated": len(text) > MAX_REPAIR_SEGMENT_CHARACTERS - 600,
                "context_before": markdown[max(paragraph_start, target_start - 300):target_start],
                "context_after": markdown[target_end:min(paragraph_end, target_end + 300)],
            }
        )

    for match in re.finditer("\ufffd", markdown):
        add("replacement_character", match.start(), match.end())
    for match in MIXED_WORD_RE.finditer(markdown):
        word = match.group(0)
        if re.search(r"[A-Za-z]", word) and re.search(r"[А-Яа-яЁё]", word):
            add("suspicious_mixed_script", match.start(), match.end())
    heading_pattern = re.compile(
        r"(?<=[.!?…])\s+([А-ЯЁA-Z][А-ЯЁA-Z0-9 -]{4,60}?)\s+(?=[А-ЯЁA-Z][а-яёa-z])"
    )
    for match in heading_pattern.finditer(markdown):
        add("probable_heading", match.start(1), match.end(1))
    line_offset = 0
    for line in markdown.splitlines(keepends=True):
        positions = sequential_enumerator_positions(line)
        if positions:
            add(
                "inline_numbered_list",
                line_offset + positions[0],
                line_offset + positions[-1] + 2,
                paragraph_target=True,
            )
        line_offset += len(line)

    ocr_pages: set[int] = set()
    if adapter:
        ocr_pages = {int(value) for value in adapter.get("ocr_pages", [])}
        for metric in adapter.get("page_metrics", []):
            if metric.get("mode") == "low_text":
                issues.append(
                    {
                        "code": "low_text_coverage",
                        "page": int(metric["page"]),
                        "provenance": "native",
                        "source_sha256": stable_hash(metric),
                        "text": "",
                        "truncated": False,
                        "context_before": "",
                        "context_after": "",
                    }
                )
    for ordinal, issue in enumerate(issues):
        if issue.get("page") in ocr_pages:
            issue["provenance"] = "ocr"
            if issue["code"] == "suspicious_mixed_script":
                issue["code"] = "image_ocr_contamination"
        issue["segment_id"] = hashlib.sha256(
            f"{issue['code']}:{issue.get('page')}:{ordinal}:{issue['source_sha256']}".encode("utf-8")
        ).hexdigest()[:20]
    return issues


def issue_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        code = str(issue["code"])
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def repair_bundle(
    source_hash: str,
    clean_hash: str,
    markdown: str,
    issues: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    material = [item for item in issues if item["code"] != "low_text_coverage" or item.get("text")]
    if not material:
        return None, False
    characters = sum(
        len(str(item.get("text", "")))
        + len(str(item.get("context_before", "")))
        + len(str(item.get("context_after", "")))
        for item in material
    )
    ratio_cap = max(1, int(len(markdown) * MAX_REPAIR_DOCUMENT_RATIO))
    over_cap = (
        len(material) > MAX_REPAIR_SEGMENTS
        or any(item.get("truncated") for item in material)
        or characters > min(MAX_REPAIR_TOTAL_CHARACTERS, ratio_cap)
    )
    bundle = {
        "version": 1,
        "source_sha256": source_hash,
        "clean_sha256": clean_hash,
        "limits": {
            "max_segments": MAX_REPAIR_SEGMENTS,
            "max_segment_characters": MAX_REPAIR_SEGMENT_CHARACTERS,
            "max_total_characters": min(MAX_REPAIR_TOTAL_CHARACTERS, ratio_cap),
        },
        "segments": material[:MAX_REPAIR_SEGMENTS],
        "over_cap": over_cap,
    }
    return bundle, over_cap


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
    stored_status = str(payload.get("status", "low_quality"))
    payload["status"] = "cached" if stored_status == "ok" else stored_status
    payload["artifacts"] = {
        "root": str(target),
        "markdown": str(markdown),
        "raw_markdown": str(target / "document.raw.md")
        if (target / "document.raw.md").is_file()
        else str(markdown),
        "docling_json": str(target / "document.docling.json")
        if (target / "document.docling.json").is_file()
        else None,
        "adapter_metadata": str(target / "document.adapter.json")
        if (target / "document.adapter.json").is_file()
        else None,
        "repair_bundle": str(target / "repair-bundle.json")
        if (target / "repair-bundle.json").is_file()
        else None,
    }
    return payload


def status_exit_code(status: str) -> int:
    return {
        "ok": EXIT_OK,
        "cached": EXIT_OK,
        "runtime_unavailable": EXIT_RUNTIME_UNAVAILABLE,
        "dependency_missing": EXIT_RUNTIME_UNAVAILABLE,
        "low_quality": EXIT_LOW_QUALITY,
        "unsupported": EXIT_CONVERSION_FAILURE,
        "conversion_failed": EXIT_CONVERSION_FAILURE,
        "needs_semantic_cleanup": EXIT_NEEDS_SEMANTIC_CLEANUP,
        "needs_user_action": EXIT_NEEDS_USER_ACTION,
    }.get(status, EXIT_CONVERSION_FAILURE)


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
                emit(cached, json_output=args.json, code=status_exit_code(str(cached["status"])))

        work = Path(tempfile.mkdtemp(prefix=f".{cache_key}.tmp-", dir=cache_root))
        try:
            structured: dict[str, Any] | None = None
            adapter_metadata: dict[str, Any] | None = None
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
                (work / "document.raw.md").write_text(markdown_text, encoding="utf-8")
            else:
                raw_dir = work / "docling-output"
                raw_dir.mkdir()
                try:
                    markdown_path, json_path, structured, adapter_metadata = docling_convert(
                        source,
                        raw_dir,
                        args.timeout,
                        args.max_pages,
                        args.max_bytes,
                    )
                except RuntimeError as exc:
                    emit(
                        {"version": 1, "status": "conversion_failed", "reason": str(exc)},
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
                        {"version": 1, "status": "conversion_failed", "reason": str(exc)},
                        json_output=args.json,
                        code=4,
                    )
                clean_markdown = deterministic_cleanup(stable_markdown)
                (work / "document.raw.md").write_text(stable_markdown, encoding="utf-8")
                (work / "document.md").write_text(clean_markdown, encoding="utf-8")
                (work / "document.docling.json").write_text(stable_json, encoding="utf-8")
                write_manifest(work / "document.adapter.json", adapter_metadata)
                shutil.rmtree(raw_dir)

            markdown_text = (work / "document.md").read_text(encoding="utf-8")
            quality_text = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.DOTALL)
            nonspace_characters = len(re.sub(r"\s+", "", quality_text))
            pages = (
                int(adapter_metadata["pages"])
                if adapter_metadata is not None and adapter_metadata.get("pages") is not None
                else page_count(structured) if structured is not None else None
            )
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
            issues = quality_issues(markdown_text, adapter_metadata) if processor == "docling" else []
            clean_hash = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()
            bundle, over_cap = repair_bundle(source_hash, clean_hash, markdown_text, issues)
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
                manifest_payload["reason"] = "normalized output is empty/short or confidence is below threshold; inspect before ingest"
            elif over_cap:
                manifest_payload["reason"] = "semantic cleanup exceeds automatic repair limits"
                manifest_payload["action"] = {
                    "kind": "confirm_large_semantic_cleanup",
                    "message": "Inspect the repair bundle and ask before processing the document.",
                }
            elif bundle is not None:
                manifest_payload["reason"] = "bounded semantic cleanup is required before synthesis"
            write_manifest(work / "manifest.json", manifest_payload)
            if target.exists():
                shutil.rmtree(target)
            os.replace(work, target)
            manifest_payload["artifacts"] = {
                "root": str(target),
                "markdown": str(target / "document.md"),
                "raw_markdown": str(target / "document.raw.md"),
                "docling_json": str(target / "document.docling.json")
                if (target / "document.docling.json").is_file()
                else None,
                "adapter_metadata": str(target / "document.adapter.json")
                if (target / "document.adapter.json").is_file()
                else None,
                "repair_bundle": str(target / "repair-bundle.json")
                if (target / "repair-bundle.json").is_file()
                else None,
            }
            emit(manifest_payload, json_output=args.json, code=status_exit_code(status_name))
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
