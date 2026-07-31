#!/usr/bin/env python3
"""Regression tests for review-callback validity evidence and prompt/validator agreement.

RT04. Two repository-owned defects are guarded here.

C1 — the callback-validity signal documented in docs/pipeline-observability.md
("Valid / invalid callbacks | Reviewer payloads accepted or rejected by the versioned
JSON contract") lost its only producer when 04cb7ed deleted the legacy
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
communicates must be the schema its own submitter accepts.
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
from harness.review_submit import ReviewSubmitError, submit_review  # noqa: E402
from harness.workflows.review import ReviewContext  # noqa: E402

# The contract enforced by review_submit._round_result.
ROUND_FIELDS = frozenset(
    {"schema_version", "axis", "verdict", "verification_iteration", "findings"}
)
FINDING_FIELDS = frozenset(
    {
        "finding_id",
        "severity",
        "file",
        "line",
        "summary",
        "evidence",
        "recommendation",
    }
)
HEAD_SHA = "d" * 40
PROFILE_SHA = "a" * 64


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

    counters = ("valid_callbacks", "invalid_callbacks")
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
        "callback validity signal is still documented",
        "Valid / invalid callbacks" in doc,
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
        actor = "review:claude:fable:full"
        events.emit_event(
            "review-round",
            actor=actor,
            counts={"valid_callbacks": 1, "iteration": 1, "duration_ms": 1000},
            root=root,
            environ={},
        )
        events.emit_event(
            "review-round",
            actor=actor,
            counts={"invalid_callbacks": 1, "iteration": 1, "duration_ms": 1000},
            status="error",
            root=root,
            environ={},
        )
        (root / "scripts").mkdir()
        shutil.copy2(
            ROOT / "scripts/pipeline-stats.py", root / "scripts/pipeline-stats.py"
        )
        env = dict(os.environ)
        env["HOME"] = str(root / "home")
        result = subprocess.run(
            [sys.executable, str(root / "scripts/pipeline-stats.py"), "--days", "1"],
            text=True,
            capture_output=True,
            env=env,
        )
        check("report exit 0 with an invalid callback", result.returncode == 0, result.stderr)
        check(
            "report counts the invalid callback",
            "| Invalid review callbacks | 1 |" in result.stdout,
            "invalid callbacks were not reported",
        )
        check(
            "report counts the valid callback",
            "| Valid review callbacks | 1 |" in result.stdout,
        )
        check(
            "schema-valid rate reflects the rejection",
            "| Callback schema-valid rate | 50.0% |" in result.stdout,
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

    sample = {
        "schema_version": 1,
        "axis": "correctness",
        "verdict": "approve",
        "verification_iteration": 1,
        "findings": [],
    }
    meta = {
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
    port = RecordingPort()
    try:
        submit_review(
            json.dumps({key: sample[key] for key in round_keys}),
            meta=meta,
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


def run() -> None:
    check_producer_exists()
    check_report_counts_invalid_callbacks()
    check_gate_prompt_states_its_schema()
    check_prompt_schema_round_trips(ROOT)


if __name__ == "__main__":
    run()
    print("review callback evidence regressions passed")
