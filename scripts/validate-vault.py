#!/usr/bin/env python3
"""Deterministic vault cap validator.

Everything here used to be "model discipline" (self-stated caps in hot.md,
lint prose in wiki-lint) and quietly degraded. This script makes the caps
machine-checked. wiki-lint runs it as Step 0; stop.sh echoes a compact
summary as a non-blocking hint.

Checks:
  hot        — hot.md: <=800 words total, Recent Changes <=15 one-line bullets
               (<=160 chars), Active Threads <=8, Last Updated <=120 words
  fold       — log.md entries since last fold <=128 (2 x 64 fold interval)
  index      — index.md AUTO-DATE marker present and not stale vs mtime
  questions  — every wiki/questions/ page carries status: open|answered
  frontmatter— required keys on wiki pages; address on post-rollout pages;
               sessions: key present (WARN — provenance convention)
  plans      — wiki/plans/ lifecycle: status in pending|executed|abandoned,
               single status line, executed (post 2026-07-03) carries a
               'Результат:' link, pending older than 30d -> WARN
  panic      — runbooks with tier: panic: last_validated <=180d, no
               "ask Claude"-style steps (they must be human-executable)
  skills     — SKILL.md descriptions: total <=15000 chars, per-skill
               hard <=1024 (Anthropic spec) / soft <=500
  guide      — every installed skill is mentioned in
               wiki/meta/daily-pipeline-guide.md (catalog drift catcher:
               "skill exists but the guide never heard of it")

Usage:
  ./scripts/validate-vault.py             # full report, exit 1 on any FAIL
  ./scripts/validate-vault.py --summary   # <=6 compact lines (for stop.sh)
  ./scripts/validate-vault.py --checks hot,fold

Caps mirrored in scripts/vault-write.py — keep in sync.
"""

from __future__ import annotations

import re
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from vault_schema import parse_frontmatter, split_frontmatter, validate_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
WIKI = REPO_ROOT / "wiki"
HOT_FILE = WIKI / "hot.md"
LOG_FILE = WIKI / "log.md"
INDEX_FILE = WIKI / "index.md"
FOLD_SCRIPT = REPO_ROOT / "scripts" / "fold-log.py"

HOT_TOTAL_WORDS = 800
RC_MAX_BULLETS = 15
RC_BULLET_CHARS = 160
THREADS_MAX = 8
NARRATIVE_WORDS = 120
FOLD_FAIL_LAG = 128
PANIC_MAX_AGE_DAYS = 180
DESC_TOTAL_BUDGET = 15000
DESC_HARD = 1024
DESC_SOFT = 500
ADDRESS_CUTOFF = date(2026, 4, 23)
ADDRESS_EXEMPT_TYPES = {"meta", "fold", "daily", "plan"}
REQUIRED_KEYS = ("type", "status", "created", "updated")
PLAN_STATUSES = {"pending", "executed", "abandoned"}
PLAN_RESULT_CUTOFF = date(2026, 7, 3)  # birth of the reap plan_close mechanism
PLAN_PENDING_MAX_AGE_DAYS = 30

class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


def section_body(lines: list[str], heading: str) -> list[str] | None:
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return lines[start + 1:end]


def check_hot(r: Report) -> None:
    if not HOT_FILE.exists():
        r.fail("hot: wiki/hot.md missing")
        return
    text = HOT_FILE.read_text(encoding="utf-8")
    words = len(text.split())
    if words > HOT_TOTAL_WORDS:
        r.fail(f"hot: {words} words (cap {HOT_TOTAL_WORDS})")
    lines = text.split("\n")

    rc = section_body(lines, "## Recent Changes")
    if rc is None:
        r.fail("hot: '## Recent Changes' section missing")
    else:
        bullets = [l for l in rc if l.lstrip().startswith("- ")]
        if len(bullets) > RC_MAX_BULLETS:
            r.fail(f"hot: Recent Changes has {len(bullets)} bullets (cap {RC_MAX_BULLETS})")
        long = [b for b in bullets if len(b) > RC_BULLET_CHARS]
        if long:
            r.fail(
                f"hot: {len(long)} Recent Changes bullet(s) over {RC_BULLET_CHARS} chars "
                f"(first: {long[0][:80]}...)"
            )

    threads = section_body(lines, "## Active Threads")
    if threads is None:
        r.fail("hot: '## Active Threads' section missing")
    else:
        n = len([l for l in threads if l.lstrip().startswith("- ")])
        if n > THREADS_MAX:
            r.fail(f"hot: Active Threads has {n} items (cap {THREADS_MAX})")

    narrative = section_body(lines, "## Last Updated")
    if narrative is not None:
        n_words = len("\n".join(narrative).split())
        if n_words > NARRATIVE_WORDS:
            r.fail(f"hot: Last Updated narrative is {n_words} words (cap {NARRATIVE_WORDS})")


def check_fold(r: Report) -> None:
    if not LOG_FILE.exists() or not FOLD_SCRIPT.is_file():
        return
    result = subprocess.run(
        [sys.executable, str(FOLD_SCRIPT), "status", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        r.fail(f"fold: status helper failed: {(result.stderr or result.stdout).strip()}")
        return
    try:
        unprocessed = int(json.loads(result.stdout)["unprocessed_entries"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        r.fail(f"fold: unreadable status: {exc}")
        return
    if unprocessed >= FOLD_FAIL_LAG:
        r.fail(f"fold: {unprocessed} unprocessed eligible log entries (cap {FOLD_FAIL_LAG}) — run /wiki-fold")
    elif unprocessed >= 64:
        r.warn(f"fold: {unprocessed} unprocessed eligible log entries — /wiki-fold due")


def check_index(r: Report) -> None:
    if not INDEX_FILE.exists():
        r.fail("index: wiki/index.md missing")
        return
    text = INDEX_FILE.read_text(encoding="utf-8")
    m = re.search(r"<!-- AUTO-DATE -->\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        r.warn("index: no AUTO-DATE marker (header freshness is unverifiable)")
        return
    mtime_date = datetime.fromtimestamp(INDEX_FILE.stat().st_mtime).date().isoformat()
    if m.group(1) < mtime_date:
        r.warn(f"index: AUTO-DATE {m.group(1)} older than file mtime {mtime_date}")


def check_questions(r: Report) -> None:
    qdir = WIKI / "questions"
    if not qdir.is_dir():
        return
    bad = []
    for f in sorted(qdir.glob("*.md")):
        if f.name == "_index.md":
            continue
        block = split_frontmatter(f.read_text(encoding="utf-8"))
        fm = parse_frontmatter(block) if block else {}
        if fm.get("status") not in ("open", "answered"):
            bad.append(f.name)
    if bad:
        r.fail(f"questions: {len(bad)} page(s) without status open|answered: {', '.join(bad[:5])}"
               + (" ..." if len(bad) > 5 else ""))


def check_frontmatter(r: Report) -> None:
    missing_keys: list[str] = []
    missing_addr: list[str] = []
    missing_sessions: list[str] = []
    for f in sorted(WIKI.rglob("*.md")):
        rel = f.relative_to(WIKI)
        if rel.name == "log.md" or "_templates" in rel.parts or rel.name == "_index.md":
            continue
        block = split_frontmatter(f.read_text(encoding="utf-8"))
        if block is None:
            missing_keys.append(f"{rel} (no frontmatter)")
            continue
        fm = parse_frontmatter(block)
        absent = [k for k in REQUIRED_KEYS if not fm.get(k)]
        if absent:
            missing_keys.append(f"{rel} (missing {','.join(absent)})")
        # sessions provenance convention: key must exist; empty list ([]) is the
        # explicit legacy-unknown marker and passes (raw regex — the flat parser
        # does not surface nested list keys)
        if not re.search(r"^sessions:", block, flags=re.M):
            missing_sessions.append(str(rel))
        ptype = fm.get("type")
        created = str(fm.get("created") or "")
        if (
            ptype not in ADDRESS_EXEMPT_TYPES
            and re.match(r"^\d{4}-\d{2}-\d{2}", created)
            and date.fromisoformat(created[:10]) >= ADDRESS_CUTOFF
            and not fm.get("address")
        ):
            missing_addr.append(str(rel))
    if missing_keys:
        r.fail(f"frontmatter: {len(missing_keys)} page(s) with gaps: {'; '.join(missing_keys[:5])}"
               + (" ..." if len(missing_keys) > 5 else ""))
    if missing_addr:
        r.fail(f"frontmatter: {len(missing_addr)} post-rollout page(s) without address: "
               + ", ".join(missing_addr[:5]) + (" ..." if len(missing_addr) > 5 else ""))
    if missing_sessions:
        r.warn(f"frontmatter: {len(missing_sessions)} page(s) missing sessions: key: "
               + ", ".join(missing_sessions[:3]) + (" ..." if len(missing_sessions) > 3 else ""))


def check_schema(r: Report) -> None:
    """Strict repository-wide schema, link, and address-state validation."""
    for issue in validate_schema(REPO_ROOT):
        message = f"schema/{issue.code}: {issue.message}"
        if issue.level == "fail":
            r.fail(message)
        else:
            r.warn(message)


def check_plans(r: Report) -> None:
    pdir = WIKI / "plans"
    if not pdir.is_dir():
        return
    today = date.today()
    bad_status: list[str] = []
    double_status: list[str] = []
    no_result: list[str] = []
    stale_pending: list[str] = []
    for f in sorted(pdir.glob("*.md")):
        if f.name == "_index.md":
            continue
        text = f.read_text(encoding="utf-8")
        block = split_frontmatter(text)
        if block is None:
            bad_status.append(f"{f.name} (no frontmatter)")
            continue
        if len(re.findall(r"^status:", block, flags=re.M)) > 1:
            double_status.append(f.name)
        fm = parse_frontmatter(block)
        status = str(fm.get("status") or "")
        if status not in PLAN_STATUSES:
            bad_status.append(f"{f.name} ({status or 'no status'})")
            continue
        created = str(fm.get("created") or "")
        cdate = (
            date.fromisoformat(created[:10])
            if re.match(r"^\d{4}-\d{2}-\d{2}", created)
            else None
        )
        if (
            status == "executed"
            and cdate
            and cdate >= PLAN_RESULT_CUTOFF
            and "Результат:" not in text
        ):
            no_result.append(f.name)
        if (
            status == "pending"
            and cdate
            and (today - cdate).days > PLAN_PENDING_MAX_AGE_DAYS
        ):
            stale_pending.append(f.name)
    if bad_status:
        r.fail(f"plans: {len(bad_status)} plan(s) with status outside "
               f"pending|executed|abandoned: {'; '.join(bad_status[:5])}"
               + (" ..." if len(bad_status) > 5 else ""))
    if double_status:
        r.fail(f"plans: {len(double_status)} plan(s) with multiple status lines: "
               + ", ".join(double_status[:5]))
    if no_result:
        r.fail(f"plans: {len(no_result)} executed plan(s) without 'Результат:' link: "
               + ", ".join(no_result[:5]))
    if stale_pending:
        r.warn(f"plans: {len(stale_pending)} pending plan(s) older than "
               f"{PLAN_PENDING_MAX_AGE_DAYS}d: " + ", ".join(stale_pending[:5])
               + (" ..." if len(stale_pending) > 5 else ""))


ASK_CLAUDE_RX = re.compile(r"(?i)(спроси\w*\s+(у\s+)?claude|ask\s+claude|попроси\w*\s+claude)")


def check_panic(r: Report) -> None:
    rdir = WIKI / "runbooks"
    if not rdir.is_dir():
        return
    today = date.today()
    for f in sorted(rdir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        block = split_frontmatter(text)
        fm = parse_frontmatter(block) if block else {}
        if fm.get("tier") != "panic":
            continue
        lv = str(fm.get("last_validated") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}", lv):
            r.fail(f"panic: {f.name} has no last_validated date")
        else:
            age = (today - date.fromisoformat(lv[:10])).days
            if age > PANIC_MAX_AGE_DAYS:
                r.fail(f"panic: {f.name} last validated {age}d ago (cap {PANIC_MAX_AGE_DAYS}d) — re-drill")
        if ASK_CLAUDE_RX.search(text):
            r.fail(f"panic: {f.name} contains an 'ask Claude' step — must be human-executable")


DESC_RX = re.compile(r"^description:\s*(.*?)^(?=[a-zA-Z_-]+:|---)", re.M | re.S)


def check_skills(r: Report) -> None:
    total = 0
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        m = DESC_RX.search(text)
        if not m:
            continue
        n = len(m.group(1))
        total += n
        name = skill_md.parent.name
        if n > DESC_HARD:
            r.fail(f"skills: {name} description {n} chars (Anthropic hard limit {DESC_HARD})")
        elif n > DESC_SOFT:
            r.warn(f"skills: {name} description {n} chars (soft cap {DESC_SOFT})")
        n_lines = text.count("\n") + 1
        if n_lines > 500:
            r.fail(f"skills: {name} SKILL.md is {n_lines} lines (cap 500)")
    if total > DESC_TOTAL_BUDGET:
        r.fail(f"skills: descriptions total {total} chars (budget {DESC_TOTAL_BUDGET})")


GUIDE_FILE = WIKI / "meta" / "daily-pipeline-guide.md"


def check_guide(r: Report) -> None:
    """Every installed skill must appear in the daily-pipeline-guide.

    Match is hyphen-aware: 'commit' does not pass via '/commit-digest'.
    Both '/name' and bare 'name' mentions count (reference skills are
    listed without a slash). The guide page is optional — a vault that
    has not adopted it skips this check instead of failing."""
    if not GUIDE_FILE.exists():
        return
    text = GUIDE_FILE.read_text(encoding="utf-8")
    missing = []
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        name = skill_md.parent.name
        if name == "_shared":
            continue
        if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            missing.append(name)
    if missing:
        r.fail(
            f"guide: {len(missing)} installed skill(s) not mentioned in "
            f"daily-pipeline-guide: {', '.join(missing)}"
        )


CHECKS = {
    "hot": check_hot,
    "fold": check_fold,
    "index": check_index,
    "questions": check_questions,
    "schema": check_schema,
    "frontmatter": check_frontmatter,
    "plans": check_plans,
    "panic": check_panic,
    "skills": check_skills,
    "guide": check_guide,
}

DEFAULT_CHECKS = [name for name in CHECKS if name != "frontmatter"]


def main(argv: list[str]) -> int:
    summary = "--summary" in argv
    selected = list(DEFAULT_CHECKS)
    if "--checks" in argv:
        try:
            selected = [c.strip() for c in argv[argv.index("--checks") + 1].split(",")]
        except IndexError:
            print("validate-vault: --checks needs a comma list", file=sys.stderr)
            return 3
        unknown = [c for c in selected if c not in CHECKS]
        if unknown:
            print(f"validate-vault: unknown checks: {unknown}", file=sys.stderr)
            return 3

    r = Report()
    t0 = time.time()
    for name in selected:
        CHECKS[name](r)

    if summary:
        for line in r.fails[:6]:
            print(f"VAULT_LINT_FAIL: {line}")
        if len(r.fails) > 6:
            print(f"VAULT_LINT_FAIL: ... and {len(r.fails) - 6} more (run scripts/validate-vault.py)")
    else:
        for line in r.fails:
            print(f"FAIL: {line}")
        for line in r.warns:
            print(f"WARN: {line}")
        print(
            f"validate-vault: {len(r.fails)} FAIL, {len(r.warns)} WARN "
            f"({', '.join(selected)}; {time.time() - t0:.1f}s)"
        )
    return 1 if r.fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
