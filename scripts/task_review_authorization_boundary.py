"""Read-only evidence boundary for the one retained fresh-review reconcile."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from verification_receipt import ReceiptError, verify_receipt


_RECONCILE_GATE_SHA256 = (
    "e5aec51f4dd92ec3c922a72e8681150b16e35e34f126c663f910c9c04daf4c30"
)
_FRESH_REVIEW_OPERATION_ID = (
    "b63552c9-60c2-54b3-99c9-093525a65c44-fresh-8016a8aa"
)
_RELEASE_FINAL_ATTEMPT_ID = "26500000-0000-4000-8000-000005afd184"
_RELEASE_FINAL_HEAD = "5afd1841373eecbf38672d1b209a34fd3f20d7ce"
_RELEASE_FINAL_TREE = "7dcd9effadd116f35dd68c083e466a3a6e68d8be"
_RELEASE_FINAL_PROFILE_SHA256 = (
    "804158ff362f6683f29d6a76f858fa8f02912051f6fe31ad2fb39270c2e56067"
)
_RELEASE_FINAL_RECEIPT = Path(
    "docs/acceptance/evidence/v2.6.5/lifecycle-simulator-5afd184/receipt.json"
)


def _git_output(worktree: Path, *argv: str) -> str | None:
    completed = subprocess.run(
        ["git", *argv],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\n")


def authorization_chain_boundary_is_valid(
    worktree: Path,
    authorization: Any,
    *,
    receipt_verifier: Callable[[Path], Mapping[str, object]] = verify_receipt,
) -> bool:
    """Prove the exact clean, unused release boundary without an effect."""

    root = worktree.expanduser().resolve()
    meta_path = root / ".task-meta.json"
    receipt_path = root / _RELEASE_FINAL_RECEIPT
    try:
        if (
            meta_path.is_symlink()
            or not meta_path.is_file()
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            return False
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return False
        task_id = str(meta.get("task_id") or "")
        vault = Path(str(meta.get("vault_root") or "")).expanduser()
        if (
            not task_id
            or task_id != authorization.continuation.dispatch_operation_id
            or Path(str(meta.get("worktree") or "")).expanduser().resolve()
            != root
            or not vault.is_absolute()
        ):
            return False
        vault = vault.resolve()
        gate_path = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / task_id
            / task_id
            / "review-gate.json"
        )
        if gate_path.is_symlink() or not gate_path.is_file():
            return False
        gate_raw = gate_path.read_bytes()
        if hashlib.sha256(gate_raw).hexdigest() != _RECONCILE_GATE_SHA256:
            return False
        gate = json.loads(gate_raw)
        if (
            not isinstance(gate, dict)
            or gate.get("schema_version") != 1
            or gate.get("owner_id") != task_id
            or gate.get("dispatch_operation_id") != task_id
            or gate.get("active_review_operation_id")
            != _FRESH_REVIEW_OPERATION_ID
            or gate.get("status") != "attention-required"
            or gate.get("fresh_reevaluation_used") is not True
        ):
            return False
        sync_path = (
            vault
            / ".vault-meta"
            / "harness"
            / "owners"
            / task_id
            / "runtime"
            / task_id
            / "post-fresh-publication-sync.json"
        )
        if sync_path.exists() or sync_path.is_symlink():
            return False
        receipt = receipt_verifier(receipt_path)
        if (
            receipt.get("attempt_id") != _RELEASE_FINAL_ATTEMPT_ID
            or receipt.get("profile") != "release-final"
            or receipt.get("profile_sha256")
            != _RELEASE_FINAL_PROFILE_SHA256
            or receipt.get("subject_head_sha") != _RELEASE_FINAL_HEAD
            or receipt.get("subject_tree_sha") != _RELEASE_FINAL_TREE
            or receipt.get("execution_relation") != "release-candidate"
            or receipt.get("status") != "passed"
        ):
            return False
    except (
        AttributeError,
        json.JSONDecodeError,
        OSError,
        ReceiptError,
        TypeError,
        ValueError,
    ):
        return False

    head = _git_output(root, "rev-parse", "HEAD")
    if (
        head is None
        or _git_output(
            root, "status", "--porcelain=v1", "--untracked-files=normal"
        )
        != ""
        or _git_output(root, "rev-parse", f"{_RELEASE_FINAL_HEAD}^{{tree}}")
        != _RELEASE_FINAL_TREE
    ):
        return False
    descendant = subprocess.run(
        ["git", "merge-base", "--is-ancestor", _RELEASE_FINAL_HEAD, head],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    receipt_relative = _RELEASE_FINAL_RECEIPT.as_posix()
    return bool(
        descendant.returncode == 0
        and _git_output(root, "rev-parse", f"HEAD:{receipt_relative}")
        == _git_output(root, "hash-object", receipt_relative)
    )
