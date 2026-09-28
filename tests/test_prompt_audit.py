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


def numbered_steps(section: str) -> dict[int, str]:
    """Read top-level ordered steps, retaining indented command payloads."""
    matches = list(re.finditer(r"(?m)^(\d+)\. ", section))
    return {
        int(match.group(1)): section[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(section)]
        for i, match in enumerate(matches)
    }


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


class IngestContracts(unittest.TestCase):
    def test_d_u6f_transaction_order(self) -> None:
        text = (ROOT / "skills/wiki-ingest/SKILL.md").read_text(encoding="utf-8")
        single = text.split("## Single Source Ingest", 1)[1].split("## Batch Ingest", 1)[0]
        batch = text.split("## Batch Ingest", 1)[1].split("## Context Window Discipline", 1)[0]
        steps = numbered_steps(single)
        contradiction = next(n for n, step in steps.items() if "Check for contradictions before dispatch" in step)
        dispatch = next(n for n, step in steps.items() if "python3 scripts/vault-write.py <<" in step)
        with self.subTest(contract="contradictions before writes"):
            self.assertLess(contradiction, dispatch, "D-U6f")
            self.assertIn("both drafted page operations", steps[contradiction])
        steps = numbered_steps(batch)
        with self.subTest(contract="accumulate batch operations"):
            draft = " ".join(steps[3].split())
            self.assertIn("single-source steps 1–6", draft)
            self.assertIn("page and manifest", draft)
            self.assertIn("without per-source dispatch", draft)
        with self.subTest(contract="one transaction and conditional index"):
            final = " ".join(steps[5].split())
            self.assertIn("one final transaction", final)
            self.assertIn("`wiki/index.md` only for a new key hub", final)
            self.assertNotIn("Update index,", final)

    def test_u2a_read_updated_pages_and_retrieve(self) -> None:
        text = (ROOT / "skills/wiki-ingest/SKILL.md").read_text(encoding="utf-8")
        context = text.split("## Context Window Discipline", 1)[1].split("## Contradictions", 1)[0]
        context = " ".join(context.split())
        self.assertIn("never replaces reading a page you will update", context)
        self.assertIn("Read the existing pages you will update", context)
        self.assertIn("page needed to rule out a duplicate", context)
        self.assertIn('./scripts/retrieve.py "<query>" --top 5 --json', context)
        self.assertNotIn("Read only 3-5", context)
        self.assertNotIn("/search/simple/", context)

    def test_u2j_one_confirmation_rule(self) -> None:
        text = (ROOT / "skills/wiki-ingest/SKILL.md").read_text(encoding="utf-8")
        batch = text.split("## Batch Ingest", 1)[1].split("## Context Window Discipline", 1)[0]
        self.assertIn("**Confirmation**", batch)
        rule = " ".join(batch.split("**Confirmation**", 1)[1].split())
        for phrase in (
            "single step 2", "batch step 1", "unclear takeaways",
            "confirm a batch plan once", "Never re-confirm an approved batch",
            "re-ask resolved questions", "unattended runs escalate to the coordinator",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rule)
        for failure in ("low_quality", "needs_user_action", "unsupported", "conversion_failed"):
            self.assertIn(f"`{failure}`", rule)
        self.assertIn("Contradictions become callouts before dispatch", rule)
        self.assertNotIn("after every 10 sources", text)
        self.assertNotIn('Ask: "What should I emphasize?', text)

    def test_u2hi_source_scope_and_syntax(self) -> None:
        text = (ROOT / "skills/wiki-ingest/SKILL.md").read_text(encoding="utf-8")
        intro = text.split("## Delta Tracking", 1)[0]
        self.assertNotIn("8-15 wiki pages", intro)
        self.assertNotIn("kepano/obsidian-skills", intro)
        self.assertIn("updating existing pages rather than creating near-duplicates", intro)
        self.assertIn("Syntax reference: the `obsidian-markdown` skill", intro)


if __name__ == "__main__":
    unittest.main()
