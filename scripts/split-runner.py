#!/usr/bin/env python3
"""Validate, start, or join one governed Split through existing dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn

from dispatch_contracts import DispatchError, validate_request
from dispatch_execution import start as start_dispatch_request
from dispatch_lifecycle import completed_replay
from dispatch_setup import materialize_current_context
from harness.contracts import ContractError
from harness.split_activation import (
    SplitLaunchReceipt,
    SplitTerminalReceipt,
    join_split,
)
from harness.split_contracts import manifest_from_dict, manifest_to_dict
from harness.split_join import ChildReceipt
from harness.split_evidence import SplitEvidenceStore, SplitTerminalProjector
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
                    base_sha=child.get("base_sha"),
                    base_ancestor=child.get("base_ancestor"),
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
    launched = {item.subplan_id: item for item in existing_launches}
    children: list[DispatchChildRequest] = []
    for item in raw_children:
        raw = dict(item["dispatch"])
        subplan_id = str(item["subplan_id"])
        request_sha = _canonical_sha256(raw)
        if subplan_id in launched:
            # A launched child is never revalidated through worktree creation;
            # its immutable launch receipt and frozen Split policy are the
            # replay boundary.  Only the fields needed for binding are read.
            launch = launched[subplan_id]
            request = {
                "request_id": raw.get("request_id"),
                "pipeline": raw.get("pipeline") or "lifecycle/default",
                "completion_policy": raw.get("completion_policy") or "attention",
                "placement": raw.get("placement") or "split",
                "worktree": Path(str(raw.get("worktree") or "")).expanduser().resolve(),
                "branch": raw.get("branch"),
                "base_sha": launch.base_sha,
                "vault_root": raw.get("vault_root"),
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


def _split_evidence_store(
    value: Mapping[str, Any], manifest_sha256: str
) -> SplitEvidenceStore:
    _manifest, _current, children = _exact_spec(value)
    roots = {
        str(item["dispatch"].get("vault_root") or "") for item in children
    }
    if len(roots) != 1:
        raise ContractError("Split children must share one exact vault root")
    root = Path(next(iter(roots))).expanduser()
    if not root.is_absolute() or not root.resolve().is_dir():
        raise ContractError("Split child vault root is unavailable")
    return SplitEvidenceStore(
        root.resolve(),
        manifest_sha256=manifest_sha256,
        activation_sha256=_canonical_sha256(value),
    )


def _request_sha256_by_id(
    value: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    _manifest, _current, children = _exact_spec(value)
    digests: dict[str, str] = {}
    requests: dict[str, dict[str, Any]] = {}
    for item in children:
        subplan_id = str(item["subplan_id"])
        request = dict(item["dispatch"])
        if subplan_id in digests:
            raise ContractError("Split child subplan ids must be unique")
        digests[subplan_id] = _canonical_sha256(request)
        requests[subplan_id] = request
    return digests, requests


def _manifest_order(
    prepared: PreparedSplitDispatch,
    receipts: tuple[SplitLaunchReceipt, ...],
) -> tuple[SplitLaunchReceipt, ...]:
    by_id = {item.subplan_id: item for item in receipts}
    if len(by_id) != len(receipts):
        raise ContractError("Split launch evidence has duplicate children")
    expected = tuple(item.subplan_id for item in prepared.activation.bindings)
    if not set(by_id).issubset(set(expected)):
        raise ContractError("Split launch evidence left the sealed manifest")
    return tuple(by_id[item] for item in expected if item in by_id)


def _recovered_launch_base(
    raw: Mapping[str, Any],
    prior: Mapping[str, Any],
    *,
    manifest_base_sha: str,
) -> str:
    """Read an accepted child's actual sealed base before writing evidence."""

    request_id = str(raw.get("request_id") or "")
    requested_worktree = Path(str(raw.get("worktree") or "")).expanduser()
    recovered_worktree = Path(str(prior.get("worktree") or "")).expanduser()
    requested_vault = Path(str(raw.get("vault_root") or "")).expanduser()
    if (
        prior.get("request_id") != request_id
        or not requested_worktree.is_absolute()
        or not recovered_worktree.is_absolute()
        or requested_worktree.resolve() != recovered_worktree.resolve()
        or prior.get("branch") != raw.get("branch")
    ):
        raise ContractError("recovered Split child dispatch identity drifted")
    meta = _read_object(recovered_worktree.resolve() / ".task-meta.json")
    recorded_vault = Path(str(meta.get("vault_root") or "")).expanduser()
    if (
        meta.get("version") != 4
        or meta.get("task_id") != request_id
        or meta.get("worktree") != str(recovered_worktree.resolve())
        or meta.get("branch") != raw.get("branch")
        or not requested_vault.is_absolute()
        or not recorded_vault.is_absolute()
        or requested_vault.resolve() != recorded_vault.resolve()
        or meta.get("split_policy") != raw.get("split")
    ):
        raise ContractError("recovered Split child task contract drifted")
    base_sha = str(meta.get("base_sha") or "")
    if (
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha) is None
        or base_sha != manifest_base_sha
    ):
        raise ContractError("recovered Split child base SHA drifted")
    return base_sha


def _authoritative_state(
    value: Mapping[str, Any],
    *,
    claimed_launches: tuple[SplitLaunchReceipt, ...],
    claimed_terminals: tuple[SplitTerminalReceipt, ...],
) -> tuple[
    PreparedSplitDispatch,
    SplitEvidenceStore,
    tuple[SplitLaunchReceipt, ...],
    tuple[SplitTerminalReceipt, ...],
]:
    manifest, _current, _children = _exact_spec(value)
    digests, requests = _request_sha256_by_id(value)
    evidence = _split_evidence_store(value, manifest.manifest_sha256)
    stored = evidence.launches(digests)
    recovered: list[tuple[SplitLaunchReceipt, str]] = []
    known = {item.subplan_id for item in stored}
    for subplan_id, raw in requests.items():
        if subplan_id in known:
            continue
        prior = completed_replay(raw, digests[subplan_id])
        if prior is None:
            continue
        if not any(
            item.subplan_id == subplan_id for item in manifest.subplans
        ):
            raise ContractError(
                "recovered Split launch left the manifest: "
                f"{subplan_id!r} not in "
                f"{tuple(item.subplan_id for item in manifest.subplans)!r}"
            )
        recovered_base_sha = _recovered_launch_base(
            raw,
            prior,
            manifest_base_sha=manifest.parent.base_sha,
        )
        recovered.append(
            (
                SplitLaunchReceipt(
                    manifest_sha256=manifest.manifest_sha256,
                    base_sha=recovered_base_sha,
                    subplan_id=subplan_id,
                    request_id=str(prior.get("request_id") or ""),
                    workspace_id=str(prior.get("task_workspace") or ""),
                    worktree_path=str(prior.get("worktree") or ""),
                    surface_id=str(prior.get("task_surface") or ""),
                    placement=str(prior.get("placement") or ""),
                ),
                digests[subplan_id],
            )
        )
    provisional = (*stored, *(item[0] for item in recovered))
    prepared = _prepared(value, existing_launches=tuple(provisional))
    if not prepared.activation.accepted:
        return prepared, evidence, (), ()
    evidence.initialize()
    for receipt, request_sha256 in recovered:
        evidence.seal_launch(receipt, request_sha256=request_sha256)
    launches = _manifest_order(prepared, evidence.launches(digests))
    if claimed_launches and claimed_launches != launches:
        raise ContractError("caller Split launch receipts are not authoritative")
    terminals = SplitTerminalProjector(prepared, evidence).project_all(launches)
    if claimed_terminals and claimed_terminals != terminals:
        raise ContractError("caller Split terminal receipts are not authoritative")
    return prepared, evidence, launches, terminals


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
        claimed_launches = _launch_receipts(
            args.launch_receipts.expanduser().resolve()
            if getattr(args, "launch_receipts", None)
            else None
        )
        if args.command == "validate":
            prepared = _prepared(value, existing_launches=())
            payload = _activation_payload(prepared)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if prepared.activation.accepted else 2
        claimed_terminals = _terminal_receipts(
            args.terminal_receipts.expanduser().resolve()
            if getattr(args, "terminal_receipts", None)
            else None
        )
        prepared, evidence, launches, terminals = _authoritative_state(
            value,
            claimed_launches=claimed_launches,
            claimed_terminals=claimed_terminals,
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
                persist_launch=lambda receipt, digest: evidence.seal_launch(
                    receipt, request_sha256=digest
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
                            "base_sha",
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
        claimed_heads = heads.get("heads")
        if not isinstance(claimed_heads, dict):
            raise ContractError("Split current heads must be an object")
        current_heads = {
            item.child.subplan_id: item.child.head_sha for item in terminals
        }
        if claimed_heads != current_heads:
            raise ContractError("caller Split HEAD map is not authoritative")
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
    except (ContractError, DispatchError, OSError, TypeError, ValueError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
