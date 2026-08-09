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
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/v267-stabilization-subject.json"
HEX_DIGITS = set("0123456789abcdef")

RECEIPT_IDENTITY_FIELDS = ("request_id", "owner_id", "store_id", "worktree_id")
RECEIPT_REQUIRED_FIELDS = RECEIPT_IDENTITY_FIELDS + (
    "schema_version",
    "run_id",
    "sequence",
    "lifecycle_subject_sha256",
    "provider_session_ids",
    "result",
    "material_finding_cycle",
    "resource_free",
    "coordinator_recovery",
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


def _validate_receipt(receipt: object, position: int) -> dict[str, object]:
    if not isinstance(receipt, dict):
        raise StabilizationError(f"receipt {position} is not an object")
    for field in RECEIPT_REQUIRED_FIELDS:
        if field not in receipt:
            raise StabilizationError(f"receipt {position} is missing {field}")
    if receipt["schema_version"] != 1:
        raise StabilizationError(f"receipt {position} schema_version must be 1")
    if type(receipt["sequence"]) is not int or receipt["sequence"] < 1:
        raise StabilizationError(f"receipt {position} requires a positive sequence")
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
    if not isinstance(sessions, list) or not all(
        isinstance(item, str) and item for item in sessions
    ):
        raise StabilizationError(
            f"receipt {position} requires provider_session_ids strings"
        )
    if receipt["result"] not in {"success", "failed", "invalidated"}:
        raise StabilizationError(f"receipt {position} has an unknown result")
    for field in ("material_finding_cycle", "resource_free", "coordinator_recovery"):
        if not isinstance(receipt[field], bool):
            raise StabilizationError(f"receipt {position} requires boolean {field}")
    return receipt


def validate_streak(
    receipts: list[object],
    *,
    expected_digest: str,
    config: SubjectConfig,
) -> dict[str, object]:
    """Fold ordered run receipts into the current RC1 streak verdict."""

    if (
        not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or not set(expected_digest) <= HEX_DIGITS
    ):
        raise StabilizationError("expected digest must be 64 hex characters")
    seen_identities: dict[str, str] = {}
    previous_sequence = 0
    validated: list[dict[str, object]] = []
    for position, raw in enumerate(receipts, start=1):
        receipt = _validate_receipt(raw, position)
        if int(receipt["sequence"]) <= previous_sequence:
            raise StabilizationError("receipts must be strictly sequence ordered")
        previous_sequence = int(receipt["sequence"])
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
    window: list[dict[str, object]] = []
    for receipt in validated:
        fresh_success = (
            receipt["result"] == "success"
            and receipt["resource_free"] is True
            and receipt["coordinator_recovery"] is False
        )
        if receipt["lifecycle_subject_sha256"] != expected_digest or not fresh_success:
            streak = 0
            window = []
            continue
        streak += 1
        window.append(receipt)
    window = window[-config.streak_target :]
    material = any(receipt["material_finding_cycle"] for receipt in window)
    complete = streak >= config.streak_target and material
    return {
        "schema_version": 1,
        "release": config.release,
        "expected_digest": expected_digest,
        "streak": streak,
        "streak_target": config.streak_target,
        "material_finding_cycle": material,
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
