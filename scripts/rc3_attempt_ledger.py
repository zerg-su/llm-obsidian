#!/usr/bin/env python3
"""Append-only, artifact-bound release-final attempt ledger for RC3."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


SHA = re.compile(r"[0-9a-f]{40,64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
AUTHORIZATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# The operator explicitly authorized bounded RC3 closure through attempt eight.
# The external accepted-deviations artifact records and digest-binds each
# extension; a ninth full-profile execution has zero authority.
MAX_ATTEMPTS = 8
LEGACY_MAX_ATTEMPTS = 7
TERMINAL = frozenset({"published", "unpublished", "test-only"})


class AttemptLedgerError(ValueError):
    """The durable attempt ledger is incomplete, mutable, or over budget."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class AttemptLedgerStore:
    """Serialize release attempt reservation and terminal artifact binding."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "attempt-ledger.json"
        self.lock_path = self.root / ".attempt-ledger.lock"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "release": "2.6.6-rc3",
            "maximum_attempts": LEGACY_MAX_ATTEMPTS,
            "attempts": [],
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AttemptLedgerError("attempt ledger is invalid JSON") from exc
        return self._validate(value)

    def _validate(self, value: object) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or set(value)
            not in (
                {"schema_version", "release", "maximum_attempts", "attempts"},
                {
                    "schema_version",
                    "release",
                    "maximum_attempts",
                    "authorizations",
                    "attempts",
                },
            )
            or value.get("schema_version") != 1
            or value.get("release") != "2.6.6-rc3"
            or value.get("maximum_attempts")
            not in {LEGACY_MAX_ATTEMPTS, MAX_ATTEMPTS}
            or not isinstance(value.get("attempts"), list)
        ):
            raise AttemptLedgerError("attempt ledger schema is invalid")
        maximum = value["maximum_attempts"]
        authorizations = value.get("authorizations")
        if maximum == LEGACY_MAX_ATTEMPTS:
            if authorizations is not None:
                raise AttemptLedgerError("legacy attempt ledger has an authorization drift")
        else:
            if not isinstance(authorizations, list) or len(authorizations) != 1:
                raise AttemptLedgerError("attempt extension authorization is missing")
            authorization = authorizations[0]
            if (
                not isinstance(authorization, dict)
                or set(authorization)
                != {
                    "authorization_id",
                    "previous_maximum_attempts",
                    "maximum_attempts",
                    "artifact_pointer",
                    "artifact_sha256",
                }
                or AUTHORIZATION.fullmatch(
                    str(authorization.get("authorization_id") or "")
                )
                is None
                or authorization.get("previous_maximum_attempts")
                != LEGACY_MAX_ATTEMPTS
                or authorization.get("maximum_attempts") != MAX_ATTEMPTS
                or not isinstance(authorization.get("artifact_pointer"), str)
                or Path(authorization["artifact_pointer"]).is_absolute()
                or ".." in Path(authorization["artifact_pointer"]).parts
                or DIGEST.fullmatch(str(authorization.get("artifact_sha256") or ""))
                is None
            ):
                raise AttemptLedgerError("attempt extension authorization is invalid")
            artifact = (self.root / authorization["artifact_pointer"]).resolve()
            if artifact == self.root or self.root not in artifact.parents:
                raise AttemptLedgerError(
                    "attempt authorization is outside ledger root"
                )
            if (
                not artifact.is_file()
                or _sha256(artifact) != authorization["artifact_sha256"]
            ):
                raise AttemptLedgerError("attempt authorization digest drift")
        rows = value["attempts"]
        if len(rows) > maximum:
            raise AttemptLedgerError("ninth full-profile attempt has zero authority")
        ids: set[str] = set()
        for ordinal, row in enumerate(rows, 1):
            if not isinstance(row, dict) or row.get("ordinal") != ordinal:
                raise AttemptLedgerError("attempt ledger ordinals are not gap-free")
            attempt_id = row.get("attempt_id")
            if (
                not isinstance(attempt_id, str)
                or ATTEMPT.fullmatch(attempt_id) is None
                or attempt_id in ids
            ):
                raise AttemptLedgerError("attempt ledger identity is invalid")
            ids.add(attempt_id)
            if (
                SHA.fullmatch(str(row.get("subject_head_sha") or "")) is None
                or DIGEST.fullmatch(str(row.get("profile_sha256") or "")) is None
                or DIGEST.fullmatch(str(row.get("runner_sha256") or "")) is None
                or row.get("execution_relation")
                not in {"release-candidate", "exact-head-reconstruction"}
                or row.get("state") not in TERMINAL | {"reserved"}
            ):
                raise AttemptLedgerError("attempt ledger row is invalid")
            if row["state"] == "reserved":
                if set(row) != {
                    "ordinal",
                    "attempt_id",
                    "subject_head_sha",
                    "profile_sha256",
                    "execution_relation",
                    "runner_sha256",
                    "state",
                }:
                    raise AttemptLedgerError("reserved attempt fields are invalid")
                continue
            required = {
                "ordinal",
                "attempt_id",
                "subject_head_sha",
                "profile_sha256",
                "execution_relation",
                "runner_sha256",
                "state",
                "exit_status",
                "artifact_pointer",
                "artifact_sha256",
            }
            if set(row) != required or type(row["exit_status"]) is not int:
                raise AttemptLedgerError("terminal attempt fields are invalid")
            pointer = row["artifact_pointer"]
            if not isinstance(pointer, str) or Path(pointer).is_absolute():
                raise AttemptLedgerError("attempt artifact pointer is invalid")
            artifact = (self.root / pointer).resolve()
            if self.root not in artifact.parents or not artifact.is_file():
                raise AttemptLedgerError("attempt artifact is unavailable")
            if (
                DIGEST.fullmatch(str(row["artifact_sha256"])) is None
                or _sha256(artifact) != row["artifact_sha256"]
            ):
                raise AttemptLedgerError("attempt artifact digest drift")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        fd, raw = tempfile.mkstemp(prefix=".attempt-ledger.", dir=self.root)
        temp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _locked(self):
        handle = self.lock_path.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def load(self) -> dict[str, Any]:
        with self._locked():
            return copy.deepcopy(self._read())

    def authorize_extension(self, artifact_path: Path) -> dict[str, Any]:
        artifact = artifact_path.expanduser().resolve()
        try:
            pointer = artifact.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise AttemptLedgerError(
                "attempt authorization is outside ledger root"
            ) from exc
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AttemptLedgerError("attempt authorization is invalid JSON") from exc
        expected_fields = {
            "schema_version",
            "release",
            "authorization_id",
            "previous_maximum_attempts",
            "maximum_attempts",
            "authorized_by",
            "finding_ids",
            "rationale",
        }
        finding_ids = payload.get("finding_ids") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_fields
            or payload.get("schema_version") != 1
            or payload.get("release") != "2.6.6-rc3"
            or AUTHORIZATION.fullmatch(str(payload.get("authorization_id") or ""))
            is None
            or payload.get("previous_maximum_attempts") != LEGACY_MAX_ATTEMPTS
            or payload.get("maximum_attempts") != MAX_ATTEMPTS
            or payload.get("authorized_by") != "operator"
            or not isinstance(finding_ids, list)
            or not finding_ids
            or len(finding_ids) != len(set(finding_ids))
            or any(
                not isinstance(item, str) or not item.strip() for item in finding_ids
            )
            or not isinstance(payload.get("rationale"), str)
            or not payload["rationale"].strip()
        ):
            raise AttemptLedgerError("attempt authorization schema is invalid")
        digest = _sha256(artifact)
        authorization = {
            "authorization_id": payload["authorization_id"],
            "previous_maximum_attempts": LEGACY_MAX_ATTEMPTS,
            "maximum_attempts": MAX_ATTEMPTS,
            "artifact_pointer": pointer,
            "artifact_sha256": digest,
        }
        with self._locked():
            value = self._read()
            if value["maximum_attempts"] == MAX_ATTEMPTS:
                if value.get("authorizations") == [authorization]:
                    return copy.deepcopy(value)
                raise AttemptLedgerError("attempt authorization identity drift")
            if (
                value["maximum_attempts"] != LEGACY_MAX_ATTEMPTS
                or len(value["attempts"]) != LEGACY_MAX_ATTEMPTS
                or any(row["state"] == "reserved" for row in value["attempts"])
            ):
                raise AttemptLedgerError(
                    "attempt extension requires seven terminal attempts"
                )
            extended = {
                **value,
                "maximum_attempts": MAX_ATTEMPTS,
                "authorizations": [authorization],
            }
            self._validate(extended)
            self._write(extended)
            return copy.deepcopy(extended)

    def reserve(
        self,
        *,
        attempt_id: str,
        subject_head_sha: str,
        profile_sha256: str,
        execution_relation: str,
        runner_sha256: str,
    ) -> dict[str, Any]:
        with self._locked():
            value = self._read()
            if any(row["state"] == "reserved" for row in value["attempts"]):
                raise AttemptLedgerError("another release attempt is already reserved")
            if any(row["attempt_id"] == attempt_id for row in value["attempts"]):
                raise AttemptLedgerError("attempt identity already exists")
            if (
                value["maximum_attempts"] != MAX_ATTEMPTS
                and len(value["attempts"]) >= LEGACY_MAX_ATTEMPTS
            ):
                raise AttemptLedgerError(
                    "eighth full-profile attempt requires immutable authorization"
                )
            if len(value["attempts"]) >= MAX_ATTEMPTS:
                raise AttemptLedgerError("ninth full-profile attempt has zero authority")
            row = {
                "ordinal": len(value["attempts"]) + 1,
                "attempt_id": attempt_id,
                "subject_head_sha": subject_head_sha,
                "profile_sha256": profile_sha256,
                "execution_relation": execution_relation,
                "runner_sha256": runner_sha256,
                "state": "reserved",
            }
            self._validate({**value, "attempts": [*value["attempts"], row]})
            value["attempts"].append(row)
            self._write(value)
            return copy.deepcopy(row)

    def finalize(
        self,
        *,
        attempt_id: str,
        classification: str,
        exit_status: int,
        artifact_path: Path,
    ) -> dict[str, Any]:
        if classification not in TERMINAL:
            raise AttemptLedgerError("attempt classification is invalid")
        artifact = artifact_path.expanduser().resolve()
        try:
            pointer = artifact.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise AttemptLedgerError("attempt artifact is outside ledger root") from exc
        if not artifact.is_file():
            raise AttemptLedgerError("attempt artifact is unavailable")
        with self._locked():
            value = self._read()
            matches = [
                (index, row)
                for index, row in enumerate(value["attempts"])
                if row["attempt_id"] == attempt_id
            ]
            if len(matches) != 1 or matches[0][1]["state"] != "reserved":
                raise AttemptLedgerError("attempt is not uniquely reserved")
            index, reserved = matches[0]
            row = {
                **reserved,
                "state": classification,
                "exit_status": exit_status,
                "artifact_pointer": pointer,
                "artifact_sha256": _sha256(artifact),
            }
            value["attempts"][index] = row
            self._validate(value)
            self._write(value)
            return copy.deepcopy(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    authorize = sub.add_parser("authorize-extension")
    authorize.add_argument("--root", type=Path, required=True)
    authorize.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = AttemptLedgerStore(args.root).authorize_extension(args.artifact)
    except AttemptLedgerError as exc:
        print(f"RC3 attempt ledger: {exc}")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
