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
            r"\bWebFetch\s*\(|\buse\s+`?WebFetch\b|"
            r"\bWebFetch\s+(?:the\b|https?://)|>\s*\.raw/",
            instruction,
            flags=re.I,
        ):
            issues.append(line)
    return issues


def preflight_block(text: str, heading: str) -> str:
    """Extract just the rewritten block, not unrelated numbered questions."""
    start = re.search(rf"(?m)^#+ {re.escape(heading)}[^\n]*", text)
    if start is None:
        raise AssertionError(f"Missing pre-flight block: {heading}")
    rest = text[start.end():]
    following = re.search(r"(?m)^#{1,3} ", rest)
    return start.group() + rest[:following.start() if following else len(rest)]


def preflight_issues(block: str) -> list[str]:
    """U-T2: reject mandatory/fixed >3-question wording in scoped blocks."""
    patterns = (
        r"Pre-flight\s*\(mandatory",
        r"(?:with|ask|asks|asking)\s+(?:all\s+|exactly\s+)?"
        r"(?:[4-9]|\d{2,}|four|five|six|seven|eight|nine|ten)\s+questions",
    )
    return [match.group() for pattern in patterns for match in re.finditer(pattern, block, re.I)]


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


class DraftContracts(unittest.TestCase):
    def test_u_t1_draft_isolation(self) -> None:
        text = (ROOT / "skills/draft/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(url_instruction_issues(text), [], "U-T1 / U6g")
        source = text.split("## Phase 1:", 1)[1].split("## Phase 2:", 1)[0]
        self.assertIn("paste", source.lower())
        self.assertIn("local file", source)
        self.assertNotIn("If a URL — fetch it", source)
        facts = text.split("## Phase 3.5:", 1)[1].split("## Phase 4:", 1)[0]
        self.assertIn("/research", facts)
        self.assertIn("docs tooling", facts)

    def test_u_t1_draft_mutations(self) -> None:
        text = (ROOT / "skills/draft/SKILL.md").read_text(encoding="utf-8")
        for instruction in (
            "allowed-tools: Read WebFetch",
            'WebFetch(url="https://example.com/thread")',
            "Verify against official documentation (WebFetch the project's docs).",
            "clean-thread > .raw/thread.md",
        ):
            with self.subTest(instruction=instruction):
                self.assertTrue(url_instruction_issues(text + "\n" + instruction))

    def test_u_t2_draft_preflight(self) -> None:
        text = (ROOT / "skills/draft/SKILL.md").read_text(encoding="utf-8")
        block = preflight_block(text, "Phase 0:")
        self.assertEqual(preflight_issues(block), [], "U-T2 / U3a")
        block = " ".join(block.split())
        self.assertIn("at most three questions", block)
        self.assertIn("Defaults:", block)
        self.assertIn("channel inferred from the source", block)
        self.assertIn("tone `formal-neutral`", block)
        self.assertIn("scope `answer-only`", block)
        self.assertIn("all constraints apply for external channels", block)
        self.assertIn(
            "Skip the phase when every item is resolved by the prompt or by the defaults above.",
            block,
        )

    def test_u_t2_mutations(self) -> None:
        for instruction in (
            "## Phase 0: Pre-flight (mandatory — AskUserQuestion)",
            "Single AskUserQuestion with 4 questions:",
            "Ask exactly four questions before proceeding.",
        ):
            with self.subTest(instruction=instruction):
                self.assertTrue(preflight_issues(instruction))
        self.assertEqual(preflight_issues("Ask at most three questions; skip resolved items."), [])
        fixture = (
            "### Phase A.0: Pre-flight\nAt most 2 questions; skip resolved items.\n"
            "### Phase A.1: Parse\nRead input.\n"
            "### Phase C.0: Pre-flight (mandatory)\nAsk 4 questions.\n"
        )
        block = preflight_block(fixture, "Phase A.0:")
        self.assertEqual(preflight_issues(block), [])
        self.assertNotIn("Phase C.0", block)

    def test_u3b_shared_output_layout(self) -> None:
        text = (ROOT / "skills/draft/SKILL.md").read_text(encoding="utf-8")
        compose = text.split("## Phase 3:", 1)[1].split("## Phase 3.5:", 1)[0]
        self.assertIn("Use the Phase 6 layout", compose)
        self.assertNotIn("Example shape:", compose)
        self.assertNotIn("The fix landed yesterday", compose)


if __name__ == "__main__":
    unittest.main()
