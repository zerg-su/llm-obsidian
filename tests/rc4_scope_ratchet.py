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

The historical RC2 snapshot is retained separately in
``tests/test_v266_rc2_scope.py``; it is evidence about a released commit, not a
constraint on this one.
"""

from __future__ import annotations

from pathlib import Path


#: Maximum tracked Python files under ``scripts/`` for the RC4 candidate.
SCRIPT_FILE_CEILING = 264

#: Maximum total lines across those files for the RC4 candidate.
SCRIPT_LINE_CEILING = 89_650


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
