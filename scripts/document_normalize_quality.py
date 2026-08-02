"""Deterministic cleanup and bounded quality evidence for normalized documents."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from document_normalize_config import (
    MAX_REPAIR_DOCUMENT_RATIO,
    MAX_REPAIR_SEGMENT_CHARACTERS,
    MAX_REPAIR_SEGMENTS,
    MAX_REPAIR_TOTAL_CHARACTERS,
    stable_hash,
)


PAGE_MARKER_RE = re.compile(r"^<!-- llm-obsidian-page: (\d+) -->$")
MIXED_WORD_RE = re.compile(r"(?u)\b\w+\b")
INLINE_ENUM_RE = re.compile(r"(?<![\w.])(\d{1,2})[.)]\s+")
TERMINAL_RE = re.compile(r"[.!?…:;»”'\")\]]$")
LOWERCASE_START_RE = re.compile(r"^[«\"'([]*[a-zа-яё]")
FENCED_CODE_RE = re.compile(
    r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*(?=\n|$)"
)


def normalize_visible_punctuation(value: str) -> str:
    parts = re.split(
        r"(```.*?```|~~~.*?~~~|`[^`\n]*`|<!--.*?-->)", value, flags=re.DOTALL
    )
    for index in range(0, len(parts), 2):
        text = parts[index]
        text = re.sub(r"[ \t]+([,.;:!?…%»\)])", r"\1", text)
        text = re.sub(r"([«\(])[ \t]+", r"\1", text)
        text = re.sub(r'"[ \t]+([^"\n]+?)[ \t]+"', r'"\1"', text)
        text = re.sub(
            r"\b(из|по|во|кое)[ \t]+-[ \t]*(?=[а-яё])",
            r"\1-",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"(?<=[а-яё])[ \t]+-[ \t]*(то|либо|нибудь)\b",
            r"-\1",
            text,
            flags=re.IGNORECASE,
        )
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


def quality_issues(
    markdown: str, adapter: dict[str, Any] | None
) -> list[dict[str, Any]]:
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
                "context_before": markdown[
                    max(paragraph_start, target_start - 300) : target_start
                ],
                "context_after": markdown[
                    target_end : min(paragraph_end, target_end + 300)
                ],
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
            f"{issue['code']}:{issue.get('page')}:{ordinal}:{issue['source_sha256']}".encode(
                "utf-8"
            )
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
    material = [
        item
        for item in issues
        if item["code"] != "low_text_coverage" or item.get("text")
    ]
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
