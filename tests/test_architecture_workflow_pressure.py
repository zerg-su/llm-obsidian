#!/usr/bin/env python3
"""Validate and drive the frozen Architecture Workflow v1 pressure corpus."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/acceptance/evidence/architecture-workflow-v1"
PRESSURE = EVIDENCE / "pressure"
SUBJECT_PATHS = ("skills", "scripts", ".claude", "config", "docs/skill-references")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LETTERS = tuple("abcdefghijklmnopqrs")
FIXTURE_RECEIPT_SHA256 = {
    "q": "920f0468d9a96b3ee4b3ddb8606db694e50e487fd42bf046f0f9825385662cec",
    "r": "b24706cf6cf0855c190fc7eed83d56919b6d0bbdd7c44c3199f73c6360fb0f78",
    "s": "a32f6caeee488acbb1a14dd4b1b4e2770d8c37c1b13c48afe1ff37a949cc1f4f",
}

runner_spec = importlib.util.spec_from_file_location(
    "architecture_pressure_runner", ROOT / "scripts/architecture_workflow_pressure.py"
)
runner = importlib.util.module_from_spec(runner_spec)
assert runner_spec.loader is not None
runner_spec.loader.exec_module(runner)

audit_spec = importlib.util.spec_from_file_location(
    "architecture_workflow_audit",
    ROOT / "scripts/architecture_workflow_audit.py",
)
audit = importlib.util.module_from_spec(audit_spec)
assert audit_spec.loader is not None
audit_spec.loader.exec_module(audit)


def pressure_case(
    letter: str,
    capability: str,
    expected_carrier: str,
    user_prompt: str,
    setup: str,
    expected: tuple[tuple[str, object], ...],
    choices: dict[str, list[object]],
    effect_mode: str = "read-only",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": f"architecture-workflow.{letter}",
        "capability": capability,
        "input": {
            "user_prompt": user_prompt,
            "setup": setup,
        },
        "response_contract": choices,
        "assertions": [
            {"kind": "equals", "path": f"artifacts.{field}", "value": value}
            for field, value in expected
        ],
        "pressure": {
            "case_id": letter,
            "expected_carrier": expected_carrier,
            "effect_mode": effect_mode,
            "fixture_vault": None if effect_mode == "read-only" else f"fixture-{letter}",
            "assertions": [
                f"{field} = {json.dumps(value, ensure_ascii=False)}"
                for field, value in expected
            ],
        },
    }


CASES = {
    "a": pressure_case(
        "a", "architecture", "architecture",
        "Спроектируй архитектуру нового проекта Atlas.",
        "No Atlas project space exists. Use only the supplied contracts and keep this turn read-only.",
        (("carrier", "architecture"), ("project_resolution", "clarify-or-propose"), ("writes_authorized", False)),
        {"carrier": ["architecture", "design", "decompose"], "project_resolution": ["clarify-or-propose", "guess-by-recency", "create-immediately"]},
    ),
    "b": pressure_case(
        "b", "architecture", "design",
        "Разберём recovery model Atlas и сравним варианты ownership state.",
        "Accepted Atlas architecture context exists; the request is one bounded design concern. Select the carrier and state how its returned artifact must be handled; this pressure run does not execute the carrier.",
        (("carrier", "design"), ("handoff", "bounded-concern"), ("returned_artifact", "collect-into-project-context"), ("architecture_decides_alternatives", False)),
        {"carrier": ["architecture", "design", "research"], "handoff": ["bounded-concern", "whole-system-map", "implementation"], "returned_artifact": ["collect-into-project-context", "discard", "promote-to-accepted"]},
    ),
    "c": pressure_case(
        "c", "architecture", "research",
        "Исследуй внешние гарантии idempotency у выбранного API для Atlas.",
        "The gap is an external current-fact question; do not perform outbound research in this pressure run. Select the carrier and state how its returned artifact must be handled.",
        (("carrier", "research"), ("uncertainty", "external-evidence"), ("returned_artifact", "collect-into-project-context"), ("architecture_claims_fact", False)),
        {"carrier": ["architecture", "research", "prototype"], "uncertainty": ["external-evidence", "empirical", "resolved"], "returned_artifact": ["collect-into-project-context", "discard", "promote-to-accepted"]},
    ),
    "d": pressure_case(
        "d", "architecture", "prototype",
        "Проверь прототипом, переживает ли этот lock crash/restart.",
        "The uncertainty is falsifiable and empirical. This read-only pressure run does not execute the carrier; state how its returned artifact must be handled.",
        (("carrier", "prototype"), ("uncertainty", "empirical"), ("returned_artifact", "collect-into-project-context"), ("production_change", False)),
        {"carrier": ["architecture", "research", "prototype"], "uncertainty": ["external-evidence", "empirical", "resolved"], "returned_artifact": ["collect-into-project-context", "discard", "promote-to-accepted"]},
    ),
    "e": pressure_case(
        "e", "decompose", "decompose",
        "Архитектура Atlas принята; разбей доставку на Work Items.",
        "Accepted durable synthetic architecture, spec, and contract authority is available.",
        (("carrier", "decompose"), ("phase", "MAP"), ("persistence", False)),
        {"carrier": ["architecture", "decompose", "implementation-plan"], "phase": ["MAP", "ACCEPT", "MATERIALIZE"]},
    ),
    "f": pressure_case(
        "f", "decompose", "decompose",
        "Принимаю эту декомпозицию.",
        "A valid MAP draft is present in this disposable fixture. Semantic acceptance is requested, not persistence.",
        (("carrier", "decompose"), ("phase", "ACCEPT"), ("writes", False), ("addresses_allocated", False)),
        {"carrier": ["decompose", "save", "implementation-plan"], "phase": ["MAP", "ACCEPT", "MATERIALIZE"]},
        "zero-effect",
    ),
    "g": pressure_case(
        "g", "decompose", "decompose",
        "Сохрани принятую декомпозицию Atlas в вики.",
        "Separate persistence authorization is explicit. The disposable fixture contains the real writer at scripts/vault-write.py and a valid one-transaction payload at materialize.json. Exercise that exact local transaction, with no effect outside the fixture.",
        (("carrier", "decompose"), ("phase", "MATERIALIZE"), ("transaction", "single-bounded"), ("writer_effect", True)),
        {"carrier": ["decompose", "save-plan", "implementation-plan"], "phase": ["MAP", "ACCEPT", "MATERIALIZE"], "transaction": ["single-bounded", "page-by-page", "none"]},
        "fixture-mutation",
    ),
    "h": pressure_case(
        "h", "implementation-plan", "implementation-plan",
        "Составь implementation plan для [[Atlas WI-001 — Recovery]].",
        "Exactly one accepted, current, valid durable Work Item and its authoritative upstream graph are supplied.",
        (("carrier", "implementation-plan"), ("accepted_input", True), ("outcome_count", 1)),
        {"carrier": ["decompose", "implementation-plan", "split"], "outcome_count": [1, 2, 3]},
    ),
    "i": pressure_case(
        "i", "implementation-plan", "decompose",
        "Составь один implementation plan сразу для WI-001, WI-002 и WI-003.",
        "The three accepted Work Items are independent delivery outcomes.",
        (("carrier", "decompose"), ("oversized_rejected", True), ("mega_plan", False)),
        {"carrier": ["decompose", "implementation-plan", "split"]},
    ),
    "j": pressure_case(
        "j", "implementation-plan", "implementation-plan",
        "Планируй WI-004, хотя ownership recovery всё ещё не решён.",
        "The accepted Work Item exposes an unresolved architecture concern that blocks downstream planning.",
        (("carrier", "implementation-plan"), ("disposition", "upstream-gap"), ("downstream_resolution", False), ("gap_fields", "source,reason,affected,owner-action")),
        {"carrier": ["architecture", "decompose", "implementation-plan"], "disposition": ["upstream-gap", "plan-anyway", "resolve-downstream"], "gap_fields": ["source,reason,affected,owner-action", "reason-only", "none"]},
    ),
    "k": pressure_case(
        "k", "implementation-plan", "implementation-plan",
        "Architecture revision changed from 2 to 3; continue WI-005.",
        "WI-005 pins revision 2 while authoritative accepted architecture is revision 3.",
        (("carrier", "implementation-plan"), ("freshness", "needs-review"), ("unrelated_may_continue", True), ("dependent_planning_blocked", True)),
        {"carrier": ["architecture", "decompose", "implementation-plan"], "freshness": ["current", "needs-review", "stale"]},
    ),
    "l": pressure_case(
        "l", "decompose", "decompose",
        "WI-006 и WI-007 независимы; зафиксируй planning frontier.",
        "Both Work Items are valid and have no depends_on edge or real concurrency clash.",
        (("carrier", "decompose"), ("planning_frontier", "WI-006,WI-007"), ("stored_parallel_relation", False)),
        {"carrier": ["decompose", "implementation-plan", "split"], "planning_frontier": ["WI-006,WI-007", "WI-006", "stored-parallel-safe"]},
    ),
    "m": pressure_case(
        "m", "decompose", "decompose",
        "Прими декомпозицию, в которой обязательный Atlas Spec — Cancel Safety не покрыт.",
        "One required accepted spec is absent from coverage, deferred intent, and Explicitly Out of Scope in this disposable fixture.",
        (("carrier", "decompose"), ("phase", "ACCEPT-rejected"), ("uncovered_intent_named", True), ("writes", False)),
        {"carrier": ["decompose", "implementation-plan", "save"], "phase": ["ACCEPT", "ACCEPT-rejected", "MATERIALIZE"]},
        "zero-effect",
    ),
    "n": pressure_case(
        "n", "decompose", "decompose",
        "Добавь WI-099 без связи с vision, spec, contract или decision.",
        "The otherwise valid MAP draft is in a disposable fixture, but WI-099 has no authoritative upstream trace.",
        (("carrier", "decompose"), ("phase", "MAP-rejected"), ("orphan_rejected", True), ("writes", False)),
        {"carrier": ["decompose", "implementation-plan", "save"], "phase": ["MAP", "MAP-rejected", "ACCEPT"]},
        "zero-effect",
    ),
    "o": pressure_case(
        "o", "decompose", "split",
        "План уже утверждён; разбей его на параллельные задачи исполнения.",
        "One approved implementation plan already exists; the request is execution and file-ownership fan-out.",
        (("carrier", "split"), ("frontier", "execution"), ("decompose_reentry", False)),
        {"carrier": ["decompose", "implementation-plan", "split"], "frontier": ["design", "planning", "execution"]},
    ),
    "p": pressure_case(
        "p", "implementation-plan", "implementation-plan",
        "Продолжи декомпозицию и спланируй WI после сбоя записи.",
        "The disposable fixture has a real vault-write recovery journal and a partial/inconsistent Work projection. Refuse partial-state consumption and downstream planning first. The exact next local writer action is python3 scripts/vault-write.py --recover; exercise it only as recovery, then report the roll-forward outcome.",
        (("carrier", "implementation-plan"), ("pre_recovery", "upstream-gap"), ("partial_consumed", False), ("recovery", "rolled-forward")),
        {"carrier": ["architecture", "decompose", "implementation-plan"], "pre_recovery": ["upstream-gap", "plan-anyway", "consume-partial"], "recovery": ["rolled-forward", "rolled-back", "ignored"]},
        "fixture-mutation",
    ),
    "q": pressure_case(
        "q", "decompose", "decompose",
        "Прими и сохрани этот Work DAG.",
        "Inspect dag-cases.json and run python3 verify-fixture.py. It presents concrete dangling, self, duplicate, and cyclic negative subcases plus one valid DAG with WI-001 before WI-002. Evaluate before ACCEPT/MATERIALIZE; do not persist, and return the emitted receipt_sha256.",
        (("carrier", "decompose"), ("invalid_dag", "rejected"), ("positive_order", "WI-001,WI-002"), ("writes", False), ("fixture_receipt_sha256", FIXTURE_RECEIPT_SHA256["q"])),
        {"carrier": ["decompose", "implementation-plan", "split"], "invalid_dag": ["rejected", "accepted", "repaired-silently"], "positive_order": ["WI-001,WI-002", "WI-002,WI-001", "unordered"]},
        "zero-effect",
    ),
    "r": pressure_case(
        "r", "decompose", "decompose",
        "Прими и материализуй Work Graph с этими upstream pins.",
        "Inspect pin-cases.json and run python3 verify-fixture.py. It presents concrete missing, extra, duplicate, mismatched, malformed, and superseded pin mappings plus one total current mapping. Evaluate without persistence and return the emitted receipt_sha256.",
        (("carrier", "decompose"), ("invalid_pins", "rejected"), ("valid_freshness", "current"), ("writes", False), ("fixture_receipt_sha256", FIXTURE_RECEIPT_SHA256["r"])),
        {"carrier": ["decompose", "implementation-plan", "save"], "invalid_pins": ["rejected", "accepted", "repaired-silently"], "valid_freshness": ["current", "needs-review", "stale"]},
        "zero-effect",
    ),
    "s": pressure_case(
        "s", "decompose", "decompose",
        "Прими и сохрани новый Atlas project artifact.",
        "Inspect path-cases.json and run python3 verify-fixture.py, which invokes the fixture copy of scripts/architecture_paths.py. It presents separators, dot segments, traversal, project-root symlink redirects, empty/invalid keys and titles, punctuation, case/normalization/cross-project/title-alias collisions, plus one valid Atlas title. Evaluate before ACCEPT/MATERIALIZE without persistence and return the emitted receipt_sha256.",
        (("carrier", "decompose"), ("invalid_paths", "rejected"), ("containment", "wiki/projects/atlas"), ("writes", False), ("fixture_receipt_sha256", FIXTURE_RECEIPT_SHA256["s"])),
        {"carrier": ["architecture", "decompose", "save"], "invalid_paths": ["rejected", "accepted", "normalized-silently"], "containment": ["wiki/projects/atlas", "wiki/projects", "wiki"]},
        "zero-effect",
    ),
}


def project_page(title: str, role: str, address: str, body: str) -> str:
    return f'''---
type: project
title: "{title}"
artifact_role: {role}
project_key: atlas
project_display_name: Atlas
artifact_revision: 1
upstream: []
upstream_pins: []
depends_on: []
status: accepted
created: 2026-08-17
updated: 2026-08-17
tags: [project, architecture-workflow, pressure-fixture]
sessions: [pressure-fixture]
address: {address}
---

# {title}

{body}
'''


def install_writer(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for source in sorted((ROOT / "scripts").glob("vault_write*.py")):
        shutil.copy2(source, scripts / source.name)
    for name in ("vault-write.py", "vault_schema.py", "pipeline_events.py", "plan_lifecycle.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (root / ".vault-meta").mkdir(exist_ok=True)
    (root / "wiki").mkdir(exist_ok=True)
    return scripts / "vault-write.py"


FIXTURE_CHECKER = r'''#!/usr/bin/env python3
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def dag_result(case):
    nodes = case["nodes"]
    names = set(nodes)
    invalid = any(
        not isinstance(edges, list)
        or len(edges) != len(set(edges))
        or name in edges
        or any(target not in names for target in edges)
        for name, edges in nodes.items()
    )
    remaining = {name: set(edges) for name, edges in nodes.items()}
    order = []
    while not invalid and remaining:
        ready = sorted(name for name, edges in remaining.items() if not edges)
        if not ready:
            invalid = True
            break
        for name in ready:
            order.append(name)
            remaining.pop(name)
        for edges in remaining.values():
            edges.difference_update(ready)
    return {"id": case["id"], "status": "invalid" if invalid else "valid", "order": [] if invalid else order}


def pin_result(case, authorities):
    upstream = case["upstream"]
    parsed = []
    malformed = False
    for pin in case["pins"]:
        match = re.fullmatch(r"(.+)@([1-9][0-9]*)", pin)
        if match is None:
            malformed = True
        else:
            parsed.append((match.group(1), int(match.group(2))))
    names = [name for name, _ in parsed]
    invalid = (
        malformed
        or len(upstream) != len(set(upstream))
        or len(names) != len(set(names))
        or set(names) != set(upstream)
    )
    statuses = case.get("authority_status", {})
    for name, revision in parsed:
        authority = authorities.get(name)
        invalid = invalid or authority is None
        if authority is not None:
            invalid = invalid or statuses.get(name, authority["status"]) == "superseded"
            invalid = invalid or revision != authority["revision"]
    return {"id": case["id"], "status": "invalid" if invalid else "current"}


def path_result(case):
    sys.path.insert(0, str(ROOT / "scripts"))
    from architecture_paths import ArchitecturePathError, artifact_destination
    kwargs = dict(case["arguments"])
    current = case.get("current_path")
    if current is not None:
        kwargs["current_path"] = ROOT / current
    try:
        destination = artifact_destination(ROOT / "wiki", **kwargs)
    except ArchitecturePathError:
        return {"id": case["id"], "status": "rejected"}
    return {
        "id": case["id"], "status": "accepted",
        "path": destination.relative_to(ROOT).as_posix(),
    }


if (ROOT / "dag-cases.json").is_file():
    source = json.loads((ROOT / "dag-cases.json").read_text(encoding="utf-8"))
    case_id = "q"
    results = [dag_result(case) for case in source["cases"]]
elif (ROOT / "pin-cases.json").is_file():
    source = json.loads((ROOT / "pin-cases.json").read_text(encoding="utf-8"))
    case_id = "r"
    authorities = {row["title"]: row for row in source["authorities"]}
    results = [pin_result(case, authorities) for case in source["cases"]]
elif (ROOT / "path-cases.json").is_file():
    source = json.loads((ROOT / "path-cases.json").read_text(encoding="utf-8"))
    case_id = "s"
    links = (
        (ROOT / "wiki/projects/redirect-in-vault", "other"),
        (ROOT / "wiki/projects/redirect-outside", "../../outside-project"),
    )
    try:
        for path, target in links:
            path.symlink_to(target, target_is_directory=True)
        results = [path_result(case) for case in source["cases"]]
    finally:
        for path, _ in links:
            path.unlink(missing_ok=True)
else:
    raise SystemExit("no recognized fixture case data")
receipt = {"schema_version": 1, "case_id": case_id, "results": results}
encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
print(json.dumps({"receipt": receipt, "receipt_sha256": hashlib.sha256(encoded).hexdigest()}, ensure_ascii=False, sort_keys=True))
'''


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def preflight_negative_fixture(letter: str, root: Path) -> None:
    before = runner.directory_state_manifest(root)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, str(root / "verify-fixture.py")], cwd=root, env=env,
        text=True, capture_output=True, check=True,
    )
    receipt = json.loads(completed.stdout)
    if receipt.get("receipt_sha256") != FIXTURE_RECEIPT_SHA256[letter]:
        raise ValueError("negative fixture receipt changed")
    if runner.directory_state_manifest(root) != before:
        raise ValueError("negative fixture preflight changed measured state")


def install_negative_fixture(letter: str, root: Path) -> None:
    (root / "verify-fixture.py").write_text(FIXTURE_CHECKER, encoding="utf-8")
    if letter == "q":
        write_json(
            root / "dag-cases.json",
            {"cases": [
                {"id": "dangling", "nodes": {"WI-001": ["WI-404"]}},
                {"id": "self", "nodes": {"WI-001": ["WI-001"]}},
                {"id": "duplicate", "nodes": {"WI-001": [], "WI-002": ["WI-001", "WI-001"]}},
                {"id": "cycle", "nodes": {"WI-001": ["WI-002"], "WI-002": ["WI-001"]}},
                {"id": "valid", "nodes": {"WI-001": [], "WI-002": ["WI-001"]}},
            ]},
        )
        return
    if letter == "r":
        architecture = "Atlas Architecture"
        spec = "Atlas Spec — Cancel Safety"
        write_json(
            root / "pin-cases.json",
            {
                "authorities": [
                    {"title": architecture, "revision": 3, "status": "accepted"},
                    {"title": spec, "revision": 2, "status": "accepted"},
                ],
                "cases": [
                    {"id": "missing", "upstream": [architecture, spec], "pins": [f"{architecture}@3"]},
                    {"id": "extra", "upstream": [architecture], "pins": [f"{architecture}@3", f"{spec}@2"]},
                    {"id": "duplicate", "upstream": [architecture], "pins": [f"{architecture}@3", f"{architecture}@3"]},
                    {"id": "mismatched", "upstream": [architecture], "pins": [f"{spec}@2"]},
                    {"id": "malformed", "upstream": [architecture], "pins": [f"{architecture}@0"]},
                    {"id": "superseded", "upstream": [architecture], "pins": [f"{architecture}@3"], "authority_status": {architecture: "superseded"}},
                    {"id": "valid", "upstream": [architecture, spec], "pins": [f"{architecture}@3", f"{spec}@2"]},
                ],
            },
        )
        return
    scripts = root / "scripts"
    scripts.mkdir()
    for name in ("architecture_paths.py", "vault_schema.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    atlas = root / "wiki/projects/atlas"
    other = root / "wiki/projects/other"
    atlas.mkdir(parents=True)
    other.mkdir(parents=True)
    (root / "outside-project").mkdir()
    (atlas / "Atlas Architecture.md").write_text(
        project_page("Atlas Architecture", "architecture", "c-880010", "Owner."), encoding="utf-8"
    )
    (other / "Atlas Spéc.md").write_text(
        project_page("Atlas Spéc", "spec", "c-880011", "Normalized owner."), encoding="utf-8"
    )
    alias_page = project_page("Other Contract", "contract", "c-880012", "Alias owner.")
    alias_page = alias_page.replace("artifact_role: contract\n", "artifact_role: contract\naliases: [\"Atlas Alias\"]\n")
    (other / "Other Contract.md").write_text(alias_page, encoding="utf-8")
    base = {"project_display_name": "Atlas", "artifact_role": "spec"}
    write_json(
        root / "path-cases.json",
        {"cases": [
            {"id": "empty-key", "arguments": {**base, "project_key": "", "artifact_title": "Atlas Spec"}},
            {"id": "invalid-key", "arguments": {**base, "project_key": "Atlas", "artifact_title": "Atlas Spec"}},
            {"id": "traversal", "arguments": {**base, "project_key": "../other", "artifact_title": "Atlas Spec"}},
            {"id": "project-symlink-in-vault", "arguments": {**base, "project_key": "redirect-in-vault", "artifact_title": "Atlas Redirected Spec"}},
            {"id": "project-symlink-outside", "arguments": {**base, "project_key": "redirect-outside", "artifact_title": "Atlas External Spec"}},
            {"id": "separator", "arguments": {**base, "project_key": "atlas", "artifact_title": "Atlas/Spec"}},
            {"id": "dot-segment", "arguments": {**base, "project_key": "atlas", "artifact_title": "Atlas .. Spec"}},
            {"id": "punctuation", "arguments": {**base, "project_key": "atlas", "artifact_title": "Atlas: Spec"}},
            {"id": "empty-title", "arguments": {**base, "project_key": "atlas", "artifact_title": ""}},
            {"id": "cross-project-current-owner", "arguments": {"project_key": "other", "project_display_name": "Atlas", "artifact_role": "architecture", "artifact_title": "Atlas Architecture"}, "current_path": "wiki/projects/atlas/Atlas Architecture.md"},
            {"id": "normalization-case-collision", "arguments": {"project_key": "other", "project_display_name": "ATLAS", "artifact_role": "spec", "artifact_title": unicodedata.normalize("NFD", "ATLAS SPÉC")}},
            {"id": "title-alias-collision", "arguments": {**base, "project_key": "atlas", "artifact_title": "Atlas Alias"}},
            {"id": "valid", "arguments": {"project_key": "atlas", "project_display_name": "Atlas", "artifact_role": "work-item", "artifact_title": "Atlas WI-002 — Delivery"}},
        ]},
    )


def prepare_fixture(letter: str, root: Path) -> None:
    if letter not in CASES or CASES[letter]["pressure"]["effect_mode"] == "read-only":
        raise ValueError("fixture preparation requires a fixture-owned pressure case")
    subject_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    root = root.resolve()
    temporary_roots = {Path(tempfile.gettempdir()).resolve()}
    for candidate in (Path("/tmp"), Path("/private/tmp")):
        if candidate.is_dir():
            temporary_roots.add(candidate.resolve())
    if (
        root.parent.parent not in temporary_roots
        or not root.parent.name.startswith(f"architecture-workflow-v1-{subject_head}")
        or root.name != letter
    ):
        raise ValueError("fixture root is outside its dedicated temporary parent")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("fixture root must be empty before preparation")
    parent_marker = root.parent / runner.FIXTURE_PARENT_MARKER
    parent_value = {"schema_version": 1, "type": "architecture-pressure-parent", "subject_head": subject_head}
    if parent_marker.exists():
        if json.loads(parent_marker.read_text(encoding="utf-8")) != parent_value:
            raise ValueError("fixture parent marker identity changed")
    else:
        write_json(parent_marker, parent_value)
    write_json(
        root / runner.FIXTURE_MARKER,
        {
            "schema_version": 1, "type": "architecture-pressure-fixture",
            "subject_head": subject_head, "case_id": letter,
            "fixture_id": CASES[letter]["pressure"]["fixture_vault"],
        },
    )
    runner._fixture_root(root, CASES[letter]["pressure"], subject_head)
    (root / "fixture-context.json").write_text(
        json.dumps(CASES[letter]["input"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if letter in {"q", "r", "s"}:
        install_negative_fixture(letter, root)
        preflight_negative_fixture(letter, root)
        return
    if letter not in {"g", "p"}:
        return
    writer = install_writer(root)
    pages = [
        {
            "op": "create",
            "path": "wiki/projects/atlas/work/Atlas Work Graph.md",
            "content": project_page("Atlas Work Graph", "work-graph", "c-880001", "Accepted projection."),
        },
        {
            "op": "create",
            "path": "wiki/projects/atlas/work/Atlas WI-001 — Recovery.md",
            "content": project_page("Atlas WI-001 — Recovery", "work-item", "c-880002", "Recovery outcome."),
        },
    ]
    payload = {
        "schema_version": 1,
        "actor": "architecture-pressure-fixture",
        "session": "pressure-fixture",
        "pages": pages,
    }
    if letter == "g":
        (root / "materialize.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return
    crash_code = r'''
import importlib.util, io, json, os, sys
from pathlib import Path
writer = Path(sys.argv[1])
sys.path.insert(0, str(writer.parent))
spec = importlib.util.spec_from_file_location("pressure_writer_crash", writer)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
original = module.atomic_write
page_writes = 0
def stop(path, text):
    global page_writes
    if path == module.JOURNAL_FILE:
        original(path, text)
        return
    if page_writes == 0:
        original(path, text)
        page_writes += 1
        return
    os._exit(99)
module.atomic_write = stop
sys.stdin = io.StringIO(sys.argv[2])
raise SystemExit(module.main([]))
'''
    crashed = subprocess.run(
        [sys.executable, "-c", crash_code, str(writer), json.dumps(payload, ensure_ascii=False)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    journal = root / ".vault-meta/.vault-write-journal.json"
    if crashed.returncode != 99 or not journal.is_file():
        raise RuntimeError(f"failed to prepare recovery journal: {crashed.stderr}")
    projection = [
        root / "wiki/projects/atlas/work/Atlas Work Graph.md",
        root / "wiki/projects/atlas/work/Atlas WI-001 — Recovery.md",
    ]
    journal_value = json.loads(journal.read_text(encoding="utf-8"))
    journal_paths = {
        entry.get("path") for entry in journal_value.get("entries", [])
        if isinstance(entry, dict)
    }
    expected_paths = {path.relative_to(root).as_posix() for path in projection}
    if journal_paths != expected_paths:
        raise RuntimeError("recovery fixture journal must cover the complete projection")
    if sum(path.is_file() for path in projection) != 1:
        raise RuntimeError("recovery fixture must expose exactly one partial projection page")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_record(letter: str) -> dict[str, Any]:
    path = PRESSURE / f"{letter}.json"
    require(path.is_file() and not path.is_symlink(), f"missing pressure record: {letter}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(letter: str, label: str, value: object, digest: str) -> None:
    require(isinstance(value, list), f"{letter}: {label} manifest missing")
    paths: list[str] = []
    for index, row in enumerate(value):
        require(isinstance(row, dict), f"{letter}: {label} manifest row {index} invalid")
        kind = row.get("kind")
        expected = {"path", "kind"} if kind == "directory" else {"path", "kind", "size", "sha256"}
        require(set(row) == expected and kind in {"directory", "file"}, f"{letter}: {label} manifest row fields changed")
        relative = row["path"]
        require(
            isinstance(relative, str)
            and relative
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts,
            f"{letter}: {label} manifest path invalid",
        )
        paths.append(relative)
        if kind == "file":
            require(type(row["size"]) is int and row["size"] >= 0, f"{letter}: {label} manifest size invalid")
            require(isinstance(row["sha256"], str) and SHA256.fullmatch(row["sha256"]) is not None, f"{letter}: {label} manifest digest invalid")
    require(paths == sorted(paths) and len(paths) == len(set(paths)), f"{letter}: {label} manifest order changed")
    require(runner.manifest_sha256(value) == digest, f"{letter}: {label} manifest hash mismatch")


def validate_recovery_projection(letter: str, pre: object, post: object) -> None:
    if letter != "p":
        return
    projection = {
        "wiki/projects/atlas/work/Atlas Work Graph.md",
        "wiki/projects/atlas/work/Atlas WI-001 — Recovery.md",
    }
    journal = ".vault-meta/.vault-write-journal.json"
    pre_paths = {row["path"] for row in pre if isinstance(row, dict)}
    post_paths = {row["path"] for row in post if isinstance(row, dict)}
    require(len(pre_paths & projection) == 1, "p: pre-state must contain exactly one projection page")
    require(journal in pre_paths, "p: pre-state recovery journal missing")
    require(projection <= post_paths, "p: recovery did not restore the complete projection")
    require(journal not in post_paths, "p: recovery journal was not cleaned up")


def transcript_result(letter: str, transcript_text: str, prompt: str) -> dict[str, Any]:
    prompt_block = f"## Prompt\n\n```text\n{prompt}\n```"
    require(prompt_block in transcript_text, f"{letter}: transcript prompt mismatch")
    marker = "\n## Response\n\n```json\n"
    require(transcript_text.count(marker) == 1, f"{letter}: transcript response boundary changed")
    response = transcript_text.split(marker, 1)[1]
    require("\n```\n" in response, f"{letter}: transcript response is unterminated")
    encoded, trailing = response.split("\n```\n", 1)
    require(not trailing.strip(), f"{letter}: transcript has trailing response content")
    try:
        result = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{letter}: transcript response is invalid JSON") from exc
    require(
        isinstance(result, dict)
        and set(result) == {"output", "artifacts"}
        and isinstance(result["output"], str)
        and result["output"].strip()
        and isinstance(result["artifacts"], dict)
        and set(result["artifacts"]) == set(runner.artifact_fields(CASES[letter])),
        f"{letter}: transcript response shape changed",
    )
    failures = runner.grade_case(CASES[letter], result)
    require(not failures, f"{letter}: transcript response failed grading: {'; '.join(failures)}")
    return result


def validate_record(
    letter: str,
    record: dict[str, Any],
    subject_head: str,
    sessions: set[str],
    coordinator_session_id: str,
    *,
    transcript_rel: str | None = None,
    transcript_bytes: bytes | None = None,
) -> None:
    expected_keys = {
        "schema_version", "case_id", "prompt", "expected_carrier",
        "observed_outcome", "verdict", "subject_head", "effect_mode",
        "fixture_vault", "pre_state_manifest", "post_state_manifest",
        "pre_state_sha256", "post_state_sha256", "execution_provenance", "assertions",
    }
    require(set(record) == expected_keys, f"{letter}: record fields changed")
    case = CASES[letter]
    metadata = case["pressure"]
    require(record["schema_version"] == 1 and record["case_id"] == letter, f"{letter}: identity mismatch")
    require(record["expected_carrier"] == metadata["expected_carrier"], f"{letter}: expected carrier drift")
    require(record["verdict"] == "pass" and isinstance(record["observed_outcome"], str) and record["observed_outcome"].strip(), f"{letter}: non-pass outcome")
    require(record["subject_head"] == subject_head, f"{letter}: cross-subject record")
    require(record["effect_mode"] == metadata["effect_mode"] and record["fixture_vault"] == metadata["fixture_vault"], f"{letter}: effect identity drift")
    require(SHA256.fullmatch(record["pre_state_sha256"]) is not None and SHA256.fullmatch(record["post_state_sha256"]) is not None, f"{letter}: invalid state digest")
    equal = record["pre_state_sha256"] == record["post_state_sha256"]
    require(equal == (record["effect_mode"] != "fixture-mutation"), f"{letter}: effect hash mismatch")
    if record["effect_mode"] == "read-only":
        require(record["pre_state_manifest"] is None and record["post_state_manifest"] is None, f"{letter}: read-only run claimed a fixture manifest")
    else:
        validate_manifest(letter, "pre-state", record["pre_state_manifest"], record["pre_state_sha256"])
        validate_manifest(letter, "post-state", record["post_state_manifest"], record["post_state_sha256"])
        require(
            (record["pre_state_manifest"] == record["post_state_manifest"])
            == (record["effect_mode"] == "zero-effect"),
            f"{letter}: manifest effect mismatch",
        )
        validate_recovery_projection(
            letter,
            record["pre_state_manifest"],
            record["post_state_manifest"],
        )
    require(record["assertions"] == metadata["assertions"], f"{letter}: assertion denominator drift")
    prompt = runner.pressure_prompt_for(case)
    require(record["prompt"] == prompt, f"{letter}: delivered prompt drift")

    provenance = record["execution_provenance"]
    require(set(provenance) == {"session_id", "coordinator_session_id", "timestamp", "route", "prompt_sha256", "transcript_path", "transcript_sha256", "harness_operation_id", "harness_receipt_id"}, f"{letter}: provenance fields changed")
    session_id = provenance["session_id"]
    try:
        runner.validate_child_session_id(session_id)
    except runner.RunnerError as exc:
        raise AssertionError(f"{letter}: malformed child session") from exc
    require(session_id not in sessions, f"{letter}: missing or replayed session")
    sessions.add(session_id)
    require(
        provenance["coordinator_session_id"] == coordinator_session_id,
        f"{letter}: executing coordinator session mismatch",
    )
    try:
        datetime.fromisoformat(provenance["timestamp"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AssertionError(f"{letter}: invalid timestamp") from exc
    route = provenance["route"]
    require(set(route) == {"alias", "runtime", "model", "effort", "routing_sha256"}, f"{letter}: route fields changed")
    config = runner.load_config(ROOT)
    require(route["alias"] in config.data["model_aliases"], f"{letter}: unregistered route alias")
    resolved = config.resolve_alias(route["alias"], "codex")
    require(route["runtime"] == resolved["runtime"] and route["model"] == resolved["model"] and route["effort"] == "medium" and route["routing_sha256"] == config.fingerprint, f"{letter}: route provenance mismatch")
    require(provenance["prompt_sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest(), f"{letter}: prompt digest mismatch")
    producer_rel = f"docs/acceptance/evidence/architecture-workflow-v1/pressure/{letter}.md"
    require(provenance["transcript_path"] == producer_rel, f"{letter}: producer transcript path drift")
    actual_rel = transcript_rel or producer_rel
    transcript = ROOT / actual_rel
    if transcript_bytes is None:
        require(transcript.is_file() and not transcript.is_symlink(), f"{letter}: missing transcript")
        transcript_bytes = transcript.read_bytes()
    require(provenance["transcript_sha256"] == hashlib.sha256(transcript_bytes).hexdigest(), f"{letter}: transcript digest mismatch")
    try:
        transcript_text = transcript_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(f"{letter}: transcript is not UTF-8") from exc
    require(
        session_id in transcript_text
        and coordinator_session_id in transcript_text
        and route["alias"] in transcript_text,
        f"{letter}: transcript provenance mismatch",
    )
    require("/Users/" not in transcript_text and "/Users/" not in json.dumps(record, ensure_ascii=False), f"{letter}: operator path leaked")
    require(provenance["harness_operation_id"] is None and provenance["harness_receipt_id"] is None, f"{letter}: non-Harness run claimed Harness identity")
    result = transcript_result(letter, transcript_text, prompt)
    require(record["observed_outcome"] == result["output"], f"{letter}: observed outcome does not match graded response")


def aggregate_state_sha256(runs: list[dict[str, Any]], field: str) -> str:
    rows = [{"case_id": row["case_id"], "state_sha256": row[field]} for row in runs]
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_dogfood_if_present(
    subject_head: str, sessions: set[str], coordinator_session_id: str
) -> None:
    path = EVIDENCE / "dogfood.json"
    if not path.exists():
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_cases = [letter for letter in LETTERS if CASES[letter]["pressure"]["effect_mode"] == "read-only"]
    require(
        set(value) == {"schema_version", "type", "subject_head", "cases", "runs", "pre_state_sha256", "post_state_sha256", "state_hash_derivation", "verdict"}
        and value["schema_version"] == 1
        and value["type"] == "architecture-workflow-v1-read-only-dogfood"
        and value["subject_head"] == subject_head
        and value["cases"] == expected_cases
        and value["verdict"] == "pass"
        and value["state_hash_derivation"] == "sha256 of canonical ordered case_id/state_sha256 rows derived from validated per-case records",
        "dogfood schema or identity mismatch",
    )
    runs = value["runs"]
    require(isinstance(runs, list) and [row.get("case_id") for row in runs if isinstance(row, dict)] == expected_cases, "dogfood run denominator drift")
    expected_run_keys = {
        "case_id", "expected_carrier", "observed_outcome", "pre_state_sha256",
        "post_state_sha256", "prompt_sha256", "record_path", "record_sha256",
        "route", "session_id", "coordinator_session_id", "timestamp", "transcript_path", "transcript_sha256", "verdict",
    }
    source_files: set[str] = set()
    for row in runs:
        letter = row["case_id"]
        require(set(row) == expected_run_keys, f"dogfood {letter}: run fields changed")
        record_rel = f"docs/acceptance/evidence/architecture-workflow-v1/dogfood-cases/{letter}.json"
        transcript_rel = f"docs/acceptance/evidence/architecture-workflow-v1/dogfood-cases/{letter}.md"
        require(row["record_path"] == record_rel and row["transcript_path"] == transcript_rel, f"dogfood {letter}: source path drift")
        record_path = ROOT / record_rel
        transcript_path = ROOT / transcript_rel
        require(record_path.is_file() and not record_path.is_symlink(), f"dogfood {letter}: source record missing")
        require(transcript_path.is_file() and not transcript_path.is_symlink(), f"dogfood {letter}: source transcript missing")
        record_bytes = record_path.read_bytes()
        transcript_bytes = transcript_path.read_bytes()
        require(row["record_sha256"] == hashlib.sha256(record_bytes).hexdigest(), f"dogfood {letter}: record digest mismatch")
        require(row["transcript_sha256"] == hashlib.sha256(transcript_bytes).hexdigest(), f"dogfood {letter}: transcript digest mismatch")
        record = json.loads(record_bytes)
        validate_record(
            letter,
            record,
            subject_head,
            sessions,
            coordinator_session_id,
            transcript_rel=transcript_rel,
            transcript_bytes=transcript_bytes,
        )
        provenance = record["execution_provenance"]
        require(
            row == {
                "case_id": letter,
                "expected_carrier": record["expected_carrier"],
                "observed_outcome": record["observed_outcome"],
                "pre_state_sha256": record["pre_state_sha256"],
                "post_state_sha256": record["post_state_sha256"],
                "prompt_sha256": provenance["prompt_sha256"],
                "record_path": record_rel,
                "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
                "route": provenance["route"],
                "session_id": provenance["session_id"],
                "coordinator_session_id": provenance["coordinator_session_id"],
                "timestamp": provenance["timestamp"],
                "transcript_path": transcript_rel,
                "transcript_sha256": provenance["transcript_sha256"],
                "verdict": record["verdict"],
            },
            f"dogfood {letter}: aggregate does not derive from source record",
        )
        require(record["pre_state_sha256"] == record["post_state_sha256"], f"dogfood {letter}: source mutated disposable product")
        source_files.update({record_path.name, transcript_path.name})
    actual_sources = {item.name for item in (EVIDENCE / "dogfood-cases").iterdir() if item.is_file()}
    require(actual_sources == source_files, "dogfood source artifact coverage mismatch")
    require(value["pre_state_sha256"] == aggregate_state_sha256(runs, "pre_state_sha256"), "dogfood pre-state derivation mismatch")
    require(value["post_state_sha256"] == aggregate_state_sha256(runs, "post_state_sha256"), "dogfood post-state derivation mismatch")
    require(value["pre_state_sha256"] == value["post_state_sha256"], "dogfood mutated product state")
    print(f"Dogfood derivation passed: {len(runs)} records, {len(runs)} fresh sessions, {len(source_files)} digest-bound artifacts")


def tampered_transcript(letter: str, raw: bytes) -> bytes:
    text = raw.decode("utf-8")
    marker = "\n## Response\n\n```json\n"
    prefix, response = text.split(marker, 1)
    encoded, trailing = response.split("\n```\n", 1)
    result = json.loads(encoded)
    field = runner.artifact_fields(CASES[letter])[0]
    original = result["artifacts"][field]
    result["artifacts"][field] = not original if type(original) is bool else f"{original}-tampered"
    return (
        prefix
        + marker
        + json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n"
        + trailing
    ).encode("utf-8")


def validate_tamper_regressions(subject_head: str) -> None:
    for label, relative_root in (
        ("pressure response", "pressure"),
        ("dogfood source", "dogfood-cases"),
    ):
        letter = "a"
        base = f"docs/acceptance/evidence/architecture-workflow-v1/{relative_root}/{letter}"
        record = json.loads((ROOT / f"{base}.json").read_text(encoding="utf-8"))
        transcript = tampered_transcript(letter, (ROOT / f"{base}.md").read_bytes())
        record["execution_provenance"]["transcript_sha256"] = hashlib.sha256(transcript).hexdigest()
        record_bytes = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        outer_digests = {
            "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
            "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
        }
        require(all(SHA256.fullmatch(value) for value in outer_digests.values()), f"{label}: updated outer digest invalid")
        try:
            validate_record(
                letter,
                record,
                subject_head,
                set(),
                runner.current_coordinator_session_id(),
                transcript_rel=f"{base}.md",
                transcript_bytes=transcript,
            )
        except AssertionError as exc:
            require("failed grading" in str(exc), f"{label}: rejected for the wrong reason")
        else:
            raise AssertionError(f"{label}: coherent semantic tamper was accepted")
    print("Semantic tamper regressions passed: pressure response and dogfood source")


def validate() -> None:
    require(tuple(sorted(CASES)) == LETTERS, "case manifest is not exactly A-S")
    records = sorted(path.name for path in PRESSURE.glob("*.json")) if PRESSURE.is_dir() else []
    require(records == [f"{letter}.json" for letter in LETTERS], f"pressure record coverage mismatch: {records}")
    loaded = {letter: load_record(letter) for letter in LETTERS}
    subject_heads = {record.get("subject_head") for record in loaded.values()}
    require(
        len(subject_heads) == 1
        and isinstance(next(iter(subject_heads)), str)
        and COMMIT_SHA.fullmatch(next(iter(subject_heads))) is not None,
        "pressure records do not bind one exact subject_head",
    )
    subject_head = next(iter(subject_heads))
    manifest = audit.load_subject_evidence_manifest(
        EVIDENCE / "subject-evidence-manifest.json"
    )
    require(
        subject_head == manifest.current_subject_head,
        "pressure subject differs from the authoritative manifest",
    )
    archive = json.loads((EVIDENCE / "archive-manifest.json").read_text(encoding="utf-8"))
    archived_subjects = {
        row.get("subject_head")
        for row in archive.get("generations", [])
        if isinstance(row, dict)
    }
    require(
        archived_subjects == set(manifest.invalidated_subject_heads),
        "archive generations differ from the authoritative invalidated-subject inventory",
    )
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", subject_head, "HEAD"], cwd=ROOT, check=False)
    require(ancestor.returncode == 0, "frozen subject is not an ancestor of evidence HEAD")
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{subject_head}..HEAD", "--", *SUBJECT_PATHS],
        cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    require(not changed, f"implementation subject changed after freeze: {changed}")
    sessions: set[str] = set()
    coordinator_session_id = runner.current_coordinator_session_id()
    for letter in LETTERS:
        validate_record(
            letter,
            loaded[letter],
            subject_head,
            sessions,
            coordinator_session_id,
        )
    validate_dogfood_if_present(subject_head, sessions, coordinator_session_id)
    validate_tamper_regressions(subject_head)
    print(f"Architecture Workflow pressure records passed: {len(LETTERS)} cases, {len(sessions)} fresh sessions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-case", choices=LETTERS)
    parser.add_argument("--prepare-fixture", choices=LETTERS)
    parser.add_argument("--fixture-root", type=Path)
    args = parser.parse_args()
    if args.emit_case:
        print(json.dumps(CASES[args.emit_case], ensure_ascii=False))
        return 0
    if args.prepare_fixture:
        if args.fixture_root is None or not args.fixture_root.is_absolute():
            parser.error("--prepare-fixture requires an absolute --fixture-root")
        prepare_fixture(args.prepare_fixture, args.fixture_root)
        print(args.fixture_root)
        return 0
    if args.fixture_root is not None:
        parser.error("--fixture-root requires --prepare-fixture")
    validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
