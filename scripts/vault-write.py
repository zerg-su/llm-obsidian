#!/usr/bin/env python3
"""Transactional vault mutation dispatcher.

The model generates ONE JSON payload per save-operation; this script fans it
out to wiki/log.md and wiki/hot.md with hard cap enforcement. Replaces the
old multi-Edit choreography (page -> index -> log -> hot) where each edit was
a drift point and caps depended on model discipline.

Payload (stdin or --file), all mutation keys optional:

    {
      "schema_version": 1,
      "request_id": "optional-idempotency/correlation-id",
      "log_entry":     "## [YYYY-MM-DD] verb | Title\\n- body...",
      "hot_bullet":    "YYYY-MM-DD: [[Page]] — one-liner (`c-NNNNNN`)",
      "hot_recent_remove_addresses": ["c-NNNNNN"],
      "hot_narrative": "replaces ## Last Updated body, <=120 words",
      "hot_threads":   {"add": ["- **Open**: ..."], "resolve": ["substring"]},
      "plan_close":    {"file": "wiki/plans/<name>.md",
                        "result_link": "[[Title]]",
                        "exec_session": "<id>|null",
                        "expected_sha256": "<approved-plan-hash>|null"},
      "pages": [
        {"op": "create", "path": "wiki/concepts/New.md", "content": "..."},
        {"op": "update", "path": "wiki/index.md", "content": "...",
         "expected_sha256": "<hash of current file>"},
        {"op": "delete", "path": "wiki/concepts/Disposable.md",
         "expected_sha256": "<hash of current file>"}
      ],
      "moves": [
        {"from": "wiki/old.md", "to": "wiki/New.md",
         "expected_sha256": "<hash of source file>"}
      ],
      "manifest_update": {"path": ".raw/.manifest.json",
                           "expected_sha256": "<hash>",
                           "merge": {"address_map": {"wiki/...": "c-000001"}}},
      "actor": "save|ingest|reap|hook|...",
      "session": "<runtime session id>"
    }

plan_close (reap final): strict lifecycle close of a plan page. Preconditions
(file inside wiki/plans/, single status line, status == pending) violated ->
exit 2, nothing written. Applies: status -> executed, updated bump, executor
session appended to sessions: (plan-capture format), 'Результат: <link>'
line appended to body.

Ownership contract for hot.md sections:
  - ## Recent Changes    — THIS SCRIPT (prepend bullet, evict >15, truncate essence only)
  - ## Last Updated      — model via hot_narrative (cap 120 words, FAIL if over)
  - ## Active Threads    — model via hot_threads (cap 8, evict oldest entries over cap)
  - ## Key Recent Facts  — model-curated durable facts; script never touches it

All file contents are built and validated before mutation. A durable journal
makes a multi-file operation recoverable by roll-forward after process death;
normal validation/conflict failures write nothing.

Usage:
  echo '{"hot_bullet": "YYYY-MM-DD: [[Page]] — essence (`c-NNNNNN`)"}' | ./scripts/vault-write.py
  ./scripts/vault-write.py --file payload.json [--dry-run]
  ./scripts/vault-write.py --file payload.json --output json
  ./scripts/vault-write.py --sha256 wiki/path.md
  ./scripts/vault-write.py --recover

Exit codes: 0 ok, 1 lock/io failure, 2 invariant/cap violation, 3 bad payload,
4 optimistic-concurrency conflict.

`--output json` returns the stable v1 response contract from
schemas/vault-write-response-v1.schema.json. Existing text output remains the
default for shell and skill compatibility.
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import IO

from pipeline_events import emit_event
from vault_write_contract import (
    CapViolation,
    ConflictError,
    PayloadError,
    safe_repo_path as _safe_repo_path,
    sha256_text,
)
from vault_write_mutations import (
    HOT_MUTATION_KEYS,
    MutationPlan,
    MutationPlanner,
    deep_merge,
)
from vault_write_pages import canonical_source_url
from vault_write_rendering import (
    HOT_ADDRESS_TOKEN_RX,
    HOT_LINK_RX,
    HOT_TOTAL_WORDS,
    NARRATIVE_HEADING,
    NARRATIVE_WORDS,
    RC_BULLET_CHARS,
    RC_HEADING,
    RC_MAX_BULLETS,
    THREADS_HEADING,
    THREADS_MAX,
    apply_hot,
    apply_log,
    bullets_of,
    one_line,
    replace_section,
    safe_hot_bullet,
    section_bounds,
    set_frontmatter_updated,
)
from vault_write_transaction import TransactionJournal, atomic_write


REPO_ROOT = Path(__file__).resolve().parents[1]
HOT_FILE = REPO_ROOT / "wiki" / "hot.md"
LOG_FILE = REPO_ROOT / "wiki" / "log.md"
LOCK_FILE = REPO_ROOT / ".vault-meta" / ".vault-write.lock"
JOURNAL_FILE = REPO_ROOT / ".vault-meta" / ".vault-write-journal.json"

MUTATION_KEYS = HOT_MUTATION_KEYS | {
    "log_entry",
    "plan_close",
    "pages",
    "moves",
    "manifest_update",
}
KNOWN_KEYS = MUTATION_KEYS | {
    "actor",
    "session",
    "schema_version",
    "request_id",
    "exact_binding",
}

OUTPUT_JSON = False
TRANSACTION_ID = ""
REQUEST_ID: str | None = None


def _planner() -> MutationPlanner:
    return MutationPlanner(REPO_ROOT)


def _transaction() -> TransactionJournal:
    # Resolve the module-level writer at call time so importers can inject the
    # historical crash-test seam without bypassing journal ownership.
    return TransactionJournal(REPO_ROOT, JOURNAL_FILE, atomic_write)


# Compatibility façade for callers that historically imported policy helpers
# from the executable module. New code should use the owned collaborator seam.
def safe_repo_path(rel: str, *, prefix: str | None = None) -> Path:
    return _safe_repo_path(REPO_ROOT, rel, prefix=prefix)


def validate_page_content(rel: str, content: str) -> None:
    _planner().pages.validate_page_content(rel, content)


def source_page_identity(content: str, *, context: str) -> str | None:
    return _planner().pages.source_page_identity(content, context=context)


def validate_unique_source_urls(
    writes: list[tuple[Path, str]], deletes: list[tuple[Path, str]]
) -> None:
    _planner().pages.validate_unique_source_urls(writes, deletes)


def page_mutations(
    specs: object,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    return _planner().pages.page_mutations(specs)


def page_moves(
    specs: object,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    return _planner().pages.page_moves(specs)


def manifest_write(spec: object) -> tuple[Path, str] | None:
    return _planner().manifest_write(spec)


def ensure_unique_writes(
    writes: list[tuple[Path, str]], deletes: list[tuple[Path, str]] | None = None
) -> None:
    _planner().ensure_unique_writes(writes, deletes)


def write_journal(
    writes: list[tuple[Path, str]], deletes: list[tuple[Path, str]] | None = None
) -> None:
    _transaction().write(writes, deletes)


def recover_journal() -> int:
    return _transaction().recover()


def apply_plan_close(spec: dict, today: str) -> tuple[Path, str]:
    return _planner().apply_plan_close(spec, today)


def result_json(
    status: str,
    *,
    written_paths: list[str] | None = None,
    warnings: list[str] | None = None,
    error: dict | None = None,
    extra: dict | None = None,
) -> None:
    payload = {
        "schema_version": 1,
        "transaction_id": TRANSACTION_ID or str(uuid.uuid4()),
        "request_id": REQUEST_ID,
        "status": status,
        "written_paths": written_paths or [],
        "warnings": warnings or [],
    }
    if error is not None:
        payload["error"] = error
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))


def fail(code: int, msg: str, *, paths: list[str] | None = None) -> int:
    if OUTPUT_JSON:
        categories = {1: "io", 2: "invariant", 3: "invalid_request", 4: "conflict"}
        result_json(
            "error",
            error={
                "category": categories.get(code, "unknown"),
                "retryable": code in {1, 4},
                "message": msg,
                "paths": paths or [],
            },
        )
        return code
    print(f"vault-write: {msg}", file=sys.stderr)
    return code


def _read_payload(argv: list[str]) -> tuple[dict, int | None]:
    global REQUEST_ID, TRANSACTION_ID

    if "--file" in argv:
        try:
            raw = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
        except (IndexError, OSError) as exc:
            return {}, fail(3, f"cannot read --file: {exc}")
    else:
        raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, fail(3, f"payload is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        return {}, fail(3, "payload must be a JSON object")
    if parsed.get("schema_version", 1) != 1:
        return {}, fail(
            3,
            f"unsupported schema_version {parsed.get('schema_version')!r}; expected 1",
        )
    request_id = parsed.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", request_id
        ):
            return {}, fail(3, "request_id must match [A-Za-z0-9._:-]{1,128}")
        REQUEST_ID = request_id
        TRANSACTION_ID = request_id
    unknown = parsed.keys() - KNOWN_KEYS
    if "index_line" in unknown:
        print(
            "vault-write: WARN index_line is not supported — index.md is a curated "
            "map; folder listings autogenerate via reindex.py --folder-indexes",
            file=sys.stderr,
        )
        unknown = unknown - {"index_line"}
    if unknown:
        return {}, fail(3, f"unknown payload keys: {sorted(unknown)}")
    if (
        "exact_binding" in parsed
        and parsed.get("actor") != "stop-hook-link-repair"
    ):
        return {}, fail(
            3,
            "exact_binding is reserved for stop-hook-link-repair",
        )
    if not parsed.keys() & MUTATION_KEYS:
        return {}, fail(3, "payload has no actionable keys")
    if "actor" in parsed and not isinstance(parsed["actor"], str):
        return {}, fail(3, "actor must be a string")
    if "session" in parsed and not isinstance(parsed["session"], str):
        return {}, fail(3, "session must be a string")
    return parsed, None


def _set_output_mode(argv: list[str]) -> int | None:
    global OUTPUT_JSON

    if "--output" not in argv:
        return None
    try:
        output_mode = argv[argv.index("--output") + 1]
    except IndexError:
        return fail(3, "--output requires text|json")
    if output_mode not in {"text", "json"}:
        return fail(3, "--output must be text or json")
    OUTPUT_JSON = output_mode == "json"
    return None


def _hash_request(argv: list[str]) -> int:
    try:
        rel = argv[argv.index("--sha256") + 1]
        path = safe_repo_path(rel)
        if not path.is_file():
            return fail(3, f"cannot hash missing file: {rel}")
        digest = sha256_text(path.read_text(encoding="utf-8"))
        if OUTPUT_JSON:
            result_json("ok", extra={"path": rel, "sha256": digest})
        else:
            print(digest)
        return 0
    except (IndexError, OSError, PayloadError) as exc:
        return fail(3, f"cannot hash path: {exc}")


def _acquire_lock() -> IO[str] | int:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("w")
    deadline = time.time() + 5
    while True:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_handle
        except OSError:
            if time.time() > deadline:
                lock_handle.close()
                return fail(1, "could not acquire vault-write lock within 5s")
            time.sleep(0.2)


def _written_paths(plan: MutationPlan) -> list[str]:
    paths = [str(path.relative_to(REPO_ROOT)) for path, _ in plan.writes]
    paths.extend(str(path.relative_to(REPO_ROOT)) for path, _ in plan.deletes)
    return paths


def _emit_write_event(payload: dict, plan: MutationPlan, recovered: int) -> None:
    page_specs = payload.get("pages") or []
    emit_event(
        "vault-write",
        actor=payload.get("actor") or "unknown",
        session=payload.get("session"),
        paths=_written_paths(plan),
        counts={
            "writes": len(plan.writes) + len(plan.deletes),
            "page_creates": sum(
                1 for spec in page_specs if spec.get("op") == "create"
            ),
            "page_updates": sum(
                1 for spec in page_specs if spec.get("op") == "update"
            ),
            "page_deletes": sum(
                1 for spec in page_specs if spec.get("op") == "delete"
            ),
            "page_moves": len(payload.get("moves") or []),
            "manifest_updates": int(bool(payload.get("manifest_update"))),
            "hot_updates": int(bool(payload.keys() & HOT_MUTATION_KEYS)),
            "log_updates": int(bool(payload.get("log_entry"))),
            "plan_closes": int(bool(payload.get("plan_close"))),
            "recovered_writes": recovered,
        },
        root=REPO_ROOT,
    )


def main(argv: list[str]) -> int:
    global TRANSACTION_ID
    TRANSACTION_ID = str(uuid.uuid4())
    output_error = _set_output_mode(argv)
    if output_error is not None:
        return output_error
    if "--sha256" in argv:
        return _hash_request(argv)

    dry_run = "--dry-run" in argv
    recover_only = "--recover" in argv
    payload: dict = {}
    if not recover_only:
        payload, payload_error = _read_payload(argv)
        if payload_error is not None:
            return payload_error

    lock_handle = _acquire_lock()
    if isinstance(lock_handle, int):
        return lock_handle
    try:
        transaction = _transaction()
        recovered = transaction.recover()
        if recovered:
            print(
                f"vault-write: RECOVERED {recovered} file(s) by roll-forward",
                file=sys.stderr,
            )
            emit_event(
                "vault-recover",
                actor="recovery",
                counts={"writes": recovered},
                root=REPO_ROOT,
            )
        if recover_only:
            if OUTPUT_JSON:
                result_json("ok", extra={"recovered_writes": recovered})
            else:
                print(f"vault-write: OK recovery ({recovered} file(s))")
            return 0

        plan = _planner().plan(payload, time.strftime("%Y-%m-%d"))
        if not OUTPUT_JSON:
            for warning in plan.warnings:
                print(f"vault-write: WARN {warning}", file=sys.stderr)
        paths = _written_paths(plan)
        if dry_run:
            if OUTPUT_JSON:
                result_json("dry-run", written_paths=paths, warnings=plan.warnings)
            else:
                for path in paths:
                    print(f"vault-write: DRY would write {path}")
            return 0

        transaction.commit(plan.writes, plan.deletes)
        _emit_write_event(payload, plan, recovered)
        if OUTPUT_JSON:
            result_json("ok", written_paths=paths, warnings=plan.warnings)
        else:
            print("vault-write: OK " + ", ".join(paths))
        return 0
    except CapViolation as exc:
        return fail(2, f"CAP VIOLATION — nothing written. {exc}")
    except ConflictError as exc:
        return fail(4, f"CONFLICT — nothing written. {exc}")
    except PayloadError as exc:
        return fail(3, f"bad payload — nothing written. {exc}")
    except (OSError, ValueError) as exc:
        return fail(
            1, f"io/recovery error — transaction may require --recover. {exc}"
        )
    finally:
        lock_handle.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
