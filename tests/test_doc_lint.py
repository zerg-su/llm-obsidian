#!/usr/bin/env python3
"""Tests for skills/tech-doc/scripts/doc_lint.py (plain technical style checks)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "tech-doc" / "scripts" / "doc_lint.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def lint(tmp: Path, text: str, *args: str, name: str = "doc.md") -> subprocess.CompletedProcess[str]:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return run(str(path), *args)


def findings(result: subprocess.CompletedProcess[str]) -> list[dict]:
    return json.loads(result.stdout)["findings"]


def rules(result: subprocess.CompletedProcess[str], level: str | None = None) -> list[str]:
    return [f["rule"] for f in findings(result) if level is None or f["level"] == level]


LONG = " ".join(["word"] * 30) + "."
WARN = " ".join(["word"] * 22) + "."

with tempfile.TemporaryDirectory(prefix="doc-lint-test.") as raw:
    tmp = Path(raw)

    result = lint(tmp, "# Title\n\nStop mongod on one member.\nCheck the state.\n", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert findings(result) == []
    print("OK   clean text passes")

    result = lint(tmp, "# T\n\n" + LONG + "\n", "--json")
    assert result.returncode == 1 and rules(result, "error") == ["sentence-length"], result.stdout
    result = lint(tmp, "# T\n\n" + WARN + "\n", "--json")
    assert result.returncode == 0 and rules(result, "warning") == ["sentence-length"], result.stdout
    print("OK   sentence length: error above 25 words, warning above 20")

    wrapped = "# T\n\n" + " ".join(["word"] * 15) + "\n" + " ".join(["word"] * 15) + ".\n"
    result = lint(tmp, wrapped, "--json")
    assert rules(result, "error") == ["sentence-length"], result.stdout
    print("OK   wrapped paragraph lines join into one sentence")

    text = "# T\n\nUse `a; b` here.\n\n```bash\necho a; echo b\n```\n"
    result = lint(tmp, text, "--json")
    assert result.returncode == 0 and findings(result) == [], result.stdout
    result = lint(tmp, "# T\n\nStop it; then start it.\n", "--json")
    assert result.returncode == 1 and rules(result) == ["semicolon"], result.stdout
    print("OK   semicolons: prose rejected, code ignored")

    result = lint(tmp, "# T\n\nWe Leverage the tool.\n", "--json")
    assert result.returncode == 1 and rules(result) == ["banned-word"], result.stdout
    result = lint(tmp, "# T\n\nRun `leverage --help` now.\n", "--json")
    assert result.returncode == 0 and findings(result) == [], result.stdout
    print("OK   banned words: case-insensitive, code ignored")

    for form in ("empowers", "leveraged", "leveraging", "harnesses"):
        result = lint(tmp, f"# T\n\nThe tool {form} teams.\n", "--json")
        assert rules(result) == ["banned-word"], form + result.stdout
    result = lint(tmp, "# T\n\nThe empowerment program starts.\n", "--json")
    assert findings(result) == [], result.stdout
    print("OK   banned words match English inflections, not other words")

    result = lint(tmp, "# T\n\nHere's the thing. Experts agree. In conclusion, it changes everything.\n", "--json")
    assert result.returncode == 0 and rules(result, "warning").count("slop-pattern") == 4, result.stdout
    result = lint(tmp, "# T\n\nПо мнению экспертов, это меняет всё.\n", "--json")
    assert rules(result, "warning").count("slop-pattern") == 2, result.stdout
    print("OK   slop patterns (EN and RU) are warnings")

    result = lint(tmp, "# T\n\nUse the flag in order to skip it.\n", "--json")
    assert result.returncode == 0 and rules(result, "warning") == ["empty-phrase"], result.stdout
    result = lint(tmp, "# T\n\nВажно отметить, что член упал.\n", "--json")
    assert result.returncode == 0 and rules(result, "warning") == ["empty-phrase"], result.stdout
    print("OK   empty phrases (EN and RU) are warnings")

    base = "# T\n\n### C-1. Check state\n\nText.\n\n"
    result = lint(tmp, base + "Run C-1 now.\n", "--json")
    assert result.returncode == 1 and rules(result) == ["unglossed-ref"], result.stdout
    for ok in (
        "Run C-1 (check state).\n",
        "Step it down first (C-1 — check state).\n",
        "Run C-1 step 2 (check state).\n",
        "Run X-9 now.\n",
    ):
        result = lint(tmp, base + ok, "--json")
        assert result.returncode == 0 and findings(result) == [], ok + result.stdout
    result = lint(tmp, base + "### C-2. Next\n\nRun C-1, then C-2 (next).\n", "--json")
    assert rules(result) == ["unglossed-ref"], result.stdout
    print("OK   references to defined IDs need a gloss")

    table = "| Situation | Go to |\n|---|---|\n| Host is down | C-1, then C-2 |\n"
    result = lint(tmp, base + "### C-2. Next\n\nText.\n\n" + table, "--json")
    assert result.returncode == 0 and findings(result) == [], result.stdout
    table = "| Situation | Action |\n|---|---|\n| Host is down | Run C-1 and wait for the reply |\n"
    result = lint(tmp, base + table, "--json")
    assert rules(result) == ["unglossed-ref"], result.stdout
    table = "| Situation | Action |\n|---|---|\n| Host is down | Run C-1 now. |\n"
    result = lint(tmp, base + table, "--json")
    assert rules(result) == ["unglossed-ref"], result.stdout
    table = "| Ситуация | Переход |\n|---|---|\n| Хост недоступен | C-1, затем C-2 |\n"
    result = lint(tmp, base + "### C-2. Next\n\nText.\n\n" + table, "--json")
    assert result.returncode == 0 and findings(result) == [], result.stdout
    print("OK   table cell with only IDs needs no gloss, prose cell does")

    for empty in ("Run C-1 ().\n", "Run C-1 (  ).\n", "Step it down first (C-1 — ).\n"):
        result = lint(tmp, base + empty, "--json")
        assert rules(result) == ["unglossed-ref"], empty + result.stdout
    print("OK   an empty gloss is not a gloss")

    en = "# T\n\n## 1. Setup\n\n### C-1. Check\n\nRun C-1 (check).\n\n```bash\nls -la\n```\n"
    ru = "# Т\n\n## 1. Подготовка\n\n### C-1. Проверка\n\nВыполнить C-1 (проверка).\n\n```bash\nls -la\n```\n"
    (tmp / "ru.md").write_text(ru, encoding="utf-8")
    result = lint(tmp, en, "--pair", str(tmp / "ru.md"), "--json", name="en.md")
    assert result.returncode == 0 and findings(result) == [], result.stdout
    (tmp / "ru.md").write_text(ru.replace("ls -la", "ls -l"), encoding="utf-8")
    result = lint(tmp, en, "--pair", str(tmp / "ru.md"), "--json", name="en.md")
    assert result.returncode == 1 and "pair-commands" in rules(result), result.stdout
    (tmp / "ru.md").write_text(ru.replace("### C-1. Проверка", "### C-2. Проверка"), encoding="utf-8")
    result = lint(tmp, en, "--pair", str(tmp / "ru.md"), "--json", name="en.md")
    assert "pair-headings" in rules(result), result.stdout
    print("OK   pair parity: headings and commands")

    for fence in ("````", "~~~"):
        left = f"# T\n\n{fence}bash\necho left\n{fence}\n"
        (tmp / "ru.md").write_text(left.replace("left", "right"), encoding="utf-8")
        result = lint(tmp, left, "--pair", str(tmp / "ru.md"), "--json", name="en.md")
        assert rules(result) == ["pair-commands"], fence + result.stdout
    print("OK   pair parity reads the language of longer and tilde fences")

    nested = "# T\n\n````markdown\n```bash\necho a; echo b\n```\nStop it; then start it.\n````\n\nText.\n"
    result = lint(tmp, nested, "--json")
    assert result.returncode == 0 and findings(result) == [], result.stdout
    print("OK   a shorter fence inside a longer fence does not close it")

    result = lint(tmp, "# T\n\nStop it; then start it.\n")
    assert result.returncode == 1 and "doc.md:3: error: semicolon:" in result.stdout, result.stdout
    assert "doc-lint: 1 errors, 0 warnings" in result.stdout, result.stdout
    print("OK   human output: file:line: level: rule")

    result = run(str(tmp / "missing.md"))
    assert result.returncode == 2, result.stdout + result.stderr
    print("OK   missing file exits 2")

result = run(str(ROOT / "docs" / "skill-references" / "plain-style.md"), "--json")
assert result.returncode in (0, 1), result.stderr
assert "lint:banned-en" in (ROOT / "docs" / "skill-references" / "plain-style.md").read_text(encoding="utf-8")
print("OK   repository style file parses")

print("\nAll doc-lint tests passed.")
