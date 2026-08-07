#!/usr/bin/env python3
"""Publish immutable prospective execution receipts for RC3 TDD slices."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
RC2_SHA = "b86a33d779bd8852915a4b875f12ef9a9b7366b3"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SLICE_IDS = frozenset("ABCDEF")
_publish_link = os.link


class ReceiptError(ValueError):
    """Reject invalid, historical, or conflicting receipt publication."""


class Publication(NamedTuple):
    status: str
    path: Path


def _git_ok(root: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True
    ).returncode == 0


def _require_prospective_subject(root: Path, subject_head_sha: object) -> str:
    if not isinstance(subject_head_sha, str) or not SHA.fullmatch(subject_head_sha):
        raise ReceiptError("receipt subject must be an exact Git SHA")
    if not _git_ok(root, "cat-file", "-e", f"{subject_head_sha}^{{commit}}"):
        raise ReceiptError("receipt subject commit is unavailable")
    if subject_head_sha == RC2_SHA or _git_ok(
        root, "merge-base", "--is-ancestor", subject_head_sha, RC2_SHA
    ):
        raise ReceiptError("historical RC2 backfill is forbidden")
    if not _git_ok(root, "merge-base", "--is-ancestor", RC2_SHA, subject_head_sha):
        raise ReceiptError("receipt subject is outside the prospective RC3 lineage")
    return subject_head_sha


def _identity(
    *, slice_id: str, subject_head_sha: str, argv: list[str], profile: str
) -> str:
    raw = json.dumps(
        {
            "slice_id": slice_id,
            "subject_head_sha": subject_head_sha,
            "argv": argv,
            "profile": profile,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _output_record(raw: bytes) -> dict[str, object]:
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def build_receipt(
    root: Path,
    *,
    slice_id: str,
    subject_head_sha: str,
    argv: list[str],
    profile: str,
    exit_status: int,
    stdout: bytes,
    stderr: bytes,
) -> dict[str, object]:
    subject_head_sha = _require_prospective_subject(root, subject_head_sha)
    if slice_id not in SLICE_IDS:
        raise ReceiptError("receipt slice_id must be A-F")
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ReceiptError("receipt argv must be a non-empty string list")
    if not isinstance(profile, str) or not profile.strip():
        raise ReceiptError("receipt profile is required")
    if type(exit_status) is not int or not 0 <= exit_status <= 255:
        raise ReceiptError("receipt exit_status is invalid")
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise ReceiptError("receipt output must be captured as bytes")
    execution_id = _identity(
        slice_id=slice_id,
        subject_head_sha=subject_head_sha,
        argv=argv,
        profile=profile,
    )
    return {
        "schema_version": 1,
        "release": "2.6.6-rc3",
        "execution_id": execution_id,
        "slice_id": slice_id,
        "subject_head_sha": subject_head_sha,
        "argv": list(argv),
        "profile": profile,
        "exit_status": exit_status,
        "stdout": _output_record(stdout),
        "stderr": _output_record(stderr),
    }


def validate_receipt(root: Path, payload: object) -> dict[str, object]:
    required = {
        "schema_version",
        "release",
        "execution_id",
        "slice_id",
        "subject_head_sha",
        "argv",
        "profile",
        "exit_status",
        "stdout",
        "stderr",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ReceiptError("receipt schema fields are invalid")
    subject = _require_prospective_subject(root, payload["subject_head_sha"])
    slice_id = payload["slice_id"]
    argv = payload["argv"]
    profile = payload["profile"]
    if payload["schema_version"] != 1 or payload["release"] != "2.6.6-rc3":
        raise ReceiptError("receipt schema identity is invalid")
    if slice_id not in SLICE_IDS or not isinstance(argv, list) or not argv:
        raise ReceiptError("receipt execution identity is invalid")
    if not all(isinstance(item, str) and item for item in argv):
        raise ReceiptError("receipt argv is invalid")
    if not isinstance(profile, str) or not profile.strip():
        raise ReceiptError("receipt profile is invalid")
    if type(payload["exit_status"]) is not int or not 0 <= payload["exit_status"] <= 255:
        raise ReceiptError("receipt exit status is invalid")
    expected_id = _identity(
        slice_id=slice_id, subject_head_sha=subject, argv=argv, profile=profile
    )
    if payload["execution_id"] != expected_id:
        raise ReceiptError("receipt execution identity drift")
    for stream in ("stdout", "stderr"):
        value = payload[stream]
        if (
            not isinstance(value, dict)
            or set(value) != {"bytes", "sha256"}
            or type(value["bytes"]) is not int
            or value["bytes"] < 0
            or not isinstance(value["sha256"], str)
            or not DIGEST.fullmatch(value["sha256"])
        ):
            raise ReceiptError(f"receipt {stream} metadata is invalid")
    return payload


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def publish_receipt(
    root: Path,
    output_root: Path,
    **values: object,
) -> Publication:
    payload = build_receipt(root, **values)
    validate_receipt(root, payload)
    target = output_root / str(payload["slice_id"]) / str(payload["execution_id"]) / "receipt.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical_bytes(payload)
    if target.exists():
        if target.read_bytes() == raw:
            return Publication("idempotent", target)
        raise ReceiptError("receipt identity conflict")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _publish_link(temporary, target)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                if target.read_bytes() == raw:
                    return Publication("idempotent", target)
                raise ReceiptError("receipt identity conflict") from exc
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return Publication("published", target)


def load_and_validate(root: Path, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt file is unavailable or invalid") from exc
    return validate_receipt(root, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("path", type=Path)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--output-root", type=Path, required=True)
    publish.add_argument("--slice-id", required=True)
    publish.add_argument("--subject-head-sha", required=True)
    publish.add_argument("--argv-json", required=True)
    publish.add_argument("--profile", required=True)
    publish.add_argument("--exit-status", type=int, required=True)
    publish.add_argument("--stdout", type=Path, required=True)
    publish.add_argument("--stderr", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "check":
            load_and_validate(ROOT, args.path)
            print("RC3 slice receipt: valid")
        else:
            argv = json.loads(args.argv_json)
            result = publish_receipt(
                ROOT,
                args.output_root,
                slice_id=args.slice_id,
                subject_head_sha=args.subject_head_sha,
                argv=argv,
                profile=args.profile,
                exit_status=args.exit_status,
                stdout=args.stdout.read_bytes(),
                stderr=args.stderr.read_bytes(),
            )
            print(json.dumps({"status": result.status, "path": str(result.path)}))
    except (ReceiptError, OSError, json.JSONDecodeError) as exc:
        print(f"RC3 slice receipt: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
