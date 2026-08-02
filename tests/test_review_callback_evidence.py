#!/usr/bin/env python3
"""Regression tests for review-callback validity evidence and prompt/validator agreement.

RT04. Two repository-owned defects are guarded here.

C1 — the callback transport signal documented in docs/pipeline-observability.md
("Accepted / rejected callbacks") lost its only producer when 04cb7ed deleted the legacy
skills/review-dispatch/scripts/spawn_review.py, while scripts/pipeline-stats.py kept
consuming the counters. The pre-existing coverage in tests/test_pipeline_events.py
synthesizes the counter it then asserts on, so it passes with zero producers in the
tree and did not detect the deletion. `producer_exists` is therefore asserted against
the tree, and the report arithmetic is asserted for a NON-ZERO invalid count, which no
other test does.

C2 — scripts/harness/review_submit.py::_round_result validates reviewer output by exact
field-set equality, but the harness review-gate prompt built by
scripts/task-review-runner.py names no field, so a reviewer cannot know the shape it
must produce. This is asserted as a round trip: the schema the generated prompt actually
communicates must be the schema its own submitter accepts. Keys are not the whole
contract, so every verdict and severity value the prompt advertises is also submitted
and must be accepted.

Review follow-ups also guarded here: the emitter targets an explicit vault_root, because
a current-checkout review has no worktree metadata for origin_vault to resolve; and it
counts exactly once per (round identity, outcome), because a callback file is re-read on
every coordinator poll and is never consumed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import CallbackEnvelope  # noqa: E402
from harness.review_submit import (  # noqa: E402
    FINDING_FIELDS as SUBMIT_FINDING_FIELDS,
    ROUND_FIELDS as SUBMIT_ROUND_FIELDS,
    ReviewSubmitError,
    submit_review,
)
from harness.workflows.review import ReviewContext  # noqa: E402
from review_contract import SEVERITIES, VERDICTS  # noqa: E402

# Imported from the code that enforces them, so the assertion cannot rot.
ROUND_FIELDS = frozenset(SUBMIT_ROUND_FIELDS)
FINDING_FIELDS = frozenset(SUBMIT_FINDING_FIELDS)
HEAD_SHA = "d" * 40
PROFILE_SHA = "a" * 64
CANONICAL = {
    "schema_version": 1,
    "axis": "correctness",
    "verdict": "approve",
    "verification_iteration": 1,
    "findings": [],
}
FINDING = {
    "finding_id": "f-1",
    "severity": "minor",
    "file": "scripts/pipeline-stats.py",
    "line": 419,
    "summary": "s",
    "evidence": "e",
    "recommendation": "r",
}
ADVERTISED_STRING_FIELDS = {
    "axis": "round",
    "verdict": "round",
    "finding_id": "finding",
    "severity": "finding",
    "file": "finding",
    "summary": "finding",
    "evidence": "finding",
    "recommendation": "finding",
}
NON_STRING_JSON_VALUES = {
    "numeric": 7,
    "bool": True,
    "list": ["text"],
    "object": {"value": "text"},
}


def _meta(worktree: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "transport": "review-round",
        "worktree": str(worktree),
        "operation_id": "op-review",
        "run_id": "run-review",
        "axis": "correctness",
        "verification_iteration": 1,
        "parent_session_operation_id": "op-parent",
        "verification_profile": {"name": "scoped", "sha256": PROFILE_SHA},
    }


class Fail(SystemExit):
    pass


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise Fail(f"FAIL {label}{': ' + detail if detail else ''}")
    print(f"OK   {label}")


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "task_review_runner_evidence", ROOT / "scripts/task-review-runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RecordingPort:
    def __init__(self) -> None:
        self.published: CallbackEnvelope | None = None

    def publish(self, envelope: CallbackEnvelope) -> None:
        self.published = envelope


# ---------------------------------------------------------------------------
# C1 - the documented callback-validity signal has a producer
# ---------------------------------------------------------------------------
def check_producer_exists() -> None:
    """A consumed, documented counter must be emitted by something in the tree."""

    counters = ("accepted_callbacks", "rejected_callbacks")
    producers: dict[str, list[str]] = {name: [] for name in counters}
    consumers: dict[str, list[str]] = {name: [] for name in counters}
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(ROOT))
        for name in counters:
            if name not in text:
                continue
            # A consumer reads counters through event_count(); anything else that
            # names the counter is emitting it.
            if f'event_count(event, "{name}")' in text or f"event_count(e, \"{name}\")" in text:
                consumers[name].append(relative)
            else:
                producers[name].append(relative)

    doc = (ROOT / "docs/pipeline-observability.md").read_text(encoding="utf-8")
    check(
        "callback transport signal is still documented",
        "Accepted / rejected callbacks" in doc,
    )
    for name in counters:
        check(
            f"{name} is consumed by the report",
            bool(consumers[name]),
            f"no consumer found for {name}",
        )
        check(
            f"{name} has at least one producer in the tree",
            bool(producers[name]),
            f"{name} is consumed by {consumers[name]} and documented, "
            f"but nothing emits it; a rejected review callback leaves no "
            f"durable content-free evidence",
        )


def check_report_counts_invalid_callbacks() -> None:
    """The report arithmetic must survive a non-zero invalid count."""

    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "pipeline_events_evidence", ROOT / "scripts/pipeline_events.py"
    )
    events = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(events)

    with tempfile.TemporaryDirectory(prefix="rt04-report.") as raw:
        root = Path(raw)
        actor = "review"
        events.emit_event(
            "review-callback",
            actor=actor,
            counts={"accepted_callbacks": 1, "iteration": 1, "duration_ms": 1000},
            root=root,
            environ={},
        )
        events.emit_event(
            "review-callback",
            actor=actor,
            counts={"rejected_callbacks": 1, "iteration": 1, "duration_ms": 1000},
            status="error",
            root=root,
            environ={},
        )
        (root / "scripts").mkdir()
        for filename in ("pipeline-stats.py", "pipeline_stats_model.py", "pipeline_stats_sources.py", "pipeline_stats_render.py", "pipeline_stats_report.py", "review_contract.py"):
            shutil.copy2(ROOT / "scripts" / filename, root / "scripts" / filename)
        env = {**os.environ, "HOME": str(root / "home")}
        result = subprocess.run(
            [sys.executable, str(root / "scripts/pipeline-stats.py"), "--days", "1"],
            text=True, capture_output=True, env=env,
        )
        check("report exit 0 with an invalid callback", result.returncode == 0, result.stderr)
        check(
            "report counts the rejected callback",
            "| Rejected review callbacks | 1 |" in result.stdout,
            "rejected callbacks were not reported",
        )
        check(
            "report counts the accepted callback",
            "| Accepted review callbacks | 1 |" in result.stdout,
        )
        check(
            "acceptance rate reflects the rejection",
            "| Callback acceptance rate | 50.0% |" in result.stdout,
            "a rejected callback must move the rate off 100%",
        )


# ---------------------------------------------------------------------------
# C2 - the harness review-gate prompt states the schema its submitter enforces
# ---------------------------------------------------------------------------
def _generate_gate_prompt(runner, tmp: Path) -> str:
    vault = tmp / "vault"
    worktree = tmp / "worktree"
    runtime_root = tmp / "runtime"
    for path in (vault, worktree, runtime_root):
        path.mkdir(parents=True, exist_ok=True)
    context = ReviewContext("packet.json", HEAD_SHA, "scoped", PROFILE_SHA)
    pointer = runner._prompt(
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context=context,
        axis="correctness",
        verification=False,
    )
    return (runtime_root / pointer).read_text(encoding="utf-8")


def check_gate_prompt_states_its_schema() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="rt04-prompt.") as raw:
        prompt = _generate_gate_prompt(runner, Path(raw))

    named = set(re.findall(r"`([a-z_]+)`", prompt))
    missing_round = sorted(ROUND_FIELDS - named)
    missing_finding = sorted(FINDING_FIELDS - named)
    check(
        "gate prompt names every enforced round field",
        not missing_round,
        f"prompt does not name {missing_round}; review_submit._round_result "
        f"rejects any object whose key set is not exactly {sorted(ROUND_FIELDS)}",
    )
    check(
        "gate prompt names every enforced finding field",
        not missing_finding,
        f"prompt does not name {missing_finding}; review_submit._round_result "
        f"rejects any finding whose key set is not exactly {sorted(FINDING_FIELDS)}",
    )


def check_prompt_schema_round_trips(worktree: Path) -> None:
    """What the prompt communicates must be what the submitter accepts."""

    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="rt04-roundtrip.") as raw:
        prompt = _generate_gate_prompt(runner, Path(raw))

    named = set(re.findall(r"`([a-z_]+)`", prompt))
    round_keys = named & ROUND_FIELDS
    finding_keys = named & FINDING_FIELDS
    check(
        "prompt communicates a round schema at all",
        round_keys == ROUND_FIELDS and finding_keys == FINDING_FIELDS,
        f"prompt communicates round={sorted(round_keys)} "
        f"finding={sorted(finding_keys)}; a reviewer cannot construct an "
        f"exactly-equal key set from this prompt",
    )

    sample = dict(CANONICAL)
    port = RecordingPort()
    try:
        submit_review(
            json.dumps({key: sample[key] for key in round_keys}),
            meta=_meta(worktree),
            worktree=worktree,
            port=port,
        )
    except (ReviewSubmitError, ValueError) as exc:
        raise Fail(
            "FAIL prompt schema is accepted by its own submitter: "
            f"a round built from exactly the prompt's fields was rejected: {exc}"
        ) from exc
    check(
        "prompt schema is accepted by its own submitter",
        port.published is not None,
    )


def check_gate_prompt_states_enforced_values() -> None:
    """Key names alone are not the contract: values are enforced too."""

    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="rt04-values.") as raw:
        prompt = _generate_gate_prompt(runner, Path(raw))

    named = set(re.findall(r"`([A-Za-z0-9_-]+)`", prompt))
    for label, vocabulary in (("verdict", VERDICTS), ("severity", SEVERITIES)):
        missing = sorted(vocabulary - named)
        check(
            f"gate prompt names every enforced {label} value",
            not missing,
            f"prompt does not name {missing}; a reviewer satisfying the key set "
            f"can still be rejected on {label}",
        )
    check(
        "gate prompt states the approve/material-finding rule",
        "approve" in prompt and "critical" in prompt and "important" in prompt,
    )
    check("gate prompt states the line rule", "`line`" in prompt)


def check_every_named_value_is_accepted(worktree: Path) -> None:
    """Each value the prompt advertises must survive its own submitter."""

    for verdict in sorted(VERDICTS):
        severities = (
            sorted(SEVERITIES - {"critical", "important"})
            if verdict == "approve"
            else sorted(SEVERITIES)
        )
        for severity in severities:
            finding = dict(FINDING, severity=severity)
            value = dict(CANONICAL, verdict=verdict, findings=[finding])
            port = RecordingPort()
            try:
                submit_review(
                    json.dumps(value), meta=_meta(worktree), worktree=worktree, port=port
                )
            except (ReviewSubmitError, ValueError) as exc:
                raise Fail(
                    f"FAIL advertised verdict/severity pair is accepted: "
                    f"{verdict}/{severity} rejected: {exc}"
                ) from exc
            check(
                f"advertised pair accepted: verdict={verdict} severity={severity}",
                port.published is not None,
            )


def check_advertised_string_fields_reject_non_strings(worktree: Path) -> None:
    """JSON containers and scalars must not be stringified into valid evidence."""

    accepted: list[str] = []
    for field, scope in ADVERTISED_STRING_FIELDS.items():
        for type_name, invalid in NON_STRING_JSON_VALUES.items():
            finding = dict(FINDING)
            value = dict(CANONICAL, findings=[finding])
            if scope == "round":
                value[field] = invalid
            else:
                finding[field] = invalid
            port = RecordingPort()
            try:
                submit_review(
                    json.dumps(value),
                    meta=_meta(worktree),
                    worktree=worktree,
                    port=port,
                )
            except (ReviewSubmitError, ValueError):
                continue
            accepted.append(f"{field}={type_name}")

    check(
        "every advertised string field rejects every non-string JSON type",
        not accepted,
        "submitter coerced raw non-strings instead of rejecting them: "
        + ", ".join(accepted),
    )


def check_emit_targets_an_explicit_vault() -> None:
    """A current-checkout review has no worktree metadata to resolve a vault."""

    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="rt04-vault.") as raw:
        tmp = Path(raw)
        # No .task-meta.json anywhere: exactly the current-checkout condition
        # under which origin_vault() returns None.
        worktree = tmp / "checkout"
        vault = tmp / "vault"
        runtime_root = tmp / "runtime"
        for path in (worktree, vault, runtime_root):
            path.mkdir(parents=True, exist_ok=True)
        round_ = _stub_round()
        runner._emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-callback",
            terminal_status="rejected",
        )
        log = vault / ".vault-meta" / "pipeline-events.jsonl"
        check(
            "current-checkout rejection reaches the vault log",
            log.is_file(),
            "no telemetry was written without worktree metadata",
        )
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        check(
            "rejection is recorded as one rejected callback",
            len(rows) == 1
            and rows[0].get("op") == "review-callback"
            and (rows[0].get("counts") or {}).get("rejected_callbacks") == 1,
            json.dumps(rows),
        )

        # Idempotence: re-polling the same round must not count it twice.
        runner._emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-callback",
            terminal_status="rejected",
        )
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        check(
            "re-polling the same round does not double-count",
            len(rows) == 1,
            f"{len(rows)} events for one round outcome",
        )

        # A distinct outcome for the same round is still recorded once.
        runner._emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-callback",
            terminal_status="accepted",
        )
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        check(
            "a distinct outcome for the same round is recorded",
            len(rows) == 2,
            f"{len(rows)} events after a valid outcome",
        )


def _stub_round():
    from harness.contracts import OperationSpec, RuntimeRoute
    from harness.workflows.review import ReviewRound

    route = RuntimeRoute("claude", "fable", "xhigh", "reviewer-readonly", "a" * 64)
    spec = OperationSpec(
        "op-round", "key-round", "review-round", "owner-1", route, "packet.json", "full"
    )
    return ReviewRound(
        parent_operation_id="op-parent",
        operation_id="op-round",
        owner_id="owner-1",
        lane_id="lane-1",
        run_id="run-1",
        axis="correctness",
        verification_iteration=1,
        spec=spec,
    )


def run() -> None:
    check_producer_exists()
    check_report_counts_invalid_callbacks()
    check_gate_prompt_states_its_schema()
    check_gate_prompt_states_enforced_values()
    check_prompt_schema_round_trips(ROOT)
    check_every_named_value_is_accepted(ROOT)
    check_advertised_string_fields_reject_non_strings(ROOT)
    check_emit_targets_an_explicit_vault()


if __name__ == "__main__":
    run()
    print("review callback evidence regressions passed")
