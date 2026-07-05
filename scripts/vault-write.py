#!/usr/bin/env python3
"""Single-pass vault bookkeeping dispatcher.

The model generates ONE JSON payload per save-operation; this script fans it
out to wiki/log.md and wiki/hot.md with hard cap enforcement. Replaces the
old multi-Edit choreography (page -> index -> log -> hot) where each edit was
a drift point and caps depended on model discipline.

Payload (stdin or --file), all keys optional:

    {
      "log_entry":     "## [YYYY-MM-DD] verb | Title\\n- body...",
      "hot_bullet":    "YYYY-MM-DD: [[Page]] — one-liner",
      "hot_narrative": "replaces ## Last Updated body, <=120 words",
      "hot_threads":   {"add": ["- **Open**: ..."], "resolve": ["substring"]},
      "plan_close":    {"file": "wiki/plans/<name>.md",
                        "result_link": "[[Title]]",
                        "exec_session": "<id>|null"}
    }

plan_close (reap final): strict lifecycle close of a plan page. Preconditions
(file inside wiki/plans/, single status line, status == pending) violated ->
exit 2, nothing written. Applies: status -> executed, updated bump, executor
session appended to sessions: (plan-capture format), 'Результат: <link>'
line appended to body.

Ownership contract for hot.md sections:
  - ## Recent Changes    — THIS SCRIPT (prepend bullet, evict >15, truncate 160 chars)
  - ## Last Updated      — model via hot_narrative (cap 120 words, FAIL if over)
  - ## Active Threads    — model via hot_threads (cap 8, FAIL with listing if over)
  - ## Key Recent Facts  — model-curated durable facts; script never touches it

All file contents are built in memory and validated BEFORE any write (atomic:
either everything lands or nothing does). Cap violation -> exit 2 with a
human-readable reason; the calling skill must fix the payload and retry.

Usage:
  echo '{"hot_bullet": "..."}' | ./scripts/vault-write.py
  ./scripts/vault-write.py --file payload.json [--dry-run]

Exit codes: 0 ok, 1 lock/io failure, 2 cap violation, 3 bad payload.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOT_FILE = REPO_ROOT / "wiki" / "hot.md"
LOG_FILE = REPO_ROOT / "wiki" / "log.md"
LOCK_FILE = REPO_ROOT / ".vault-meta" / ".vault-write.lock"


def atomic_write(path: Path, text: str) -> None:
    """Same-dir tmp + os.replace: a crash between the hot.md and log.md writes
    can no longer leave a half-written file (readers see old or new, never a
    torn write). ".tmp." infix keeps strays covered by the *.tmp.* gitignore."""
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

# Caps (mirrored in scripts/validate-vault.py — keep in sync)
HOT_TOTAL_WORDS = 800
RC_MAX_BULLETS = 15
RC_BULLET_CHARS = 160
THREADS_MAX = 8
NARRATIVE_WORDS = 120

KNOWN_KEYS = {"log_entry", "hot_bullet", "hot_narrative", "hot_threads", "plan_close"}

RC_HEADING = "## Recent Changes"
THREADS_HEADING = "## Active Threads"
NARRATIVE_HEADING = "## Last Updated"


def fail(code: int, msg: str) -> int:
    print(f"vault-write: {msg}", file=sys.stderr)
    return code


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Return (start, end) line indexes of a section's BODY (heading excluded).
    end is the index of the next '## ' heading or len(lines)."""
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start + 1, end


def bullets_of(lines: list[str]) -> list[str]:
    return [l for l in lines if l.lstrip().startswith("- ")]


def replace_section(lines: list[str], heading: str, new_body: list[str]) -> list[str]:
    bounds = section_bounds(lines, heading)
    if bounds is None:
        raise ValueError(f"section not found: {heading}")
    start, end = bounds
    return lines[:start] + [""] + new_body + [""] + lines[end:]


def set_frontmatter_updated(text: str, today: str) -> str:
    return re.sub(r"^updated: .*$", f"updated: {today}", text, count=1, flags=re.M)


def apply_hot(payload: dict, hot_text: str, today: str) -> tuple[str, list[str]]:
    """Return (new_hot_text, warnings). Raises CapViolation on hard failures."""
    warnings: list[str] = []
    lines = hot_text.split("\n")

    # Recent Changes: prepend + truncate + evict
    bullet = payload.get("hot_bullet")
    if bullet:
        b = one_line(bullet)
        if not b.startswith("- "):
            b = "- " + b
        if len(b) > RC_BULLET_CHARS:
            b = b[: RC_BULLET_CHARS - 1].rstrip() + "…"
            warnings.append(f"hot_bullet truncated to {RC_BULLET_CHARS} chars")
        bounds = section_bounds(lines, RC_HEADING)
        if bounds is None:
            raise CapViolation(f"hot.md has no '{RC_HEADING}' section")
        existing = bullets_of(lines[bounds[0]:bounds[1]])
        kept = [b] + existing
        if len(kept) > RC_MAX_BULLETS:
            evicted = len(kept) - RC_MAX_BULLETS
            kept = kept[:RC_MAX_BULLETS]
            warnings.append(f"Recent Changes: evicted {evicted} oldest bullet(s)")
        lines = replace_section(lines, RC_HEADING, kept)

    # Last Updated narrative
    narrative = payload.get("hot_narrative")
    if narrative:
        n_words = len(narrative.split())
        if n_words > NARRATIVE_WORDS:
            raise CapViolation(
                f"hot_narrative is {n_words} words (cap {NARRATIVE_WORDS}) — shorten it"
            )
        lines = replace_section(lines, NARRATIVE_HEADING, narrative.strip().split("\n"))

    # Active Threads
    threads = payload.get("hot_threads") or {}
    if threads:
        bounds = section_bounds(lines, THREADS_HEADING)
        if bounds is None:
            raise CapViolation(f"hot.md has no '{THREADS_HEADING}' section")
        current = bullets_of(lines[bounds[0]:bounds[1]])
        for pat in threads.get("resolve", []):
            before = len(current)
            current = [t for t in current if pat not in t]
            if len(current) == before:
                warnings.append(f"hot_threads.resolve: no thread matched {pat!r}")
        for add in threads.get("add", []):
            a = one_line(add)
            if not a.startswith("- "):
                a = "- " + a
            current.insert(0, a)
        if len(current) > THREADS_MAX:
            listing = "\n".join(f"  {t[:120]}" for t in current)
            raise CapViolation(
                f"Active Threads would be {len(current)} (cap {THREADS_MAX}). "
                f"Resolve some first:\n{listing}"
            )
        lines = replace_section(lines, THREADS_HEADING, current)

    new_text = set_frontmatter_updated("\n".join(lines), today)
    total_words = len(new_text.split())
    if total_words > HOT_TOTAL_WORDS:
        raise CapViolation(
            f"hot.md would be {total_words} words (cap {HOT_TOTAL_WORDS}). "
            "Model-owned sections (Last Updated / Key Recent Facts / Active Threads) "
            "are too fat — trim them (or run the one-time hot rebuild)."
        )
    return new_text, warnings


def apply_log(log_entry: str, log_text: str, today: str) -> str:
    entry = log_entry.strip()
    if not entry.startswith("## ["):
        raise CapViolation("log_entry must start with '## [' (log heading format)")
    m = re.search(r"^## \[", log_text, flags=re.M)
    if m:
        idx = m.start()
        new_text = log_text[:idx] + entry + "\n\n" + log_text[idx:]
    else:
        new_text = log_text.rstrip("\n") + "\n\n" + entry + "\n"
    return set_frontmatter_updated(new_text, today)


class CapViolation(Exception):
    pass


def apply_plan_close(spec: dict, today: str) -> tuple[Path, str]:
    """Strictly close a plan page: pending -> executed + provenance.

    Any precondition miss raises CapViolation (exit 2, nothing written):
    re-closing an executed plan is a logic error (wrong plan_file or a
    double reap), not something to paper over."""
    if not isinstance(spec, dict):
        raise CapViolation("plan_close must be an object {file, result_link, exec_session}")
    rel = str(spec.get("file") or "")
    result_link = str(spec.get("result_link") or "").strip()
    exec_session = spec.get("exec_session") or None
    if not result_link:
        raise CapViolation("plan_close.result_link is required, e.g. '[[Page Title]]'")

    path = (REPO_ROOT / rel).resolve()
    plans_dir = (REPO_ROOT / "wiki" / "plans").resolve()
    if plans_dir not in path.parents:
        raise CapViolation(f"plan_close.file must live in wiki/plans/ (got {rel!r})")
    if not path.is_file():
        raise CapViolation(f"plan_close.file not found: {rel}")

    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?\n)---\n", text, flags=re.S)
    if not m:
        raise CapViolation(f"plan_close: {rel} has no frontmatter")
    fm, body = m.group(1), text[m.end():]

    statuses = re.findall(r"^status:\s*(\S+)", fm, flags=re.M)
    if len(statuses) != 1:
        raise CapViolation(f"plan_close: {rel} has {len(statuses)} status lines (expected 1)")
    if statuses[0] != "pending":
        raise CapViolation(
            f"plan_close: {rel} status is '{statuses[0]}' (expected 'pending') — "
            "already closed or wrong plan_file"
        )
    fm = re.sub(r"^status:\s*pending\s*$", "status: executed", fm, count=1, flags=re.M)

    if exec_session:
        entry = [f"  - id: {exec_session}", f"    date: {today}"]
        lines = fm.split("\n")
        block_start = next(
            (i for i, l in enumerate(lines) if re.match(r"sessions:\s*$", l)), None
        )
        if block_start is not None:
            # insert after the last non-empty indented line of the block —
            # NOT at the first non-indented line, or a trailing blank line
            # (sessions being the last fm key) would swallow the final newline
            insert_at = block_start + 1
            for j in range(block_start + 1, len(lines)):
                if lines[j].strip() and lines[j].startswith((" ", "\t")):
                    insert_at = j + 1
                elif lines[j].strip():
                    break
            lines[insert_at:insert_at] = entry
            fm = "\n".join(lines)
        elif re.search(r"^sessions:\s*\[\s*\]\s*$", fm, flags=re.M):
            fm = re.sub(
                r"^sessions:\s*\[\s*\]\s*$", "\n".join(["sessions:"] + entry),
                fm, count=1, flags=re.M,
            )
        else:
            fm = fm.rstrip("\n") + "\n" + "\n".join(["sessions:"] + entry) + "\n"

    if not fm.endswith("\n"):
        fm += "\n"
    body = body.rstrip("\n") + f"\n\nРезультат: {result_link} (reaped {today})\n"
    new_text = set_frontmatter_updated("---\n" + fm + "---\n" + body, today)
    return path, new_text


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    if "--file" in argv:
        try:
            raw = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
        except (IndexError, OSError) as e:
            return fail(3, f"cannot read --file: {e}")
    else:
        raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return fail(3, f"payload is not valid JSON: {e}")
    if not isinstance(payload, dict):
        return fail(3, "payload must be a JSON object")

    for key in payload.keys() - KNOWN_KEYS:
        if key == "index_line":
            print(
                "vault-write: WARN index_line is not supported — index.md is a curated "
                "map; folder listings autogenerate via reindex.py --folder-indexes",
                file=sys.stderr,
            )
        else:
            print(f"vault-write: WARN unknown payload key {key!r} ignored", file=sys.stderr)

    if not payload.keys() & KNOWN_KEYS:
        return fail(3, "payload has no actionable keys")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_FILE.open("w")
    deadline = time.time() + 5
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                return fail(1, "could not acquire vault-write lock within 5s")
            time.sleep(0.2)

    try:
        today = time.strftime("%Y-%m-%d")
        writes: list[tuple[Path, str]] = []
        warnings: list[str] = []

        if payload.keys() & {"hot_bullet", "hot_narrative", "hot_threads"}:
            hot_text = HOT_FILE.read_text(encoding="utf-8")
            new_hot, w = apply_hot(payload, hot_text, today)
            writes.append((HOT_FILE, new_hot))
            warnings.extend(w)

        if payload.get("log_entry"):
            log_text = LOG_FILE.read_text(encoding="utf-8")
            writes.append((LOG_FILE, apply_log(payload["log_entry"], log_text, today)))

        if payload.get("plan_close"):
            writes.append(apply_plan_close(payload["plan_close"], today))

        for w in warnings:
            print(f"vault-write: WARN {w}", file=sys.stderr)
        if dry:
            for path, _ in writes:
                print(f"vault-write: DRY would write {path.relative_to(REPO_ROOT)}")
            return 0
        for path, text in writes:
            atomic_write(path, text)
        print(
            "vault-write: OK "
            + ", ".join(str(p.relative_to(REPO_ROOT)) for p, _ in writes)
        )
        return 0
    except CapViolation as e:
        return fail(2, f"CAP VIOLATION — nothing written. {e}")
    except (OSError, ValueError) as e:
        return fail(1, f"io error — nothing written. {e}")
    finally:
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
