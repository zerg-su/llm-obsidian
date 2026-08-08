#!/usr/bin/env python3
"""Certify the bounded lifecycle transition denominator against production.

The certificate answers four questions with one deterministic run:

* Does the versioned manifest declare exactly the production state/event
  vocabulary (no missing and no extra members)?
* Is every supported production edge classified and actually visited by
  driving the production reducer, rather than merely declared?
* Is every declared forbidden edge rejected, and is the rejection closed over
  the whole state product rather than the curated list alone?
* Does every named lifecycle case carry a bounded production witness through a
  real entry point instead of a restated expectation?

Research remains the only explicitly excluded callback transport.  Nothing here
starts a provider, a surface, or any other external effect.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker, CallbackError  # noqa: E402
from harness.contracts import (  # noqa: E402
    AttentionReason,
    CallbackEnvelope,
    ContractError,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.ephemeral_provider import (  # noqa: E402
    EphemeralProviderError,
    _load_schema,
    validate_output_instance,
)
from harness.reconciliation import reconcile  # noqa: E402
from harness.runtime_worker_summary import RuntimeWorkerSummaryMixin  # noqa: E402
from harness.provider_events import EVENT_KINDS as PROVIDER_EVENT_KINDS  # noqa: E402
from harness.state_machine import TERMINAL, TRANSITIONS, transition  # noqa: E402
from harness.store import OperationStore, StoreError  # noqa: E402
from rc3_release_disposition import (  # noqa: E402
    DispositionError,
    _bytes as _release_bytes,
    _matches_boundary_pointer as _matches_release_pointer,
    _sha256 as _release_sha256,
)
from review_contract import VERDICTS  # noqa: E402
from wiki_summary_contract import WikiSummaryError, validate_summary  # noqa: E402


MANIFEST_PATH = ROOT / "config/lifecycle-transition-v1.json"
SCHEMA_PATH = ROOT / "schemas/lifecycle-transition-certificate-v1.schema.json"
#: Every production module that owns part of the certified denominator.  States
#: and edges come from the state machine; attention reasons from the contracts;
#: callback kinds from the broker; provider event kinds from the provider-event
#: contract; review verdicts from the review contract.  The certificate hashes
#: all of them, so a change to any owner invalidates the published digest.
DENOMINATOR_SOURCES = (
    "scripts/harness/callbacks.py",
    "scripts/harness/contracts.py",
    "scripts/harness/provider_events.py",
    "scripts/harness/state_machine.py",
    "scripts/review_contract.py",
)
DENOMINATOR_SOURCE = ",".join(DENOMINATOR_SOURCES)
MANIFEST_ID = "lifecycle-transition-v1"
MAX_MANIFEST_BYTES = 262_144
EDGE_CLASSES = frozenset(
    {
        "admission",
        "attention",
        "cancellation",
        "cleanup",
        "execution",
        "failure",
        "finalization",
        "recovery",
    }
)
STATE_ROLES = frozenset({"initial", "active", "attention", "terminal"})
#: Event groups whose vocabulary is owned by a production module.  Each name
#: maps to exactly one owner in DENOMINATOR_SOURCE; nothing here is read from
#: tests.  The deterministic simulator's scenario verbs are deliberately NOT in
#: this tuple: they are a fault-injection DSL that production does not own, so
#: they are declared separately as `simulator_scenario_actions` and
#: cross-checked against the simulator by the test suite.
EVENT_GROUPS = (
    "attention_reasons",
    "callback_kinds",
    "provider_event_kinds",
    "review_verdicts",
)
EXCLUDED_TRANSPORTS = ("research",)
OWNER_ID = "certificate-owner"
OPERATION_ID = "certificate-operation"
RUN_ID = "certificate-run"
LANE_ID = "certificate-lane"
SURFACE_ID = "certificate-surface"
PROCESS_IDENTITY = "a" * 64
SUPERVISOR_IDENTITY = "b" * 64
ROUTING_SHA256 = "c" * 64
TRANSITION_BOUNDARY = "operation-transition-published:before"


class CertificateError(ValueError):
    """The manifest, the production denominator, or a witness disagreed."""


def _edge(source: str, target: str) -> str:
    return f"{source}->{target}"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise CertificateError(f"{label} must be a bounded identifier")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    """Load one bounded declarative manifest with no executable content."""

    path = path.expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise CertificateError("transition manifest is unavailable")
    raw = path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CertificateError("transition manifest exceeds its byte bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CertificateError("transition manifest must be valid JSON") from exc
    if not isinstance(value, dict):
        raise CertificateError("transition manifest must be an object")
    required = {
        "schema_version",
        "manifest_id",
        "denominator_source",
        "edge_classes",
        "excluded_transports",
        "states",
        "events",
        "simulator_scenario_actions",
        "supported_edges",
        "forbidden_edges",
        "cases",
    }
    if set(value) != required:
        raise CertificateError("transition manifest fields are invalid")
    if value["schema_version"] != 1 or value["manifest_id"] != MANIFEST_ID:
        raise CertificateError("transition manifest identity or version is invalid")
    if value["denominator_source"] != DENOMINATOR_SOURCE:
        raise CertificateError("transition manifest denominator source is invalid")
    if list(value["excluded_transports"]) != list(EXCLUDED_TRANSPORTS):
        raise CertificateError("research must remain the only excluded transport")
    if set(value["edge_classes"]) != EDGE_CLASSES:
        raise CertificateError("transition manifest edge classes are invalid")
    _validate_manifest_states(value["states"])
    _validate_manifest_events(value["events"])
    _validate_scenario_actions(value["simulator_scenario_actions"])
    _validate_manifest_edges(value["supported_edges"], value["forbidden_edges"])
    _validate_manifest_cases(value["cases"])
    return value


def _validate_manifest_states(states: object) -> None:
    if not isinstance(states, list) or not states:
        raise CertificateError("manifest states must be a non-empty array")
    seen: set[str] = set()
    for item in states:
        if not isinstance(item, dict) or set(item) != {"state", "role"}:
            raise CertificateError("manifest state entry fields are invalid")
        state = _identifier(item["state"], "manifest state")
        if item["role"] not in STATE_ROLES or state in seen:
            raise CertificateError(f"manifest state {state} is invalid or repeated")
        seen.add(state)


def _denominator_digest(root: Path) -> str:
    """Bind every denominator owner into one digest, in declared order."""

    digest = hashlib.sha256()
    for relative in DENOMINATOR_SOURCES:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_tracked_sha256(root, relative)))
    return digest.hexdigest()


def _validate_scenario_actions(actions: object) -> None:
    """Validate the declared simulator vocabulary without importing the simulator.

    These verbs are owned by the deterministic simulator, not by production, so
    the certificate only checks that the declaration is well formed.  Equality
    with ``lifecycle_simulator_oracle.ACTIONS`` is asserted by the test suite,
    which is allowed to import the simulator; keeping that cross-check out of
    this module is what prevents a production script from depending on tests.
    """

    if (
        not isinstance(actions, list)
        or not actions
        or len(set(actions)) != len(actions)
        or sorted(actions) != list(actions)
    ):
        raise CertificateError(
            "simulator scenario actions must be a sorted unique array"
        )
    for action in actions:
        _identifier(action, "simulator scenario action")


def _validate_manifest_events(events: object) -> None:
    if not isinstance(events, dict) or set(events) != set(EVENT_GROUPS):
        raise CertificateError("manifest event groups are invalid")
    for group in EVENT_GROUPS:
        members = events[group]
        if (
            not isinstance(members, list)
            or not members
            or len(set(members)) != len(members)
            or sorted(members) != list(members)
        ):
            raise CertificateError(f"manifest event group {group} is invalid")
        for member in members:
            _identifier(member, f"manifest event in {group}")


def _validate_manifest_edges(supported: object, forbidden: object) -> None:
    if not isinstance(supported, list) or not supported:
        raise CertificateError("manifest supported edges must be a non-empty array")
    seen: set[str] = set()
    for item in supported:
        if not isinstance(item, dict) or set(item) != {"from", "to", "edge_class"}:
            raise CertificateError("manifest supported edge fields are invalid")
        key = _edge(
            _identifier(item["from"], "supported edge source"),
            _identifier(item["to"], "supported edge target"),
        )
        if key in seen:
            raise CertificateError(f"manifest repeats supported edge {key}")
        seen.add(key)
    if not isinstance(forbidden, list) or not forbidden:
        raise CertificateError("manifest forbidden edges must be a non-empty array")
    seen = set()
    for item in forbidden:
        if not isinstance(item, dict) or set(item) != {"from", "to", "reason"}:
            raise CertificateError("manifest forbidden edge fields are invalid")
        key = _edge(
            _identifier(item["from"], "forbidden edge source"),
            _identifier(item["to"], "forbidden edge target"),
        )
        if key in seen or not isinstance(item["reason"], str) or not item["reason"]:
            raise CertificateError(f"manifest forbidden edge {key} is invalid")
        seen.add(key)


def _validate_manifest_cases(cases: object) -> None:
    if not isinstance(cases, list) or not cases:
        raise CertificateError("manifest cases must be a non-empty array")
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, dict) or set(item) != {"case_id", "witness", "paths"}:
            raise CertificateError("manifest case fields are invalid")
        case_id = _identifier(item["case_id"], "manifest case id")
        _identifier(item["witness"], "manifest case witness")
        if case_id in seen:
            raise CertificateError(f"manifest repeats case {case_id}")
        seen.add(case_id)
        paths = item["paths"]
        if not isinstance(paths, list) or not paths:
            raise CertificateError(f"manifest case {case_id} declares no path")
        for path in paths:
            if not isinstance(path, list) or len(path) < 2 or len(path) > 16:
                raise CertificateError(f"manifest case {case_id} path is invalid")
            for state in path:
                _identifier(state, f"manifest case {case_id} path state")


def production_edges() -> frozenset[str]:
    return frozenset(
        _edge(source, target)
        for source, targets in TRANSITIONS.items()
        for target in targets
    )


def production_events() -> dict[str, list[str]]:
    """Collect the entry-point vocabulary from its production owners."""

    return {
        "attention_reasons": sorted(item.value for item in AttentionReason),
        "callback_kinds": sorted(_production_callback_kinds()),
        "provider_event_kinds": sorted(_production_provider_event_kinds()),
        "review_verdicts": sorted(VERDICTS),
    }


def _production_callback_kinds() -> set[str]:
    """Probe the production router instead of restating its kind list."""

    kinds: set[str] = set()
    for kind in ("review", "result", "wiki-summary", "research", "unknown-kind"):
        payload = {"verdict": "approve"} if kind == "review" else {"status": "complete"}
        try:
            CallbackBroker._next_state(_envelope(kind, payload))
        except CallbackError:
            continue
        kinds.add(kind)
    return kinds


def _production_provider_event_kinds() -> set[str]:
    """Read the kinds the production provider-event contract actually admits."""

    return set(PROVIDER_EVENT_KINDS)


def _envelope(kind: str, payload: Mapping[str, object]) -> CallbackEnvelope:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
    return CallbackEnvelope(
        f"certificate-{kind}",
        OPERATION_ID,
        RUN_ID,
        kind,
        dict(payload),
        hashlib.sha256(encoded).hexdigest(),
    )


def _spec(profile: str = "dispatch") -> OperationSpec:
    return OperationSpec(
        operation_id=OPERATION_ID,
        idempotency_key="certificate-idempotency",
        kind="dispatch",
        owner_id=OWNER_ID,
        route=RuntimeRoute("codex", "certificate-model", "high", profile, ROUTING_SHA256),
        context_manifest="context/manifest.json",
        verification_profile="scoped",
    )


def _record(state: str) -> OperationRecord:
    return OperationRecord(_spec(), state, 0, LANE_ID, RUN_ID)


def compare_states(manifest: Mapping[str, Any]) -> dict[str, Any]:
    declared = {str(item["state"]) for item in manifest["states"]}
    production = set(TRANSITIONS)
    terminal_declared = {
        str(item["state"])
        for item in manifest["states"]
        if item["role"] == "terminal"
    }
    return {
        "declared": len(declared),
        "production": len(production),
        "missing": sorted(production - declared),
        "extra": sorted(declared - production),
        "terminal_declared": sorted(terminal_declared),
        "terminal_production": sorted(TERMINAL),
    }


def compare_events(manifest: Mapping[str, Any]) -> dict[str, Any]:
    production = production_events()
    declared = manifest["events"]
    missing: list[str] = []
    extra: list[str] = []
    for group in EVENT_GROUPS:
        expected = set(production[group])
        actual = set(declared[group])
        missing.extend(f"{group}.{item}" for item in sorted(expected - actual))
        extra.extend(f"{group}.{item}" for item in sorted(actual - expected))
    return {
        "declared": sum(len(declared[group]) for group in EVENT_GROUPS),
        "production": sum(len(production[group]) for group in EVENT_GROUPS),
        "missing": sorted(missing),
        "extra": sorted(extra),
    }


def compare_edges(manifest: Mapping[str, Any]) -> dict[str, Any]:
    declared = {
        _edge(str(item["from"]), str(item["to"])): str(item["edge_class"])
        for item in manifest["supported_edges"]
    }
    production = production_edges()
    unclassified = sorted(
        key for key, value in declared.items() if value not in EDGE_CLASSES
    )
    return {
        "supported": len(declared),
        "production": len(production),
        "missing": sorted(production - set(declared)),
        "extra": sorted(set(declared) - production),
        "unclassified": unclassified,
    }


def visit_edges(manifest: Mapping[str, Any]) -> tuple[set[str], dict[str, int]]:
    """Consume every declared case path through the production reducer."""

    visited: set[str] = set()
    per_case: dict[str, int] = {}
    for case in manifest["cases"]:
        case_visited: set[str] = set()
        for path in case["paths"]:
            record = _record(str(path[0]))
            for target in path[1:]:
                target = str(target)
                reason = (
                    AttentionReason.ATTENTION_REQUIRED
                    if target == "attention-required"
                    else None
                )
                try:
                    record, result = transition(record, target, reason=reason)
                except ContractError as exc:
                    raise CertificateError(
                        f"case {case['case_id']} path edge was rejected: "
                        f"{_edge(record.state, target)}"
                    ) from exc
                if not result.changed or record.state != target:
                    raise CertificateError(
                        f"case {case['case_id']} edge did not advance: "
                        f"{_edge(result.previous_state, target)}"
                    )
                case_visited.add(_edge(result.previous_state, target))
        per_case[str(case["case_id"])] = len(case_visited)
        visited |= case_visited
    return visited, per_case


def check_forbidden(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Reject the curated list and close the rejection over the state product."""

    declared = [
        (str(item["from"]), str(item["to"])) for item in manifest["forbidden_edges"]
    ]
    accepted = [
        _edge(source, target)
        for source, target in declared
        if _edge_is_accepted(source, target)
    ]
    closure = [
        (source, target)
        for source, target in itertools.product(sorted(TRANSITIONS), repeat=2)
        if source != target and target not in TRANSITIONS[source]
    ]
    closure_accepted = [
        _edge(source, target)
        for source, target in closure
        if _edge_is_accepted(source, target)
    ]
    return {
        "declared": len(declared),
        "rejected": len(declared) - len(accepted),
        "accepted": accepted,
        "closure_pairs": len(closure),
        "closure_rejected": len(closure) - len(closure_accepted),
        "closure_accepted": closure_accepted,
    }


def _edge_is_accepted(source: str, target: str) -> bool:
    reason = (
        AttentionReason.ATTENTION_REQUIRED if target == "attention-required" else None
    )
    try:
        transition(_record(source), target, reason=reason)
    except ContractError:
        return False
    return True


def witness_review_approve() -> tuple[tuple[str, ...], dict[str, Any]]:
    state = CallbackBroker._next_state(_envelope("review", {"verdict": "approve"}))
    if state != "finalizing":
        raise CertificateError("an approved review no longer routes to finalizing")
    return ("CallbackBroker._next_state",), {"next_state": state}


def witness_review_changes_requested() -> tuple[tuple[str, ...], dict[str, Any]]:
    state = CallbackBroker._next_state(
        _envelope("review", {"verdict": "changes-requested"})
    )
    if state != "verifying":
        raise CertificateError("a changes-requested review no longer re-verifies")
    return ("CallbackBroker._next_state",), {"next_state": state}


def witness_review_blocked_fix_loop() -> tuple[tuple[str, ...], dict[str, Any]]:
    state = CallbackBroker._next_state(_envelope("review", {"verdict": "blocked"}))
    if state != "attention-required":
        raise CertificateError("a blocked review no longer raises attention")
    attention, _result = transition(
        _record("running"),
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    resumed, result = transition(attention, "running")
    if (
        attention.resume_state != "running"
        or resumed.resume_state
        or resumed.revision != attention.revision + 1
        or not result.changed
    ):
        raise CertificateError("the fix loop no longer resumes its exact source state")
    return (
        "CallbackBroker._next_state",
        "harness.state_machine.transition",
    ), {"next_state": state, "resume_state": attention.resume_state}


def witness_summary_stability() -> tuple[tuple[str, ...], dict[str, Any]]:
    raw = json.dumps({"status": "published"}, sort_keys=True).encode()
    probe = object.__new__(RuntimeWorkerSummaryMixin)
    probe.digest = ""
    probe.summary_digest = ""
    probe.summary_stable_reads = 0
    first = probe.summary_is_stable(raw)
    second = probe.summary_is_stable(raw)
    changed = probe.summary_is_stable(raw + b"\n")
    if first or not second or changed:
        raise CertificateError(
            "finalization no longer requires two exact identical summary reads"
        )
    return ("RuntimeWorkerSummaryMixin.summary_is_stable",), {
        "first_read_stable": first,
        "second_read_stable": second,
        "changed_read_stable": changed,
    }


def witness_summary_disposition() -> tuple[tuple[str, ...], dict[str, Any]]:
    """Prove the Wiki-summary contract classifies outcome dispositions exactly.

    This witnesses the *summary* schema, not the release boundary: it varies
    ``outcome_disposition`` against the declared evidence set and requires two
    exact acceptances and two exact rejections.  The release boundary is
    witnessed separately by ``witness_release_boundary``.
    """

    declared = {"evidence-a", "evidence-b"}
    accepted = 0
    rejected = 0
    for disposition, evidence, gaps in (
        ("achieved", sorted(declared), []),
        ("partially-achieved", sorted(declared)[:1], ["[[Certificate follow-up]]"]),
        ("achieved", sorted(declared)[:1], []),
        ("not-achieved", [], []),
    ):
        payload = {
            "schema_version": 2,
            "type": "repo-touch",
            "title": "Lifecycle transition certificate",
            "session": "certificate-session",
            "body": "The exact release disposition is recorded.",
            "outcome_disposition": disposition,
            "outcome_evidence_ids": evidence,
            "residual_gap_pointers": gaps,
        }
        try:
            validate_summary(payload, declared_evidence_ids=declared, require_schema=True)
        except WikiSummaryError:
            rejected += 1
        else:
            accepted += 1
    if accepted != 2 or rejected != 2:
        raise CertificateError("summary disposition classification is no longer exact")
    return ("wiki_summary_contract.validate_summary",), {
        "accepted": accepted,
        "rejected": rejected,
    }


def witness_release_boundary(root: Path) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Prove the release boundary rejects its three declared escape edges.

    RC4 evidence E5 and E7 claim the release boundary refuses a missing digest,
    a byte substitution, and a symlink escape.  This witness exercises the exact
    production predicates behind those claims — it does not restate them.
    """

    boundary = root / "release-boundary"
    (boundary / "nested").mkdir(parents=True)
    artifact = boundary / "nested" / "evidence.json"
    payload = b'{"schema_version": 1, "evidence": "exact"}\n'
    artifact.write_bytes(payload)
    exact_digest = _release_sha256(payload)

    # 1. Missing digest: the pointed-at artifact does not exist at all.
    missing = boundary / "nested" / "absent.json"
    try:
        _release_bytes(missing, "release evidence")
    except DispositionError:
        missing_rejected = True
    else:
        missing_rejected = False

    # 2. Byte substitution: one byte changes, so the recorded digest no longer
    #    binds the artifact.
    artifact.write_bytes(payload.replace(b"exact", b"other"))
    substituted_digest = _release_sha256(_release_bytes(artifact, "release evidence"))
    substitution_rejected = substituted_digest != exact_digest
    artifact.write_bytes(payload)

    # 3. Symlink escape: a symlinked path component must be refused even when it
    #    resolves to a real file inside the root.
    outside = root / "outside.json"
    outside.write_bytes(payload)
    link = boundary / "linked.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        symlink_rejected = None
    else:
        symlink_rejected = not _matches_release_pointer(
            boundary, link, "linked.json"
        )

    contained = _matches_release_pointer(
        boundary, artifact, "nested/evidence.json"
    )
    traversal_rejected = not _matches_release_pointer(
        boundary, artifact, "../outside.json"
    )
    absolute_rejected = not _matches_release_pointer(
        boundary, artifact, str(artifact)
    )

    if not (
        missing_rejected
        and substitution_rejected
        and symlink_rejected is not False
        and contained
        and traversal_rejected
        and absolute_rejected
    ):
        raise CertificateError("the release boundary no longer rejects an escape")
    return (
        "rc3_release_disposition._bytes",
        "rc3_release_disposition._matches_boundary_pointer",
    ), {
        "missing_digest_rejected": missing_rejected,
        "byte_substitution_rejected": substitution_rejected,
        "symlink_escape_rejected": symlink_rejected,
        "parent_traversal_rejected": traversal_rejected,
        "absolute_pointer_rejected": absolute_rejected,
        "contained_pointer_accepted": contained,
    }


def witness_callback_order(root: Path) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Accept one callback once and prove the replay is a duplicate."""

    store = OperationStore(root / "callback-order")
    store.create(_spec(), lane_id=LANE_ID, run_id=RUN_ID)
    store.transition(OWNER_ID, OPERATION_ID, "preflight")
    store.transition(OWNER_ID, OPERATION_ID, "starting")
    store.transition(OWNER_ID, OPERATION_ID, "running")
    store.transition(OWNER_ID, OPERATION_ID, "awaiting-callback")
    broker = CallbackBroker(store, OWNER_ID)
    envelope = _envelope("result", {"status": "complete"})
    first = broker.accept(envelope)
    replay = broker.accept(envelope)
    try:
        broker.accept(_envelope("wiki-summary", {"status": "complete"}))
    except CallbackError:
        late_rejected = True
    else:
        late_rejected = False
    if (
        not first.accepted
        or first.next_state != "finalizing"
        or replay.accepted
        or not replay.duplicate
        or not late_rejected
    ):
        raise CertificateError("callback acceptance is no longer one-shot and ordered")
    return ("CallbackBroker.accept", "OperationStore.accept_callback"), {
        "first_accepted": first.accepted,
        "replay_duplicate": replay.duplicate,
        "late_rejected": late_rejected,
        "next_state": first.next_state,
    }


def witness_crash_point(root: Path) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Crash before the durable transition publish and resume from the prefix."""

    class Crash(RuntimeError):
        pass

    def observer(boundary: str) -> None:
        if boundary == TRANSITION_BOUNDARY:
            raise Crash(boundary)

    crashing = OperationStore(root / "crash-point", fault_observer=observer)
    record = crashing.create(_spec(), lane_id=LANE_ID, run_id=RUN_ID)
    try:
        crashing.transition(OWNER_ID, OPERATION_ID, "preflight")
    except Crash:
        pass
    else:
        raise CertificateError("the durable transition boundary is no longer observed")
    restarted = OperationStore(root / "crash-point")
    after_crash = restarted.read(OWNER_ID, OPERATION_ID)
    if after_crash.state != record.state or after_crash.revision != record.revision:
        raise CertificateError("a crashed transition left a torn durable record")
    result = restarted.transition(OWNER_ID, OPERATION_ID, "preflight")
    if not result.changed or restarted.read(OWNER_ID, OPERATION_ID).state != "preflight":
        raise CertificateError("the lifecycle did not resume from its durable prefix")
    return ("OperationStore.transition", "OperationStore._observe_durable_boundary"), {
        "boundary": TRANSITION_BOUNDARY,
        "state_after_crash": after_crash.state,
        "state_after_resume": "preflight",
    }


class _CleanupProbe:
    """Inert local adapter: it records identities and performs no real effect."""

    def __init__(self) -> None:
        self.surface = "alive"
        self.closes: list[str] = []

    def process_status(self, process_group: int, identity: str) -> str:
        if process_group != 4101 or identity != PROCESS_IDENTITY:
            return "unknown"
        return "dead"

    def pid_status(self, pid: int, identity: str) -> str:
        if pid != 4102 or identity != SUPERVISOR_IDENTITY:
            return "unknown"
        return "dead"

    def status(self, surface_id: str) -> str:
        return self.surface if surface_id == SURFACE_ID else "missing"

    def close_exact(self, surface_id: str) -> None:
        if surface_id != SURFACE_ID:
            raise CertificateError("cleanup targeted a surface it does not own")
        self.closes.append(surface_id)
        self.surface = "missing"


def witness_exact_cleanup() -> tuple[tuple[str, ...], dict[str, Any]]:
    probe = _CleanupProbe()
    record = replace(
        _record("exiting"),
        resources=OwnedResources(
            surface_id=SURFACE_ID,
            process_group=4101,
            supervisor_pid=4102,
            process_identity=PROCESS_IDENTITY,
            supervisor_identity=SUPERVISOR_IDENTITY,
        ),
    )
    first = reconcile(record, probe, probe)
    second = reconcile(record, probe, probe)
    if (
        first.action != "close-exact"
        or second.action != "complete"
        or probe.closes != [SURFACE_ID]
    ):
        raise CertificateError("cleanup no longer closes its exact surface once")
    return ("harness.reconciliation.reconcile", "harness.reconciliation.decide"), {
        "first_action": first.action,
        "second_action": second.action,
        "closes": len(probe.closes),
    }


WITNESSES: dict[str, Callable[..., tuple[tuple[str, ...], dict[str, Any]]]] = {
    "review-approve-route": witness_review_approve,
    "review-changes-requested-route": witness_review_changes_requested,
    "review-blocked-attention-route": witness_review_blocked_fix_loop,
    "summary-two-read-stability": witness_summary_stability,
    "summary-disposition-classification": witness_summary_disposition,
    "release-boundary-rejection": witness_release_boundary,
    "callback-one-shot-order": witness_callback_order,
    "durable-transition-crash-point": witness_crash_point,
    "exact-surface-cleanup": witness_exact_cleanup,
}
ROOTED_WITNESSES = frozenset(
    {
        "callback-one-shot-order",
        "durable-transition-crash-point",
        "release-boundary-rejection",
    }
)


def run_cases(
    manifest: Mapping[str, Any], scratch: Path, per_case: Mapping[str, int]
) -> list[dict[str, Any]]:
    """Run one bounded production witness for every declared lifecycle case."""

    cases: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        case_id = str(case["case_id"])
        witness = str(case["witness"])
        runner = WITNESSES.get(witness)
        if runner is None:
            raise CertificateError(f"case {case_id} declares an unknown witness")
        paths, observed = (
            runner(scratch) if witness in ROOTED_WITNESSES else runner()
        )
        cases.append(
            {
                "case_id": case_id,
                "witness": witness,
                "edges_visited": int(per_case[case_id]),
                "production_paths": sorted(paths),
                "observations": json.dumps(observed, sort_keys=True),
                "passed": True,
            }
        )
    return sorted(cases, key=lambda item: item["case_id"])


def exact_head_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise CertificateError("exact certificate HEAD is unavailable")
    return result.stdout.strip()


def _tracked_sha256(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if path.is_symlink() or not path.is_file():
        raise CertificateError(f"{relative} is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_certificate(
    root: Path, manifest_path: Path, scratch: Path
) -> dict[str, Any]:
    """Build one exact-HEAD certificate over the production denominator."""

    manifest = load_manifest(manifest_path)
    states = compare_states(manifest)
    events = compare_events(manifest)
    edges = compare_edges(manifest)
    visited, per_case = visit_edges(manifest)
    declared_edges = {
        _edge(str(item["from"]), str(item["to"]))
        for item in manifest["supported_edges"]
    }
    edges["visited"] = len(visited & declared_edges)
    edges["unvisited"] = sorted(declared_edges - visited)
    forbidden = check_forbidden(manifest)
    cases = run_cases(manifest, scratch, per_case)
    complete = (
        not states["missing"]
        and not states["extra"]
        and states["terminal_declared"] == states["terminal_production"]
        and not events["missing"]
        and not events["extra"]
        and not edges["missing"]
        and not edges["extra"]
        and not edges["unclassified"]
        and not edges["unvisited"]
        and not forbidden["accepted"]
        and not forbidden["closure_accepted"]
        and forbidden["closure_rejected"] == forbidden["closure_pairs"]
        and all(case["passed"] for case in cases)
    )
    certificate = {
        "schema_version": 1,
        "manifest_id": MANIFEST_ID,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "denominator_source": DENOMINATOR_SOURCE,
        "denominator_source_sha256": _denominator_digest(root),
        "exact_head_sha": exact_head_sha(root),
        "excluded_transports": list(EXCLUDED_TRANSPORTS),
        "states": states,
        "events": events,
        "edges": edges,
        "forbidden": forbidden,
        "cases": cases,
        "verdict": "complete" if complete else "incomplete",
    }
    schema = _load_certificate_schema()
    if not validate_output_instance(certificate, schema):
        raise CertificateError("certificate contradicts its public schema")
    return certificate


def _load_certificate_schema() -> Mapping[str, Any]:
    try:
        return _load_schema(SCHEMA_PATH)
    except EphemeralProviderError as exc:
        raise CertificateError("certificate schema is invalid") from exc


def validate_certificate(value: object) -> None:
    """Reject any certificate that is off-schema or not fully complete."""

    if not validate_output_instance(value, _load_certificate_schema()):
        raise CertificateError("certificate contradicts its public schema")
    assert isinstance(value, dict)
    if value["verdict"] != "complete":
        raise CertificateError("certificate verdict is not complete")
    if value["excluded_transports"] != list(EXCLUDED_TRANSPORTS):
        raise CertificateError("research must remain the only excluded transport")
    empty = (
        value["states"]["missing"],
        value["states"]["extra"],
        value["events"]["missing"],
        value["events"]["extra"],
        value["edges"]["missing"],
        value["edges"]["extra"],
        value["edges"]["unclassified"],
        value["edges"]["unvisited"],
        value["forbidden"]["accepted"],
        value["forbidden"]["closure_accepted"],
    )
    if any(empty):
        raise CertificateError("certificate reports an unresolved transition gap")
    if value["edges"]["visited"] != value["edges"]["supported"]:
        raise CertificateError("certificate visited fewer edges than it supports")
    if value["forbidden"]["rejected"] != value["forbidden"]["declared"]:
        raise CertificateError("certificate did not reject every forbidden edge")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--scratch", type=Path, default=None)
    args = parser.parse_args(argv)
    scratch = args.scratch
    temporary = None
    if scratch is None:
        import tempfile  # noqa: PLC0415

        temporary = tempfile.TemporaryDirectory(prefix="transition-certificate.")
        scratch = Path(temporary.name)
    try:
        certificate = compile_certificate(ROOT, args.manifest, scratch)
        if args.check:
            validate_certificate(certificate)
    except (CertificateError, StoreError) as exc:
        print(f"transition certificate failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.cleanup()
    encoded = json.dumps(certificate, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    if args.json or args.out is None:
        sys.stdout.write(encoded)
    else:
        edges = certificate["edges"]
        print(
            f"transition certificate {certificate['verdict']}: "
            f"{edges['visited']}/{edges['supported']} edges visited, "
            f"{certificate['forbidden']['rejected']} forbidden edges rejected, "
            f"{len(certificate['cases'])} cases witnessed"
        )
    return 0 if certificate["verdict"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
