#!/usr/bin/env python3
"""Sanitized backup of the Claude auto-memory directory into the vault repo.

Copies ~/.claude/projects/<this-project>/memory/*.md -> .claude-memory/ with a
credential sanitizer pass, so the backup can live in the (private) vault repo
without violating the repo policy «не коммитим креды даже в приватный репо».

Redaction is line-oriented and conservative: it masks the secret VALUE, keeps
the surrounding context readable. Restoring redacted values is manual (from the
secret store) — see wiki/runbooks/Restore Claude Memory from Vault Backup.md.

Invoked by .claude/hooks/stop.sh (phase 2b). Safe to run manually:

    python3 scripts/memory-backup.py [--check]

--check: exit 1 if backup is stale (source has newer files), print summary only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
# Claude Code project dir = vault path with every non-alphanumeric char dashed
MEMORY_DIR = (Path.home() / ".claude" / "projects"
              / re.sub(r"[^A-Za-z0-9]", "-", str(VAULT_ROOT)) / "memory")
BACKUP_DIR = VAULT_ROOT / ".claude-memory"

# Redaction rules live in scripts/lib_sanitize.py (shared with the
# command-capture hook in .claude/hooks/command-capture.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_sanitize import sanitize  # noqa: E402


def newest_mtime(d: Path) -> float:
    files = list(d.glob("*.md"))
    return max((f.stat().st_mtime for f in files), default=0.0)


def main() -> int:
    check_only = "--check" in sys.argv
    if not MEMORY_DIR.is_dir():
        print(f"memory dir not found: {MEMORY_DIR}", file=sys.stderr)
        return 2

    if check_only:
        src, dst = newest_mtime(MEMORY_DIR), newest_mtime(BACKUP_DIR) if BACKUP_DIR.is_dir() else 0.0
        stale = src > dst + 1
        print(f"backup {'STALE' if stale else 'fresh'} (src={src:.0f} dst={dst:.0f})")
        return 1 if stale else 0

    BACKUP_DIR.mkdir(exist_ok=True)
    synced = redacted_total = 0
    src_names = set()
    for f in sorted(MEMORY_DIR.glob("*.md")):
        src_names.add(f.name)
        text = f.read_text(encoding="utf-8", errors="replace")
        clean, n = sanitize(text)
        dst = BACKUP_DIR / f.name
        if not dst.exists() or dst.read_text(encoding="utf-8", errors="replace") != clean:
            dst.write_text(clean, encoding="utf-8")
            synced += 1
        redacted_total += n
    # prune deleted memories from backup
    pruned = 0
    for f in BACKUP_DIR.glob("*.md"):
        if f.name not in src_names:
            f.unlink()
            pruned += 1
    print(f"memory-backup: {len(src_names)} files, {synced} updated, {pruned} pruned, {redacted_total} redactions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
