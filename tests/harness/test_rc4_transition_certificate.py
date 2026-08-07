#!/usr/bin/env python3
"""RC4 transition denominator: manifest, certificate, and mutation sensitivity.

The suite proves four things that a restated expectation cannot: the manifest
matches the production transition table exactly, every supported edge is
actually consumed by the production reducer, every forbidden edge is rejected
(curated list plus closure over the state product), and a coordinated edit of
both the config and its scenarios still fails because the denominator is read
from production rather than from the manifest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.callbacks import CallbackBroker, CallbackError  # noqa: E402
from harness.contracts import (  # noqa: E402
    AttentionReason,
    CallbackEnvelope,
    ContractError,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
    to_dict,
)
from harness.state_machine import TERMINAL, TRANSITIONS, transition  # noqa: E402
from harness.store import OperationStore  # noqa: E402

from lifecycle_simulator import LifecycleWorld, SimulatedCrash  # noqa: E402
from lifecycle_simulator_oracle import (  # noqa: E402
    InvariantViolation,
    assert_snapshot,
)
from lifecycle_transition_certificate import (  # noqa: E402
    EDGE_CLASSES,
    EVENT_GROUPS,
    MANIFEST_PATH,
    CertificateError,
    compile_certificate,
    load_manifest,
    production_edges,
    production_events,
    validate_certificate,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def expect_error(label: str, action) -> CertificateError:
    try:
        action()
    except CertificateError as exc:
        print(f"OK   {label}")
        return exc
    raise AssertionError(f"{label}: mutation unexpectedly passed")


def certificate_for(
    manifest: dict[str, object], scratch: Path, name: str
) -> dict[str, object]:
    path = scratch / f"{name}.json"
    path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return compile_certificate(ROOT, path, scratch / name)


def rc4_envelope(kind: str, payload: dict[str, object]) -> CallbackEnvelope:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return CallbackEnvelope(
        f"rc4-{kind}",
        "rc4-operation",
        "rc4-run",
        kind,
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )


def record(state: str) -> OperationRecord:
    return OperationRecord(
        OperationSpec(
            operation_id="rc4-operation",
            idempotency_key="rc4-idempotency",
            kind="dispatch",
            owner_id="rc4-owner",
            route=RuntimeRoute("codex", "rc4-model", "high", "dispatch", "c" * 64),
            context_manifest="context/manifest.json",
            verification_profile="scoped",
        ),
        state,
        0,
        "rc4-lane",
        "rc4-run",
    )


# --- vocabulary and manifest -------------------------------------------------

MANIFEST = load_manifest(MANIFEST_PATH)
check(
    "manifest declares the exact production state vocabulary",
    {str(item["state"]) for item in MANIFEST["states"]} == set(TRANSITIONS)
    and {
        str(item["state"]) for item in MANIFEST["states"] if item["role"] == "terminal"
    }
    == set(TERMINAL),
)
check(
    "manifest declares the exact production edge denominator",
    {
        f"{item['from']}->{item['to']}" for item in MANIFEST["supported_edges"]
    }
    == set(production_edges()),
)
check(
    "every supported edge carries one closed-vocabulary classification",
    all(item["edge_class"] in EDGE_CLASSES for item in MANIFEST["supported_edges"]),
)
PRODUCTION_EVENTS = production_events()
check(
    "manifest declares the exact production entry-point event vocabulary",
    all(
        set(MANIFEST["events"][group]) == set(PRODUCTION_EVENTS[group])
        for group in EVENT_GROUPS
    ),
)
check(
    "research is the only explicitly excluded callback transport",
    MANIFEST["excluded_transports"] == ["research"]
    and "research" in MANIFEST["events"]["callback_kinds"],
)
check(
    "every declared forbidden edge is absent from the production table",
    all(
        item["to"] not in TRANSITIONS.get(str(item["from"]), set())
        for item in MANIFEST["forbidden_edges"]
    ),
)
check(
    "the eight named lifecycle cases are declared exactly once each",
    sorted(str(case["case_id"]) for case in MANIFEST["cases"])
    == [
        "approval",
        "callback-order",
        "changes-requested",
        "cleanup",
        "crash-point",
        "finalization",
        "fix-loop",
        "release-disposition",
    ],
)


# --- certificate over the exact working tree ---------------------------------

with tempfile.TemporaryDirectory(prefix="rc4-certificate.") as raw:
    scratch = Path(raw)
    CERTIFICATE = compile_certificate(ROOT, MANIFEST_PATH, scratch / "base")
    validate_certificate(CERTIFICATE)
    check(
        "certificate reports zero missing, extra, unclassified, or unvisited edges",
        CERTIFICATE["verdict"] == "complete"
        and CERTIFICATE["edges"]["visited"] == CERTIFICATE["edges"]["supported"]
        and CERTIFICATE["edges"]["supported"] == len(production_edges())
        and not CERTIFICATE["edges"]["missing"]
        and not CERTIFICATE["edges"]["extra"]
        and not CERTIFICATE["edges"]["unclassified"]
        and not CERTIFICATE["edges"]["unvisited"],
        CERTIFICATE["edges"],
    )
    check(
        "certificate rejects the curated forbidden list and its whole closure",
        not CERTIFICATE["forbidden"]["accepted"]
        and not CERTIFICATE["forbidden"]["closure_accepted"]
        and CERTIFICATE["forbidden"]["rejected"]
        == CERTIFICATE["forbidden"]["declared"]
        and CERTIFICATE["forbidden"]["closure_rejected"]
        == CERTIFICATE["forbidden"]["closure_pairs"],
        CERTIFICATE["forbidden"],
    )
    check(
        "certificate binds the exact head and the production denominator bytes",
        len(str(CERTIFICATE["exact_head_sha"])) == 40
        and CERTIFICATE["denominator_source"] == "scripts/harness/state_machine.py"
        and len(str(CERTIFICATE["denominator_source_sha256"])) == 64,
    )
    check(
        "every named case carries a passing bounded production witness",
        len(CERTIFICATE["cases"]) == 8
        and all(
            case["passed"] and case["production_paths"] and case["edges_visited"]
            for case in CERTIFICATE["cases"]
        ),
        CERTIFICATE["cases"],
    )

    # --- schema and verdict enforcement -------------------------------------

    off_schema = {**CERTIFICATE, "unexpected_field": True}
    expect_error(
        "off-schema certificate is rejected",
        lambda: validate_certificate(off_schema),
    )
    incomplete = copy.deepcopy(CERTIFICATE)
    incomplete["verdict"] = "incomplete"
    expect_error(
        "an incomplete verdict is rejected",
        lambda: validate_certificate(incomplete),
    )
    hidden_gap = copy.deepcopy(CERTIFICATE)
    hidden_gap["edges"]["unvisited"] = ["running->verifying"]
    expect_error(
        "a reported gap under a complete verdict is rejected",
        lambda: validate_certificate(hidden_gap),
    )
    wrong_transport = copy.deepcopy(CERTIFICATE)
    wrong_transport["excluded_transports"] = []
    expect_error(
        "an empty excluded-transport claim is rejected",
        lambda: validate_certificate(wrong_transport),
    )

    # --- manifest mutation sensitivity --------------------------------------

    dropped = copy.deepcopy(MANIFEST)
    dropped["supported_edges"] = [
        item
        for item in dropped["supported_edges"]
        if (item["from"], item["to"]) != ("verifying", "running")
    ]
    mutated = certificate_for(dropped, scratch, "dropped-edge")
    check(
        "removing one declared edge is reported as a missing production edge",
        mutated["verdict"] == "incomplete"
        and mutated["edges"]["missing"] == ["verifying->running"],
        mutated["edges"],
    )

    invented = copy.deepcopy(MANIFEST)
    invented["supported_edges"].append(
        {"from": "complete", "to": "running", "edge_class": "recovery"}
    )
    mutated = certificate_for(invented, scratch, "invented-edge")
    check(
        "inventing an unsupported edge is reported as extra",
        mutated["verdict"] == "incomplete"
        and mutated["edges"]["extra"] == ["complete->running"],
        mutated["edges"],
    )

    unvisited = copy.deepcopy(MANIFEST)
    for case in unvisited["cases"]:
        if case["case_id"] == "changes-requested":
            case["paths"] = [
                path for path in case["paths"] if path[-2:] != ["verifying", "running"]
            ]
    mutated = certificate_for(unvisited, scratch, "unvisited-edge")
    check(
        "removing the only covering scenario is reported as an unvisited edge",
        mutated["verdict"] == "incomplete"
        and mutated["edges"]["unvisited"] == ["verifying->running"],
        mutated["edges"],
    )

    coordinated = copy.deepcopy(unvisited)
    coordinated["supported_edges"] = [
        item
        for item in coordinated["supported_edges"]
        if (item["from"], item["to"]) != ("verifying", "running")
    ]
    mutated = certificate_for(coordinated, scratch, "coordinated-edit")
    check(
        "editing config and scenarios together still fails against production",
        mutated["verdict"] == "incomplete"
        and mutated["edges"]["missing"] == ["verifying->running"]
        and not mutated["edges"]["unvisited"],
        mutated["edges"],
    )

    forbidden_drift = copy.deepcopy(MANIFEST)
    forbidden_drift["forbidden_edges"].append(
        {
            "from": "created",
            "to": "preflight",
            "reason": "a legal admission edge misdeclared as forbidden",
        }
    )
    mutated = certificate_for(forbidden_drift, scratch, "forbidden-drift")
    check(
        "declaring a legal edge forbidden is reported as an accepted transition",
        mutated["verdict"] == "incomplete"
        and mutated["forbidden"]["accepted"] == ["created->preflight"]
        and mutated["forbidden"]["rejected"] < mutated["forbidden"]["declared"],
        mutated["forbidden"],
    )

    dropped_event = copy.deepcopy(MANIFEST)
    dropped_event["events"]["review_verdicts"] = [
        item for item in dropped_event["events"]["review_verdicts"] if item != "blocked"
    ]
    mutated = certificate_for(dropped_event, scratch, "dropped-event")
    check(
        "removing a production event is reported as a missing event",
        mutated["verdict"] == "incomplete"
        and mutated["events"]["missing"] == ["review_verdicts.blocked"],
        mutated["events"],
    )

    invented_event = copy.deepcopy(MANIFEST)
    invented_event["events"]["callback_kinds"] = sorted(
        [*invented_event["events"]["callback_kinds"], "invented-kind"]
    )
    mutated = certificate_for(invented_event, scratch, "invented-event")
    check(
        "inventing an event is reported as extra",
        mutated["verdict"] == "incomplete"
        and mutated["events"]["extra"] == ["callback_kinds.invented-kind"],
        mutated["events"],
    )

    unclassified = copy.deepcopy(MANIFEST)
    unclassified["supported_edges"][0]["edge_class"] = "invented"
    mutated = certificate_for(unclassified, scratch, "unclassified-edge")
    first_edge = MANIFEST["supported_edges"][0]
    check(
        "an unknown classification is reported as an unclassified edge",
        mutated["verdict"] == "incomplete"
        and mutated["edges"]["unclassified"]
        == [f"{first_edge['from']}->{first_edge['to']}"],
        mutated["edges"],
    )

    for name, mutate in (
        (
            "extra excluded transport",
            lambda value: value["excluded_transports"].append("review"),
        ),
        (
            "unknown case witness",
            lambda value: value["cases"][0].update({"witness": "invented-witness"}),
        ),
        (
            "repeated supported edge",
            lambda value: value["supported_edges"].append(
                dict(value["supported_edges"][0])
            ),
        ),
        (
            "drifted denominator source",
            lambda value: value.update({"denominator_source": "scripts/harness/cli.py"}),
        ),
    ):
        broken = copy.deepcopy(MANIFEST)
        mutate(broken)
        expect_error(
            f"manifest with {name} is refused",
            lambda value=broken, key=name: certificate_for(
                value, scratch, key.replace(" ", "-")
            ),
        )


# --- release rejection over the production reducer ---------------------------

rejected = 0
for item in MANIFEST["forbidden_edges"]:
    source, target = str(item["from"]), str(item["to"])
    reason = (
        AttentionReason.ATTENTION_REQUIRED if target == "attention-required" else None
    )
    try:
        transition(record(source), target, reason=reason)
    except ContractError:
        rejected += 1
    else:
        raise AssertionError(f"forbidden release edge accepted: {source}->{target}")
check(
    "every declared forbidden edge is rejected by the production reducer",
    rejected == len(MANIFEST["forbidden_edges"]) == 16,
    rejected,
)
for source in sorted(TERMINAL):
    for target in sorted(TRANSITIONS):
        if source == target:
            continue
        try:
            transition(record(source), target, reason=None)
        except ContractError:
            continue
        raise AssertionError(f"terminal release state was resurrected: {source}->{target}")
check("no terminal release state accepts any outgoing transition", True)


# --- callback order over the production broker -------------------------------

with tempfile.TemporaryDirectory(prefix="rc4-callback-order.") as raw:
    store = OperationStore(Path(raw) / "store")
    spec = OperationSpec(
        operation_id="rc4-operation",
        idempotency_key="rc4-idempotency",
        kind="dispatch",
        owner_id="rc4-owner",
        route=RuntimeRoute("codex", "rc4-model", "high", "dispatch", "c" * 64),
        context_manifest="context/manifest.json",
        verification_profile="scoped",
    )
    store.create(spec, lane_id="rc4-lane", run_id="rc4-run")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("rc4-owner", "rc4-operation", state)
    broker = CallbackBroker(store, "rc4-owner")
    envelope = rc4_envelope("result", {"status": "complete"})
    first = broker.accept(envelope)
    replay = broker.accept(envelope)
    try:
        broker.accept(rc4_envelope("wiki-summary", {"status": "complete"}))
    except CallbackError:
        distinct_rejected = True
    else:
        distinct_rejected = False
    check(
        "one accepted callback wins, its replay is a duplicate, a rival is refused",
        first.accepted
        and first.next_state == "finalizing"
        and replay.duplicate
        and not replay.accepted
        and distinct_rejected,
        {"first": first, "replay": replay, "distinct_rejected": distinct_rejected},
    )
    for verdict, expected in (
        ("approve", "finalizing"),
        ("changes-requested", "verifying"),
        ("blocked", "attention-required"),
    ):
        routed = CallbackBroker._next_state(
            rc4_envelope("review", {"verdict": verdict})
        )
        if routed != expected:
            raise AssertionError(f"review verdict {verdict} routed to {routed}")
    check("every production review verdict routes to its exact next state", True)


# --- crash point and cleanup over the deterministic world --------------------

with tempfile.TemporaryDirectory(prefix="rc4-crash-point.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply(
        {
            "action": "crash-at",
            "failpoint": "operation-transition-published",
            "phase": "before",
        }
    )
    try:
        world.apply({"action": "start-worker"})
    except SimulatedCrash:
        crashed = True
    else:
        crashed = False
    durable = world.durable_digest()
    restarted = LifecycleWorld.restart(Path(raw))
    check(
        "a crash before the durable publish leaves an untorn causal prefix",
        crashed
        and restarted.record().state == "created"
        and restarted.durable_digest() == durable,
        {"crashed": crashed, "state": restarted.record().state},
    )
    restarted.apply({"action": "start-worker"})
    restarted.apply({"action": "publish-provider-event", "kind": "result-published"})
    restarted.apply({"action": "publish-callback", "kind": "result"})
    restarted.apply({"action": "close"})
    snapshot = restarted.snapshot()
    assert_snapshot(snapshot)
    check(
        "the resumed lifecycle reaches resource-free completion with one close receipt",
        restarted.record().state == "complete"
        and not any(snapshot["operation"]["resources"].values())
        and restarted.resource_close_count() == 1
        and snapshot["effects"][0]["deliveries"] == 1,
        snapshot,
    )
    restarted.assert_no_real_effects()
    restarted.crashes.assert_consumed()
    check(
        "the crash and cleanup case crossed no real external effect boundary",
        restarted.real_effect_counts()
        == {"provider": 0, "model": 0, "cmux": 0, "network": 0},
        restarted.real_effect_counts(),
    )

    leaked = copy.deepcopy(snapshot)
    leaked["operation"]["resources"]["surface_id"] = "leaked-surface"
    try:
        assert_snapshot(leaked)
    except InvariantViolation as exc:
        check(
            "the independent oracle still catches a leaked terminal resource",
            exc.invariant_id == "SIM-INV-RESOURCE-FREE",
            exc,
        )
    else:
        raise AssertionError("leaked terminal resource unexpectedly passed")

    resurrected = restarted.record()
    OperationStore._write(
        restarted.store._operation_path("sim-owner", "sim-operation"),
        to_dict(
            type(resurrected)(
                resurrected.spec,
                "running",
                resurrected.revision + 1,
                resurrected.lane_id,
                resurrected.run_id,
            )
        ),
    )
    restarted._publish_liveness()
    try:
        assert_snapshot(restarted.snapshot())
    except InvariantViolation as exc:
        check(
            "the independent oracle still catches terminal release resurrection",
            exc.invariant_id == "SIM-INV-TERMINAL-MONOTONIC",
            exc,
        )
    else:
        raise AssertionError("terminal resurrection unexpectedly passed")


print("\nAll RC4 transition certificate tests passed.")
