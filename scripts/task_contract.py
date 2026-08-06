#!/usr/bin/env python3
"""Validate dispatch task policies and unattended review/reap gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn

from review_contract import MATERIAL_SEVERITIES, SEVERITIES
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_escalation_records import EscalationRecordError, load_attention


SUMMARY_TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
REVIEW_MODES = {"simple", "deep", "full", "skip"}
REVIEW_VERIFY_BUDGETS = {"simple": 1, "deep": 2, "full": 2, "skip": 0}
REVIEW_POLICY_V4_FIELDS = {
    "mode",
    "cross_model",
    "runtime",
    "model",
    "effort",
    "max_verify_iterations",
    "verification_profile",
    "verification_profile_sha256",
}
REVIEW_POLICY_V3_FIELDS = {
    *REVIEW_POLICY_V4_FIELDS,
    "auto_resolve_severities",
    "escalate_severities",
}
V3_META_FIELDS = {
    "version",
    "project_id",
    "task_id",
    "task_name",
    "wiki_runtime",
    "executor_runtime",
    "runtime",
    "origin_session",
    "spawned_at",
    "wiki_surface",
    "wiki_surface_ref",
    "task_surface",
    "task_surface_ref",
    "task_workspace",
    "task_workspace_ref",
    "task_window",
    "task_window_ref",
    "worktree",
    "target_repo",
    "vault_root",
    "branch",
    "base_branch",
    "codex_home",
    "codex_profile",
    "wiki_reap_command",
    "review_skill",
    "routing",
    "plan_file",
    "approved_plan_sha256",
    "interaction_policy",
    "pipeline_policy",
    "review_policy",
    "reap_policy",
    "surface_policy",
    "watchdog_policy",
    "forbidden_actions",
    "suggested_agents",
    "model",
    "effort",
}
V4_META_FIELDS = V3_META_FIELDS | {
    "outcome_contract_sha256",
    "finalization_policy",
    "split_policy",
    "base_sha",
}
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
    """Keep the historical name while validating exact v3/v4 task bindings."""

    if meta.get("version") not in {3, 4} or not session_id:
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


def _validate_pipeline_policy(meta: dict[str, Any], version: int) -> None:
    if version in {3, 4}:
        pipeline = meta.get("pipeline_policy")
        base_pipeline_fields = {
            "name",
            "definition_sha256",
            "completion_policy",
            "total_pass_limit",
        }
        if not isinstance(pipeline, dict) or frozenset(pipeline) not in {
            frozenset(base_pipeline_fields),
            frozenset(base_pipeline_fields | {"source", "baseline"}),
        }:
            raise ContractError(
                f"v{version} pipeline_policy must contain the complete compiled selection"
            )
        name = pipeline.get("name")
        completion = pipeline.get("completion_policy")
        pass_limit = pipeline.get("total_pass_limit")
        definition_sha256 = pipeline.get("definition_sha256")
        if name not in {
            "lifecycle/default",
            "engineering/change",
            "engineering/fix",
            "custom",
        }:
            raise ContractError(f"v{version} pipeline_policy.name is invalid")
        if (
            not isinstance(definition_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", definition_sha256)
        ):
            raise ContractError(
                f"v{version} pipeline_policy.definition_sha256 is invalid"
            )
        if completion not in {"attention", "autonomous"}:
            raise ContractError(
                f"v{version} pipeline_policy.completion_policy is invalid"
            )
        expected_limit = {
            "attention": 2,
            "autonomous": 3,
        }[completion]
        if pass_limit != expected_limit:
            raise ContractError(
                f"v{version} pipeline_policy total_pass_limit mismatches completion_policy"
            )
        if name == "custom":
            if (
                pipeline.get("source") != "custom"
                or pipeline.get("baseline")
                not in {
                    "lifecycle/default",
                    "engineering/change",
                    "engineering/fix",
                }
            ):
                raise ContractError(
                    f"v{version} custom pipeline requires its exact source and baseline"
                )
        elif set(pipeline) != base_pipeline_fields:
            raise ContractError(
                f"v{version} built-in pipeline cannot carry custom source metadata"
            )
        if completion == "autonomous" and name not in {
            "engineering/fix",
            "custom",
        }:
            raise ContractError(
                f"v{version} autonomous completion requires engineering/fix or custom"
            )


def _validate_review_policy(meta: dict[str, Any], version: int) -> dict[str, Any]:
    review = meta.get("review_policy")
    if not isinstance(review, dict) or review.get("mode") not in REVIEW_MODES:
        raise ContractError(
            "review_policy.mode must be simple, deep, full, or skip"
        )
    expected_review_fields = (
        REVIEW_POLICY_V3_FIELDS if version == 3 else REVIEW_POLICY_V4_FIELDS
    )
    if version in {3, 4} and set(review) != expected_review_fields:
        raise ContractError(
            f"v{version} review_policy must contain the complete deterministic preset"
        )
    mode = review["mode"]
    max_verify = review.get("max_verify_iterations")
    if isinstance(max_verify, bool) or not isinstance(max_verify, int) or not 0 <= max_verify <= 5:
        raise ContractError("review_policy.max_verify_iterations must be 0..5")
    if version in {3, 4}:
        expected_budget = REVIEW_VERIFY_BUDGETS[mode]
        if max_verify != expected_budget:
            raise ContractError(
                f"v{version} {mode} review requires exactly {expected_budget} "
                "verification iteration(s)"
            )
        cross_model = review.get("cross_model")
        runtime = review.get("runtime")
        model = review.get("model")
        effort = review.get("effort")
        if not isinstance(cross_model, bool):
            raise ContractError("review_policy.cross_model must be boolean")
        if runtime not in {"", "claude", "codex"}:
            raise ContractError(
                "review_policy.runtime must be empty, claude, or codex"
            )
        if not isinstance(model, str) or (
            model
            and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model
            )
        ):
            raise ContractError(
                "review_policy.model must be empty or a bounded alias"
            )
        if effort not in {
            "",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ContractError("review_policy.effort is invalid")
        if mode == "skip" and any(
            (cross_model, runtime, model, effort)
        ):
            raise ContractError(
                "skip review cannot carry expert overrides"
            )
        if mode == "full" and (runtime or model):
            raise ContractError(
                "full review requires both providers; use deep for a "
                "single-model review"
            )
        profile = review.get("verification_profile")
        digest = review.get("verification_profile_sha256")
        if not isinstance(profile, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", profile
        ):
            raise ContractError(
                "review_policy.verification_profile is invalid"
            )
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise ContractError(
                "review_policy.verification_profile_sha256 must be a sha256"
            )
    if version in {2, 3}:
        auto = review.get("auto_resolve_severities")
        escalate = review.get("escalate_severities")
        if not isinstance(auto, list) or any(
            x not in {"warning", "nit"} for x in auto
        ):
            raise ContractError(
                "auto_resolve_severities may contain warning and nit"
            )
        if len(auto) != len(set(auto)):
            raise ContractError("auto_resolve_severities must be unique")
        if escalate != ["blocking"]:
            raise ContractError("blocking must be the sole escalate severity")
    return review


def _validate_finalization_policy(
    meta: dict[str, Any], version: int
) -> dict[str, Any] | None:
    raw = meta.get("finalization_policy")
    if raw is None:
        return None
    if version != 4:
        raise ContractError("finalization_policy requires v4 task metadata")
    from harness.finalization_policy import (
        FinalizationPolicyError,
        finalization_policy_payload,
        parse_finalization_policy,
        require_registered_finalization_routes,
    )

    try:
        policy = parse_finalization_policy(raw)
        require_registered_finalization_routes(policy)
        return finalization_policy_payload(policy)
    except FinalizationPolicyError as exc:
        raise ContractError(str(exc)) from exc


def _finalization_projection(
    finalization: dict[str, Any] | None,
) -> dict[str, Any]:
    return (
        {}
        if finalization is None
        else {"finalization_policy": finalization}
    )


def _validate_split_policy(
    meta: dict[str, Any], version: int
) -> dict[str, object] | None:
    raw = meta.get("split_policy")
    if raw is None:
        return None
    if version != 4:
        raise ContractError("split_policy requires v4 task metadata")
    from harness.contracts import ContractError as HarnessContractError
    from harness.split_activation import (
        parse_split_child_policy,
        split_child_policy_payload,
    )

    try:
        policy = split_child_policy_payload(parse_split_child_policy(raw))
    except HarnessContractError as exc:
        raise ContractError(str(exc)) from exc
    surface = meta.get("surface_policy")
    if not isinstance(surface, dict) or surface.get("placement") != "workspace":
        raise ContractError("split_policy requires child workspace placement")
    if policy.get("base_sha") != meta.get("base_sha"):
        raise ContractError("split_policy base_sha differs from task base_sha")
    return policy


def _split_projection(
    split_policy: dict[str, object] | None,
) -> dict[str, object]:
    return {} if split_policy is None else {"split_policy": split_policy}


def _validate_base_sha(meta: dict[str, Any]) -> str | None:
    base_sha = meta.get("base_sha")
    if base_sha is None:
        return None
    if not isinstance(base_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha
    ):
        raise ContractError("v4 metadata base_sha must be an exact lowercase commit")
    return base_sha


def _v4_projection(
    version: int,
    outcome_digest: str,
    base_sha: str | None,
    finalization: dict[str, Any] | None,
    split_policy: dict[str, object] | None,
) -> dict[str, Any]:
    if version != 4:
        return {}
    result: dict[str, Any] = {"outcome_contract_sha256": outcome_digest}
    if base_sha is not None:
        result["base_sha"] = base_sha
    result.update(_finalization_projection(finalization))
    result.update(_split_projection(split_policy))
    return result


def normalize(meta: dict[str, Any], *, verify_plan_hash: bool = True) -> dict[str, Any]:
    version = meta.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractError("task metadata version must be an integer")
    if version == 1:
        return {
            "version": 1,
            "interaction_policy": "interactive",
            "review_policy": {
                "mode": "deep",
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
    if version not in {2, 3, 4}:
        raise ContractError(f"unsupported task metadata version: {version!r}")

    if version in {3, 4}:
        expected_meta_fields = V4_META_FIELDS if version == 4 else V3_META_FIELDS
        unknown = set(meta) - expected_meta_fields
        if unknown:
            raise ContractError(
                f"v{version} task metadata has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        for field in ("project_id", "task_id"):
            value = meta.get(field)
            try:
                normalized = str(uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                raise ContractError(f"v{version} {field} must be a UUID") from None
            if normalized != value:
                raise ContractError(
                    f"v{version} {field} must be a canonical lowercase UUID"
                )

    for field in ("task_name", "origin_session"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise ContractError(f"{field} must be a non-empty string")
    if meta.get("executor_runtime") not in {"claude", "codex"}:
        raise ContractError("executor_runtime must be claude or codex")

    policy = meta.get("interaction_policy")
    if policy not in {"interactive", "unattended"}:
        raise ContractError("interaction_policy must be interactive or unattended")
    _validate_pipeline_policy(meta, version)
    plan_value = meta.get("plan_file")
    hash_value = meta.get("approved_plan_sha256")
    plan_raw = plan_value.strip() if isinstance(plan_value, str) else ""
    plan_hash = hash_value.strip() if isinstance(hash_value, str) else ""
    if not plan_raw or len(plan_hash) != 64 or any(c not in "0123456789abcdef" for c in plan_hash):
        raise ContractError(f"v{version} metadata requires plan_file and lowercase approved_plan_sha256")
    plan = Path(plan_raw).expanduser().resolve()
    if not plan.is_file():
        raise ContractError(f"approved plan is missing: {plan}")
    outcome_digest = ""
    if version == 4:
        raw_outcome_digest = meta.get("outcome_contract_sha256")
        if not isinstance(raw_outcome_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", raw_outcome_digest
        ):
            raise ContractError(
                "v4 metadata requires lowercase outcome_contract_sha256"
            )
        try:
            current_outcome_digest = extract_from_bytes(plan.read_bytes()).sha256
        except (OSError, OutcomeContractError) as exc:
            raise ContractError(f"approved plan Outcome Contract is invalid: {exc}") from exc
        if current_outcome_digest != raw_outcome_digest:
            raise ContractError(
                "approved plan Outcome Contract digest changed after dispatch approval"
            )
        outcome_digest = raw_outcome_digest
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

    review = _validate_review_policy(meta, version)
    finalization = _validate_finalization_policy(meta, version)
    split_policy = _validate_split_policy(meta, version)
    base_sha = _validate_base_sha(meta)

    reap = meta.get("reap_policy")
    if not isinstance(reap, dict):
        raise ContractError("reap_policy must be an object")
    allowed = reap.get("allowed_types")
    title_value = reap.get("title")
    title = title_value.strip() if isinstance(title_value, str) else ""
    if reap.get("mode") not in {"final", "shared"} or not isinstance(reap.get("auto_file"), bool):
        raise ContractError(
            "reap_policy requires final/shared mode and boolean auto_file"
        )
    if not isinstance(allowed, list) or len(allowed) != 1 or allowed[0] not in SUMMARY_TYPES:
        raise ContractError("reap_policy.allowed_types must contain exactly one known type")
    if not title:
        raise ContractError("reap_policy.title is required")

    surface = meta.get("surface_policy")
    if not isinstance(surface, dict) or not isinstance(surface.get("auto_close"), bool):
        raise ContractError("surface_policy.auto_close must be boolean")
    placement = str(surface.get("placement") or "split")
    if placement not in {"split", "workspace"}:
        raise ContractError("surface_policy.placement must be split or workspace")
    surface = {**surface, "placement": placement}
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
    result = {
        "version": version,
        "interaction_policy": policy,
        "review_policy": review,
        "reap_policy": reap,
        "surface_policy": surface,
        "watchdog_policy": watchdog,
    }
    result.update(
        _v4_projection(
            version, outcome_digest, base_sha, finalization, split_policy
        )
    )
    return result


def normalize_for_runtime(meta: dict[str, Any], worktree: Path) -> dict[str, Any]:
    """Accept an approved plan or its coordinator-prepared final close.

    Long-running task observers remain alive across the atomic ``plan_close``
    transaction.  They may accept that one mutation only when the preparation
    marker binds the exact metadata, summary, plan path, and closed-plan hash.
    All other plan drift remains fail-closed.
    """
    policy = normalize(meta, verify_plan_hash=False)
    if policy["version"] not in {2, 3, 4}:
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
        if meta.get("version") in {3, 4}:
            if not v3_session_is_bound(meta, prepared_session):
                raise ContractError(
                    "reap preparation session is not bound to the exact task"
                )
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
    expected_summary_version = 2 if policy["version"] == 4 else 1
    if summary.get("schema_version", 1) != expected_summary_version:
        raise ContractError(
            f"v{policy['version']} task requires Wiki Summary v{expected_summary_version}"
        )
    if policy["interaction_policy"] != "unattended":
        raise ContractError("legacy/interactive task requires user confirmation")
    origin = str(meta.get("origin_session") or "")
    if meta.get("version") in {3, 4}:
        if not v3_session_is_bound(meta, current_session):
            raise ContractError("current session is not bound to the exact task")
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
    if review.get("verdict") == "blocked":
        return "escalate"
    if review.get("verdict") == "approve" and not findings:
        return "approve"
    if not findings:
        return "escalate"
    if iteration >= rp["max_verify_iterations"]:
        return "escalate"
    severities = {f.get("severity") for f in findings if isinstance(f, dict)}
    if policy["version"] == 4:
        if not severities <= set(SEVERITIES):
            return "escalate"
        return (
            "resolve"
            if severities & MATERIAL_SEVERITIES
            else "approve"
        )
    if "blocking" in severities:
        return "escalate"
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
            try:
                attention = load_attention(meta_path.expanduser().resolve().parent)
            except EscalationRecordError as exc:
                raise ContractError(f"invalid task escalation record: {exc}") from exc
            if attention is not None and attention.get("status") != "resolved":
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
