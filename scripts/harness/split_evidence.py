"""Code-owned launch and terminal evidence for activated Split runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from wiki_summary_contract import WikiSummaryError, validate_summary_for_task

from .contracts import ContractError, OwnedResources
from .review_finalization import task_review_status
from .split_activation import (
    CHILD_PLACEMENT,
    SplitLaunchReceipt,
    SplitTerminalReceipt,
    split_child_policy,
    split_child_policy_payload,
)
from .split_join import ChildReceipt
from .state_machine import TERMINAL
from .store import OperationStore, StoreError


MAX_JSON_BYTES = 250_000


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is unavailable")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ContractError(f"{label} must be non-empty and bounded")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value, raw


def _atomic_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    encoded = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if exclusive:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != encoded:
                raise ContractError("immutable Split evidence changed") from None
            return
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git(worktree: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ContractError(f"Split child Git evidence failed: git {' '.join(argv)}")
    return result.stdout.strip()


class SplitEvidenceStore:
    """Seal one activation's dispatch and projected child receipts once."""

    def __init__(
        self,
        vault_root: Path,
        *,
        manifest_sha256: str,
        activation_sha256: str,
    ) -> None:
        self.vault_root = vault_root.expanduser().resolve()
        self.root = (
            self.vault_root
            / ".vault-meta"
            / "harness"
            / "split-operations"
            / manifest_sha256
        )
        self.manifest_sha256 = manifest_sha256
        self.activation_sha256 = activation_sha256

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        _atomic_json(
            self.root / "binding.json",
            {
                "schema_version": 1,
                "manifest_sha256": self.manifest_sha256,
                "activation_sha256": self.activation_sha256,
            },
            exclusive=True,
        )

    def _locked(self):
        class Locked:
            def __init__(inner, path: Path) -> None:
                inner.path = path
                inner.handle = None

            def __enter__(inner):
                self.initialize()
                inner.handle = (inner.path / ".lock").open("a+", encoding="utf-8")
                os.chmod(inner.path / ".lock", 0o600)
                fcntl.flock(inner.handle.fileno(), fcntl.LOCK_EX)
                return inner

            def __exit__(inner, *_args: object) -> None:
                assert inner.handle is not None
                fcntl.flock(inner.handle.fileno(), fcntl.LOCK_UN)
                inner.handle.close()

        return Locked(self.root)

    def _receipt_path(self, kind: str, subplan_id: str) -> Path:
        return self.root / kind / f"{subplan_id}.json"

    def seal_launch(
        self,
        receipt: SplitLaunchReceipt,
        *,
        request_sha256: str,
    ) -> SplitLaunchReceipt:
        payload = {
            "schema_version": 1,
            "request_sha256": request_sha256,
            "receipt": asdict(receipt),
        }
        with self._locked():
            _atomic_json(
                self._receipt_path("launches", receipt.subplan_id),
                payload,
                exclusive=True,
            )
        return receipt

    def launches(
        self, request_sha256_by_id: Mapping[str, str]
    ) -> tuple[SplitLaunchReceipt, ...]:
        directory = self.root / "launches"
        if not directory.is_dir():
            return ()
        receipts: list[SplitLaunchReceipt] = []
        for path in sorted(directory.glob("*.json")):
            value, _raw = _read_object(path, "Split launch evidence")
            subplan_id = path.stem
            receipt_value = value.get("receipt")
            if (
                set(value) != {"schema_version", "request_sha256", "receipt"}
                or value.get("schema_version") != 1
                or value.get("request_sha256")
                != request_sha256_by_id.get(subplan_id)
                or not isinstance(receipt_value, dict)
            ):
                raise ContractError("Split launch evidence identity changed")
            receipt = SplitLaunchReceipt(**receipt_value)
            if (
                receipt.manifest_sha256 != self.manifest_sha256
                or receipt.subplan_id != subplan_id
            ):
                raise ContractError("Split launch evidence changed")
            receipts.append(receipt)
        return tuple(receipts)

    def seal_terminal(
        self, receipt: SplitTerminalReceipt
    ) -> SplitTerminalReceipt:
        with self._locked():
            _atomic_json(
                self._receipt_path("terminals", receipt.child.subplan_id),
                {
                    "schema_version": 1,
                    "receipt": asdict(receipt),
                },
                exclusive=True,
            )
        return receipt

    def terminals(self) -> tuple[SplitTerminalReceipt, ...]:
        directory = self.root / "terminals"
        if not directory.is_dir():
            return ()
        values: list[SplitTerminalReceipt] = []
        for path in sorted(directory.glob("*.json")):
            value, _raw = _read_object(path, "Split terminal evidence")
            receipt_value = value.get("receipt")
            if (
                set(value) != {"schema_version", "receipt"}
                or value.get("schema_version") != 1
                or not isinstance(receipt_value, dict)
            ):
                raise ContractError("Split terminal evidence changed")
            child_value = receipt_value.get("child")
            if not isinstance(child_value, dict):
                raise ContractError("Split terminal child evidence changed")
            receipt = SplitTerminalReceipt(
                child=ChildReceipt(
                    **{
                        **child_value,
                        "evidence_ids": tuple(child_value.get("evidence_ids", ())),
                    }
                ),
                **{
                    key: item
                    for key, item in receipt_value.items()
                    if key != "child"
                },
            )
            if receipt.child.subplan_id != path.stem:
                raise ContractError("Split terminal evidence identity changed")
            values.append(receipt)
        return tuple(values)


class SplitTerminalProjector:
    """Derive approved child completion from durable product and harness facts."""

    def __init__(self, prepared: object, store: SplitEvidenceStore) -> None:
        self.prepared = prepared
        self.store = store

    def _request(self, subplan_id: str) -> Mapping[str, Any]:
        matches = [
            item.request
            for item in self.prepared.children
            if item.subplan_id == subplan_id
        ]
        if len(matches) != 1:
            raise ContractError("Split child request coverage changed")
        return matches[0]

    def project(self, launch: SplitLaunchReceipt) -> SplitTerminalReceipt | None:
        request = self._request(launch.subplan_id)
        worktree = Path(launch.worktree_path).expanduser().resolve()
        if worktree != Path(str(request.get("worktree") or "")).expanduser().resolve():
            raise ContractError("Split child worktree changed after launch")
        meta_path = worktree / ".task-meta.json"
        summary_path = worktree / ".task-summary.json"
        if not meta_path.is_file() or not summary_path.is_file():
            return None
        meta, _meta_raw = _read_object(meta_path, "Split child task metadata")
        summary_raw, summary_bytes = _read_object(
            summary_path, "Split child task summary"
        )
        manifest = self.prepared.activation.validated.manifest
        candidate = next(
            (item for item in manifest.subplans if item.subplan_id == launch.subplan_id),
            None,
        )
        if candidate is None:
            raise ContractError("Split child left the sealed manifest")
        expected_policy = split_child_policy_payload(
            split_child_policy(manifest, candidate)
        )
        expected_branch = str(request.get("branch") or "")
        if (
            meta.get("version") != 4
            or meta.get("task_id") != launch.request_id
            or meta.get("worktree") != str(worktree)
            or meta.get("branch") != expected_branch
            or meta.get("vault_root") != str(self.store.vault_root)
            or meta.get("split_policy") != expected_policy
        ):
            raise ContractError("Split child task contract changed after launch")
        try:
            summary = validate_summary_for_task(
                summary_raw, meta, allow_missing_session=False, require_schema=True
            )
        except WikiSummaryError as exc:
            raise ContractError("Split child summary is invalid") from exc
        summary_sha256 = hashlib.sha256(summary_bytes).hexdigest()
        callback_payload_sha256 = _canonical_sha256(summary)

        if Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
            raise ContractError("Split child Git root changed")
        head = _git(worktree, "rev-parse", "HEAD")
        branch = _git(worktree, "symbolic-ref", "--short", "HEAD")
        if branch != expected_branch:
            raise ContractError("Split child branch changed")
        tracked_status = _git(
            worktree, "status", "--porcelain", "--untracked-files=no"
        )
        if tracked_status:
            return None
        base = str(meta.get("base_branch") or "")
        changed = tuple(
            item
            for item in _git(
                worktree, "diff", "--name-only", f"{base}..{head}"
            ).splitlines()
            if item
        )
        if not changed or not set(changed).issubset(set(candidate.owned_paths)):
            raise ContractError("Split child committed paths exceed exact ownership")

        harness_root = self.store.vault_root / ".vault-meta" / "harness"
        try:
            operations = OperationStore(harness_root).list(launch.request_id)
        except StoreError as exc:
            raise ContractError("Split child harness state is invalid") from exc
        parents = [
            item
            for item in operations
            if item.spec.operation_id == launch.request_id
            and item.spec.kind == "dispatch"
        ]
        if len(parents) != 1:
            raise ContractError("Split child parent operation cardinality changed")
        parent = parents[0]
        if parent.state not in TERMINAL:
            return None
        if (
            parent.state != "complete"
            or parent.resources != OwnedResources()
            or parent.pending_effect
            or parent.accepted_callback_kind != "wiki-summary"
            or parent.accepted_callback_sha256 != callback_payload_sha256
            or any(
                item.state not in TERMINAL
                or item.resources != OwnedResources()
                or item.pending_effect
                for item in operations
            )
        ):
            raise ContractError("Split child harness lifecycle is not resource-free")
        callback_path = (
            harness_root
            / "owners"
            / launch.request_id
            / "runtime"
            / launch.request_id
            / "callback-receipt.json"
        )
        callback, _callback_raw = _read_object(
            callback_path, "Split child accepted callback"
        )
        if callback != {
            "schema_version": 1,
            "status": "accepted",
            "callback_id": parent.accepted_callback_id,
            "operation_id": launch.request_id,
            "run_id": parent.run_id,
            "payload_sha256": callback_payload_sha256,
        }:
            raise ContractError("Split child callback receipt changed")

        review = task_review_status(
            meta,
            worktree,
            expected_vault=self.store.vault_root,
            expected_operation_id=launch.request_id,
        )
        if review.status not in {"approved", "skipped"}:
            return None
        gate_path = review.gate_root / "review-gate.json"
        gate, gate_raw = _read_object(gate_path, "Split child review gate")
        context = gate.get("context")
        if (
            not isinstance(context, dict)
            or context.get("head_sha") != head
            or context.get("implementer_summary_sha256") != summary_sha256
        ):
            raise ContractError("Split child review evidence is stale")

        return self.store.seal_terminal(
            SplitTerminalReceipt(
                child=ChildReceipt(
                    manifest_sha256=manifest.manifest_sha256,
                    subplan_id=launch.subplan_id,
                    branch=branch,
                    head_sha=head,
                    summary_sha256=summary_sha256,
                    review_receipt_sha256=hashlib.sha256(gate_raw).hexdigest(),
                    evidence_ids=candidate.evidence_ids,
                    status="approved",
                ),
                request_id=launch.request_id,
                workspace_id=launch.workspace_id,
                worktree_path=launch.worktree_path,
                executor_placement=CHILD_PLACEMENT,
                review_placement=CHILD_PLACEMENT,
                verification_placement=CHILD_PLACEMENT,
                resources_closed=True,
            )
        )

    def project_all(
        self, launches: tuple[SplitLaunchReceipt, ...]
    ) -> tuple[SplitTerminalReceipt, ...]:
        existing = {item.child.subplan_id: item for item in self.store.terminals()}
        launch_ids = {item.subplan_id for item in launches}
        if not set(existing).issubset(launch_ids):
            raise ContractError("Split terminal evidence has no exact launch")
        for launch in launches:
            projected = self.project(launch)
            if projected is None:
                if launch.subplan_id in existing:
                    raise ContractError("sealed Split terminal evidence became stale")
                continue
            prior = existing.get(launch.subplan_id)
            if prior is not None and prior != projected:
                raise ContractError("sealed Split terminal evidence changed")
            existing[launch.subplan_id] = projected
        manifest_ids = tuple(
            item.subplan_id
            for item in self.prepared.activation.validated.manifest.subplans
        )
        return tuple(existing[item] for item in manifest_ids if item in existing)
