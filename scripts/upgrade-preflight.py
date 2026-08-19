#!/usr/bin/env python3
"""Fail-closed overlay-upgrade gate for active sessions and legacy routing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_routing import (
    LOCAL_CONFIG,
    RoutingError,
    load_tracked_config,
    validate_local_config,
)
from harness.contracts import (
    ContractError,
    OwnedResources,
    operation_record_from_dict,
)
from harness.state_machine import TERMINAL


def worktrees(root: Path) -> list[Path]:
    result = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RoutingError("cannot enumerate git worktrees")
    return [Path(line.removeprefix("worktree ")).resolve() for line in result.stdout.splitlines() if line.startswith("worktree ")]


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def same_path(value: Any, path: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).expanduser().resolve() == path
    except OSError:
        return False


def _identity_recovery(
    root: Path,
    classification: str,
    resource: str,
    *,
    owner_id: str,
    operation_id: str,
    worktree: Path | None = None,
) -> dict[str, Any]:
    operation_command = [
        "python3",
        str(root / "scripts/harness-cli.py"),
        "--store",
        str(root / ".vault-meta/harness"),
        "--owner",
        owner_id,
    ]
    inspect_operation = operation_command + ["inspect", operation_id]
    if classification == "active":
        return {
            "action": "finish-or-cancel-exact-operation",
            "inspect_command": inspect_operation,
            "cancel_command": operation_command + ["cancel", operation_id],
            "guidance": (
                "Inspect this exact operation, then finish or cancel it with the "
                "installed runtime and rerun upgrade-preflight."
            ),
        }
    if classification == "proven-stale" and resource == "worktree":
        if worktree is None:
            return {
                "action": "inspect-identity-evidence",
                "guidance": (
                    "Inspect the listed metadata evidence. Do not emit or run a Git "
                    "recovery command without one concrete worktree path."
                ),
            }
        return {
            "action": "inspect-then-remove-exact-worktree",
            "inspect_command": [
                "git", "-C", str(worktree), "status", "--short"
            ],
            "recovery_command": [
                "git", "-C", str(root), "worktree", "remove", str(worktree)
            ],
            "guidance": (
                "Review the exact worktree for useful changes first. If it is clean "
                "and no longer needed, remove only this worktree with Git; this "
                "diagnostic never runs the command."
            ),
        }
    if classification == "proven-stale":
        return {
            "action": "retain-terminal-operation-record",
            "inspect_command": inspect_operation,
            "guidance": (
                "The terminal resource-free operation is evidence, not a cleanup "
                "target. Retain it and recover only its exact stale worktree mirror."
            ),
        }
    if classification == "ambiguous":
        if not owner_id or not operation_id:
            return {
                "action": "inspect-identity-evidence",
                "guidance": (
                    "Inspect the listed metadata evidence. Do not infer a missing "
                    "operation owner or remove the worktree; recover through the "
                    "installed runtime only after the identity is complete."
                ),
            }
        return {
            "action": "inspect-and-reconcile-exact-ownership",
            "inspect_command": inspect_operation,
            "guidance": (
                "Inspect the exact recorded identity and reconcile its pending effect "
                "or owned resources with the installed runtime. Do not remove a "
                "worktree or infer ownership while evidence is incomplete."
            ),
        }
    return {
        "action": "resolve-identity-mismatch",
        "guidance": (
            "Compare the recorded and path-bound identities. Resolve the conflict "
            "through the installed runtime; do not choose an owner or remove either "
            "resource from this diagnostic."
        ),
    }


def _operation_identity_diagnostics(root: Path) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    harness_root = root / ".vault-meta/harness/owners"
    if not harness_root.is_dir():
        return diagnostics
    for operation_path in sorted(harness_root.glob("*/operations/*.json")):
        operation = read_object(operation_path)
        raw_spec = operation.get("spec")
        raw_kind = (
            str(raw_spec.get("kind") or "unknown")
            if isinstance(raw_spec, dict)
            else "unknown"
        )
        raw_operation_id = (
            str(raw_spec.get("operation_id") or operation_path.stem)
            if isinstance(raw_spec, dict)
            else operation_path.stem
        )
        path_owner_id = operation_path.parents[1].name
        path_operation_id = operation_path.stem
        evidence = [operation_path.relative_to(root).as_posix()]
        try:
            record = operation_record_from_dict(operation)
        except (AttributeError, ContractError, TypeError, ValueError):
            identity = {
                "operation_id": raw_operation_id,
                "owner_id": (
                    str(raw_spec.get("owner_id") or "")
                    if isinstance(raw_spec, dict)
                    else ""
                ),
                "path_operation_id": path_operation_id,
                "path_owner_id": path_owner_id,
            }
            diagnostics.append({
                "classification": "ambiguous",
                "resource": "operation",
                "identity": identity,
                "evidence": evidence,
                "blocker": f"harness:{raw_kind}:{raw_operation_id}",
                "recovery": _identity_recovery(
                    root,
                    "ambiguous",
                    "operation",
                    owner_id=path_owner_id,
                    operation_id=path_operation_id,
                ),
            })
            continue
        kind = record.spec.kind
        operation_id = record.spec.operation_id
        owner_id = record.spec.owner_id
        identity = {
            "operation_id": operation_id,
            "owner_id": owner_id,
        }
        if (
            operation_id != path_operation_id
            or owner_id != path_owner_id
            or (kind == "dispatch" and owner_id != operation_id)
        ):
            identity.update({
                "path_operation_id": path_operation_id,
                "path_owner_id": path_owner_id,
            })
            classification = "mismatched"
        elif record.state not in TERMINAL:
            classification = "active"
        elif record.pending_effect or record.resources != OwnedResources():
            classification = "ambiguous"
        elif kind == "dispatch":
            classification = "proven-stale"
        else:
            continue
        row: dict[str, Any] = {
            "classification": classification,
            "resource": "operation",
            "identity": identity,
            "evidence": evidence,
            "recovery": _identity_recovery(
                root,
                classification,
                "operation",
                owner_id=owner_id,
                operation_id=operation_id,
            ),
        }
        if classification != "proven-stale":
            row["blocker"] = f"harness:{kind}:{operation_id}"
        diagnostics.append(row)
    return diagnostics


def identity_diagnostics(root: Path) -> list[dict[str, Any]]:
    diagnostics = _operation_identity_diagnostics(root)
    operations: dict[str, list[dict[str, Any]]] = {}
    for row in diagnostics:
        identity = row["identity"]
        operation_id = identity.get("operation_id")
        if isinstance(operation_id, str) and operation_id:
            operations.setdefault(operation_id, []).append(row)

    for tree in worktrees(root):
        task_path = tree / ".task-meta.json"
        if (
            not task_path.is_file()
            or (tree / ".task-reap-complete.json").is_file()
        ):
            continue
        task = read_object(task_path)
        raw_task_id = task.get("task_id") if task else None
        task_id = str(raw_task_id or "")
        recorded_worktree = task.get("worktree") if task else None
        identity: dict[str, Any] = {
            "task_id": task_id,
            "worktree": str(tree),
        }
        if isinstance(recorded_worktree, str):
            identity["recorded_worktree"] = recorded_worktree
        evidence = [str(task_path)]
        candidates = operations.get(task_id, []) if task_id else []
        exact_candidate = candidates[0] if len(candidates) == 1 else None

        if (
            not task
            or task.get("version") not in {3, 4}
            or not task_id
            or not isinstance(recorded_worktree, str)
        ):
            classification = "ambiguous"
        elif not same_path(recorded_worktree, tree):
            classification = "mismatched"
        elif len(candidates) != 1:
            classification = "ambiguous"
        else:
            classification = str(exact_candidate["classification"])
            evidence.extend(exact_candidate["evidence"])

        owner_id = ""
        operation_id = ""
        if exact_candidate is not None:
            candidate_identity = exact_candidate["identity"]
            identity["operation_identity"] = dict(candidate_identity)
            owner_id = str(candidate_identity.get("owner_id") or task_id)
            operation_id = str(
                candidate_identity.get("operation_id") or task_id
            )
        row = {
            "classification": classification,
            "resource": "worktree",
            "identity": identity,
            "evidence": evidence,
            "recovery": _identity_recovery(
                root,
                classification,
                "worktree",
                owner_id=owner_id,
                operation_id=operation_id,
                worktree=tree,
            ),
        }
        if classification != "proven-stale":
            row["blocker"] = f"task:{tree.name}"
        diagnostics.append(row)

    return sorted(
        diagnostics,
        key=lambda row: (
            str(row["identity"].get("operation_id") or row["identity"].get("task_id") or ""),
            str(row["resource"]),
            str(row["evidence"][0]),
        ),
    )


def identity_diagnostic_packet(root: Path) -> dict[str, Any]:
    diagnostics = identity_diagnostics(root)
    counts = {
        classification: sum(
            row["classification"] == classification for row in diagnostics
        )
        for classification in (
            "active", "proven-stale", "ambiguous", "mismatched"
        )
    }
    attention = counts["active"] + counts["ambiguous"] + counts["mismatched"]
    return {
        "schema_version": 1,
        "status": (
            "attention-required"
            if attention
            else "stale"
            if counts["proven-stale"]
            else "healthy"
        ),
        "read_only": True,
        "root": str(root),
        "counts": counts,
        "diagnostics": diagnostics,
    }


def active_sessions(root: Path) -> list[str]:
    diagnostics = identity_diagnostics(root)
    active = [str(row["blocker"]) for row in diagnostics if "blocker" in row]
    released_dispatches = {
        str(row["identity"]["operation_id"])
        for row in diagnostics
        if row["resource"] == "operation"
        and row["classification"] == "proven-stale"
    }
    for tree in worktrees(root):
        review = read_object(tree / ".review-meta.json")
        if (
            review
            and review.get("archive_status") != "archived"
            and review.get("status") not in {"finish_sent", "finished", "archived"}
        ):
            active.append(f"review:{tree.name}")
    state_root = root / ".vault-meta/research-runs"
    if state_root.is_dir():
        for state_path in sorted(state_root.glob("*/state.json")):
            state = read_object(state_path)
            if not state or state.get("status") in {"complete", "fetch_rejected", "rejected"}:
                continue
            suffix = ":legacy-route" if not isinstance(state.get("routing"), dict) else ""
            active.append(f"research:{state_path.parent.name}{suffix}")
    broker_root = root / ".vault-meta/task-sessions/projects"
    if broker_root.is_dir():
        for task_path in sorted(broker_root.glob("*/tasks/*/task.json")):
            task = read_object(task_path)
            if not task or task.get("status") == "archived":
                continue
            raw_task_id = task.get("task_id")
            task_id = str(raw_task_id or task_path.parent.name)
            worktrees_value = task.get("worktrees")
            released = (
                task.get("schema_version") == 1
                and task.get("status") == "active"
                and raw_task_id == task_path.parent.name
                and task.get("project_id") == task_path.parents[2].name
                and task_id in released_dispatches
                and isinstance(worktrees_value, list)
                and bool(worktrees_value)
                and all(isinstance(value, str) and value for value in worktrees_value)
                and not (task_path.parent / "lanes").exists()
            )
            if not released:
                active.append(f"broker-task:{task_id}")
    return sorted(set(active))


def legacy_routing(root: Path) -> dict[str, dict[str, str]]:
    path = root / ".codex/dispatch-env.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("codex_dispatch", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tracked = load_tracked_config(root)
    result: dict[str, dict[str, str]] = {}
    for runtime in ("codex", "claude"):
        model = data.get(f"{runtime}_review_model")
        effort = data.get(f"{runtime}_review_effort")
        present: dict[str, str] = {}
        if isinstance(model, str):
            present["model"] = model
        if isinstance(effort, str):
            present["effort"] = effort
        defaults = tracked.reviewer_default(runtime)
        legacy_defaults = tracked.legacy_reviewer_default(runtime)
        customized = not (
            all(value == defaults[key] for key, value in present.items())
            or all(value == legacy_defaults[key] for key, value in present.items())
        )
        if present and customized:
            result[runtime] = {}
            if isinstance(model, str):
                result[runtime]["model"] = model
            if isinstance(effort, str):
                result[runtime]["effort"] = effort
    return result


def render_local(values: dict[str, dict[str, str]]) -> str:
    lines = ["# Migrated from .codex/dispatch-env.toml after explicit confirmation."]
    registry: dict[str, str] = {}
    for runtime in ("codex", "claude"):
        if runtime not in values:
            continue
        lines.extend([f"[review_profiles.simple.{runtime}]"])
        for key in ("model", "effort"):
            if key in values[runtime]:
                lines.append(f'{key} = {json.dumps(values[runtime][key])}')
        lines.append("")
        if "model" in values[runtime]:
            registry[values[runtime]["model"]] = runtime
    if registry:
        lines.append("[model_registry]")
        for model, runtime in sorted(registry.items()):
            lines.append(f'{json.dumps(model)} = {json.dumps(runtime)}')
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-routing-migration", action="store_true")
    parser.add_argument("--diagnose-identities", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.diagnose_identities and (
        args.apply or args.confirm_routing_migration
    ):
        parser.error(
            "--diagnose-identities is read-only and cannot be combined with migration options"
        )
    try:
        if args.diagnose_identities:
            packet = identity_diagnostic_packet(root)
            print(json.dumps(packet, sort_keys=True))
            return 4 if packet["status"] == "attention-required" else 0
        running = active_sessions(root)
        if running:
            print(
                "upgrade-preflight: active operations block clean-cut upgrade: "
                + ", ".join(running),
                file=sys.stderr,
            )
            print(
                "Recovery: finish or cancel live operations with the installed runtime; "
                "terminal harness records retaining an effect or owned resource require "
                "exact ownership reconciliation. Then rerun upgrade-preflight.",
                file=sys.stderr,
            )
            return 4
        legacy = legacy_routing(root)
        if legacy:
            if not args.confirm_routing_migration:
                print("upgrade-preflight: legacy custom model routing needs --confirm-routing-migration", file=sys.stderr)
                return 5
            target = root / LOCAL_CONFIG
            if target.exists():
                print(f"upgrade-preflight: refusing to overwrite existing {LOCAL_CONFIG}", file=sys.stderr)
                return 5
            rendered = render_local(legacy)
            validate_local_config(root, rendered)
            if args.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
                try:
                    tmp.write_text(rendered, encoding="utf-8")
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
        print(json.dumps({"status": "ready", "active_sessions": [], "legacy_routing": bool(legacy), "migration_applied": bool(legacy and args.apply)}, sort_keys=True))
        return 0
    except RoutingError as exc:
        print(f"upgrade-preflight: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
