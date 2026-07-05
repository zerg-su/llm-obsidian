#!/usr/bin/env python3
"""Permission allowlist suggestions: which Bash commands run often but are not
covered by any allow rule -> candidates for .claude/settings.local.json.

Analog of Boris Cherny's /fewer-permission-prompts idea (Tips Digest c-000358),
built on the same local sources as pipeline-stats.py (nothing leaves the machine):

  * ~/.claude/projects/<proj>/*.jsonl — session transcripts: Bash tool_use blocks
    (every command that actually ran, i.e. was allowed by a rule or approved by hand)
  * .claude/settings.json            — project allow/deny rules
  * .claude/settings.local.json      — per-machine allow rules
  * ~/.claude/settings.json          — user-global rules (if present)

Logic: a command that ran N times and matches NO allow rule was either approved
by hand N times (prompt fatigue -> allowlist candidate) or ran under a broader
mechanism. Commands matching a deny rule are never suggested. Output is ADVISORY:
the user reviews safety before adding anything (mutating commands are flagged).

Rule matching is a deliberate simplification of Claude Code semantics:
  Bash(x:*)  -> command == x or command startswith "x "
  Bash(x *)  -> fnmatch glob
  Bash(x)    -> exact match
Good enough for suggestion ranking; not a permission engine.

Usage:
    ./scripts/permission-stats.py [--days N] [--top N] [--transcript-dir DIR]

--transcript-dir: override transcript location (testing / non-default homes).
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRANSCRIPT_DIR = (Path.home() / ".claude" / "projects"
                          / re.sub(r"[^A-Za-z0-9]", "-", str(VAULT_ROOT)))
SETTINGS_FILES = [
    VAULT_ROOT / ".claude" / "settings.json",
    VAULT_ROOT / ".claude" / "settings.local.json",
    Path.home() / ".claude" / "settings.json",
]
# First tokens we never suggest allowing blind (review-by-hand only).
MUTATING_TOKENS = {"rm", "mv", "dd", "kill", "killall", "shutdown", "reboot",
                   "chmod", "chown", "curl", "wget", "ssh", "scp", "sudo"}


def iter_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def load_rules() -> tuple[list[str], list[str]]:
    allow, deny = [], []
    for f in SETTINGS_FILES:
        if not f.is_file():
            continue
        try:
            perms = json.loads(f.read_text(encoding="utf-8")).get("permissions", {})
        except ValueError:
            continue
        allow += [r for r in perms.get("allow", []) if r.startswith("Bash(")]
        deny += [r for r in perms.get("deny", []) if r.startswith("Bash(")]
    return allow, deny


def rule_matches(rule: str, cmd: str) -> bool:
    """Simplified Claude Code Bash(...) rule matching (see module docstring)."""
    inner = rule[len("Bash("):-1] if rule.endswith(")") else rule[len("Bash("):]
    if inner.endswith(":*"):
        prefix = inner[:-2]
        return cmd == prefix or cmd.startswith(prefix + " ")
    if "*" in inner or "?" in inner:
        return fnmatch.fnmatch(cmd, inner)
    return cmd == inner


def covered(cmd: str, rules: list[str]) -> bool:
    return any(rule_matches(r, cmd) for r in rules)


def bucket(cmd: str) -> str:
    """Aggregation key: first two tokens of the first segment of the command."""
    first_seg = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd, maxsplit=1)[0]
    toks = first_seg.split()
    if not toks:
        return ""
    # commands invoked via interpreter keep 3 tokens (python3 -c / bash -c are noise)
    n = 3 if toks[0] in ("python3", "python", "bash", "sh", "env") else 2
    return " ".join(toks[:n])


def scan_bash_commands(tdir: Path, cutoff: dt.datetime):
    """Yield (command, ts) for every Bash tool_use in transcripts."""
    if not tdir.is_dir():
        return
    for f in sorted(tdir.glob("*.jsonl")):
        for rec in iter_jsonl(f):
            if not isinstance(rec, dict):
                continue
            ts_raw = (rec.get("timestamp") or "")[:19]
            try:
                ts = (dt.datetime.fromisoformat(ts_raw)
                      .replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            content = (rec.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for blk in content:
                if (isinstance(blk, dict) and blk.get("type") == "tool_use"
                        and blk.get("name") == "Bash"):
                    cmd = " ".join(str((blk.get("input") or {}).get("command") or "").split())
                    if cmd:
                        yield cmd, ts


def main() -> int:
    days, top = 30, 20
    tdir = DEFAULT_TRANSCRIPT_DIR
    argv = sys.argv
    try:
        if "--days" in argv:
            days = int(argv[argv.index("--days") + 1])
        if "--top" in argv:
            top = int(argv[argv.index("--top") + 1])
        if "--transcript-dir" in argv:
            tdir = Path(argv[argv.index("--transcript-dir") + 1])
    except (IndexError, ValueError):
        print("usage: permission-stats.py [--days N] [--top N] [--transcript-dir DIR]",
              file=sys.stderr)
        return 2
    cutoff = dt.datetime.now() - dt.timedelta(days=days)

    allow, deny = load_rules()
    if not allow and not deny:
        print("warning: no Bash permission rules found in settings files", file=sys.stderr)

    bucket_count: dict[str, int] = defaultdict(int)
    bucket_example: dict[str, str] = {}
    total = uncovered_total = denied_total = 0
    for cmd, _ts in scan_bash_commands(tdir, cutoff):
        total += 1
        if covered(cmd, deny):       # deny wins in Claude Code; never suggestable
            denied_total += 1
            continue
        if covered(cmd, allow):
            continue
        uncovered_total += 1
        b = bucket(cmd)
        if not b:
            continue
        bucket_count[b] += 1
        bucket_example.setdefault(b, cmd)

    print(f"# Permission stats — last {days}d (bash runs: {total}, "
          f"deny-matched: {denied_total}, not covered by allow rules: {uncovered_total})")
    print(f"# transcripts: {tdir}")
    print(f"# allow rules: {len(allow)}, deny rules: {len(deny)}")
    print()
    if not bucket_count:
        print("No uncovered command groups found (or no transcripts in window).")
        return 0
    print("| Runs | Command group | Suggested rule | Note |")
    print("|---|---|---|---|")
    shown = 0
    for b, n in sorted(bucket_count.items(), key=lambda kv: -kv[1]):
        if shown >= top:
            break
        suggestion = f"Bash({b}:*)"
        if covered(b, deny) or covered(b + " x", deny):
            note = "DENY-listed, never allow"
            suggestion = "-"
        elif b.split()[0] in MUTATING_TOKENS:
            note = "mutating/network, review by hand"
        elif n < 3:
            note = "low frequency"
        else:
            note = "candidate"
        print(f"| {n} | `{b}` | `{suggestion}` | {note} |")
        shown += 1
    print()
    print("> Advisory only: review each suggestion before adding to "
          ".claude/settings.local.json (allow). Matching here is a simplification "
          "of Claude Code semantics; deny rules always win and are never suggested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
