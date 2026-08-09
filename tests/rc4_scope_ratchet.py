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

The holistic Fix2 review then reserved a live-program floor ahead of bounded
history/issues, centralized the verification input identity at its production
owner, and made stale exact-HEAD gate evidence fail closed. Together with the
single-pass ANSI matcher and restored extraction rationale, this moves only the
line ceiling, by the exact measured 120 lines, to 91,297.

The follow-up review moves verification input composition from the provider
runtime into the dependency-free verification-attempt value module, restoring
the read-only projection boundary. Expanded explicit imports add exactly three
lines and move the ceiling to 91,300 without adding a file.

RC4 Fix3 binds Codex profile synchronization to a target repository that owns
its own dispatch profile, while retaining the vault as the explicit fallback.
The focused repair adds exactly ten production lines and no production file.

Fix3 also makes the frozen RC4 engineering eval portable across the registered
Swarm fork without weakening its source contract. Three exact branding aliases
normalize to the canonical prompt bytes; every other source mutation still
fails closed. The projection adds exactly thirty-two production lines and no
file, including canonical receipt hashing of the projected bytes.

The historical RC2 snapshot is retained separately in
``tests/test_v266_rc2_scope.py``; it is evidence about a released commit, not a
constraint on this one.

2.6.7 RC1 then added exactly the two production scripts its approved plan
names: ``v267_stabilization.py`` (Slice 0's read-only subject-digest, streak,
and release-stop validator) and ``harness/finalization_pivot.py`` (Slice 4's
bounded third-failure pivot packet).  The line growth is those two modules
plus the Slice 3 owner repairs and Slice 4 mechanism-neutral cycle
accounting inside existing files; the ceilings move just above that
candidate so the next unplanned script still fails the build.

The accepted RC1 Sol High finding rc1-gate-unreachable then required one
additional production script, ``live_acceptance_rc1_gate.py``: the
preflight facade that consumes the three configured RC1 gate cells and
emits streak-consumable receipts.  Two Sol implementation review rounds
grew exactly two owners: the facade gained the reserve/launch/record
execution boundary wired to the existing dispatch owner, then a
linearizable file-locked claim bound to the dispatch spec digest and
request identity with launched-only receipt recording; and
``v267_stabilization.py`` gained durable-artifact evidence validation
(containment, existence, content hashes, typed semantic records bound to
cell and corrected HEAD, and fix-OID commit resolution).  The line
ceiling moves just above that reviewed candidate; the file ceiling does
not move.

The accepted RC1 architecture-stop repair and bounded cleanup recovery then
completed the same approved stabilization corridor without adding another
production script.  Their measured final delta moves only the line ceiling to
93,750, leaving 15 lines of explicit headroom at the packaged RC1 candidate.
"""

from __future__ import annotations

from pathlib import Path


#: Maximum tracked Python files under ``scripts/`` for the 2.6.7 RC1 candidate.
SCRIPT_FILE_CEILING = 272

#: Maximum total lines across those files for the 2.6.7 RC1 candidate.
SCRIPT_LINE_CEILING = 93_750


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
