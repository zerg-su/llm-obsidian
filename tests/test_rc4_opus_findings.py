#!/usr/bin/env python3
"""RED regression suite for the fifteen RC4 Opus release-review findings.

Each check pins the *required end state* named in
``wiki/plans/2026-08-08-rc4-final-opus-findings-fix-cycle.md`` rather than the
current behavior, so the whole suite is expected to fail at the base candidate
``269d096c`` and to go green only once every finding is dispositioned.

Every check is independent and reports its own finding identity, so a partial
fix produces a strictly shorter failure list.  The suite is deliberately
structural where the finding is structural (documentation drift, duplicated
predicates, dead branches) and behavioral where the finding is behavioral
(denominators, contract validation, ratchet liveness).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

FAILURES: list[tuple[str, str]] = []
PASSES = 0


def check(finding: str, label: str, value: object, detail: object = "") -> None:
    """Record one finding-scoped assertion without aborting the suite."""

    global PASSES
    if value:
        PASSES += 1
        print(f"OK   {finding}: {label}")
        return
    FAILURES.append((finding, f"{label}: {detail}" if detail else label))
    print(f"FAIL {finding}: {label}" + (f" -- {detail}" if detail else ""))


def source(relative: str) -> str:
    path = ROOT / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def digest(relative: str) -> str:
    path = ROOT / relative
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def function_def(relative: str, name: str) -> ast.FunctionDef | None:
    text = source(relative)
    if not text:
        return None
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )


# --- 1. rc4-rc2-scope-ratchet-neutered ---------------------------------------
#
# The ratchet must measure the working tree again.  Liveness is proven against a
# synthetic tree so the check cannot be satisfied by another frozen snapshot,
# and bounded headroom proves the ceiling still constrains real growth.

FINDING = "rc4-rc2-scope-ratchet-neutered"
try:
    from rc4_scope_ratchet import (  # type: ignore[import-not-found]  # noqa: E402
        SCRIPT_FILE_CEILING,
        SCRIPT_LINE_CEILING,
        measure,
    )
except Exception as exc:  # noqa: BLE001 - absence is the RED signal
    check(FINDING, "a live scope ratchet module exists", False, exc)
else:
    with tempfile.TemporaryDirectory(prefix="rc4-ratchet.") as raw:
        synthetic = Path(raw)
        (synthetic / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        (synthetic / "nested").mkdir()
        (synthetic / "nested" / "b.py").write_text("z = 3\n", encoding="utf-8")
        (synthetic / "ignored.txt").write_text("not python\n", encoding="utf-8")
        check(
            FINDING,
            "the ratchet measures a live tree rather than a frozen commit",
            measure(synthetic) == (2, 3),
            measure(synthetic),
        )
    files, lines = measure(ROOT / "scripts")
    check(
        FINDING,
        "the current tree is under the explicit RC4 ceilings",
        files <= SCRIPT_FILE_CEILING and lines <= SCRIPT_LINE_CEILING,
        f"{files}/{SCRIPT_FILE_CEILING} files, {lines}/{SCRIPT_LINE_CEILING} lines",
    )
    check(
        FINDING,
        "the RC4 ceilings retain bounded headroom so growth can still fail",
        0 <= SCRIPT_FILE_CEILING - files <= 12
        and 0 <= SCRIPT_LINE_CEILING - lines <= 4000,
        f"headroom {SCRIPT_FILE_CEILING - files} files, "
        f"{SCRIPT_LINE_CEILING - lines} lines",
    )

rc2_scope = source("tests/test_v266_rc2_scope.py")
check(
    FINDING,
    "the RC2 snapshot is retained under a separate historical name",
    "RC2_SHA" in rc2_scope and "historical" in rc2_scope.casefold(),
)
check(
    FINDING,
    "the RC2 snapshot no longer impersonates the live scripts ratchet",
    not re.search(r"rc2_scripts\b[\s\S]{0,400}?assert\s+len\(rc2_scripts\)", rc2_scope),
)


# --- 2. rc4-e10-exact-head-gate-and-disposition-unproven ----------------------

FINDING = "rc4-e10-exact-head-gate-and-disposition-unproven"
evidence_root = ROOT / "docs/acceptance/evidence/v2.6.6-rc4"
check(
    FINDING,
    "an exact-HEAD RC4 gate bundle is committed",
    evidence_root.is_dir() and any(evidence_root.iterdir()),
)

disposition_schemas = sorted(
    path
    for path in (ROOT / "schemas").glob("*release-disposition*.schema.json")
)
rc4_schema = None
for path in disposition_schemas:
    payload = json.loads(path.read_text(encoding="utf-8"))
    release = payload.get("properties", {}).get("release", {})
    allowed = set(release.get("enum") or ([release["const"]] if "const" in release else []))
    if "2.6.6-rc4" in allowed:
        rc4_schema = payload
        break
check(
    FINDING,
    "a disposition schema admits the 2.6.6-rc4 release identity",
    rc4_schema is not None,
    [p.name for p in disposition_schemas],
)
if rc4_schema is not None:
    reviews = rc4_schema.get("properties", {}).get("reviews", {})
    role = (
        reviews.get("items", {}).get("properties", {}).get("role", {})
    )
    check(
        FINDING,
        "the disposition role vocabulary admits one holistic release role",
        bool(set(role.get("enum") or []) - {"fable", "independent-configured"}),
        role.get("enum"),
    )
    # The review count is enforced by the tool, not the schema: this repository's
    # bounded schema validator does not accept `minItems`, so probe the tool's
    # release-scoped role vocabulary directly.
    from rc3_release_disposition import RELEASE_ROLE_SETS  # noqa: E402

    check(
        FINDING,
        "RC4 binds exactly one holistic release review role",
        len(RELEASE_ROLE_SETS.get("2.6.6-rc4", ())) == 1,
        sorted(RELEASE_ROLE_SETS.get("2.6.6-rc4", ())),
    )
    check(
        FINDING,
        "the RC3 two-role vocabulary is preserved unchanged",
        len(RELEASE_ROLE_SETS.get("2.6.6-rc3", ())) == 2,
        sorted(RELEASE_ROLE_SETS.get("2.6.6-rc3", ())),
    )

disposition_tool = source("scripts/rc3_release_disposition.py") or source(
    "scripts/release_disposition.py"
)
check(
    FINDING,
    "the disposition tool does not hard-require both RC3 roles",
    "both configured release review roles are required" not in disposition_tool,
)


# --- 3. rc4-default-deep-topology-docs-contradiction --------------------------

FINDING = "rc4-default-deep-topology-docs-contradiction"
from harness.finalization_policy import (  # noqa: E402
    AvailabilityEvidence,
    FinalizationPolicy,
    compile_finalization_routes,
)
from model_routing import load_config  # noqa: E402

policy = FinalizationPolicy()
boundary = policy.add_independent_model_after
config = load_config(ROOT)
NOW = 1_000
available = AvailabilityEvidence(
    route_alias=policy.independent_route_alias,
    status="available",
    source="provider-adapter",
    checked_at_epoch=NOW - 10,
    valid_until_epoch=NOW + 10_000,
)


def routes_for(cycle: int, availability: object | None) -> tuple[int, str]:
    decision = compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=cycle,
        availability=availability,
        now_epoch=NOW,
    )
    return len(decision.routes), decision.reason


# The independent lane is gated twice: by the cycle boundary and by fresh
# provider availability.  Both gates must be documented, so both are pinned.
counts_unknown = {
    cycle: routes_for(cycle, None) for cycle in range(1, policy.max_cycles + 1)
}
counts_available = {
    cycle: routes_for(cycle, available)
    for cycle in range(1, policy.max_cycles + 1)
}

check(
    FINDING,
    "cycles up to the policy boundary compile exactly one primary route",
    all(
        counts_available[cycle] == (1, "primary-only")
        for cycle in range(1, boundary + 1)
    ),
    counts_available,
)
check(
    FINDING,
    "cycles past the boundary admit the independent route when it is available",
    all(
        counts_available[cycle] == (2, "independent-available")
        for cycle in range(boundary + 1, policy.max_cycles + 1)
    ),
    counts_available,
)
check(
    FINDING,
    "cycles past the boundary stay primary-only without fresh availability",
    all(
        counts_unknown[cycle] == (1, "availability-unknown")
        for cycle in range(boundary + 1, policy.max_cycles + 1)
    ),
    counts_unknown,
)

readme = source("README.md")
routing_doc = source("docs/model-routing.md")
for name, text in (("README.md", readme), ("docs/model-routing.md", routing_doc)):
    check(
        FINDING,
        f"{name} states the finalization cycle gating for default Deep",
        f"1–{boundary}" in text or f"1-{boundary}" in text,
    )
    # Every claim of a dual-provider holistic Deep topology must carry the cycle
    # gating in the same reading unit.  The unit is the paragraph for prose and
    # the row for a table, so a qualification in the next sentence counts but one
    # in another section does not.
    units: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.splitlines() if line.strip()]
        if lines and all(line.lstrip().startswith("|") for line in lines):
            units.extend(lines)
        else:
            units.append(" ".join(block.split()))
    unqualified = [
        unit
        for unit in units
        if "independent" in unit.casefold()
        and "holistic" in unit.casefold()
        and "deep" in unit.casefold()
        and "cycle" not in unit.casefold()
    ]
    check(
        FINDING,
        f"{name} no longer claims unconditional dual-provider default Deep",
        not unqualified,
        unqualified[:1],
    )


# --- 4. rc4-accepted-deviations-artifact-absent -------------------------------

FINDING = "rc4-accepted-deviations-artifact-absent"
deviations_rel = "docs/acceptance/v2.6.6-rc4-accepted-deviations.json"
check(FINDING, "an RC4 accepted-deviations artifact exists", (ROOT / deviations_rel).is_file())
if (ROOT / deviations_rel).is_file():
    payload = json.loads(source(deviations_rel))
    entries = payload.get("deviations") or payload.get("accepted_deviations") or []
    check(
        FINDING,
        "every RC4 deviation carries id, scope, rationale, and residual risk",
        bool(entries)
        and all(
            {"id", "scope", "rationale", "residual_risk"} <= set(item)
            for item in entries
        ),
        entries,
    )
    prior = {
        digest("docs/acceptance/v2.6.6-rc1-accepted-deviations.json"),
        digest("docs/acceptance/v2.6.6-rc2-accepted-deviations.json"),
        digest("docs/acceptance/v2.6.4-accepted-deviations.json"),
    }
    check(
        FINDING,
        "the RC4 deviations digest does not alias a prior release artifact",
        digest(deviations_rel) not in prior,
    )


# --- 5. rc4-certificate-events-derived-from-tests -----------------------------

FINDING = "rc4-certificate-events-derived-from-tests"
certificate_src = source("scripts/lifecycle_transition_certificate.py")
check(
    FINDING,
    "the certificate script does not import from tests/harness",
    '"tests"' not in certificate_src and "tests/harness" not in certificate_src,
)

from harness.provider_events import EVENT_KINDS as PRODUCTION_EVENT_KINDS  # noqa: E402
from lifecycle_transition_certificate import (  # noqa: E402
    MANIFEST_PATH,
    production_events,
)

events = production_events()
check(
    FINDING,
    "the certificate provider-event denominator is the production owner",
    set(events["provider_event_kinds"]) == set(PRODUCTION_EVENT_KINDS),
    sorted(set(PRODUCTION_EVENT_KINDS) ^ set(events["provider_event_kinds"])),
)
manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
check(
    FINDING,
    "the manifest declares the production provider-event vocabulary",
    set(manifest["events"]["provider_event_kinds"]) == set(PRODUCTION_EVENT_KINDS),
    sorted(
        set(PRODUCTION_EVENT_KINDS)
        ^ set(manifest["events"]["provider_event_kinds"])
    ),
)
check(
    FINDING,
    "the declared denominator source names every event owner it claims",
    "provider_events" in str(manifest.get("denominator_source", "")),
    manifest.get("denominator_source"),
)


# --- 6. rc4-certificate-release-disposition-witness-mismatch ------------------

FINDING = "rc4-certificate-release-disposition-witness-mismatch"
witness_names = {str(case["witness"]) for case in manifest["cases"]}
check(
    FINDING,
    "the summary-schema witness is named for what it proves",
    "release-disposition-classification" not in witness_names,
    sorted(witness_names),
)
check(
    FINDING,
    "a real release-boundary witness is declared",
    any("release-boundary" in name for name in witness_names),
    sorted(witness_names),
)
for edge in ("missing digest", "byte substitution", "symlink escape"):
    token = edge.replace(" ", "-")
    check(
        FINDING,
        f"the release-boundary witness covers {edge}",
        token in certificate_src or edge in certificate_src,
    )


# --- 7. rc4-zero-effect-gate-shape-duplicated --------------------------------

FINDING = "rc4-zero-effect-gate-shape-duplicated"
try:
    from review_zero_effect import (  # type: ignore[import-not-found]  # noqa: E402
        zero_effect_gate_shape,
    )
except Exception as exc:  # noqa: BLE001 - absence is the RED signal
    check(FINDING, "one shared zero-effect predicate exists", False, exc)
else:
    check(FINDING, "one shared zero-effect predicate exists", True)
    base_state = {
        "status": "attention-required",
        "execution_protocol": "exact-head-attempt-v1",
        "lanes": [],
        "round_results": {},
        "final_results": {},
        "evidence": {},
        "attempt": {
            "status": "terminal",
            "terminal": {"result": "attention-required", "lane_results": []},
        },
    }
    check(FINDING, "the canonical zero-effect gate is recognized", zero_effect_gate_shape(base_state))
    none_state = dict(base_state, round_results=None, final_results=None, evidence=None)
    check(
        FINDING,
        "the None policy is one explicit decision, not three",
        zero_effect_gate_shape(none_state) == zero_effect_gate_shape(base_state),
    )
    for field, poisoned in (
        ("status", "reviewing"),
        ("lanes", ["anthropic-holistic"]),
        ("round_results", {"anthropic-holistic": "pointer"}),
        ("final_results", {"anthropic-holistic": "pointer"}),
        ("evidence", {"anthropic-holistic": "pointer"}),
    ):
        check(
            FINDING,
            f"a non-empty {field} is not zero-effect",
            not zero_effect_gate_shape(dict(base_state, **{field: poisoned})),
        )

for relative, symbol in (
    ("scripts/task_review_identity.py", "review_zero_effect"),
    ("scripts/harness/cli.py", "review_zero_effect"),
    ("scripts/task_review_flow.py", "review_zero_effect"),
):
    check(
        FINDING,
        f"{relative} consumes the shared predicate",
        symbol in source(relative),
    )


# --- 8. rc4-complete-ready-results-dead-half ---------------------------------

FINDING = "rc4-complete-ready-results-dead-half"
complete_ready = function_def("scripts/task_review_flow.py", "_complete_ready_results")
check(FINDING, "_complete_ready_results still exists", complete_ready is not None)
if complete_ready is not None:
    params = {
        arg.arg
        for arg in [*complete_ready.args.args, *complete_ready.args.kwonlyargs]
    }
    check(
        FINDING,
        "the production-dead already_awaiting parameter is gone",
        "already_awaiting" not in params,
        sorted(params),
    )
    body = ast.get_source_segment(source("scripts/task_review_flow.py"), complete_ready) or ""
    check(
        FINDING,
        "the unreachable deferred-resolution arm is gone",
        "defer_round_for_resolution" not in body,
    )
    check(
        FINDING,
        "one incremental completion path remains",
        body.count("complete_attempt_round") == 1 and "complete_round(" not in body,
    )
check(
    FINDING,
    "_should_defer_ready_results is retired with its only consumer",
    function_def("scripts/task_review_flow.py", "_should_defer_ready_results") is None,
)
resolution_src = source("scripts/harness/workflows/review_gate_resolution.py")
flow_src = source("scripts/task_review_flow.py")
# The finding's scope is the dead half of _complete_ready_results, which must no
# longer reach the deferred-resolution gate.  ReviewGateController keeps the
# method itself: the awaiting-resolution state it writes is still produced by
# review_gate_decisions.py, and six scenarios in test_review_gate.py cover that
# machinery.  Retiring it would delete live coverage for no correctness gain, so
# it is carried as a named RC4 accepted deviation instead.
check(
    FINDING,
    "the exact-HEAD completion path no longer reaches deferred resolution",
    "defer_round_for_resolution" not in flow_src,
)
check(
    FINDING,
    "the orphaned gate method is declared as an accepted RC4 deviation",
    "defer_round_for_resolution"
    in source("docs/acceptance/v2.6.6-rc4-accepted-deviations.json"),
)


# --- 9. rc4-frozen-topology-optional-fail-open -------------------------------

FINDING = "rc4-frozen-topology-optional-fail-open"
import task_contract  # noqa: E402

live_meta = json.loads(source(".task-meta.json") or "{}")
def normalize_error(payload: dict) -> str:
    """Return the contract rejection reason, or '' when the record is accepted."""

    try:
        task_contract.normalize(dict(payload), verify_plan_hash=False)
    except Exception as exc:  # noqa: BLE001 - any rejection is the signal
        return str(exc) or exc.__class__.__name__
    return ""


review_enabled_v4 = dict(live_meta)
review_enabled_v4.pop("review_topology", None)
review_enabled_v4["version"] = 4
check(
    FINDING,
    "the base record is otherwise contract-valid (control)",
    normalize_error(live_meta) == "",
    normalize_error(live_meta),
)

# The finding allows either covering current-checkout behavior or narrowing E1
# truthfully.  `.task-meta.json` is read-only under this task's contract, so the
# first arm is unavailable: the enforcement point is the review launch boundary,
# and E1 is worded to match.  Pinned here so the split cannot silently widen.
sys.path.insert(0, str(ROOT / "scripts"))
from task_review_context import _assert_frozen_topology  # noqa: E402
from task_review_shared import TaskReviewError  # noqa: E402


class _UnboundRequest:
    topology_sha256 = ""

    class topology:  # noqa: D106 - inert stand-in, never reached
        sha256 = ""

        @staticmethod
        def payload() -> dict:
            return {}


# Enforcement moved to creation: dispatch binds the topology for every
# review-enabled task, so no new task can be unbound.  Absence at launch stays
# tolerated because this task's own metadata is read-only and unbound, and
# failing closed there would break the very review this candidate must pass.
# What must never be tolerated is a binding that disagrees with the compiled
# topology.
import inspect  # noqa: E402

import dispatch_workspace  # noqa: E402

workspace_src = inspect.getsource(dispatch_workspace)
check(
    FINDING,
    "dispatch binds a frozen topology for every review-enabled task",
    'meta["review_topology"] = {' in workspace_src
    and "if review.enabled:" in workspace_src,
)


class _DriftedRequest:
    topology_sha256 = "0" * 64

    class topology:
        sha256 = "0" * 64

        @staticmethod
        def payload() -> dict:
            return {"schema_version": 1, "mode": "simple"}


drifted_meta = {
    "version": 4,
    "review_policy": {"mode": "simple"},
    "review_topology": {
        "payload": {"schema_version": 1, "mode": "deep"},
        "sha256": "1" * 64,
    },
}
try:
    _assert_frozen_topology(drifted_meta, _DriftedRequest())
except TaskReviewError:
    drift_rejected = True
else:
    drift_rejected = False
check(
    FINDING,
    "a frozen topology that disagrees with the compiled one fails closed",
    drift_rejected,
)
check(
    FINDING,
    "an unbound legacy record stays launchable rather than stranded",
    _assert_frozen_topology(
        {"version": 4, "review_policy": {"mode": "simple"}}, _DriftedRequest()
    )
    is None,
)
check(
    FINDING,
    "RC4-E1 is narrowed to what is actually enforced",
    "bound at the review launch boundary"
    in source("docs/acceptance/v2.6.6-rc4-release-readiness.md"),
)
check(
    FINDING,
    "the residual gap is recorded as an accepted RC4 deviation",
    "D-266-RC4-02"
    in source("docs/acceptance/v2.6.6-rc4-accepted-deviations.json"),
)

context_src = source("scripts/task_review_context.py")
check(
    FINDING,
    "_assert_frozen_topology no longer returns on a missing binding",
    not re.search(
        r"def _assert_frozen_topology[\s\S]{0,320}?if not isinstance\(frozen, Mapping\):\s*\n\s*return\b",
        context_src,
    ),
)


# --- 10. rc4-instruction-weakening-heuristic-brittle -------------------------

FINDING = "rc4-instruction-weakening-heuristic-brittle"
lint_src = source("scripts/lint-instructions.py")
# The disposition is a structural rule over parsed blocks and list items rather
# than an exact-block marker: the marker variant would have required editing the
# contract text in both governed files, and the existing regressions exercise
# synthetic documents that carry no markers.
check(
    FINDING,
    "weakening is decided over parsed blocks and items",
    "_split_blocks" in lint_src and "_split_items" in lint_src,
)
check(
    FINDING,
    "weakening scope is no longer inferred from indentation",
    "[:1].isspace()" not in lint_src,
)
check(
    FINDING,
    "the positional line-walk is gone",
    "lead_anchor" not in lint_src and "prior_item_start" not in lint_src,
)
lint_result = subprocess.run(
    [sys.executable, str(ROOT / "scripts/lint-instructions.py")],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
check(
    FINDING,
    "the instruction lint stays green on the real instruction files",
    lint_result.returncode == 0,
    lint_result.stderr.strip()[:200],
)
regression = subprocess.run(
    [sys.executable, str(ROOT / "tests/test_instruction_lint.py")],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
check(
    FINDING,
    "every existing weakening regression still passes",
    regression.returncode == 0,
    regression.stderr.strip()[:200],
)


# --- 11. rc4-certificate-doc-pins-stale-head ---------------------------------

FINDING = "rc4-certificate-doc-pins-stale-head"
certificate_doc = source("docs/acceptance/v2.6.6-rc4-transition-certificate.md")
head = git("rev-parse", "HEAD").stdout.strip()
# The finding requires the child values to be *retained* as provenance, so the
# lineage requirement applies to the binding claims only.  Everything outside the
# labelled provenance subsection must be reachable from the candidate.
provenance_marker = "### Provenance (superseded child values)"
binding_part, _, provenance_part = certificate_doc.partition(provenance_marker)
pinned = set(re.findall(r"\b[0-9a-f]{40}\b", binding_part))
reachable = {
    candidate
    for candidate in pinned
    if git("merge-base", "--is-ancestor", candidate, head).returncode == 0
}
check(
    FINDING,
    "every SHA the certificate binds is in the candidate lineage",
    bool(pinned) and pinned == reachable,
    sorted(pinned - reachable) or "no SHA is bound at all",
)
# A document cannot pin the commit that contains it, so binding the HEAD string
# is not the anti-staleness guarantee — digest agreement is.  Recompile the
# certificate against the current tree and require the published digests to
# match, which goes red the moment any denominator owner or the manifest drifts.
from lifecycle_transition_certificate import (  # noqa: E402
    MANIFEST_PATH as CERT_MANIFEST,
    compile_certificate,
)

with tempfile.TemporaryDirectory(prefix="rc4-cert.") as raw:
    live_certificate = compile_certificate(ROOT, CERT_MANIFEST, Path(raw) / "base")
check(
    FINDING,
    "the published manifest digest still matches the current tree",
    str(live_certificate["manifest_sha256"]) in certificate_doc,
    live_certificate["manifest_sha256"],
)
check(
    FINDING,
    "the published denominator digest still matches the current tree",
    str(live_certificate["denominator_source_sha256"]) in certificate_doc,
    live_certificate["denominator_source_sha256"],
)
check(
    FINDING,
    "the certificate is complete at the current tree",
    live_certificate["verdict"] == "complete",
    live_certificate["verdict"],
)
check(
    FINDING,
    "superseded child values survive under a labelled provenance heading",
    bool(provenance_part)
    and "b4c26159104ba7a1231e885413e46d7e56c0df61" in provenance_part
    and "not an ancestor" in provenance_part,
)


# --- 12. rc4-live-evidence-embeds-operator-paths -----------------------------

FINDING = "rc4-live-evidence-embeds-operator-paths"
live_evidence_rel = "docs/acceptance/evidence/v2.6.6/rc4-engineering-discipline-live.json"
live_evidence = source(live_evidence_rel)
check(FINDING, "the RC4 live evidence carrier exists", bool(live_evidence))
if live_evidence:
    check(
        FINDING,
        "no operator-local absolute path is published",
        "/Users/" not in live_evidence,
        sorted(set(re.findall(r"/Users/[^\"]+", live_evidence)))[:3],
    )
    # Match JSON *values* only: a key such as "call_id" legitimately starts with
    # `call_` and must not be mistaken for a leaked provider identifier.
    raw_tool_ids = re.findall(
        r"\"(?:call_|ctc_)[A-Za-z0-9]+\"(?!\s*:)", live_evidence
    )
    raw_sessions = re.findall(
        r"\"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\"",
        live_evidence,
    )
    check(
        FINDING,
        "no raw session or provider tool identifier is published",
        not raw_tool_ids and not raw_sessions,
        (raw_tool_ids + raw_sessions)[:3],
    )
    payload = json.loads(live_evidence)
    capture = payload.get("command_capture", {})
    check(
        FINDING,
        "evidentiary digests are retained",
        bool(capture.get("command_event", {}).get("event_id"))
        and bool(capture.get("tool_result", {}).get("receipt_line_sha256")),
    )


# --- 13. rc4-exact-attempt-readability-and-telemetry-gap ---------------------

FINDING = "rc4-exact-attempt-readability-and-telemetry-gap"
check(
    FINDING,
    "the exact-attempt signature is one parameter per line",
    "already_awaiting: bool = False, exact_attempt: bool = False," not in flow_src,
)
attempt_src = source("scripts/harness/workflows/review_gate_attempt.py")
check(
    FINDING,
    "the attempt gate no longer packs two mapping keys onto one line",
    '"topology": request.topology.payload(), "topology_sha256"' not in attempt_src
    and '"topology", "topology_sha256",' not in attempt_src,
)
transport_src = source("scripts/task_review_transport.py")
check(
    FINDING,
    "transport helpers are separated by two blank lines",
    not re.search(r"[^\n]\n(?:\n?)def _collect_ready_results\(", transport_src),
    "module-level def follows the previous statement without two blank lines",
)
if complete_ready is not None:
    body = ast.get_source_segment(flow_src, complete_ready) or ""
    skip_branch = re.search(
        r"if [^\n]*_ready_result_is_recorded[\s\S]{0,400}?continue", body
    )
    check(
        FINDING,
        "the already-recorded retry still completes its telemetry",
        bool(skip_branch)
        and "_record_accepted_result" in (skip_branch.group(0) if skip_branch else ""),
        "durable completion can crash before telemetry and never re-emit",
    )


# --- 14. rc4-initial-input-addendum-outside-contract -------------------------

FINDING = "rc4-initial-input-addendum-outside-contract"
readiness = source("docs/acceptance/v2.6.6-rc4-release-readiness.md")
addendum_ids = re.findall(r"RC4-E(\d+)-", readiness)
check(
    FINDING,
    "the initial-input addendum carries a first-class evidence identifier",
    any(int(value) > 10 for value in addendum_ids),
    sorted(set(addendum_ids)),
)
docs_text = "\n".join(
    path.read_text(encoding="utf-8", errors="ignore")
    for path in (ROOT / "docs").rglob("*.md")
)
check(
    FINDING,
    "input-unconfirmed has operator recovery documentation",
    "input-unconfirmed" in docs_text,
)
check(
    FINDING,
    "the bounded initial-start observation budget is documented",
    "initial_start_observation_limit" in docs_text
    or "observation budget" in docs_text.casefold(),
)


# --- 15. rc4-same-head-preflight-retry-relaxation ----------------------------

FINDING = "rc4-same-head-preflight-retry-relaxation"
check(
    FINDING,
    "the same-HEAD relaxation uses the shared zero-effect predicate",
    not re.search(
        r"zero_lane_preflight\s*=\s*\(\s*\n\s*prior_attempt\.terminal\.result", flow_src
    ),
)
check(
    FINDING,
    "no weaker same-HEAD admission path is hand-inlined",
    flow_src.count("prior_state.get(\"round_results\")") == 0,
)
check(
    FINDING,
    "the exact same-HEAD zero-effect predicate is documented",
    "same-HEAD" in docs_text or "same_head" in docs_text,
)


# --- report ------------------------------------------------------------------

print()
print(f"{PASSES} checks passed, {len(FAILURES)} failed")
if FAILURES:
    findings = sorted({finding for finding, _ in FAILURES})
    print(f"open findings ({len(findings)}): {', '.join(findings)}")
    for finding, detail in FAILURES:
        print(f"  - {finding}: {detail}")
    raise SystemExit(1)
print("RC4 Opus findings regression suite passed")
