#!/usr/bin/env python3
"""Plan U mechanical contracts; behavioral acceptance is coordinator-owned."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def url_instruction_issues(text: str) -> list[str]:
    """U-T1: detect instructing forms, without treating prohibitions as calls.

    Scope is defuddle/draft, so no protected-flow operation needs an exemption.
    Keep checks narrow: this is structural regression evidence, not NLP policy.
    """
    issues = []
    for line in text.splitlines():
        if re.search(r"^allowed-tools:.*\bWebFetch\b", line):
            issues.append(line)
            continue
        # A prohibition must not hide an instruction elsewhere on the same line.
        instruction = re.sub(
            r"\b(?:never|do not|don't)\s+(?:use\s+)?`?WebFetch`?\b",
            "",
            line,
            flags=re.I,
        )
        if re.search(
            r"\bWebFetch\s*\(|\buse\s+`?WebFetch\b|>\s*\.raw/",
            instruction,
            flags=re.I,
        ):
            issues.append(line)
    return issues


class DefuddleContracts(unittest.TestCase):
    def test_d_u1_local_only(self) -> None:
        text = (ROOT / "skills/defuddle/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(url_instruction_issues(text), [], "D-U1 / U-T1")
        self.assertNotRegex(text, r"\bdefuddle\s+https?://")
        self.assertNotRegex(text, r"(?im)^\s*(?:curl|wget)\s+")
        self.assertNotRegex(text, r"(?i)\buse\s+`?(?:curl|wget)\b")
        for phrase in ("manual fallback", "never describe raw", "copyright/footer"):
            self.assertIn(phrase, text)

    def test_d_u1_documented_input(self) -> None:
        docs = (ROOT / "docs/ru/skills.md").read_text(encoding="utf-8")
        row = next(line for line in docs.splitlines() if line.startswith("| `defuddle`"))
        self.assertIn("Local read", row)
        self.assertIn("локаль", row)
        self.assertIn("HTML", row)
        self.assertIn("/defuddle page.html", row)
        self.assertIn("/wiki-ingest <URL>", row)
        self.assertNotIn("Network read", row)

    def test_u_t1_mutations(self) -> None:
        for instruction in (
            'WebFetch("https://example.com")',
            "If unavailable: use WebFetch, then clean the result.",
            "allowed-tools: Read Bash WebFetch",
            "defuddle page.html > .raw/articles/page.md",
            "never WebFetch; use WebFetch instead",
        ):
            with self.subTest(instruction=instruction):
                self.assertTrue(url_instruction_issues(instruction))
        for prohibition in (
            "never WebFetch",
            "Never use WebFetch.",
            "Do not use WebFetch; use /wiki-ingest <URL>.",
            "not with `defuddle <URL>`, `curl`/`wget`, or WebFetch.",
        ):
            with self.subTest(prohibition=prohibition):
                self.assertEqual(url_instruction_issues(prohibition), [])


if __name__ == "__main__":
    unittest.main()
