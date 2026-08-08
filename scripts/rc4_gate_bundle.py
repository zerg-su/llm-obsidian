#!/usr/bin/env python3
"""Compile the RC4 exact-HEAD gate bundle from one real gate run.

RC4 evidence E10 requires a committed exact-HEAD gate receipt.  Producing it by
hand is how the certificate ended up pinning a HEAD outside its own lineage, so
this tool runs the gate itself and records what actually happened: the exact
commit, the tree digest, the exact command, its exit status, and the per-stage
outcome parsed from the run.

A receipt can never be committed in the commit it attests — the file would have
to contain its own hash.  It therefore binds the commit it was produced at, and
the RC4 regression suite requires that commit to be an ancestor of HEAD and the
recorded verdict to be green.  The tree digest lets a reader confirm that the
attested tree is the one they are looking at, modulo the receipt itself.

Usage:

    python3 scripts/rc4_gate_bundle.py --run      # run `make test` and record it
    python3 scripts/rc4_gate_bundle.py --verify   # re-check a committed receipt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "docs/acceptance/evidence/v2.6.6-rc4"
RECEIPT_PATH = BUNDLE_DIR / "exact-head-gate.json"
LOG_PATH = BUNDLE_DIR / "exact-head-gate.log"
GATE_COMMAND = ("make", "test")
STAGE_RE = re.compile(r"^=== (?P<name>.+) ===$", re.MULTILINE)
MAX_LOG_BYTES = 1_048_576


class GateBundleError(RuntimeError):
    """The gate did not run, or its receipt disagrees with the repository."""


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _tracked_tree_digest(exclude: tuple[str, ...] = ()) -> str:
    """Hash every tracked path and its blob, excluding the receipt itself."""

    entries = _git("ls-tree", "-r", "HEAD").splitlines()
    digest = hashlib.sha256()
    for entry in sorted(entries):
        meta, _, relative = entry.partition("\t")
        if relative in exclude:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(meta.split()[2].encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _stages(output: str) -> list[str]:
    return [match.group("name").strip() for match in STAGE_RE.finditer(output)]


def run_gate() -> dict[str, object]:
    """Run the full gate once and compile its receipt."""

    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    head = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    completed = subprocess.run(
        list(GATE_COMMAND), cwd=ROOT, text=True, capture_output=True, check=False
    )
    output = completed.stdout + completed.stderr
    tail = output.encode("utf-8")[-MAX_LOG_BYTES:].decode("utf-8", "ignore")
    LOG_PATH.write_text(tail, encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "release": "2.6.6-rc4",
        "evidence_id": "RC4-E10-documentation-and-release-gate",
        "command": list(GATE_COMMAND),
        "produced_at_head_sha": head,
        "worktree_clean_at_run": not dirty,
        "tracked_tree_sha256": _tracked_tree_digest(
            exclude=(
                str(RECEIPT_PATH.relative_to(ROOT)),
                str(LOG_PATH.relative_to(ROOT)),
            )
        ),
        "exit_code": completed.returncode,
        "verdict": "green" if completed.returncode == 0 else "red",
        "stages_run": _stages(output),
        "log_pointer": str(LOG_PATH.relative_to(ROOT)),
        "log_sha256": hashlib.sha256(LOG_PATH.read_bytes()).hexdigest(),
        "note": (
            "A receipt cannot be committed in the commit it attests, so this "
            "record binds the commit it was produced at. The RC4 regression "
            "suite requires that commit to be an ancestor of HEAD and this "
            "verdict to be green."
        ),
    }
    RECEIPT_PATH.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def verify() -> dict[str, object]:
    """Re-check a committed receipt against the current repository."""

    if not RECEIPT_PATH.is_file():
        raise GateBundleError("RC4 exact-HEAD gate receipt is missing")
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "release",
        "evidence_id",
        "command",
        "produced_at_head_sha",
        "worktree_clean_at_run",
        "tracked_tree_sha256",
        "exit_code",
        "verdict",
        "stages_run",
        "log_pointer",
        "log_sha256",
        "note",
    }
    if set(receipt) != required or receipt.get("schema_version") != 1:
        raise GateBundleError("RC4 gate receipt fields are invalid")
    if receipt.get("release") != "2.6.6-rc4":
        raise GateBundleError("RC4 gate receipt binds another release")
    if receipt.get("verdict") != "green" or receipt.get("exit_code") != 0:
        raise GateBundleError("RC4 gate receipt does not record a green gate")
    if not receipt.get("stages_run"):
        raise GateBundleError("RC4 gate receipt records no stage")
    produced = str(receipt.get("produced_at_head_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", produced):
        raise GateBundleError("RC4 gate receipt head is not an exact commit")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", produced, "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode:
        raise GateBundleError(
            "RC4 gate receipt binds a commit outside the candidate lineage"
        )
    if not LOG_PATH.is_file():
        raise GateBundleError("RC4 gate log is missing")
    if hashlib.sha256(LOG_PATH.read_bytes()).hexdigest() != receipt["log_sha256"]:
        raise GateBundleError("RC4 gate log does not match its receipt digest")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        receipt = run_gate() if args.run else verify()
    except (GateBundleError, subprocess.CalledProcessError) as exc:
        print(f"rc4 gate bundle failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"RC4 gate bundle {receipt['verdict']} at {receipt['produced_at_head_sha']} "
        f"({len(receipt['stages_run'])} stages)"
    )
    return 0 if receipt["verdict"] == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
