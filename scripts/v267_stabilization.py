#!/usr/bin/env python3
"""2.6.7 stabilization denominator: subject digest, streak, and stop rule.

Read-only release validation with no runtime transition authority.  The
lifecycle subject digest binds the RC1 live streak to behavioral content
(production Harness/dispatch/review code, behavioral runtime config, schemas,
hooks, and behavior-changing skills) instead of raw Git HEAD, so wiki,
ordinary documentation, release evidence, test-only, and user-editor changes
never reset the streak while any behavioral change always does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rc1_live_authority import (
    LiveAuthorityError,
    validate_live_corridor,
    validate_live_non_success,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/v267-stabilization-subject.json"
DEFAULT_MANIFEST = ROOT / "config/acceptance-cells.toml"
HEX_DIGITS = set("0123456789abcdef")

RECEIPT_IDENTITY_FIELDS = ("request_id", "owner_id", "store_id", "worktree_id")
RECEIPT_ROUTE_FIELDS = ("executor_route", "review_route")
RECEIPT_REQUIRED_FIELDS = RECEIPT_IDENTITY_FIELDS + RECEIPT_ROUTE_FIELDS + (
    "schema_version",
    "run_id",
    "sequence",
    "cell_id",
    "corridor",
    "lifecycle_subject_sha256",
    "provider_session_ids",
    "result",
    "material_cycle",
    "resource_free",
    "coordinator_recovery",
)
MATERIAL_CYCLE_ARTIFACT_FIELDS = (
    "findings_artifact",
    "fix_head",
    "refreshed_summary_artifact",
    "second_verification_artifact",
    "re_review_artifact",
)
MATERIAL_CYCLE_FILE_FIELDS = tuple(
    field for field in MATERIAL_CYCLE_ARTIFACT_FIELDS if field != "fix_head"
)
#: Required typed content of each material artifact file: the artifact must
#: declare itself, bind the run's cell, and bind the corrected HEAD.
MATERIAL_ARTIFACT_TYPES = {
    "findings_artifact": "findings",
    "refreshed_summary_artifact": "refreshed-summary",
    "second_verification_artifact": "second-verification",
    "re_review_artifact": "re-review",
}
GIT_OID = set("0123456789abcdef")
EXECUTOR_ROUTE_KEYS = ("runtime", "model", "effort")
REVIEW_ROUTE_KEYS = ("mode", "runtime", "model", "effort")
RC1_CELL_KIND = "engineering-change-corridor"
#: The one supported corridor, including the complete material-cycle branch
#: (findings publication, fix, refreshed summary, second scoped verification,
#: approving re-review) between the first review and reap.
RC1_FULL_CORRIDOR_TRACE = (
    "dispatch",
    "summary",
    "scoped-verify",
    "simple-review",
    "findings",
    "fix",
    "refreshed-summary",
    "scoped-verify-2",
    "re-review-approve",
    "reap",
    "cleanup",
)
DEFECT_REQUIRED_FIELDS = (
    "defect_id",
    "root_cause_class",
    "seam",
    "alias_of",
    "reproducer",
    "durable_pre_state",
    "expected_owner",
    "expected_transition",
    "observed_post_state",
    "effect_ambiguity",
    "focused_regression",
    "disposition",
)


class StabilizationError(ValueError):
    """The denominator, a receipt, or a ledger record is invalid."""


@dataclass(frozen=True)
class SubjectConfig:
    schema_version: int
    release: str
    digest_algorithm: str
    known_defect_seam: str
    release_stop_class_limit: int
    streak_target: int
    excluded_roots: frozenset[str]
    excluded_paths: frozenset[str]
    included_documents: frozenset[str]
    included_document_roots: tuple[str, ...]
    root_markdown_excluded: bool


def load_subject_config(path: Path = DEFAULT_CONFIG) -> SubjectConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StabilizationError(f"cannot load subject config: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise StabilizationError("subject config schema_version must be 1")
    rules = raw.get("subject_rules")
    if not isinstance(rules, dict):
        raise StabilizationError("subject config requires subject_rules")
    for key in ("release", "digest_algorithm", "known_defect_seam"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise StabilizationError(f"subject config requires {key}")
    for key in ("release_stop_class_limit", "streak_target"):
        if type(raw.get(key)) is not int or raw[key] < 1:
            raise StabilizationError(f"subject config requires positive {key}")
    for key in ("excluded_roots", "excluded_paths", "included_documents", "included_document_roots"):
        value = rules.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise StabilizationError(f"subject rules require string list {key}")
    if rules.get("root_markdown_excluded") is not True:
        raise StabilizationError("subject rules must exclude root markdown")
    config = SubjectConfig(
        schema_version=1,
        release=raw["release"],
        digest_algorithm=raw["digest_algorithm"],
        known_defect_seam=raw["known_defect_seam"],
        release_stop_class_limit=raw["release_stop_class_limit"],
        streak_target=raw["streak_target"],
        excluded_roots=frozenset(rules["excluded_roots"]),
        excluded_paths=frozenset(rules["excluded_paths"]),
        included_documents=frozenset(rules["included_documents"]),
        included_document_roots=tuple(rules["included_document_roots"]),
        root_markdown_excluded=True,
    )
    for document in sorted(config.included_documents):
        if document in config.excluded_paths:
            raise StabilizationError(f"contradictory rule for {document}")
    return config


def classify_path(relative: str, config: SubjectConfig) -> bool:
    """True when the tracked path is part of the lifecycle subject."""

    posix = Path(relative).as_posix()
    parts = Path(posix).parts
    if not parts:
        raise StabilizationError("cannot classify an empty path")
    if posix in config.included_documents:
        return True
    if any(posix.startswith(prefix) for prefix in config.included_document_roots):
        return True
    if posix in config.excluded_paths:
        return False
    if parts[0] in config.excluded_roots:
        return False
    if (
        config.root_markdown_excluded
        and len(parts) == 1
        and Path(posix).suffix.casefold() == ".md"
    ):
        return False
    # Unknown paths fail safe: treating them as behavioral can only reset the
    # streak, never silently preserve it across a behavior change.
    return True


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise StabilizationError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def _dirty_subject_paths(root: Path, config: SubjectConfig) -> list[str]:
    dirty: set[str] = set()
    for args in (
        ("diff", "--name-only", "-z", "HEAD", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        for raw in _git(root, *args).split("\0"):
            if raw:
                dirty.add(raw)
    return sorted(
        relative for relative in dirty if classify_path(relative, config)
    )


def subject_entries(root: Path, config: SubjectConfig) -> list[tuple[str, str]]:
    """Sorted (path, blob sha) pairs for every tracked subject path at HEAD."""

    entries: list[tuple[str, str]] = []
    for line in _git(root, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        if not line:
            continue
        meta, _, relative = line.partition("\t")
        if not relative:
            raise StabilizationError("unparseable ls-tree entry")
        if classify_path(relative, config):
            entries.append((relative, meta.split()[2]))
    return sorted(entries)


def lifecycle_subject_sha256(root: Path, config: SubjectConfig) -> str:
    dirty = _dirty_subject_paths(root, config)
    if dirty:
        raise StabilizationError(
            "behavioral subject paths are dirty or untracked: " + ", ".join(dirty)
        )
    digest = hashlib.sha256()
    digest.update(config.digest_algorithm.encode("ascii"))
    digest.update(b"\0")
    for relative, blob in subject_entries(root, config):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(blob.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class RC1GateCell:
    cell_id: str
    sequence: int
    executor: tuple[tuple[str, str], ...]
    review: tuple[tuple[str, str], ...]
    expected: tuple[str, ...]

    @property
    def executor_route(self) -> dict[str, str]:
        return dict(self.executor)

    @property
    def review_route(self) -> dict[str, str]:
        return dict(self.review)


@dataclass(frozen=True)
class RC1Gate:
    schema_version: int
    corridor: str
    streak_target: int
    required_material_cycle_runs: int
    subject_config: str
    evidence_root: str
    cells: tuple[RC1GateCell, ...]

    def cell_by_id(self, cell_id: str) -> RC1GateCell:
        for cell in self.cells:
            if cell.cell_id == cell_id:
                return cell
        raise StabilizationError(f"unknown RC1 cell {cell_id}")


def _route_items(
    cell_id: str, role: str, value: object, keys: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(value, dict)
        or sorted(value) != sorted(keys)
        or not all(isinstance(value[key], str) and value[key] for key in keys)
    ):
        raise StabilizationError(
            f"RC1 cell {cell_id} {role} route requires exactly {'/'.join(keys)}"
        )
    return tuple((key, value[key]) for key in keys)


def load_rc1_gate(manifest_path: Path = DEFAULT_MANIFEST) -> RC1Gate:
    """Load and validate the typed RC1 gate declaration from the manifest."""

    try:
        manifest = tomllib.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StabilizationError(f"cannot load acceptance manifest: {exc}") from exc
    rc1 = manifest.get("rc1")
    if not isinstance(rc1, dict):
        raise StabilizationError("acceptance manifest declares no [rc1] gate")
    if rc1.get("schema_version") != 1:
        raise StabilizationError("RC1 gate schema_version must be 1")
    for key in ("corridor", "subject_config", "evidence_root"):
        if not isinstance(rc1.get(key), str) or not rc1[key]:
            raise StabilizationError(f"RC1 gate requires string {key}")
    for key in ("streak_target", "required_material_cycle_runs"):
        if type(rc1.get(key)) is not int or rc1[key] < 1:
            raise StabilizationError(f"RC1 gate requires positive {key}")
    if rc1["required_material_cycle_runs"] > rc1["streak_target"]:
        raise StabilizationError(
            "RC1 gate cannot require more material runs than its streak target"
        )
    raw_cells = rc1.get("cells")
    if not isinstance(raw_cells, dict) or not raw_cells:
        raise StabilizationError("RC1 gate requires a cells table")
    cells: list[RC1GateCell] = []
    for cell_id, cell in raw_cells.items():
        if not isinstance(cell, dict):
            raise StabilizationError(f"RC1 cell {cell_id} is not a table")
        if cell.get("kind") != RC1_CELL_KIND:
            raise StabilizationError(
                f"RC1 cell {cell_id} must be kind {RC1_CELL_KIND}"
            )
        if type(cell.get("sequence")) is not int or cell["sequence"] < 1:
            raise StabilizationError(
                f"RC1 cell {cell_id} requires a positive sequence"
            )
        if tuple(cell.get("expected", ())) != RC1_FULL_CORRIDOR_TRACE:
            raise StabilizationError(
                f"RC1 cell {cell_id} must declare the complete supported "
                "corridor trace including the material-cycle branch"
            )
        cells.append(
            RC1GateCell(
                cell_id=cell_id,
                sequence=cell["sequence"],
                executor=_route_items(
                    cell_id, "executor", cell.get("executor"), EXECUTOR_ROUTE_KEYS
                ),
                review=_route_items(
                    cell_id, "review", cell.get("review"), REVIEW_ROUTE_KEYS
                ),
                expected=RC1_FULL_CORRIDOR_TRACE,
            )
        )
    cells.sort(key=lambda cell: cell.sequence)
    if [cell.sequence for cell in cells] != list(range(1, len(cells) + 1)):
        raise StabilizationError("RC1 cell sequences must be contiguous from 1")
    if len(cells) != rc1["streak_target"]:
        raise StabilizationError(
            "RC1 gate must declare exactly one cell per streak run"
        )
    return RC1Gate(
        schema_version=1,
        corridor=rc1["corridor"],
        streak_target=rc1["streak_target"],
        required_material_cycle_runs=rc1["required_material_cycle_runs"],
        subject_config=rc1["subject_config"],
        evidence_root=rc1["evidence_root"],
        cells=tuple(cells),
    )


def _validate_evidence_file(
    reference: object, position: int, field: str, *, gate: RC1Gate, root: Path
) -> bytes:
    """The artifact must exist under the evidence root with matching bytes."""

    if (
        not isinstance(reference, dict)
        or sorted(reference) != ["path", "sha256"]
        or not isinstance(reference.get("path"), str)
        or not reference["path"]
        or not isinstance(reference.get("sha256"), str)
    ):
        raise StabilizationError(
            f"receipt {position} material_cycle {field} requires "
            "{path, sha256}"
        )
    declared = reference["sha256"]
    if len(declared) != 64 or not set(declared) <= HEX_DIGITS:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} sha256 is malformed"
        )
    relative = reference["path"]
    parts = Path(relative).parts
    prefix = gate.evidence_root.rstrip("/") + "/"
    if (
        Path(relative).is_absolute()
        or ".." in parts
        or not Path(relative).as_posix().startswith(prefix)
    ):
        raise StabilizationError(
            f"receipt {position} material_cycle {field} must be a relative "
            f"path under {gate.evidence_root}"
        )
    resolved = (Path(root) / relative).resolve()
    evidence_dir = (Path(root) / gate.evidence_root).resolve()
    if not resolved.is_relative_to(evidence_dir):
        raise StabilizationError(
            f"receipt {position} material_cycle {field} escapes the "
            "evidence root"
        )
    if not resolved.is_file() or resolved.is_symlink():
        raise StabilizationError(
            f"receipt {position} material_cycle {field} does not exist as "
            "a durable evidence file"
        )
    payload = resolved.read_bytes()
    if not payload:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} is empty"
        )
    if hashlib.sha256(payload).hexdigest() != declared:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} content does not "
            "match its declared sha256"
        )
    return payload


def _validate_artifact_semantics(
    payload: bytes,
    position: int,
    field: str,
    *,
    cell_id: str,
    fix_head: str,
) -> None:
    """The artifact bytes must be the typed record they claim to be.

    Arbitrary well-hashed bytes are rejected: each artifact must parse as a
    typed JSON record naming its own kind, the run's exact cell, and the
    exact corrected HEAD (`findings` precede the fix, so they bind any full
    commit id, while the refreshed summary, second verification, and
    approving re-review must bind `fix_head`), and the re-review must be an
    approval.
    """

    expected_type = MATERIAL_ARTIFACT_TYPES[field]
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StabilizationError(
            f"receipt {position} material_cycle {field} is not a typed "
            "JSON evidence record"
        ) from None
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} requires a "
            "schema_version 1 evidence record"
        )
    if record.get("type") != expected_type:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} must declare type "
            f"{expected_type}"
        )
    if record.get("cell_id") != cell_id:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} binds the wrong run"
        )
    head = record.get("head_sha")
    if not isinstance(head, str) or len(head) != 40 or not set(head) <= GIT_OID:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} requires a full "
            "head_sha commit binding"
        )
    if field != "findings_artifact" and head != fix_head:
        raise StabilizationError(
            f"receipt {position} material_cycle {field} binds the wrong "
            "corrected HEAD"
        )
    if field == "re_review_artifact" and record.get("verdict") != "approve":
        raise StabilizationError(
            f"receipt {position} material_cycle re-review is not an approval"
        )


def _validate_material_cycle(
    value: object,
    position: int,
    *,
    gate: RC1Gate,
    root: Path,
    cell_id: str,
) -> bool:
    """True when the receipt proves a complete material-finding cycle.

    Evidence is validated, not just named: each artifact must exist as a
    non-empty file contained under the configured evidence root, match its
    declared content hash, and parse as the typed record it claims to be,
    bound to this run's cell and to the exact corrected HEAD.  The live
    corridor authority below establishes whether that HEAD is a real,
    accepted review result; these caller-selected artifacts cannot do so.
    """

    if value is None:
        return False
    if not isinstance(value, dict):
        raise StabilizationError(
            f"receipt {position} material_cycle must be null or an artifact "
            "object; self-asserted flags are rejected"
        )
    if sorted(value) != sorted(MATERIAL_CYCLE_ARTIFACT_FIELDS):
        raise StabilizationError(
            f"receipt {position} material_cycle requires exactly the durable "
            "artifacts: " + ", ".join(MATERIAL_CYCLE_ARTIFACT_FIELDS)
        )
    fix_head = value["fix_head"]
    if (
        not isinstance(fix_head, str)
        or len(fix_head) != 40
        or not set(fix_head) <= GIT_OID
    ):
        raise StabilizationError(
            f"receipt {position} material_cycle fix_head must be a full "
            "40-hex Git object id"
        )
    for field in MATERIAL_CYCLE_FILE_FIELDS:
        payload = _validate_evidence_file(
            value[field], position, field, gate=gate, root=root
        )
        _validate_artifact_semantics(
            payload, position, field, cell_id=cell_id, fix_head=fix_head
        )
    return True


def _validate_live_corridor(
    receipt: dict[str, object], position: int, *, root: Path
) -> tuple[bool, str]:
    try:
        return validate_live_corridor(receipt, position, root=root)
    except LiveAuthorityError as exc:
        raise StabilizationError(str(exc)) from exc


def _validate_live_non_success(
    receipt: dict[str, object], position: int, *, root: Path
) -> None:
    try:
        validate_live_non_success(receipt, position, root=root)
    except LiveAuthorityError as exc:
        raise StabilizationError(str(exc)) from exc


def _validate_receipt(
    receipt: object, position: int, cell: RC1GateCell, gate: RC1Gate
) -> dict[str, object]:
    if not isinstance(receipt, dict):
        raise StabilizationError(f"receipt {position} is not an object")
    for field in RECEIPT_REQUIRED_FIELDS:
        if field not in receipt:
            raise StabilizationError(f"receipt {position} is missing {field}")
    if receipt["schema_version"] != 2:
        raise StabilizationError(
            f"receipt {position} schema_version must be 2; unbound legacy "
            "receipts are rejected"
        )
    if receipt["cell_id"] != cell.cell_id:
        raise StabilizationError(
            f"receipt {position} must bind configured cell {cell.cell_id}, "
            f"not {receipt['cell_id']}"
        )
    if receipt["sequence"] != cell.sequence:
        raise StabilizationError(
            f"receipt {position} sequence must equal configured sequence "
            f"{cell.sequence}"
        )
    if receipt["corridor"] != gate.corridor:
        raise StabilizationError(
            f"receipt {position} must bind the {gate.corridor} corridor"
        )
    if receipt["executor_route"] != cell.executor_route:
        raise StabilizationError(
            f"receipt {position} executor route drifts from the configured "
            f"{cell.cell_id} route"
        )
    if receipt["review_route"] != cell.review_route:
        raise StabilizationError(
            f"receipt {position} review route drifts from the configured "
            f"{cell.cell_id} route"
        )
    digest = receipt["lifecycle_subject_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or not set(digest) <= HEX_DIGITS
    ):
        raise StabilizationError(f"receipt {position} has a malformed subject digest")
    for field in ("run_id",) + RECEIPT_IDENTITY_FIELDS:
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise StabilizationError(f"receipt {position} requires string {field}")
    sessions = receipt["provider_session_ids"]
    if (
        not isinstance(sessions, list)
        or not sessions
        or not all(isinstance(item, str) and item for item in sessions)
    ):
        raise StabilizationError(
            f"receipt {position} requires non-empty provider_session_ids"
        )
    if receipt["result"] not in {"success", "failed", "invalidated"}:
        raise StabilizationError(f"receipt {position} has an unknown result")
    for field in ("resource_free", "coordinator_recovery"):
        if not isinstance(receipt[field], bool):
            raise StabilizationError(f"receipt {position} requires boolean {field}")
    return receipt


def validate_streak(
    receipts: list[object],
    *,
    expected_digest: str,
    config: SubjectConfig,
    gate: RC1Gate,
    root: Path = ROOT,
) -> dict[str, object]:
    """Fold gate-bound run receipts into the current RC1 streak verdict."""

    if (
        not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or not set(expected_digest) <= HEX_DIGITS
    ):
        raise StabilizationError("expected digest must be 64 hex characters")
    if gate.streak_target != config.streak_target:
        raise StabilizationError(
            "RC1 gate streak target disagrees with the stabilization denominator"
        )
    if len(receipts) > len(gate.cells):
        raise StabilizationError(
            "more receipts than configured RC1 cells"
        )
    seen_identities: dict[str, str] = {}
    validated: list[dict[str, object]] = []
    for position, raw in enumerate(receipts, start=1):
        # Receipts bind positionally to the configured cells, so a skipped,
        # reordered, or repeated cell is a hard error, never a silent reset.
        receipt = _validate_receipt(raw, position, gate.cells[position - 1], gate)
        identities = [(field, str(receipt[field])) for field in RECEIPT_IDENTITY_FIELDS]
        identities.extend(
            ("provider_session_id", session)
            for session in receipt["provider_session_ids"]
        )
        for field, value in identities:
            key = f"{field}:{value}"
            if key in seen_identities:
                raise StabilizationError(
                    f"{field} {value} is reused across runs "
                    f"({seen_identities[key]} and {receipt['run_id']})"
                )
            seen_identities[key] = str(receipt["run_id"])
        validated.append(receipt)

    streak = 0
    window: list[bool] = []
    for position, receipt in enumerate(validated, start=1):
        if receipt["result"] != "success":
            if receipt["material_cycle"] is not None:
                raise StabilizationError(
                    f"receipt {position} negative closure cannot claim a "
                    "material cycle"
                )
            _validate_live_non_success(receipt, position, root=root)
            streak = 0
            window = []
            continue
        declared_material = _validate_material_cycle(
            receipt["material_cycle"],
            position,
            gate=gate,
            root=root,
            cell_id=str(receipt["cell_id"]),
        )
        live_material, live_head = _validate_live_corridor(
            receipt, position, root=root
        )
        if declared_material != live_material or (
            declared_material
            and receipt["material_cycle"]["fix_head"] != live_head
        ):
            raise StabilizationError(
                f"receipt {position} material cycle is not live-corridor-derived"
            )
        fresh_success = receipt["resource_free"] is True and (
            receipt["coordinator_recovery"] is False
        )
        if receipt["lifecycle_subject_sha256"] != expected_digest or not fresh_success:
            streak = 0
            window = []
            continue
        streak += 1
        window.append(live_material)
    window = window[-config.streak_target :]
    material_runs = sum(1 for flag in window if flag)
    material_met = material_runs >= gate.required_material_cycle_runs
    complete = streak >= config.streak_target and material_met
    return {
        "schema_version": 1,
        "release": config.release,
        "expected_digest": expected_digest,
        "streak": streak,
        "streak_target": config.streak_target,
        "material_finding_cycle": material_met,
        "complete": complete,
    }


def _validate_defect(record: object, position: int) -> dict[str, object]:
    if not isinstance(record, dict):
        raise StabilizationError(f"defect {position} is not an object")
    for field in DEFECT_REQUIRED_FIELDS:
        if field not in record:
            raise StabilizationError(f"defect {position} is missing {field}")
    for field in DEFECT_REQUIRED_FIELDS:
        value = record[field]
        if field == "alias_of":
            if value is not None and (not isinstance(value, str) or not value):
                raise StabilizationError(
                    f"defect {position} alias_of must be null or a defect id"
                )
            continue
        if not isinstance(value, str) or not value:
            raise StabilizationError(f"defect {position} requires string {field}")
    return record


def release_stop(ledger: object, *, config: SubjectConfig) -> dict[str, object]:
    """Apply the three-independent-class release stop rule to the ledger."""

    if not isinstance(ledger, dict) or ledger.get("schema_version") != 1:
        raise StabilizationError("defect ledger schema_version must be 1")
    raw_defects = ledger.get("defects")
    if not isinstance(raw_defects, list):
        raise StabilizationError("defect ledger requires a defects list")
    records = [
        _validate_defect(record, position)
        for position, record in enumerate(raw_defects, start=1)
    ]
    by_id: dict[str, dict[str, object]] = {}
    for record in records:
        defect_id = str(record["defect_id"])
        if defect_id in by_id:
            raise StabilizationError(f"duplicate defect id {defect_id}")
        by_id[defect_id] = record
    new_classes: set[str] = set()
    for record in records:
        alias_of = record["alias_of"]
        if alias_of is not None:
            canonical = by_id.get(str(alias_of))
            if canonical is None:
                raise StabilizationError(
                    f"defect {record['defect_id']} aliases unknown {alias_of}"
                )
            if canonical["root_cause_class"] != record["root_cause_class"]:
                raise StabilizationError(
                    f"alias {record['defect_id']} disagrees with its root cause class"
                )
            continue
        if record["seam"] == config.known_defect_seam:
            continue
        new_classes.add(str(record["root_cause_class"]))
    stop = len(new_classes) >= config.release_stop_class_limit
    return {
        "schema_version": 1,
        "release": config.release,
        "known_defect_seam": config.known_defect_seam,
        "new_class_count": len(new_classes),
        "new_classes": sorted(new_classes),
        "class_limit": config.release_stop_class_limit,
        "stop": stop,
    }


def _load_json(path: Path) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StabilizationError(f"cannot load {path}: {exc}") from exc


def main() -> int:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    digest = sub.add_parser(
        "digest", parents=[shared], help="print the lifecycle subject digest"
    )
    digest.add_argument("--root", type=Path, default=ROOT)
    classify = sub.add_parser(
        "classify", parents=[shared], help="classify tracked paths"
    )
    classify.add_argument("paths", nargs="+")
    streak = sub.add_parser(
        "streak", parents=[shared], help="validate an RC1 streak receipt list"
    )
    streak.add_argument("--receipts", type=Path, required=True)
    streak.add_argument("--expected-digest", required=True)
    streak.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    streak.add_argument("--root", type=Path, default=ROOT)
    ledger = sub.add_parser(
        "ledger", parents=[shared], help="apply the release stop rule"
    )
    ledger.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_subject_config(args.config)
        if args.command == "digest":
            root = args.root.expanduser().resolve()
            entries = subject_entries(root, config)
            value = {
                "schema_version": 1,
                "release": config.release,
                "digest_algorithm": config.digest_algorithm,
                "lifecycle_subject_sha256": lifecycle_subject_sha256(root, config),
                "subject_path_count": len(entries),
            }
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "classify":
            value = {
                relative: classify_path(relative, config)
                for relative in args.paths
            }
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "streak":
            payload = _load_json(args.receipts)
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise StabilizationError("receipt file schema_version must be 1")
            receipts = payload.get("receipts")
            if not isinstance(receipts, list):
                raise StabilizationError("receipt file requires a receipts list")
            verdict = validate_streak(
                receipts,
                expected_digest=args.expected_digest,
                config=config,
                gate=load_rc1_gate(args.manifest),
                root=args.root.expanduser().resolve(),
            )
            print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
            return 0 if verdict["complete"] else 1
        verdict = release_stop(_load_json(args.ledger), config=config)
        print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
        return 2 if verdict["stop"] else 0
    except StabilizationError as exc:
        print(f"v267-stabilization: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
