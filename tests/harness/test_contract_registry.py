#!/usr/bin/env python3
"""Five-family model contract registry and canonical template fault table."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    CanonicalContractTemplate,
    ContractDisposition,
    ContractError,
    ContractFamily,
    contract_boundary_inventory,
    contract_registry,
    contract_registry_audit,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


registry = contract_registry()
check(
    "registry contains exactly the five approved model-owned families",
    set(registry) == set(ContractFamily),
    registry,
)
check(
    "registered correction ceilings preserve current behavior",
    registry[ContractFamily.TASK_SUMMARY].same_session_corrections == 1
    and registry[ContractFamily.PIPELINE_STEP_RESULT].same_session_corrections == 1
    and registry[ContractFamily.REVIEW_INPUT].same_session_corrections == 2
    and registry[ContractFamily.REVIEW_RESOLUTION].same_session_corrections == 2
    and registry[ContractFamily.VERIFICATION_ESCALATION].same_session_corrections == 1,
)
check(
    "registry values are immutable policy with no runtime or provider port",
    all(dataclasses.is_dataclass(value) for value in registry.values())
    and all(value.__dataclass_params__.frozen for value in registry.values())
    and all(
        not hasattr(value, name)
        for value in registry.values()
        for name in ("provider", "runtime", "store", "process", "cmux")
    ),
)

audit = contract_registry_audit()
inventory = contract_boundary_inventory()
by_disposition = {
    disposition: {item.name for item in audit if item.disposition == disposition}
    for disposition in ContractDisposition
}
check(
    "audit covers all supported families",
    by_disposition[ContractDisposition.COVERED]
    == {family.value for family in ContractFamily},
    by_disposition,
)
check(
    "audit exactly classifies the independent model boundary inventory",
    {item.name for item in audit} == {item.name for item in inventory}
    and all(item.owner for item in inventory),
    inventory,
)
check(
    "audit records deferred and test-only model boundaries",
    {
        "protected-research",
        "daily-summary",
        "custom-pipeline-authoring",
    }
    <= by_disposition[ContractDisposition.DEFERRED]
    and {"live-dispatch-ack"}
    <= by_disposition[ContractDisposition.TEST_ONLY],
    by_disposition,
)
check(
    "audit forbids every code-owned authority named by RC5",
    {
        "operation-store",
        "callbacks",
        "receipts",
        "task-metadata",
        "verification-authority",
        "finalization-ledger",
        "reap-markers",
        "archive-authority",
        "permissions",
        "dependencies",
        "external-state",
    }
    <= by_disposition[ContractDisposition.FORBIDDEN],
    by_disposition,
)

summary = {
    "schema_version": 2,
    "type": "repo-touch",
    "title": "<title>",
    "session": "session-1",
    "body": "<body>",
    "outcome_disposition": "partially-achieved",
    "outcome_evidence_ids": [],
    "residual_gap_pointers": ["wiki/plans/approved.md"],
}
template = CanonicalContractTemplate.create(
    ContractFamily.TASK_SUMMARY,
    attempt_id="task-summary-session-1",
    target_pointer=".task-summary.json",
    value=summary,
    code_owned_fields={"schema_version", "type", "session"},
    model_owned_fields={
        "title",
        "body",
        "outcome_disposition",
        "outcome_evidence_ids",
        "residual_gap_pointers",
    },
)
sidecar = template.as_dict()
check(
    "canonical template binds family attempt target ownership and exact bytes",
    sidecar["family"] == "task-summary"
    and sidecar["attempt_id"] == "task-summary-session-1"
    and sidecar["target_pointer"] == ".task-summary.json"
    and sidecar["template"] == summary
    and len(sidecar["template_sha256"]) == 64
    and template.matches(sidecar),
    sidecar,
)
mutated = json.loads(json.dumps(sidecar))
mutated["template"]["session"] = "stale-session"
check("template digest is mutation-sensitive", not template.matches(mutated))

faults = {
    "unknown family": lambda: CanonicalContractTemplate.create(
        "unknown",
        attempt_id="attempt-1",
        target_pointer="artifact.json",
        value={"body": ""},
        code_owned_fields=set(),
        model_owned_fields={"body"},
    ),
    "path traversal": lambda: CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="attempt-1",
        target_pointer="../artifact.json",
        value=summary,
        code_owned_fields={"schema_version", "type", "session"},
        model_owned_fields=set(summary) - {"schema_version", "type", "session"},
    ),
    "overlapping ownership": lambda: CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="attempt-1",
        target_pointer=".task-summary.json",
        value=summary,
        code_owned_fields={"schema_version", "type", "session", "body"},
        model_owned_fields=set(summary) - {"schema_version", "type", "session"},
    ),
    "unregistered ownership": lambda: CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="attempt-1",
        target_pointer=".task-summary.json",
        value={**summary, "provider": "codex"},
        code_owned_fields={"schema_version", "type", "session", "provider"},
        model_owned_fields=set(summary) - {"schema_version", "type", "session"},
    ),
}
for label, action in faults.items():
    try:
        action()
    except ContractError:
        check(f"fault table rejects {label}", True)
    else:
        check(f"fault table rejects {label}", False)

print("contract registry fault table: ok")
