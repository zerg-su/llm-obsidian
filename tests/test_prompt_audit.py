#!/usr/bin/env python3
"""Plan U mechanical contracts; behavioral acceptance is coordinator-owned."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
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


class LintContracts(unittest.TestCase):
    def test_d_u6e_dead_link_categories(self) -> None:
        text = (ROOT / "skills/wiki-lint/SKILL.md").read_text(encoding="utf-8")
        report = text.split("## Lint Report Format", 1)[1].split("## Naming Conventions", 1)[0]
        links = report.split("## Dead Links", 1)[1].split("## Missing Pages", 1)[0]
        self.assertNotIn("create stub or remove link", links)
        self.assertEqual(
            re.findall(r"(?m)^- \*\*(.+?)\*\*:", links),
            ["Renamed/moved", "Frontier (not yet created)", "Obsolete intent"],
        )
        self.assertIn("no auto-stub", links)
        self.assertIn("human review", links)

    def test_lint_writer_stages(self) -> None:
        text = (ROOT / "skills/wiki-lint/SKILL.md").read_text(encoding="utf-8")
        intro = " ".join(text.split("## Lint Checks", 1)[0].split())
        for phrase in (
            "scripts/vault-write.py", "one transaction per approved stage",
            "report (+ dashboard) first", "approved fixes afterwards",
            "separate transaction", "expected_sha256", "--sha256 <path>",
        ):
            self.assertIn(phrase, intro)
        reference = (ROOT / "skills/wiki-lint/references/semantic-tiling.md").read_text(encoding="utf-8")
        for content in (text, reference):
            self.assertNotIn("--report wiki/", content)
            self.assertIn("stdout", content)
            self.assertIn("report transaction", content)

    def test_lint_severity_and_generic_guidance(self) -> None:
        text = (ROOT / "skills/wiki-lint/SKILL.md").read_text(encoding="utf-8")
        for obsolete in (
            "All five issues are", "WARN-level patterns", "per план Phase 4.9 P1",
            "added 2026-06-09", "added 2026-06-10", "deterministic since 2026-07-03",
            "Frontmatter Discipline (P1)", "Disabled in this vault", "zero usage",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertFalse(obsolete in text, f"Obsolete guidance: {obsolete}")
        self.assertIn("severity from validator output", text)
        self.assertIn("cosmetic findings", text)
        self.assertIn("explicit `sessions: []`", text)
        self.assertIn("summary content appended into the date field silently bloats hot.md", text)
        self.assertIn("Lint does not generate canvas maps", text)
        self.assertIn("only on explicit request", text)
        self.assertIn("canvas core-plugin is enabled", text)


class QueryContracts(unittest.TestCase):
    def test_query_retrieval_modes(self) -> None:
        text = (ROOT / "skills/wiki-query/SKILL.md").read_text(encoding="utf-8")
        standard = next(line for line in text.splitlines() if line.startswith("| **Standard**"))
        self.assertIn("hot.md + retrieve.py sections + 3-5 pages", standard)
        self.assertIn("index when navigational or retrieval coverage looks thin", standard)
        deep = text.split("## Deep Mode", 1)[1].split("## Section retrieval", 1)[0]
        candidates = numbered_steps(deep)[2]
        self.assertIn('./scripts/retrieve.py "<question>" --top 10 --json', candidates)
        self.assertIn("index scan", candidates)
        self.assertIn("`tag-search.py` as optional cross-check", candidates)

    def test_wiki_curated_index(self) -> None:
        text = (ROOT / "skills/wiki/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("master catalog of key hubs; folder `_index.md` listings regenerate automatically", text)
        self.assertFalse("update on every ingest" in text, "U2k: unconditional master-index update")


class SaveContracts(unittest.TestCase):
    def test_u_t2_save_fast_path(self) -> None:
        text = (ROOT / "skills/save/SKILL.md").read_text(encoding="utf-8")
        block = preflight_block(text, "Phase 0 —")
        self.assertEqual(preflight_issues(block), [])
        self.assertEqual(list(numbered_steps(block)), [1, 2, 3])
        self.assertIn("**infer, show, proceed**", block)
        self.assertIn("without AskUserQuestion", block)
        self.assertIn("ask ONE AskUserQuestion", block)
        self.assertTrue(preflight_issues(block + "\nAsk four questions."))

    def test_save_provenance_and_dated_wording(self) -> None:
        text = (ROOT / "skills/save/SKILL.md").read_text(encoding="utf-8")
        for obsolete in (
            "~95 calls/month", "34 fixed in lint 2026-06-09",
            "sanctioned exception", "feedback_skill_preflight_clarification",
            "feedback_session_id_in_frontmatter",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertFalse(obsolete in text, f"Obsolete guidance: {obsolete}")
        self.assertIn("[[file-name|Label]]", text)
        template = text.split("## Frontmatter Template", 1)[1].split("## Writing Style", 1)[0]
        self.assertIn("CLAUDE.md", template)
        self.assertIn("./scripts/current-session-id.sh", template)
        self.assertFalse("[[.raw/" in template, "U6b: raw source must be path metadata")


class FindSessionContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / "skills/find-session/SKILL.md").read_text(encoding="utf-8")
        scratch = tempfile.TemporaryDirectory(prefix="prompt-audit-sessions-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()
        self.assertFalse(self.scratch.is_relative_to(ROOT.resolve()))

    def snippet(self, heading: str) -> str:
        section = self.text.split(heading, 1)[1]
        match = re.search(r"```bash\n([\s\S]*?)\n```", section)
        self.assertIsNotNone(match, f"Missing snippet after {heading}")
        return match.group(1)

    def write_jsonl(self, relative: str, records: list[dict]) -> Path:
        path = self.scratch / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def run_snippet(self, snippet: str) -> list[str]:
        before = {p.relative_to(self.scratch): p.read_bytes() for p in self.scratch.rglob("*") if p.is_file()}
        result = subprocess.run(
            ["bash", "-c", snippet], cwd=self.scratch, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {p.relative_to(self.scratch): p.read_bytes() for p in self.scratch.rglob("*") if p.is_file()}
        self.assertEqual(after, before, "Retrieval snippets must not mutate their fixture sources")
        return result.stdout.splitlines()

    def test_d_u3cde_reverse_lookup(self) -> None:
        target = "wiki/concepts/Vault lock.md"
        self.write_jsonl(".vault-meta/session-to-pages.jsonl", [
            {"sid": "sid-first", "pages": ["wiki/concepts/Other.md", target]},
            {"sid": "sid-prefix", "pages": [target + ".backup"]},
            {"sid": "sid-second", "pages": [target]},
            {"sid": target, "pages": []},
        ])
        snippet = self.snippet("Reverse lookup:").replace('"<page-path>"', shlex.quote(target))
        self.assertEqual(self.run_snippet(snippet), ["sid-first", "sid-second"])

    def test_d_u3cde_history_lookup(self) -> None:
        project = str(self.scratch / "current project")
        display = "Fixed VAULT-write lock " + "x" * 100
        history = self.write_jsonl(".claude/history.jsonl", [
            {"sessionId": "sid-current", "timestamp": 1790596800000, "project": project, "display": display},
            {"sessionId": "sid-other", "timestamp": 1790510400000, "project": "/other-project", "display": "vault-write retry"},
            {"sessionId": "sid-unrelated", "timestamp": 1790424000000, "project": project, "display": "parser refactor"},
            {"sessionId": "sid-metadata-only", "timestamp": 1790337600000, "project": "/vault-write", "display": "another topic"},
        ])
        for scope, keyword, expected in (
            ("current", "vault-WRITE", [f"sid-current 1790596800000 {display[:80]}"]),
            ("all", "vault-WRITE", [f"sid-current 1790596800000 {display[:80]}", "sid-other 1790510400000 vault-write retry"]),
            ("current", "absent-keyword", []),
        ):
            with self.subTest(scope=scope, keyword=keyword):
                snippet = self.snippet("### Prong B:")
                # Redirect only the input path to TMPDIR; do not change HOME.
                snippet = snippet.replace('"~/.claude/history.jsonl"', json.dumps(str(history)))
                snippet = snippet.replace("~/.claude/history.jsonl", shlex.quote(str(history)))
                for placeholder, value in (("<keyword>", keyword), ("<scope: current|all>", scope), ("<repo-root>", project)):
                    snippet = snippet.replace('"' + placeholder + '"', json.dumps(value))
                self.assertEqual(self.run_snippet(snippet), expected)

    def test_find_session_dead_branches(self) -> None:
        for obsolete in ("auto-inject", "extract_technical_nouns", "keywords < 2"):
            with self.subTest(obsolete=obsolete):
                self.assertFalse(obsolete in self.text.lower(), f"Obsolete find-session branch: {obsolete}")
        self.assertIsNone(re.search(r"grep -l[^\n]*session-to-pages\.jsonl", self.text))
        self.assertIn("Keep up to 8 keywords.", self.text)
        self.assertIn("Ask only when no topic can be determined (see Phase 0).", self.text)

    def test_u_t2_find_session_preflight(self) -> None:
        block = preflight_block(self.text, "Phase 0:")
        self.assertEqual(preflight_issues(block), [])
        self.assertIn("skip clarifying questions", block)
        for default in ("last 30 days", "current project only", "preview only"):
            self.assertIn(default, block)
        self.assertIn("the search topic cannot be extracted", block)
        self.assertIn("single `AskUserQuestion`", block)
        self.assertTrue(preflight_issues(block + "\nAsk four questions."))


class BacklogContracts(unittest.TestCase):
    def test_d_u3f_slug_lookup(self) -> None:
        text = (ROOT / "skills/backlog/SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Phase C.1:", 1)[1]
        match = re.search(r"```bash\n([\s\S]*?)\n```", section)
        self.assertIsNotNone(match)
        lines = [
            "- [2026-09-28] blog-theme-upgrade-more — prefix collision",
            "- [2026-09-28] blog-theme-upgrade — exact entry",
            "- [2026-09-28] blog-theme-upgrade-2 — suffixed entry",
            "- [2026-09-28] blog-theme-upgrade",
            "- [2026-09-28] blog-theme-upgradeish — glued suffix",
        ]
        with tempfile.TemporaryDirectory(prefix="prompt-audit-backlog-") as tmp:
            scratch = Path(tmp).resolve()
            self.assertFalse(scratch.is_relative_to(ROOT.resolve()))
            path = scratch / "wiki/backlog.md"
            path.parent.mkdir()
            content = "\n".join(lines) + "\n"
            path.write_text(content, encoding="utf-8")
            for slug, code, expected in (
                ("blog-theme-upgrade", 0, [f"2:{lines[1]}", f"4:{lines[3]}"]),
                ("missing-slug", 1, []),
            ):
                with self.subTest(slug=slug):
                    result = subprocess.run(
                        ["bash", "-c", match.group(1).replace("<slug>", slug)],
                        cwd=scratch, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
                    )
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), expected)
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_backlog_uniqueness_and_single_source_rules(self) -> None:
        text = (ROOT / "skills/backlog/SKILL.md").read_text(encoding="utf-8")
        draft = preflight_block(text, "Phase A.2:")
        rule = "If the slug exists, append `-2`, `-3`, …"
        self.assertIn(rule, draft)
        self.assertLess(draft.index("Read `wiki/backlog.md`"), draft.index(rule))
        self.assertLess(draft.index(rule), draft.index("--sha256"))
        self.assertIn("Exit 4 means re-read, re-check slug uniqueness, and retry.", draft)
        for obsolete in ("NEVER auto-drop", "NEVER duplicate state", "❌ Pre-flight questions on `add`", "❌ Slug conflicts"):
            with self.subTest(obsolete=obsolete):
                self.assertFalse(obsolete in text, f"Obsolete backlog rule: {obsolete}")
        self.assertIn("Don't drop items automatically; the user decides.", text)
        self.assertIn("After promote the item leaves the backlog, so state lives in one place; `wiki/log.md` keeps the history.", text)

    def test_u_t2_backlog_add_preflight(self) -> None:
        text = (ROOT / "skills/backlog/SKILL.md").read_text(encoding="utf-8")
        add = text.split("## Mode: add", 1)[1].split("## Mode: list", 1)[0]
        self.assertEqual(preflight_issues(add), [])
        preflight = preflight_block(add, "Phase A.0:")
        self.assertIn("At most 2 questions (skip any that are already clear", preflight)
        self.assertIn("Optional.", preflight)
        self.assertIn("NO scope/audience/mode questions", preflight)
        self.assertTrue(preflight_issues(add + "\nAsk four questions."))
        self.assertTrue(preflight_issues(preflight_block(text, "Phase C.0:")), "Promote preflight remains outside U-T2 scope")


class CanvasContracts(unittest.TestCase):
    def test_canvas_read_write_boundary(self) -> None:
        text = (ROOT / "skills/canvas/SKILL.md").read_text(encoding="utf-8")
        default = text.split("## Default Canvas", 1)[1].split("```json", 1)[0]
        self.assertIn("only in an explicit write operation", default)
        self.assertIn("read operations report a missing file", default)
        read = text.split("### open / status", 1)[1].split("### new", 1)[0]
        missing = numbered_steps(read)[3]
        self.assertIn("report the missing file and stop", missing)
        self.assertNotRegex(missing, r"(?i)\b(create|write)\b")

    def test_canvas_shared_preflight_reference(self) -> None:
        text = (ROOT / "skills/canvas/SKILL.md").read_text(encoding="utf-8")
        preflight = preflight_block(text, "Pre-flight")
        self.assertFalse("feedback_skill_preflight_clarification" in preflight)
        self.assertIn("правило pre-flight из CLAUDE.md", preflight)
        self.assertIn("Read-операции (open / list / показать) — без pre-flight.", preflight)


if __name__ == "__main__":
    unittest.main()
