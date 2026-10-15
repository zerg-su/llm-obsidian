#!/usr/bin/env python3
"""Check Markdown against the plain technical style.

Rules and word lists live in docs/skill-references/plain-style.md (the
``text lint:<name>`` blocks). This script only applies them.

Exit codes: 0 no errors, 1 errors found, 2 usage, IO or style-file error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STYLE = ROOT / "docs" / "skill-references" / "plain-style.md"

FENCE_RX = re.compile(r"^\s*(```|~~~)")
LIST_RX = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
HEADING_RX = re.compile(r"^(#{1,6})\s+(.*)$")
HEADING_KEY_RX = re.compile(r"^((?:\d+(?:\.\d+)*\.?)|[A-Z]{1,3}-\d+)\b")
ID_RX = re.compile(r"\b[A-Z]{1,3}-\d+\b")
INLINE_CODE_RX = re.compile(r"`[^`\n]*`")
LINK_RX = re.compile(r"\[([^\]]*)\]\([^)]*\)")
COMMENT_RX = re.compile(r"<!--.*?-->")
SENTENCE_END_RX = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ0-9\"«(\[`])")
WORD_RX = re.compile(r"[\w'’-]*\w")
GLOSS_WINDOW = 60
COMMAND_LANGS = {"bash", "sh", "shell", "fish", "console", "zsh"}


@dataclass
class Finding:
    file: str
    line: int
    level: str
    rule: str
    message: str


@dataclass
class Unit:
    """One prose block: a paragraph, list item, table row or heading."""

    kind: str
    line: int
    text: str


def load_lists(style: Path) -> dict[str, list[str]]:
    text = style.read_text(encoding="utf-8")
    lists: dict[str, list[str]] = {}
    for match in re.finditer(r"```text lint:([a-z-]+)\n(.*?)\n```", text, re.S):
        items = [item.strip().lower() for item in match.group(2).split(",")]
        lists[match.group(1)] = [item for item in items if item]
    if "banned-en" not in lists:
        raise ValueError(f"{style}: no 'text lint:banned-en' block")
    return lists


def scrub(text: str) -> str:
    """Replace inline code with one token, keep link text, drop comments."""
    text = COMMENT_RX.sub("", text)
    text = INLINE_CODE_RX.sub("CODE", text)
    return LINK_RX.sub(r"\1", text)


def parse_units(lines: list[str]) -> tuple[list[Unit], list[tuple[str, str]]]:
    """Split Markdown into prose units. Also return (lang, line) code lines."""
    units: list[Unit] = []
    code: list[tuple[str, str]] = []
    fence: str | None = None
    lang = ""
    current: Unit | None = None
    for number, line in enumerate(lines, start=1):
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
            else:
                code.append((lang, line))
            continue
        match = FENCE_RX.match(line)
        if match:
            fence, lang, current = match.group(1), line.strip()[3:].split(" ")[0].lower(), None
            continue
        stripped = line.strip()
        if not stripped:
            current = None
            continue
        heading = HEADING_RX.match(line)
        if heading or stripped.startswith("|"):
            units.append(Unit("heading" if heading else "table", number, stripped))
            current = None
        elif LIST_RX.match(line) or current is None or stripped.startswith(">"):
            current = Unit("prose", number, LIST_RX.sub("", stripped).lstrip("> "))
            units.append(current)
        else:
            current.text += " " + stripped
    return units, code


def word_count(sentence: str) -> int:
    return len(WORD_RX.findall(sentence))


def check_length(path: str, unit: Unit, max_words: int, warn_words: int) -> list[Finding]:
    found = []
    for sentence in SENTENCE_END_RX.split(scrub(unit.text)):
        count = word_count(sentence)
        if count > max_words:
            found.append(Finding(path, unit.line, "error", "sentence-length", f"{count} words > {max_words}"))
        elif count > warn_words:
            found.append(Finding(path, unit.line, "warning", "sentence-length", f"{count} words > {warn_words}"))
    return found


def phrase_rx(phrase: str, inflect: bool = False) -> re.Pattern[str]:
    """Match a phrase as whole words. With inflect, also match English forms
    of a single word: -s, -es, -d, -ed, -ing (leverage -> leveraging)."""
    body = re.escape(phrase)
    if inflect and re.fullmatch(r"[a-z]+", phrase):
        stem = re.escape(phrase[:-1]) if phrase.endswith("e") else body
        body = rf"(?:{body}(?:s|es|d|ed)?|{stem}ing)"
    return re.compile(r"(?<![\w-])" + body + r"(?![\w-])", re.I)


def check_words(path: str, unit: Unit, lists: dict[str, list[str]]) -> list[Finding]:
    text = scrub(unit.text).replace("\u2019", "'")
    found = []
    if ";" in text:
        found.append(Finding(path, unit.line, "error", "semicolon", "split into sentences or a list"))
    for name, level, rule in (
        ("banned-en", "error", "banned-word"),
        ("empty-en", "warning", "empty-phrase"),
        ("empty-ru", "warning", "empty-phrase"),
        ("patterns-en", "warning", "slop-pattern"),
        ("patterns-ru", "warning", "slop-pattern"),
    ):
        for phrase in lists.get(name, []):
            if phrase_rx(phrase, inflect=name == "banned-en").search(text):
                found.append(Finding(path, unit.line, level, rule, f"'{phrase}'"))
    return found


def is_glossed(text: str, start: int, end: int) -> bool:
    if start > 0 and text[start - 1] == "(":
        return re.match(r"(?:[^()\n]{0,%d}?)\s+[—–-]\s+" % GLOSS_WINDOW, text[end:]) is not None
    window = text[end : end + GLOSS_WINDOW]
    paren = window.find("(")
    if paren < 0:
        return False
    between = window[:paren]
    return not ID_RX.search(between) and not re.search(r"[.!?]\s", between)


def ref_only(cell: str) -> bool:
    """A table cell like 'P-5, then P-6': the row itself explains the IDs."""
    return word_count(ID_RX.sub("", cell)) <= 3


def check_refs(path: str, unit: Unit, defined: set[str]) -> list[Finding]:
    parts = unit.text.strip("|").split("|") if unit.kind == "table" else [unit.text]
    found = []
    for part in parts:
        text = scrub(part)
        if unit.kind == "table" and ref_only(text):
            continue
        for match in ID_RX.finditer(text):
            if match.group(0) in defined and not is_glossed(text, match.start(), match.end()):
                found.append(Finding(path, unit.line, "error", "unglossed-ref", f"{match.group(0)} without a short gloss"))
    return found


def defined_ids(units: list[Unit]) -> set[str]:
    ids = set()
    for unit in units:
        heading = HEADING_RX.match(unit.text) if unit.kind == "heading" else None
        key = HEADING_KEY_RX.match(heading.group(2)) if heading else None
        if key and ID_RX.fullmatch(key.group(1)):
            ids.add(key.group(1))
    return ids


def skeleton(units: list[Unit]) -> list[tuple[int, str]]:
    result = []
    for unit in units:
        heading = HEADING_RX.match(unit.text) if unit.kind == "heading" else None
        if heading:
            key = HEADING_KEY_RX.match(heading.group(2))
            result.append((len(heading.group(1)), key.group(1).rstrip(".") if key else "-"))
    return result


def commands(code: list[tuple[str, str]]) -> list[str]:
    return [
        line.strip()
        for lang, line in code
        if lang in COMMAND_LANGS and line.strip() and not line.strip().startswith("#")
    ]


def ref_counts(units: list[Unit]) -> Counter[str]:
    return Counter(match for unit in units if unit.kind != "heading" for match in ID_RX.findall(scrub(unit.text)))


def first_diff(left: list, right: list) -> str:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return f"item {index + 1}: {a!r} != {b!r}"
    return f"length {len(left)} != {len(right)}"


def check_pair(path: str, main: tuple, other_path: str, other: tuple) -> list[Finding]:
    found = []
    (units, code), (other_units, other_code) = main, other
    if skeleton(units) != skeleton(other_units):
        found.append(Finding(path, 1, "error", "pair-headings", f"vs {other_path}: {first_diff(skeleton(units), skeleton(other_units))}"))
    if ref_counts(units) != ref_counts(other_units):
        delta = (ref_counts(units) - ref_counts(other_units)) + (ref_counts(other_units) - ref_counts(units))
        found.append(Finding(path, 1, "error", "pair-refs", f"vs {other_path}: counts differ for {sorted(delta)}"))
    if commands(code) != commands(other_code):
        found.append(Finding(path, 1, "error", "pair-commands", f"vs {other_path}: {first_diff(commands(code), commands(other_code))}"))
    return found


def lint(path: Path, lists: dict[str, list[str]], args: argparse.Namespace) -> list[Finding]:
    units, code = parse_units(path.read_text(encoding="utf-8").splitlines())
    ids = defined_ids(units)
    found: list[Finding] = []
    for unit in units:
        if unit.kind == "heading":
            continue
        if unit.kind == "prose":
            found += check_length(str(path), unit, args.max_words, args.warn_words)
        found += check_words(str(path), unit, lists)
        found += check_refs(str(path), unit, ids)
    if args.pair:
        other = parse_units(args.pair.read_text(encoding="utf-8").splitlines())
        found += check_pair(str(path), (units, code), str(args.pair), other)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--pair", type=Path, help="other-language version; check structure parity")
    parser.add_argument("--style", type=Path, default=DEFAULT_STYLE)
    parser.add_argument("--max-words", type=int, default=25)
    parser.add_argument("--warn-words", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        found = lint(args.file, load_lists(args.style), args)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(f"doc-lint: {exc}", file=sys.stderr)
        return 2
    errors = sum(1 for item in found if item.level == "error")
    if args.json:
        print(json.dumps({"findings": [asdict(item) for item in found], "errors": errors}, ensure_ascii=False, indent=2))
    else:
        for item in found:
            print(f"{item.file}:{item.line}: {item.level}: {item.rule}: {item.message}")
        print(f"doc-lint: {errors} errors, {len(found) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
