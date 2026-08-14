"""Host-owned approval boundary for model-authored custom pipelines."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness.contracts import ContractError as HarnessContractError
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    compile_custom_spec,
    parse_pipeline_spec,
    pipeline_spec_payload,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry
from dispatch_contracts import (
    DispatchError,
    _approval_lock,
    atomic_json,
    read_object,
    sha256_file,
)
from dispatch_custom_contracts import (
    _review_from_snapshot,
    custom_approval_path,
    custom_approval_plan_path,
)


HOST_APPROVAL_PROGRAM = Path("/usr/bin/osascript")


def host_custom_approval_decision(challenge: dict[str, Any]) -> str:
    """Ask through a host-owned macOS dialog; stdin/argv cannot approve."""

    if sys.platform != "darwin" or not HOST_APPROVAL_PROGRAM.is_file():
        raise DispatchError(
            "custom approval requires the macOS host confirmation dialog"
        )
    script = """
on run argv
  set challengeDigest to item 1 of argv
  set messageText to "Approve exact custom pipeline challenge?" & return & return & challengeDigest
  set answer to display dialog messageText with title "LLM Obsidian" buttons {"Reject", "Revise", "Approve"} default button "Revise"
  return button returned of answer
end run
""".strip()
    try:
        result = subprocess.run(
            [
                str(HOST_APPROVAL_PROGRAM),
                "-e",
                script,
                challenge["challenge_sha256"],
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatchError(f"host approval dialog failed: {exc}") from exc
    if result.returncode != 0:
        raise DispatchError("host approval dialog did not return a decision")
    decision = result.stdout.strip().lower()
    if decision not in {"approve", "reject", "revise"}:
        raise DispatchError("host approval dialog returned an invalid decision")
    return decision


def record_custom_approval_decision(
    request: dict[str, Any],
    challenge: dict[str, Any],
    challenge_sha256: str,
    *,
    host_decision: Any = host_custom_approval_decision,
) -> dict[str, Any]:
    """Persist a decision produced only by the host confirmation boundary."""

    if challenge_sha256 != challenge["challenge_sha256"]:
        raise DispatchError("custom approval challenge digest does not match")
    path = custom_approval_path(request)
    if path.is_symlink() or not path.is_file():
        raise DispatchError("custom pipeline must be validated before decision")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DispatchError("custom approval challenge is not owner-only")
    lock = _approval_lock(path)
    try:
        record = read_object(path)
        if record.get("challenge") != challenge:
            raise DispatchError(
                "custom approval challenge no longer matches validation"
            )
        if record.get("status") != "pending":
            raise DispatchError("custom approval decision is already durable")
        decision = host_decision(challenge)
        if decision not in {"approve", "reject", "revise"}:
            raise DispatchError("host approval decision is invalid")
        token = secrets.token_hex(32) if decision == "approve" else ""
        record.update(
            {
                "status": "approved" if decision == "approve" else decision,
                "decision": decision,
                "actor": "host-user-dialog",
                "approval_token_sha256": (
                    hashlib.sha256(token.encode()).hexdigest() if token else ""
                ),
            }
        )
        atomic_json(path, record)
    finally:
        os.close(lock)
    result = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "status": record["status"],
        "decision": decision,
    }
    if token:
        result["approval_token"] = token
    return result


def authorize_custom_request(
    request: dict[str, Any],
    request_sha256: str,
    approval_token: str,
) -> dict[str, Any]:
    """Atomically consume a revalidated custom snapshot for start."""

    if approval_token and not re.fullmatch(r"[0-9a-f]{64}", approval_token):
        raise DispatchError(
            "custom approval token must be 64 lowercase hex characters"
        )
    path = custom_approval_path(request)
    if path.is_symlink() or not path.is_file():
        raise DispatchError("custom pipeline must be validated before start")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DispatchError("custom approval challenge is not owner-only")
    lock = _approval_lock(path)
    try:
        persisted = read_object(path)
        challenge = persisted.get("challenge")
        snapshot = persisted.get("snapshot")
        if not isinstance(challenge, dict) or not isinstance(snapshot, dict):
            raise DispatchError("custom approval snapshot is unavailable")
        if challenge.get("request_sha256") != request_sha256:
            raise DispatchError("custom approval request bytes changed")
        coordinator = challenge.get("coordinator")
        if coordinator != {
            "origin_surface": request["origin_surface"],
            "origin_session": request["origin_session"],
            "placement": request["placement"],
        }:
            raise DispatchError("custom approval coordinator identity changed")
        policy_valid = (
            persisted.get("status") == "pending"
            and persisted.get("decision") == ""
            and persisted.get("actor") == ""
        )
        host_approved = (
            persisted.get("status") == "approved"
            and persisted.get("decision") == "approve"
            and persisted.get("actor") == "host-user-dialog"
        )
        if not policy_valid and not host_approved:
            raise DispatchError("custom pipeline has no approved decision receipt")
        if approval_token and (
            not host_approved
            or persisted.get("approval_token_sha256")
            != hashlib.sha256(approval_token.encode()).hexdigest()
        ):
            raise DispatchError("custom approval token does not match")
        plan_path = custom_approval_plan_path(request)
        plan_info = plan_path.stat() if plan_path.exists() else None
        if (
            plan_path.is_symlink()
            or not plan_path.is_file()
            or plan_info is None
            or plan_info.st_uid != os.getuid()
            or stat.S_IMODE(plan_info.st_mode) & 0o077
            or sha256_file(plan_path) != challenge.get("plan_sha256")
        ):
            raise DispatchError("approved plan snapshot is unavailable")
        try:
            spec = parse_pipeline_spec(
                json.dumps(snapshot["pipeline_spec"], sort_keys=True)
            )
            policy = CustomPipelinePolicy.default()
            compiled = compile_custom_spec(
                spec,
                builtin_registry(),
                policy=policy,
                capabilities=("route:resolved",),
            )
        except (KeyError, HarnessContractError, ValueError) as exc:
            raise DispatchError("approved custom snapshot is invalid") from exc
        card = render_custom_approval(spec, compiled, policy=policy) + "\n".join(
            (
                "Inherited harness permissions: cmux-target:policy-only",
                "Inherited harness side effects: cmux-surface:policy-only",
                "Coordinator target: "
                f"surface={request['origin_surface']}; "
                f"session={request['origin_session']}; "
                f"placement={request['placement']}",
                "",
            )
        )
        prompt = snapshot.get("prompt")
        if (
            compiled.definition_sha256 != challenge.get("definition_sha256")
            or hashlib.sha256(
                json.dumps(
                    pipeline_spec_payload(spec),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            != challenge.get("pipeline_spec_sha256")
            or hashlib.sha256(card.encode()).hexdigest()
            != challenge.get("approval_card_sha256")
            or card != snapshot.get("approval_card")
            or not isinstance(prompt, str)
            or hashlib.sha256(prompt.encode()).hexdigest()
            != challenge.get("prompt_sha256")
            or snapshot.get("plan_sha256") != challenge.get("plan_sha256")
            or snapshot.get("effective") != challenge.get("route")
            or snapshot.get("review") != challenge.get("review")
        ):
            raise DispatchError("approved custom snapshot no longer matches")
        expected_session = {
            "schema_version": 1,
            "session_id": request["origin_session"],
            **request["session_route"],
            "config_sha256": snapshot["effective"]["config_sha256"],
        }
        if snapshot.get("session") != expected_session:
            raise DispatchError("approved custom session snapshot changed")
        review = _review_from_snapshot(snapshot["review"])
        persisted["status"] = "consumed"
        atomic_json(path, persisted)
    finally:
        os.close(lock)
    approved = dict(request)
    approved["_custom_approval"] = ExplicitPipelineApproval.for_card(
        definition_sha256=compiled.definition_sha256,
        approval_card=card,
        actor="policy-valid-snapshot" if policy_valid else "host-user-dialog",
        decision="approve",
    )
    approved["_approved_custom_contract"] = (spec, compiled, policy, card)
    approved["_approved_plan_file"] = plan_path
    approved["_approved_plan_sha256"] = challenge["plan_sha256"]
    approved["_approved_prompt"] = prompt
    approved["_approved_session_route"] = snapshot["session"]
    approved["_approved_effective_route"] = snapshot["effective"]
    approved["_approved_review"] = review
    return approved
