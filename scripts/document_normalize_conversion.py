"""Networkless text and Docling conversion adapters."""

from __future__ import annotations

import csv
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from document_normalize_config import (
    HTML_EXTENSIONS,
    OCR_LANGUAGES,
    PDF_RASTER_THRESHOLD,
    PDF_TEXT_THRESHOLD,
)
from document_normalize_runtime import (
    custom_docling_adapter,
    default_models_path,
    docling_adapter,
    docling_python,
)


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
        elif tag in {"p", "div", "section", "article", "table", "tr"} or re.fullmatch(
            r"h[1-6]", tag
        ):
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
    command = (
        [str(custom)]
        if custom is not None
        else [str(docling_python()), str(docling_adapter())]
    )
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
        raise RuntimeError(
            f"Docling exceeded the {timeout_seconds}s document timeout"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"Docling conversion failed: {tail[:500]}")

    markdown = output / "document.md"
    document_json = output / "document.json"
    adapter_json = output / "adapter.json"
    if not markdown.is_file() or not document_json.is_file() or not adapter_json.is_file():
        raise RuntimeError(
            "Docling adapter completed without Markdown, JSON, and metadata artifacts"
        )
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
            raise RuntimeError(
                f"Docling adapter did not prove isolation invariant: {required}"
            )
    return markdown, document_json, structured, adapter_metadata


def stable_docling_text(path: Path, raw_dir: Path) -> str:
    """Rewrite Docling's temporary absolute artifact paths as cache-relative paths."""
    text = path.read_text(encoding="utf-8")
    prefix = raw_dir.resolve().as_posix().rstrip("/") + "/"
    stable = text.replace(prefix, "")
    if prefix in stable:
        raise RuntimeError(
            f"Docling artifact still references temporary output: {path.name}"
        )
    return stable
