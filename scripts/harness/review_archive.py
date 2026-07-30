#!/usr/bin/env python3
"""Archive an approved review only when its bound evidence is still current."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.contracts import CallbackEnvelope, ContractError
from harness.verification import VerificationError, load_profiles
from review_contract import ReviewContractError, validate_review


class ArchiveError(ValueError):
    pass


def fail(message: str) -> int:
    print(f"review-archive: {message}", file=sys.stderr)
    return 3


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def required_text(value: dict[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ArchiveError(f"review metadata is missing {field}")
    return raw.strip()


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"{path.name} must contain an object")
    return value


def validated_callback(
    operation: Path, meta: dict[str, Any], worktree: Path, vault: Path
) -> tuple[CallbackEnvelope, dict[str, Any]]:
    if meta.get("schema_version") != 1:
        raise ArchiveError("review metadata has an unsupported schema")
    expected_worktree = Path(required_text(meta, "worktree")).expanduser().resolve()
    if expected_worktree != worktree:
        raise ArchiveError("worktree identity mismatch")
    raw_profile = meta.get("verification_profile")
    if not isinstance(raw_profile, dict):
        raise ArchiveError("review metadata is missing verification_profile")
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if current_head.returncode:
        raise ArchiveError("cannot resolve current worktree HEAD")
    if required_text(meta, "head_sha") != current_head.stdout.strip():
        raise ArchiveError("review evidence is stale for the current worktree HEAD")
    try:
        profiles = load_profiles(vault / "config/verification-profiles.toml")
    except (OSError, VerificationError) as exc:
        raise ArchiveError(f"cannot load current verification profiles: {exc}") from exc
    profile_name = str(raw_profile.get("name") or "")
    current_profile = profiles.get(profile_name)
    if (
        current_profile is None
        or str(raw_profile.get("sha256") or "") != current_profile.sha256
    ):
        raise ArchiveError("review evidence is stale for the current verification profile")
    raw = read_object(operation / ".review-callback.json")
    expected_envelope_fields = {
        "schema_version",
        "callback_id",
        "operation_id",
        "run_id",
        "kind",
        "payload",
        "payload_sha256",
    }
    if set(raw) != expected_envelope_fields:
        raise ArchiveError("review callback envelope has invalid fields")
    try:
        envelope = CallbackEnvelope(
            callback_id=raw["callback_id"],
            operation_id=raw["operation_id"],
            run_id=raw["run_id"],
            kind=raw["kind"],
            payload=raw["payload"],
            payload_sha256=raw["payload_sha256"],
            schema_version=raw["schema_version"],
        )
    except (ContractError, KeyError, TypeError) as exc:
        raise ArchiveError(f"review callback envelope is invalid: {exc}") from exc
    operation_id = required_text(meta, "operation_id")
    run_id = required_text(meta, "run_id")
    if (
        envelope.operation_id != operation_id
        or envelope.run_id != run_id
        or envelope.kind != "review"
    ):
        raise ArchiveError("review callback identity does not match the operation")
    try:
        review = validate_review(
            dict(envelope.payload),
            expected_operation_id=operation_id,
            expected_run_id=run_id,
            expected_mode=required_text(meta, "review_mode"),
            expected_head_sha=required_text(meta, "head_sha"),
            expected_profile=str(raw_profile.get("name") or ""),
            expected_profile_sha256=str(raw_profile.get("sha256") or ""),
        )
    except ReviewContractError as exc:
        raise ArchiveError(f"review evidence is invalid: {exc}") from exc
    if review["verdict"] != "approve":
        raise ArchiveError("only an approved review can satisfy final reap")
    return envelope, review


def title_component(value: str) -> str:
    normalized = re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", value)
    normalized = " ".join(normalized.split()).strip(" .-")
    return (normalized or "review")[:100].strip(" .-") or "review"


def render_page(title: str, review_id: str, review: dict[str, Any], address: str) -> str:
    today = date.today().isoformat()
    profile = review["verification_profile"]
    lines = [
        "---",
        "type: review",
        "status: active",
        f"created: {today}",
        f"updated: {today}",
        "tags: [review, harness]",
        "sessions: []",
        f'review_id: "{review_id}"',
        f'address: "{address}"',
        "---",
        "",
        f"# {title}",
        "",
        f"Final verdict: `{review['verdict']}`.",
        "",
        "## Bound evidence",
        "",
        f"- Operation: `{review['operation_id']}`",
        f"- Run: `{review['run_id']}`",
        f"- Mode: `{review['mode']}`",
        f"- HEAD: `{review['head_sha']}`",
        f"- Verification profile: `{profile['name']}` (`{profile['sha256']}`)",
    ]
    for axis in review["axes"]:
        lines.extend(
            [
                "",
                f"## Axis: {axis['axis']}",
                "",
                f"- Verdict: `{axis['verdict']}`",
                f"- Verification iteration: {axis['verification_iteration']}",
                "",
                "### Findings",
                "",
            ]
        )
        if not axis["findings"]:
            lines.append("- None")
        for finding in axis["findings"]:
            location = finding["file"] + (
                f":{finding['line']}" if finding["line"] else ""
            )
            lines.extend(
                [
                    f"- **{finding['finding_id']} · {finding['severity']} · {finding['summary']}**",
                    f"  - File: `{location}`",
                    f"  - Evidence: {finding['evidence']}",
                    f"  - Recommendation: {finding['recommendation']}",
                ]
            )
    for heading, key in (
        ("Verification gaps", "verification_gaps"),
        ("Residual risks", "residual_risks"),
        ("Notes for executor", "notes_for_executor"),
    ):
        lines.extend(["", f"## {heading}", ""])
        values = review[key]
        lines.extend(f"- {value}" for value in values) if values else lines.append("- None")
    lines.extend(
        [
            "",
            "## Archive boundary",
            "",
            "Raw prompts, transcripts, commands, sockets, and cmux/process identifiers are excluded.",
            "",
        ]
    )
    return "\n".join(lines)


def current_archive(
    operation: Path,
    vault: Path,
    expected: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a verified replay result without repeating the vault mutation."""

    marker_path = operation / ".review-archive.json"
    if not marker_path.exists():
        return None
    marker = read_object(marker_path)
    if marker.get("schema_version") != 1 or marker.get("status") not in {
        "archived",
        "already-current",
    }:
        raise ArchiveError("existing review archive marker is invalid")
    for field in (
        "review_id",
        "operation_id",
        "run_id",
        "head_sha",
        "verification_profile",
        "path",
        "title",
        "wikilink",
        "verdict",
        "rounds",
    ):
        if marker.get(field) != expected.get(field):
            raise ArchiveError("existing review archive marker is stale")
    relative = Path(str(marker.get("path") or ""))
    page = (vault / relative).resolve()
    try:
        page.relative_to(vault)
    except ValueError as exc:
        raise ArchiveError("existing review archive page escapes the vault") from exc
    digest = str(marker.get("content_sha256") or "")
    if (
        not page.is_file()
        or page.is_symlink()
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or hashlib.sha256(page.read_bytes()).hexdigest() != digest
    ):
        raise ArchiveError("existing review archive page is unavailable")
    return {**marker, "status": "already-current"}


ARCHIVE_IDENTITY_FIELDS = (
    "review_id",
    "operation_id",
    "run_id",
    "head_sha",
    "verification_profile",
    "path",
    "title",
    "wikilink",
    "verdict",
    "rounds",
)


def prepared_archive(
    operation: Path,
    vault: Path,
    expected: dict[str, Any],
    review: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Persist one stable address/body before the external vault mutation."""

    intent_path = operation / ".review-archive-intent.json"
    if intent_path.exists():
        intent = read_object(intent_path)
        if (
            intent.get("schema_version") != 1
            or intent.get("status") != "prepared"
        ):
            raise ArchiveError("existing review archive intent is invalid")
        for field in ARCHIVE_IDENTITY_FIELDS:
            if intent.get(field) != expected.get(field):
                raise ArchiveError("existing review archive intent is stale")
        address = str(intent.get("address") or "")
        request_id = str(intent.get("request_id") or "")
        if (
            not re.fullmatch(r"c-\d{6}", address)
            or not re.fullmatch(
                r"review-archive:[0-9a-f]{12}:[0-9a-f]{12}",
                request_id,
            )
        ):
            raise ArchiveError("existing review archive intent identity is invalid")
        body = render_page(
            str(intent["title"]),
            str(intent["review_id"]),
            review,
            address,
        )
        if hashlib.sha256(body.encode()).hexdigest() != intent.get(
            "content_sha256"
        ):
            raise ArchiveError("existing review archive intent content drifted")
        return intent, body

    address = subprocess.run(
        [str(vault / "scripts/allocate-address.sh")],
        cwd=vault,
        text=True,
        capture_output=True,
        check=False,
    )
    if address.returncode:
        raise ArchiveError("address allocation failed")
    allocated = address.stdout.strip()
    if not re.fullmatch(r"c-\d{6}", allocated):
        raise ArchiveError("address allocation returned an invalid address")
    body = render_page(
        str(expected["title"]),
        str(expected["review_id"]),
        review,
        allocated,
    )
    digest = hashlib.sha256(body.encode()).hexdigest()
    short = hashlib.sha256(str(expected["review_id"]).encode()).hexdigest()[:12]
    intent = {
        **expected,
        "status": "prepared",
        "address": allocated,
        "content_sha256": digest,
        "request_id": f"review-archive:{short}:{digest[:12]}",
    }
    atomic_json(intent_path, intent)
    return intent, body


def committed_archive(vault: Path, intent: dict[str, Any]) -> bool:
    """Prove whether the stable intended page already committed."""

    relative = Path(str(intent.get("path") or ""))
    page = (vault / relative).resolve()
    try:
        page.relative_to(vault)
    except ValueError as exc:
        raise ArchiveError("review archive intent page escapes the vault") from exc
    if not page.exists():
        return False
    digest = str(intent.get("content_sha256") or "")
    if (
        not page.is_file()
        or page.is_symlink()
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or hashlib.sha256(page.read_bytes()).hexdigest() != digest
    ):
        raise ArchiveError("committed review archive conflicts with its intent")
    return True


def archived_result(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "archived",
        **{field: intent[field] for field in ARCHIVE_IDENTITY_FIELDS},
        "content_sha256": intent["content_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--operation-dir", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    worktree = args.worktree.expanduser().resolve()
    operation = args.operation_dir.expanduser().resolve()
    vault = args.vault_root.expanduser().resolve()
    try:
        meta = read_object(operation / ".review-meta.json")
        _envelope, review = validated_callback(operation, meta, worktree, vault)
        operation_id = review["operation_id"]
        review_id = str(meta.get("review_id") or operation_id)
        if review_id != operation_id:
            raise ArchiveError("review identity does not match the operation")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", review_id):
            raise ArchiveError("review identity is invalid")
        task = title_component(str(meta.get("task_name") or worktree.name))
        short = hashlib.sha256(review_id.encode()).hexdigest()[:12]
        title = f"Cross-model review — {task} — {short}"
        relative = Path("wiki/meta/reviews") / f"{title}.md"
        body = render_page(title, review_id, review, "c-000001")
        rounds = 1 + max(
            axis["verification_iteration"] for axis in review["axes"]
        )
        result = {
            "schema_version": 1,
            "status": "dry-run" if args.dry_run else "archived",
            "review_id": review_id,
            "operation_id": operation_id,
            "run_id": review["run_id"],
            "head_sha": review["head_sha"],
            "verification_profile": review["verification_profile"],
            "path": relative.as_posix(),
            "title": title,
            "wikilink": f"[[{title}]]",
            "verdict": review["verdict"],
            "rounds": rounds,
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
        if not args.dry_run:
            replay = current_archive(operation, vault, result)
            if replay is not None:
                (operation / ".review-archive-intent.json").unlink(
                    missing_ok=True
                )
                print(
                    json.dumps(replay, ensure_ascii=False, sort_keys=True)
                    if args.json
                    else replay["wikilink"]
                )
                return 0
            intent, body = prepared_archive(
                operation,
                vault,
                result,
                review,
            )
            if not committed_archive(vault, intent):
                writer = subprocess.run(
                    [
                        sys.executable,
                        str(vault / "scripts/vault-write.py"),
                        "--output",
                        "json",
                    ],
                    cwd=vault,
                    input=json.dumps(
                        {
                            "schema_version": 1,
                            "request_id": intent["request_id"],
                            "actor": "harness-review-archive",
                            "session": "unknown",
                            "pages": [
                                {
                                    "op": "create",
                                    "path": relative.as_posix(),
                                    "content": body,
                                }
                            ],
                        }
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if writer.returncode:
                    raise ArchiveError(
                        (writer.stderr or writer.stdout).strip()
                        or "vault write failed"
                    )
                if not committed_archive(vault, intent):
                    raise ArchiveError(
                        "vault write returned without the intended review page"
                    )
            result = archived_result(intent)
            atomic_json(operation / ".review-archive.json", result)
            (operation / ".review-archive-intent.json").unlink()
        print(
            json.dumps(result, ensure_ascii=False, sort_keys=True)
            if args.json
            else result["wikilink"]
        )
        return 0
    except (ArchiveError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
