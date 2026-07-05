#!/usr/bin/env python3
"""session-map.py — cheap daily session->task map.

Lists Claude Code sessions active on a given date for THIS vault's project and
labels each from the wiki pages that session touched (via .vault-meta/index.jsonl
`sessions` field). Falls back to the first substantive user prompt in the transcript
for sessions that touched no wiki page. No AI, pure file ops — runs in milliseconds.

Output: markdown lines ready to paste under a daily page's `## Сессии` section:

    - <label> · `<full-session-uuid>`

Sessions that could not be auto-labeled are emitted with `⟨label?⟩` so a single
downstream pass (or the user) can fill them in.

Usage:
    ./scripts/session-map.py                 # today
    ./scripts/session-map.py 2026-06-26      # explicit date (YYYY-MM-DD)
    ./scripts/session-map.py --json          # machine form (list of objects)

Exit codes: 0 ok (even if no sessions), 2 project transcript dir not found.
"""
import datetime
import glob
import json
import os
import re
import sys

VAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(VAULT, ".vault-meta", "index.jsonl")

# Claude Code encodes the project working dir into a slug by replacing every
# non-alphanumeric char with '-' (so '/Users/a.b/x' -> '-Users-a-b-x').
SLUG = re.sub(r"[^A-Za-z0-9]", "-", VAULT)
PROJ = os.path.expanduser(os.path.join("~/.claude/projects", SLUG))

MAX_TITLES = 2          # how many page titles to join into a label
LABEL_PLACEHOLDER = "⟨label?⟩"
# Structural page types make poor task labels (date pages, indexes, rollups) —
# use them only if nothing more descriptive is available.
LOW_VALUE_TYPES = {"daily", "meta", "fold", "log"}


def parse_args(argv):
    date = None
    as_json = False
    for a in argv[1:]:
        if a == "--json":
            as_json = True
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
            date = a
        else:
            sys.stderr.write(f"session-map: ignoring unknown arg {a!r}\n")
    if date is None:
        date = datetime.date.today().isoformat()
    return date, as_json


def sessions_on(date):
    """Return [(mtime_epoch, session_id)] for transcripts modified on `date`."""
    out = []
    for f in glob.glob(os.path.join(PROJ, "*.jsonl")):
        try:
            st = os.stat(f)
        except OSError:
            continue
        d = datetime.date.fromtimestamp(st.st_mtime).isoformat()
        if d == date:
            sid = os.path.basename(f)[:-len(".jsonl")]
            out.append((st.st_mtime, sid))
    out.sort()  # chronological by last activity
    return out


def labels_from_wiki():
    """Map session_id -> [(type, title)] from the retrieval index."""
    by_sid = {}
    if not os.path.exists(INDEX):
        return by_sid
    with open(INDEX, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            title = rec.get("title") or rec.get("path", "")
            ptype = rec.get("type") or ""
            for sid in rec.get("sessions") or []:
                entries = by_sid.setdefault(sid, [])
                if title not in [t for _, t in entries]:
                    entries.append((ptype, title))
    return by_sid


def first_prompt(sid):
    """Best-effort: first substantive typed user prompt in the transcript.

    Skips sidechain (sub-agent) turns, tool_result blocks ('[...'), command
    wrappers ('<...', '/...') and one-liners under 4 words. Returns '' if none.
    """
    path = os.path.join(PROJ, f"{sid}.jsonl")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user" or rec.get("isSidechain"):
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, str):
                    continue
                t = content.strip()
                if not t or t[0] in "[</":
                    continue
                # Strip pasted-chat noise: leading "**author**" and "(dd.mm.yyyy hh:mm)".
                t = re.sub(r"^\*\*[^*]+\*\*\s*", "", t)
                t = re.sub(r"^\(\d{2}\.\d{2}\.\d{4}[^)]*\)\s*", "", t)
                t = " ".join(t.split())
                if len(t.split()) < 4:
                    continue
                return t[:80]
    except OSError:
        return ""
    return ""


def compose_label(titles):
    if not titles:
        return ""
    head = titles[:MAX_TITLES]
    label = "; ".join(head)
    extra = len(titles) - len(head)
    if extra > 0:
        label += f" (+{extra})"
    return label


def build(date):
    wiki = labels_from_wiki()
    rows = []
    for mtime, sid in sessions_on(date):
        entries = wiki.get(sid, [])
        high = [t for ty, t in entries if ty not in LOW_VALUE_TYPES]
        low = [t for ty, t in entries if ty in LOW_VALUE_TYPES]
        # Prefer descriptive content pages; then the opening prompt; then whatever
        # structural pages were touched; else drop as an accidental/empty open.
        label = compose_label(high)
        source = "wiki"
        if not label:
            label = first_prompt(sid)
            source = "prompt" if label else ""
        if not label:
            label = compose_label(low)
            source = "wiki-structural" if label else "none"
        if source == "none":
            continue
        rows.append({
            "session": sid,
            "label": label or LABEL_PLACEHOLDER,
            "source": source,
            "time": datetime.datetime.fromtimestamp(mtime).strftime("%H:%M"),
            "pages": [t for _, t in entries],
        })
    return rows


def main():
    date, as_json = parse_args(sys.argv)
    if not os.path.isdir(PROJ):
        sys.stderr.write(f"session-map: project transcript dir not found: {PROJ}\n")
        return 2
    rows = build(date)
    if as_json:
        print(json.dumps({"date": date, "sessions": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"# no sessions on {date}")
        return 0
    for r in rows:
        print(f"- {r['label']} · `{r['session']}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
