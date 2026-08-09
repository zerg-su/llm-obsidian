"""The live scripts scope ratchet and its explicitly justified RC4 ceilings.

A ratchet has to measure the tree you are about to ship.  The RC2 ceilings were
breached by real RC3/RC4 release-governance work, and the response at
``269d096c`` was to repoint both assertions at a frozen commit
(``b86a33d7``) — which made them measure history instead of the candidate and
therefore incapable of ever failing again.  This module restores a current-tree
measurement and states new ceilings that someone has to defend.

Why the ceilings moved
----------------------

Six production scripts were added between the RC2 baseline and RC4, all of them
the release-governance surface the plan authorized:

``lifecycle_transition_certificate.py``, ``rc3_attempt_ledger.py``,
``rc3_coverage.py``, ``rc3_inventory.py``, ``rc3_release_disposition.py``, and
``rc3_slice_receipt.py``.  RC4 then added ``review_zero_effect.py`` while
removing duplicated predicates from three call sites.

The ceilings below sit just above the RC4 candidate rather than at a round
number: the point of a ratchet is that the next unplanned script or the next
few hundred unplanned lines fails the build and has to be argued for.  Raise
them only together with a written reason, in the same commit as the growth.

RC4 Fix2 then added the read-only harness dashboard the plan authorized:
``harness/dashboard_projection.py`` and ``harness/dashboard_view.py``.  They are
two files rather than one on purpose — projection decides what is true and the
view may only shorten it — so the ceiling moves by exactly two files and by the
lines those two modules cost.  Nothing else grew.

RC4 Fix2 Slice F then corrected that dashboard after dogfooding it: exact
operation lineage so one dispatch renders as one tree, frozen route metadata on
the step that consumes it, verification-aware step status, and resource-free
nonterminal records reported as unresolved rather than live.  That cost lines
inside the same two modules and added no script, so the file ceiling is
unchanged and only the line ceiling moves.

The final Fix2 dashboard slices add the approved standalone live CLI and
external cmux launcher in ``harness-dashboard.py``.  The existing file ceiling
already had room for that one script; the line ceiling moves by exactly its
bounded implementation cost.

The final typed review then required authoritative read-only receipt checks,
recoverable external-launch reservations, uncapped active roots, exact bounded
loop counts, and a stopped marker.  The receipt authority is extracted into
``harness/dashboard_receipts.py`` so projection remains under the per-file
quality ceiling.  The file ceiling moves by one and the line ceiling by the
review correction's measured 312-line cost.

The second typed review binds fix visits to the production callback acceptance
fact, serializes recovery of the external observer marker with atomic writes,
and distinguishes failed or cancelled roots from successful completion.  The
file surface stays fixed while the line ceiling moves by the measured 221-line
correction.

The third typed review adds read-only resolution of the exact frozen custom
pipeline and collapses launcher placement/marker ownership into one critical
section with bounded stale-start recovery.  No production file is added; the
line ceiling moves by the measured 100-line correction.

The coordinator-authorized ContextPacket repair transports one exact validated
protected amendment from the authoritative escalation chain and binds its
identity and digest into review metadata.  It adds no production file and moves
the line ceiling by the measured 64-line repair.

The fourth typed review makes child and lane bounds current-work aware, exposes
their dropped counts, and separates exact-HEAD verification truth from bounded
historical visits.  It adds no production file and moves the line ceiling by
the measured 141-line correction.

The fifth typed review makes missing exact-HEAD verification evidence explicit
without hiding a running verification child, and rejects a split response that
aliases the coordinator surface.  It adds no production file and moves the line
ceiling by the measured 13-line correction.

RC4 Fix2 visible-live-work restores the hard per-file quality gate by extracting
three cohesive modules: immutable dashboard policy, read-only CLI commands, and
CLI argument/output adaptation.  The file ceiling therefore moves by exactly
three.  Semantic colors, exact-attempt truth, caller-safe marker recovery, and
height-bounded live rendering bring the measured tree to 91,177 lines; the line
ceiling is pinned to that exact candidate with no blanket headroom.

The historical RC2 snapshot is retained separately in
``tests/test_v266_rc2_scope.py``; it is evidence about a released commit, not a
constraint on this one.
"""

from __future__ import annotations

from pathlib import Path


#: Maximum tracked Python files under ``scripts/`` for the RC4 candidate.
SCRIPT_FILE_CEILING = 268

#: Maximum total lines across those files for the RC4 candidate.
SCRIPT_LINE_CEILING = 91_177


def measure(scripts_dir: Path) -> tuple[int, int]:
    """Count the Python files and lines that actually exist in ``scripts_dir``."""

    paths = sorted(Path(scripts_dir).rglob("*.py"))
    lines = sum(
        len(path.read_text(encoding="utf-8").splitlines()) for path in paths
    )
    return len(paths), lines


def assert_within_ceilings(scripts_dir: Path) -> tuple[int, int]:
    """Fail when the live tree exceeds either explicitly justified ceiling."""

    files, lines = measure(scripts_dir)
    if files > SCRIPT_FILE_CEILING:
        raise AssertionError(
            f"scripts/ holds {files} Python files, above the RC4 ceiling "
            f"{SCRIPT_FILE_CEILING}; justify and raise the ceiling in the same "
            "commit as the growth"
        )
    if lines > SCRIPT_LINE_CEILING:
        raise AssertionError(
            f"scripts/ holds {lines} lines, above the RC4 ceiling "
            f"{SCRIPT_LINE_CEILING}; justify and raise the ceiling in the same "
            "commit as the growth"
        )
    return files, lines


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    counted, counted_lines = assert_within_ceilings(root / "scripts")
    print(
        f"OK   live scripts ratchet: {counted}/{SCRIPT_FILE_CEILING} files, "
        f"{counted_lines}/{SCRIPT_LINE_CEILING} lines"
    )
