#!/usr/bin/env python3
"""Validate, start, or join one governed Split through existing dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn

from dispatch_contracts import DispatchError, validate_request
from dispatch_execution import start as start_dispatch_request
from dispatch_setup import materialize_current_context
from harness.contracts import ContractError
from harness.split_activation import (
    SplitLaunchReceipt,
    SplitTerminalReceipt,
    join_split,
)
from harness.split_contracts import manifest_from_dict, manifest_to_dict
from harness.split_join import ChildReceipt
from split_dispatch import (
    DispatchChildRequest,
    PreparedSplitDispatch,
    drive_split_dispatch,
    prepare_split_dispatch,
)


def die(message: str, code: int = 3) -> NoReturn:
    print(f"split-runner: {message}", file=sys.stderr)
    raise SystemExit(code)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read exact JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"exact JSON input must be an object: {path}")
    return value


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _exact_spec(value: Mapping[str, Any]) -> tuple[object, dict[str, Any], list[dict[str, Any]]]:
    expected = {
        "schema_version",
        "manifest",
        "current_parent",
        "registered_pipelines",
        "children",
    }
    if set(value) != expected or value.get("schema_version") != 1:
        raise ContractError("Split activation request fields changed")
    manifest = manifest_from_dict(value.get("manifest", {}))
    current = value.get("current_parent")
    if not isinstance(current, dict) or set(current) != {
        "plan_sha256",
        "outcome_contract_sha256",
    }:
        raise ContractError("Split activation current_parent fields changed")
    pipelines = value.get("registered_pipelines")
    if (
        not isinstance(pipelines, list)
        or not pipelines
        or len(set(pipelines)) != len(pipelines)
        or any(not isinstance(item, str) or not item for item in pipelines)
    ):
        raise ContractError("registered_pipelines must be a non-empty unique list")
    children = value.get("children")
    if not isinstance(children, list) or any(
        not isinstance(item, dict)
        or set(item) != {"subplan_id", "dispatch"}
        or not isinstance(item.get("dispatch"), dict)
        for item in children
    ):
        raise ContractError("Split activation children changed")
    return manifest, current, children


def _launch_receipts(path: Path | None) -> tuple[SplitLaunchReceipt, ...]:
    if path is None:
        return ()
    raw = _read_object(path)
    if set(raw) != {"schema_version", "receipts"} or raw.get("schema_version") != 1:
        raise ContractError("Split launch receipt envelope changed")
    receipts = raw.get("receipts")
    if not isinstance(receipts, list):
        raise ContractError("Split launch receipts must be a list")
    return tuple(SplitLaunchReceipt(**item) for item in receipts)


def _terminal_receipts(path: Path | None) -> tuple[SplitTerminalReceipt, ...]:
    if path is None:
        return ()
    raw = _read_object(path)
    if set(raw) != {"schema_version", "receipts"} or raw.get("schema_version") != 1:
        raise ContractError("Split terminal receipt envelope changed")
    receipts = raw.get("receipts")
    if not isinstance(receipts, list):
        raise ContractError("Split terminal receipts must be a list")
    values: list[SplitTerminalReceipt] = []
    for item in receipts:
        if not isinstance(item, dict) or "child" not in item:
            raise ContractError("Split terminal receipt entry changed")
        child = item["child"]
        if not isinstance(child, dict):
            raise ContractError("Split terminal child receipt changed")
        values.append(
            SplitTerminalReceipt(
                child=ChildReceipt(
                    manifest_sha256=child.get("manifest_sha256"),
                    subplan_id=child.get("subplan_id"),
                    branch=child.get("branch"),
                    head_sha=child.get("head_sha"),
                    summary_sha256=child.get("summary_sha256"),
                    review_receipt_sha256=child.get("review_receipt_sha256"),
                    evidence_ids=tuple(child.get("evidence_ids", ())),
                    status=child.get("status"),
                ),
                **{key: value for key, value in item.items() if key != "child"},
            )
        )
    return tuple(values)


def _prepared(
    value: Mapping[str, Any],
    *,
    existing_launches: tuple[SplitLaunchReceipt, ...],
) -> PreparedSplitDispatch:
    manifest, current, raw_children = _exact_spec(value)
    launched = {item.subplan_id for item in existing_launches}
    children: list[DispatchChildRequest] = []
    for item in raw_children:
        raw = dict(item["dispatch"])
        subplan_id = str(item["subplan_id"])
        request_sha = _canonical_sha256(raw)
        if subplan_id in launched:
            # A launched child is never revalidated through worktree creation;
            # its immutable launch receipt and frozen Split policy are the
            # replay boundary.  Only the fields needed for binding are read.
            request = {
                "request_id": raw.get("request_id"),
                "pipeline": raw.get("pipeline") or "lifecycle/default",
                "completion_policy": raw.get("completion_policy") or "attention",
                "placement": raw.get("placement") or "split",
                "worktree": Path(str(raw.get("worktree") or "")).expanduser().resolve(),
                "split": raw.get("split"),
            }
        else:
            request = validate_request(materialize_current_context(raw))
        children.append(
            DispatchChildRequest(
                subplan_id=subplan_id,
                request_sha256=request_sha,
                request=request,
            )
        )
    return prepare_split_dispatch(
        manifest,
        current_plan_sha256=current["plan_sha256"],
        current_outcome_contract_sha256=current["outcome_contract_sha256"],
        registered_pipelines=value["registered_pipelines"],
        children=tuple(children),
    )


def _activation_payload(prepared: PreparedSplitDispatch) -> dict[str, Any]:
    activation = prepared.activation
    return {
        "schema_version": 1,
        "status": "valid" if activation.accepted else "rejected",
        "issue_codes": list(activation.issue_codes),
        "effects": {
            "dispatches": 0,
            "provider_calls": 0,
            "surfaces_created": 0,
            "worktrees_created": 0,
        },
        "manifest": (
            manifest_to_dict(activation.validated.manifest)
            if activation.validated is not None
            else None
        ),
        "waves": (
            [
                [child.subplan_id for child in wave.children]
                for wave in activation.execution.waves
            ]
            if activation.execution is not None
            else []
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--spec", type=Path, required=True)
    start = sub.add_parser("start")
    start.add_argument("--spec", type=Path, required=True)
    start.add_argument("--launch-receipts", type=Path)
    start.add_argument("--terminal-receipts", type=Path)
    join = sub.add_parser("join")
    join.add_argument("--spec", type=Path, required=True)
    join.add_argument("--launch-receipts", type=Path, required=True)
    join.add_argument("--terminal-receipts", type=Path, required=True)
    join.add_argument("--current-heads", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = _read_object(args.spec.expanduser().resolve())
        launches = _launch_receipts(
            args.launch_receipts.expanduser().resolve()
            if getattr(args, "launch_receipts", None)
            else None
        )
        prepared = _prepared(value, existing_launches=launches)
        if args.command == "validate":
            payload = _activation_payload(prepared)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if prepared.activation.accepted else 2
        terminals = _terminal_receipts(
            args.terminal_receipts.expanduser().resolve()
            if getattr(args, "terminal_receipts", None)
            else None
        )
        if args.command == "start":
            before = len(launches)
            result = drive_split_dispatch(
                prepared,
                terminal_receipts=terminals,
                launch_receipts=launches,
                start_dispatch=lambda request, digest: start_dispatch_request(
                    dict(request), digest
                ),
            )
            after = len(result.launch_receipts)
            payload = {
                "schema_version": 1,
                "disposition": result.disposition,
                "reason": result.reason,
                "effects": {
                    "dispatches": after - before,
                    "provider_calls": after - before,
                    "surfaces_created": after - before,
                    "worktrees_created": after - before,
                },
                "launch_receipts": [
                    {
                        key: getattr(item, key)
                        for key in (
                            "manifest_sha256",
                            "subplan_id",
                            "request_id",
                            "workspace_id",
                            "worktree_path",
                            "surface_id",
                            "placement",
                        )
                    }
                    for item in result.launch_receipts
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if result.disposition in {"awaiting-children", "ready-to-join"} else 2
        heads = _read_object(args.current_heads.expanduser().resolve())
        if set(heads) != {"schema_version", "heads"} or heads.get("schema_version") != 1:
            raise ContractError("Split current-head envelope changed")
        current_heads = heads.get("heads")
        if not isinstance(current_heads, dict):
            raise ContractError("Split current heads must be an object")
        decision = join_split(
            prepared.activation,
            launch_receipts=launches,
            terminal_receipts=terminals,
            current_heads=current_heads,
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "disposition": decision.disposition,
                    "reason": decision.reason,
                    "integration_order": [
                        {
                            "subplan_id": item.subplan_id,
                            "branch": item.branch,
                            "head_sha": item.head_sha,
                            "summary_sha256": item.summary_sha256,
                            "review_receipt_sha256": item.review_receipt_sha256,
                        }
                        for item in decision.integration_order
                    ],
                    "parent_evidence_proven": list(decision.parent_evidence_proven),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if decision.disposition == "ready" else 2
    except (ContractError, DispatchError, OSError, ValueError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
