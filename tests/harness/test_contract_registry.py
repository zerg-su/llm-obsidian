#!/usr/bin/env python3
"""Five-family model contract registry and canonical template fault table."""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    CanonicalContractTemplate,
    ContractAuditEntry,
    ContractDisposition,
    ContractBoundaryInventoryEntry,
    ContractError,
    ContractFamily,
    ModelContract,
    contract_boundary_inventory,
    contract_registry,
    contract_registry_audit,
    validate_contract_boundary_classification,
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
try:
    validate_contract_boundary_classification(
        inventory
        + (
            ContractBoundaryInventoryEntry(
                "unclassified-production-boundary",
                "scripts/harness/unclassified.py",
            ),
        ),
        audit,
    )
except ContractError:
    pass
else:
    raise AssertionError("an unclassified production seam passed the audit")
check("an unclassified production seam fails the registry gate", True)
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
    "registered target mismatch": lambda: CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="attempt-1",
        target_pointer="artifact.json",
        value=summary,
        code_owned_fields={"schema_version", "type", "session"},
        model_owned_fields=set(summary) - {"schema_version", "type", "session"},
    ),
    "non-JSON template value": lambda: CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="attempt-1",
        target_pointer=".task-summary.json",
        value={**summary, "body": {"not-json"}},
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

for label, action in {
    "overlapping registry ownership": lambda: ModelContract(
        ContractFamily.TASK_SUMMARY,
        ".task-summary.json",
        frozenset({"body"}),
        frozenset({"body"}),
        frozenset(),
        1,
        "validator",
    ),
    "registry target traversal": lambda: ModelContract(
        ContractFamily.TASK_SUMMARY,
        "../task-summary.json",
        frozenset({"body"}),
        frozenset(),
        frozenset(),
        1,
        "validator",
    ),
    "invalid audit identity": lambda: ContractAuditEntry(
        "", ContractDisposition.COVERED
    ),
    "invalid inventory owner": lambda: ContractBoundaryInventoryEntry(
        "task-summary", ""
    ),
}.items():
    try:
        action()
    except ContractError:
        check(f"registry boundary rejects {label}", True)
    else:
        check(f"registry boundary rejects {label}", False)

for label, declaration in {
    "syntax-invalid declaration": "MODEL_JSON_BOUNDARIES = (\n",
    "non-literal declaration": "MODEL_JSON_BOUNDARIES = tuple(['task-summary'])\n",
    "empty declaration": "MODEL_JSON_BOUNDARIES = ()\n",
    "duplicate declarations": (
        "MODEL_JSON_BOUNDARIES = ('task-summary',)\n"
        "SECOND = 1\n"
    ),
}.items():
    with tempfile.TemporaryDirectory(prefix="contract-inventory.") as raw:
        fake = Path(raw)
        scripts = fake / "scripts"
        scripts.mkdir()
        (scripts / "one.py").write_text(declaration, encoding="utf-8")
        if label == "duplicate declarations":
            (scripts / "two.py").write_text(
                "MODEL_JSON_BOUNDARIES = ('task-summary',)\n",
                encoding="utf-8",
            )
        try:
            contract_boundary_inventory(fake)
        except ContractError:
            check(f"inventory rejects {label}", True)
        else:
            check(f"inventory rejects {label}", False)

print("contract registry fault table: ok")
