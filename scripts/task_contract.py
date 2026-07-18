#!/usr/bin/env python3
"""Validate dispatch task policies and unattended review/reap gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn


SUMMARY_TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
REVIEW_MODES = {"light", "full", "skip"}
FORBIDDEN_ACTIONS = [
    "push",
    "deploy",
    "publish",
    "delete-worktree",
    "delete-branch",
    "expand-scope",
]
DEFAULT_WATCHDOG_POLICY = {
    "enabled": False,
    "poll_seconds": 30,
    "warn_after_seconds": 900,
    "alert_after_seconds": 1200,
}


class ContractError(ValueError):
    pass


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(f"missing file: {path}")
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def v3_session_is_bound(meta: dict[str, Any], session_id: str) -> bool:
    if meta.get("version") != 3 or not session_id:
        return False
    raw_vault = str(meta.get("vault_root") or "").strip()
    if not raw_vault:
        return False
    root = Path(raw_vault).expanduser().resolve() / ".vault-meta" / "task-sessions" / "session-bindings"
    if not root.is_dir():
        return False
    for path in root.glob("*/*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if (
            value.get("session_id") == session_id
            and value.get("project_id") == meta.get("project_id")
            and value.get("task_id") == meta.get("task_id")
        ):
            return True
    return False


def normalize(meta: dict[str, Any], *, verify_plan_hash: bool = True) -> dict[str, Any]:
    version = meta.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractError("task metadata version must be an integer")
    if version == 1:
        return {
            "version": 1,
            "interaction_policy": "interactive",
            "review_policy": {
                "mode": "full",
                "max_verify_iterations": 0,
                "auto_resolve_severities": [],
                "escalate_severities": ["blocking"],
            },
            "reap_policy": {
                "mode": "interim",
                "auto_file": False,
                "allowed_types": [],
                "title": "",
            },
            "surface_policy": {"auto_close": False},
            "watchdog_policy": dict(DEFAULT_WATCHDOG_POLICY),
        }
    if version not in {2, 3}:
        raise ContractError(f"unsupported task metadata version: {version!r}")

    if version == 3:
        for field in ("project_id", "task_id"):
            value = meta.get(field)
            try:
                normalized = str(uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                raise ContractError(f"v3 {field} must be a UUID") from None
            if normalized != value:
                raise ContractError(f"v3 {field} must be a canonical lowercase UUID")

    for field in ("task_name", "origin_session"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise ContractError(f"{field} must be a non-empty string")
    if meta.get("executor_runtime") not in {"claude", "codex"}:
        raise ContractError("executor_runtime must be claude or codex")

    policy = meta.get("interaction_policy")
    if policy not in {"interactive", "unattended"}:
        raise ContractError("interaction_policy must be interactive or unattended")
    plan_value = meta.get("plan_file")
    hash_value = meta.get("approved_plan_sha256")
    plan_raw = plan_value.strip() if isinstance(plan_value, str) else ""
    plan_hash = hash_value.strip() if isinstance(hash_value, str) else ""
    if not plan_raw or len(plan_hash) != 64 or any(c not in "0123456789abcdef" for c in plan_hash):
        raise ContractError(f"v{version} metadata requires plan_file and lowercase approved_plan_sha256")
    plan = Path(plan_raw).expanduser().resolve()
    if not plan.is_file():
        raise ContractError(f"approved plan is missing: {plan}")
    if verify_plan_hash and sha256_file(plan) != plan_hash:
        raise ContractError("approved plan hash changed after dispatch approval")
    vault_raw = meta.get("vault_root")
    if vault_raw is not None:
        if not isinstance(vault_raw, str) or not vault_raw.strip():
            raise ContractError("vault_root must be a non-empty absolute path")
        declared_vault = Path(vault_raw).expanduser()
        if not declared_vault.is_absolute():
            raise ContractError("vault_root must be a non-empty absolute path")
        declared_vault = declared_vault.resolve()
        if not (declared_vault / "wiki").is_dir():
            raise ContractError("vault_root must contain the coordinator wiki")
        if (
            plan.parent.name != "plans"
            or plan.parent.parent.name != "wiki"
            or plan.parents[2] != declared_vault
        ):
            raise ContractError("plan_file must belong to vault_root/wiki/plans")

    review = meta.get("review_policy")
    if not isinstance(review, dict) or review.get("mode") not in REVIEW_MODES:
        raise ContractError("review_policy.mode must be light, full, or skip")
    max_verify = review.get("max_verify_iterations")
    if isinstance(max_verify, bool) or not isinstance(max_verify, int) or not 0 <= max_verify <= 5:
        raise ContractError("review_policy.max_verify_iterations must be 0..5")
    auto = review.get("auto_resolve_severities")
    escalate = review.get("escalate_severities")
    if not isinstance(auto, list) or any(x not in {"warning", "nit"} for x in auto):
        raise ContractError("auto_resolve_severities may contain warning and nit")
    if len(auto) != len(set(auto)):
        raise ContractError("auto_resolve_severities must be unique")
    if escalate != ["blocking"]:
        raise ContractError("blocking must be the sole escalate severity")

    reap = meta.get("reap_policy")
    if not isinstance(reap, dict):
        raise ContractError("reap_policy must be an object")
    allowed = reap.get("allowed_types")
    title_value = reap.get("title")
    title = title_value.strip() if isinstance(title_value, str) else ""
    if reap.get("mode") != "final" or not isinstance(reap.get("auto_file"), bool):
        raise ContractError("reap_policy requires final mode and boolean auto_file")
    if not isinstance(allowed, list) or len(allowed) != 1 or allowed[0] not in SUMMARY_TYPES:
        raise ContractError("reap_policy.allowed_types must contain exactly one known type")
    if not title:
        raise ContractError("reap_policy.title is required")

    surface = meta.get("surface_policy")
    if not isinstance(surface, dict) or not isinstance(surface.get("auto_close"), bool):
        raise ContractError("surface_policy.auto_close must be boolean")
    raw_watchdog = meta.get("watchdog_policy")
    if raw_watchdog is None:
        watchdog = dict(DEFAULT_WATCHDOG_POLICY)
    else:
        required_watchdog = {
            "enabled", "poll_seconds", "warn_after_seconds", "alert_after_seconds"
        }
        if not isinstance(raw_watchdog, dict) or set(raw_watchdog) != required_watchdog:
            raise ContractError("watchdog_policy must contain the complete bounded policy")
        watchdog = dict(raw_watchdog)
        if not isinstance(watchdog["enabled"], bool):
            raise ContractError("watchdog_policy.enabled must be boolean")
        for field, lower, upper in (
            ("poll_seconds", 5, 300),
            ("warn_after_seconds", 300, 7200),
            ("alert_after_seconds", 600, 14400),
        ):
            value = watchdog[field]
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ContractError(f"watchdog_policy.{field} must be {lower}..{upper}")
        if watchdog["alert_after_seconds"] <= watchdog["warn_after_seconds"]:
            raise ContractError("watchdog alert must follow the warning threshold")
    if meta.get("forbidden_actions") != FORBIDDEN_ACTIONS:
        raise ContractError("forbidden_actions must match the unattended safety boundary")
    return {
        "version": version,
        "interaction_policy": policy,
        "review_policy": review,
        "reap_policy": reap,
        "surface_policy": surface,
        "watchdog_policy": watchdog,
    }


def normalize_for_runtime(meta: dict[str, Any], worktree: Path) -> dict[str, Any]:
    """Accept an approved plan or its coordinator-prepared final close.

    Long-running task observers remain alive across the atomic ``plan_close``
    transaction.  They may accept that one mutation only when the preparation
    marker binds the exact metadata, summary, plan path, and closed-plan hash.
    All other plan drift remains fail-closed.
    """
    policy = normalize(meta, verify_plan_hash=False)
    if policy["version"] not in {2, 3}:
        return policy
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    approved = str(meta.get("approved_plan_sha256") or "")
    if sha256_file(plan) == approved:
        return policy

    root = worktree.expanduser().resolve()
    try:
        prepared = read_json(root / ".task-reap-prepared.json")
        meta_path = root / ".task-meta.json"
        summary_path = root / ".task-summary.json"
        closed = str(prepared.get("closed_plan_sha256") or "")
        if prepared.get("version") != 1:
            raise ContractError("unsupported reap preparation marker")
        if prepared.get("task_name") != meta.get("task_name"):
            raise ContractError("reap preparation task mismatch")
        prepared_session = str(prepared.get("current_session") or "")
        if meta.get("version") == 3:
            if not v3_session_is_bound(meta, prepared_session):
                raise ContractError("reap preparation session is not bound to the exact v3 task")
        elif prepared_session != meta.get("origin_session"):
            raise ContractError("reap preparation session mismatch")
        if prepared.get("approved_plan_sha256") != approved:
            raise ContractError("reap preparation approval mismatch")
        if prepared.get("meta_sha256") != sha256_file(meta_path):
            raise ContractError("reap preparation metadata mismatch")
        if prepared.get("summary_sha256") != sha256_file(summary_path):
            raise ContractError("reap preparation summary mismatch")
        if Path(str(prepared.get("plan_path") or "")).expanduser().resolve() != plan:
            raise ContractError("reap preparation plan mismatch")
        if len(closed) != 64 or any(char not in "0123456789abcdef" for char in closed):
            raise ContractError("reap preparation closed hash is invalid")
        previous_closed = str(prepared.get("previous_closed_plan_sha256") or "")
        if sha256_file(plan) not in {closed, previous_closed}:
            raise ContractError("reap preparation closed plan mismatch")
    except (ContractError, OSError):
        raise ContractError("approved plan hash changed after dispatch approval") from None
    return policy


def validate_handoff(
    meta: dict[str, Any],
    summary: dict[str, Any],
    current_session: str,
    *,
    verify_plan_hash: bool = True,
) -> dict[str, Any]:
    policy = normalize(meta, verify_plan_hash=verify_plan_hash)
    if policy["interaction_policy"] != "unattended":
        raise ContractError("legacy/interactive task requires user confirmation")
    origin = str(meta.get("origin_session") or "")
    if meta.get("version") == 3:
        if not v3_session_is_bound(meta, current_session):
            raise ContractError("current session is not bound to the exact v3 task")
    elif not origin or not current_session or origin != current_session:
        raise ContractError("origin session mismatch; unattended filing refused")
    reap = policy["reap_policy"]
    if not reap["auto_file"]:
        raise ContractError("automatic filing is disabled")
    if summary.get("type") not in reap["allowed_types"]:
        raise ContractError("summary type is outside the approved reap policy")
    if str(summary.get("title") or "").strip() != reap["title"]:
        raise ContractError("summary title differs from the approved reap target")
    return policy


def review_action(meta: dict[str, Any], review: dict[str, Any], iteration: int) -> str:
    policy = normalize(meta)
    if policy["interaction_policy"] != "unattended":
        return "interactive"
    if iteration < 0:
        raise ContractError("review iteration cannot be negative")
    rp = policy["review_policy"]
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise ContractError("review findings must be an array")
    if review.get("verdict") == "blocked" or any(f.get("severity") == "blocking" for f in findings if isinstance(f, dict)):
        return "escalate"
    if review.get("verdict") == "approve" and not findings:
        return "approve"
    if not findings:
        return "escalate"
    if iteration >= rp["max_verify_iterations"]:
        return "escalate"
    severities = {f.get("severity") for f in findings if isinstance(f, dict)}
    if severities <= set(rp["auto_resolve_severities"]):
        return "resolve"
    return "escalate"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--meta", default=".task-meta.json")
    handoff = sub.add_parser("check-handoff")
    handoff.add_argument("--meta", default=".task-meta.json")
    handoff.add_argument("--summary", default=".task-summary.json")
    handoff.add_argument("--current-session", required=True)
    action = sub.add_parser("review-action")
    action.add_argument("--meta", default=".task-meta.json")
    action.add_argument("--review", required=True)
    action.add_argument("--iteration", type=int, required=True)
    args = parser.parse_args()
    try:
        meta_path = Path(args.meta)
        meta = read_json(meta_path)
        if args.command == "validate":
            result = normalize(meta)
        elif args.command == "check-handoff":
            attention_path = meta_path.expanduser().resolve().parent / ".task-needs-attention.json"
            if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
                raise ContractError("task has an unresolved coordinator escalation")
            result = validate_handoff(meta, read_json(Path(args.summary)), args.current_session)
        else:
            print(review_action(meta, read_json(Path(args.review)), args.iteration))
            return 0
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ContractError as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
